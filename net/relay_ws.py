"""
relay_ws.py - il frontale WebSocket del relay (2026-08-23). Solo stdlib.

    browser (pannello web) --ws/wss--> relay_ws.py --UDP 127.0.0.1--> relay.py
    emulatore / client.py  ------------------------UDP---------------> relay.py

I browser non fanno UDP. Invece di scrivere un secondo relay che parla
WebSocket (e che prima o poi divergerebbe dal primo), questo programma si
mette DAVANTI a relay.py: ogni connessione WebSocket diventa una socket UDP
sua verso il relay, e i byte passano in entrambi i versi SENZA essere
interpretati. Le stanze, i T_BYE, i timeout, il rebinding: tutto resta in
relay.py, che e' l'unica fonte della verita'. Conseguenza che conta: un
giocatore dal browser e uno da mGBA/GBA+client.py stanno nella STESSA stanza
e si vedono, perche' per relay.py sono due peer UDP come tutti gli altri.

Il messaggio WebSocket (binario) E' il datagramma: header OWL1 + corpo, byte
per byte come su UDP (protocol.py). Qui si guarda l'header solo per due
cose: scartare cio' che non e' nostro (niente fa' cadere il frontale) e
ricordare peer_id/stanza della connessione, cosi' alla CHIUSURA del socket
si manda al relay un T_BYE a nome di quel peer - il browser che chiude la
scheda sparisce subito dalla stanza invece che al timeout, e l'amico vede
il VIA entro un secondo.

RFC 6455 a mano (handshake, frame mascherati dal client, ping/pong, close,
frammentazione): ~150 righe, nessuna dipendenza, gira su un VPS con il solo
python3 come relay.py. TLS NON lo fa: davanti ci va un reverse proxy
(Caddy fa https e wss da solo, vedi web/README.md) - il browser pretende
wss:// quando la pagina e' https://.

Uso:
    python relay_ws.py [--port 9001] [--relay 127.0.0.1:9000] [--bind 0.0.0.0]
                       [--timeout 30] [--origin https://esempio.it ...] [--verbose]

GET / (senza Upgrade) risponde un JSON di stato: serve a `curl` e al
pannello per dire "il relay c'e'" prima di aprire la partita.
"""

import argparse
import base64
import hashlib
import json
import os
import selectors
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from protocol import T_BYE, TYPE_NAMES, pack, unpack  # noqa: E402

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HEADER = 8192          # intestazione HTTP oltre questa = non e' un browser
MAX_FRAME = 65536          # un datagramma nostro e' < 100 byte: oltre e' abuso

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 10


def ws_accept_key(key):
    return base64.b64encode(hashlib.sha1(key.strip().encode("ascii") + WS_GUID).digest()).decode("ascii")


def ws_frame(opcode, payload=b""):
    """Frame server -> client: mai mascherato (RFC 6455 5.1)."""
    n = len(payload)
    head = bytes([0x80 | (opcode & 0x0F)])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def ws_unmask(data, mask):
    # I messaggi nostri sono decine di byte: il ciclo in Python basta e
    # avanza; per i rari frame grossi il costo resta lineare.
    out = bytearray(data)
    for i in range(len(out)):
        out[i] ^= mask[i & 3]
    return bytes(out)


class Conn:
    def __init__(self, sock, addr, relay_addr):
        self.sock = sock
        self.addr = addr
        self.buf = b""
        self.out = b""
        self.handshaken = False
        self.closing = False
        self.frag_op = None
        self.frag = b""
        self.peer_id = None
        self.room = None
        self.rx = 0          # messaggi dal browser
        self.tx = 0          # messaggi verso il browser
        self.junk = 0        # messaggi che non erano datagrammi OWL1
        self.last_seen = time.monotonic()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setblocking(False)
        self.udp.connect(relay_addr)

    def descr(self):
        return "%s:%d" % (self.addr[0], self.addr[1])


class RelayWs:
    def __init__(self, port, relay_addr, bind="0.0.0.0", timeout=30.0,
                 origins=None, verbose=False):
        self.relay_addr = relay_addr
        self.timeout = timeout
        self.origins = set(origins or ())
        self.verbose = verbose
        self.conns = {}              # sock -> Conn (la TCP)
        self.by_udp = {}             # udp sock -> Conn
        self.running = False
        self.total = 0
        self.sel = selectors.DefaultSelector()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # NIENTE SO_REUSEADDR su Windows (vedi relay.py): due frontali accesi
        # per sbaglio si spartirebbero le connessioni in silenzio.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        elif hasattr(socket, "SO_REUSEADDR"):
            # Su Linux invece SENZA questo il riavvio del servizio aspetta il
            # TIME_WAIT: un minuto di "porta occupata" ad ogni restart.
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.listener.bind((bind, port))
        except OSError as exc:
            raise SystemExit("[ws   ] porta TCP %d gia' occupata (%s): c'e' gia' un relay_ws acceso?"
                             % (port, exc))
        self.listener.listen(16)
        self.listener.setblocking(False)
        self.sel.register(self.listener, selectors.EVENT_READ, ("listen", None))
        self.port = self.listener.getsockname()[1]
        self.log("in ascolto su TCP %s:%d, relay UDP %s:%d, timeout %ds%s"
                 % (bind, self.port, relay_addr[0], relay_addr[1], timeout,
                    (", origini ammesse: " + ", ".join(sorted(self.origins))) if self.origins else ""))

    def log(self, msg):
        print("[ws   ] %s" % msg, flush=True)

    # --- ciclo ------------------------------------------------------------

    def run(self):
        self.running = True
        next_sweep = time.monotonic() + 1.0
        while self.running:
            try:
                events = self.sel.select(timeout=0.25)
            except KeyboardInterrupt:
                self.log("chiusura")
                break
            for key, mask in events:
                kind, conn = key.data
                try:
                    if kind == "listen":
                        self._accept()
                    elif kind == "tcp":
                        if mask & selectors.EVENT_READ:
                            self._tcp_read(conn)
                        if mask & selectors.EVENT_WRITE and conn.sock in self.conns:
                            self._tcp_flush(conn)
                    elif kind == "udp":
                        # La TCP puo' essere caduta in questo stesso giro di
                        # select: l'evento UDP e' stantio e la socket e' chiusa.
                        if conn.sock in self.conns:
                            self._udp_read(conn)
                except Exception as exc:     # una connessione rotta non ferma le altre
                    if self.verbose:
                        self.log("errore su %s: %r" % (conn.descr() if conn else "?", exc))
                    if conn is not None:
                        self._drop(conn, "errore: %r" % exc)
            now = time.monotonic()
            if now >= next_sweep:
                next_sweep = now + 1.0
                self._sweep(now)
        self._shutdown()

    def stop(self):
        self.running = False

    def _shutdown(self):
        for conn in list(self.conns.values()):
            self._drop(conn, "spegnimento", quiet=True)
        try:
            self.sel.unregister(self.listener)
        except Exception:
            pass
        self.listener.close()

    def _accept(self):
        try:
            sock, addr = self.listener.accept()
        except OSError:
            return
        sock.setblocking(False)
        conn = Conn(sock, addr, self.relay_addr)
        self.conns[sock] = conn
        self.by_udp[conn.udp] = conn
        self.sel.register(sock, selectors.EVENT_READ, ("tcp", conn))
        self.sel.register(conn.udp, selectors.EVENT_READ, ("udp", conn))
        self.total += 1

    # --- TCP / WebSocket --------------------------------------------------

    def _tcp_read(self, conn):
        try:
            data = conn.sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            self._drop(conn, "lettura fallita: %s" % exc)
            return
        if not data:
            self._drop(conn, "chiuso dal browser")
            return
        conn.buf += data
        conn.last_seen = time.monotonic()
        if not conn.handshaken:
            self._handshake(conn)
            if not conn.handshaken:
                return
        self._parse_frames(conn)

    def _handshake(self, conn):
        end = conn.buf.find(b"\r\n\r\n")
        if end < 0:
            if len(conn.buf) > MAX_HEADER:
                self._drop(conn, "intestazione HTTP troppo lunga")
            return
        head, conn.buf = conn.buf[:end].decode("latin-1"), conn.buf[end + 4:]
        lines = head.split("\r\n")
        req = lines[0].split(" ")
        headers = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            headers[k.strip().lower()] = v.strip()
        method = req[0] if req else ""
        path = req[1] if len(req) > 1 else "/"
        upgrade = headers.get("upgrade", "").lower() == "websocket"
        key = headers.get("sec-websocket-key")

        if method != "GET":
            self._reply_http(conn, "405 Method Not Allowed", "text/plain", b"solo GET\n")
            return
        if not upgrade or not key:
            # Stato in JSON: per curl, per il pannello, per chi vuole sapere
            # se il relay e' vivo prima di aprire la partita.
            body = json.dumps({"relay_ws": True, "connessioni": len(self.conns) - 1,
                               "totali": self.total,
                               "relay": "%s:%d" % self.relay_addr}).encode("utf-8")
            self._reply_http(conn, "200 OK", "application/json", body)
            return
        if self.origins:
            origin = headers.get("origin", "")
            if origin not in self.origins:
                self.log("rifiutata %s: origine %r non ammessa" % (conn.descr(), origin))
                self._reply_http(conn, "403 Forbidden", "text/plain", b"origine non ammessa\n")
                return
        resp = ("HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: %s\r\n\r\n" % ws_accept_key(key))
        conn.out += resp.encode("ascii")
        self._tcp_flush(conn)
        conn.handshaken = True
        self.log("connesso %s (path %s, %d connessioni)"
                 % (conn.descr(), path, len(self.conns)))

    def _reply_http(self, conn, status, ctype, body):
        conn.out += ("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                     "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n"
                     % (status, ctype, len(body))).encode("ascii") + body
        self._tcp_flush(conn)
        self._drop(conn, "risposta HTTP", quiet=True)

    def _parse_frames(self, conn):
        buf = conn.buf
        while len(buf) >= 2 and not conn.closing:
            b0, b1 = buf[0], buf[1]
            fin = bool(b0 & 0x80)
            op = b0 & 0x0F
            masked = bool(b1 & 0x80)
            ln = b1 & 0x7F
            pos = 2
            if ln == 126:
                if len(buf) < 4:
                    break
                ln = struct.unpack_from(">H", buf, 2)[0]
                pos = 4
            elif ln == 127:
                if len(buf) < 10:
                    break
                ln = struct.unpack_from(">Q", buf, 2)[0]
                pos = 10
            if ln > MAX_FRAME:
                self._drop(conn, "frame di %d byte: troppo grande" % ln)
                return
            mask = None
            if masked:
                if len(buf) < pos + 4:
                    break
                mask = buf[pos:pos + 4]
                pos += 4
            if len(buf) < pos + ln:
                break
            payload = buf[pos:pos + ln]
            buf = buf[pos + ln:]
            if mask is not None:
                payload = ws_unmask(payload, mask)
            self._frame(conn, fin, op, payload)
        conn.buf = buf

    def _frame(self, conn, fin, op, payload):
        if op == OP_PING:
            self._send_raw(conn, ws_frame(OP_PONG, payload))
            return
        if op == OP_PONG:
            return
        if op == OP_CLOSE:
            # Eco del close come vuole la RFC, poi giu' tutto (e T_BYE al relay).
            conn.closing = True
            self._send_raw(conn, ws_frame(OP_CLOSE, payload[:2]))
            self._drop(conn, "close dal browser")
            return
        if op in (OP_TEXT, OP_BIN):
            if not fin:
                conn.frag_op = op
                conn.frag = payload
                return
            self._message(conn, payload)
            return
        if op == OP_CONT:
            if conn.frag_op is None:
                self._drop(conn, "continuazione senza inizio")
                return
            conn.frag += payload
            if len(conn.frag) > MAX_FRAME:
                self._drop(conn, "messaggio frammentato troppo grande")
                return
            if fin:
                msg, conn.frag, conn.frag_op = conn.frag, b"", None
                self._message(conn, msg)
            return
        self._drop(conn, "opcode %d sconosciuto" % op)

    def _message(self, conn, payload):
        parsed = unpack(payload)
        if parsed is None:
            # Non e' un datagramma nostro: si conta e si ignora. Un browser
            # con una pagina vecchia o un curioso non devono poter toccare il
            # relay.
            conn.junk += 1
            if conn.junk == 1:
                self.log("%s manda roba che non e' OWL1 (%d byte): ignorata" % (conn.descr(), len(payload)))
            return
        kind, peer_id, room_id, seq, body = parsed
        if conn.peer_id is None:
            self.log("%s e' il peer %d (stanza %d)" % (conn.descr(), peer_id, room_id))
        conn.peer_id = peer_id
        if room_id:
            conn.room = room_id
        conn.rx += 1
        try:
            conn.udp.send(payload)
        except OSError as exc:
            self.log("%s: invio al relay fallito: %s" % (conn.descr(), exc))
        if self.verbose:
            self.log("%s -> relay %s (%d byte)" % (conn.descr(), TYPE_NAMES.get(kind, kind), len(body)))

    def _send_raw(self, conn, data):
        conn.out += data
        self._tcp_flush(conn)

    def _tcp_flush(self, conn):
        if not conn.out:
            return
        try:
            n = conn.sock.send(conn.out)
        except (BlockingIOError, InterruptedError):
            n = 0
        except OSError as exc:
            self._drop(conn, "scrittura fallita: %s" % exc)
            return
        conn.out = conn.out[n:]
        try:
            self.sel.modify(conn.sock,
                            selectors.EVENT_READ | (selectors.EVENT_WRITE if conn.out else 0),
                            ("tcp", conn))
        except Exception:
            pass

    # --- UDP (dal relay verso il browser) --------------------------------

    def _udp_read(self, conn):
        while True:
            try:
                data = conn.udp.recv(2048)
            except (BlockingIOError, InterruptedError):
                return
            except ConnectionResetError:
                # Windows: ICMP "porta non raggiungibile" = il relay non c'e'.
                self.log("%s: il relay %s:%d non risponde (e' acceso?)" % ((conn.descr(),) + self.relay_addr))
                return
            except OSError as exc:
                self.log("%s: lettura dal relay fallita: %s" % (conn.descr(), exc))
                return
            if not conn.handshaken or conn.closing:
                continue
            conn.tx += 1
            self._send_raw(conn, ws_frame(OP_BIN, data))

    # --- vita e morte ----------------------------------------------------

    def _drop(self, conn, reason, quiet=False):
        if conn.sock not in self.conns:
            return
        # Il T_BYE a nome del peer: e' l'unica cosa che il frontale INVENTA,
        # e la inventa solo perche' una socket chiusa e' un fatto che il
        # relay da solo non puo' vedere prima del timeout.
        if conn.peer_id is not None:
            try:
                conn.udp.send(pack(T_BYE, conn.peer_id, conn.room or 0, 0))
            except OSError:
                pass
        for s in (conn.sock, conn.udp):
            try:
                self.sel.unregister(s)
            except Exception:
                pass
            try:
                s.close()
            except OSError:
                pass
        self.conns.pop(conn.sock, None)
        self.by_udp.pop(conn.udp, None)
        if not quiet:
            self.log("%s chiuso (%s)%s, rx %d tx %d, %d connessioni"
                     % (conn.descr(), reason,
                        (" peer %d" % conn.peer_id) if conn.peer_id is not None else "",
                        conn.rx, conn.tx, len(self.conns)))

    def _sweep(self, now):
        for conn in list(self.conns.values()):
            if now - conn.last_seen > self.timeout:
                self._drop(conn, "silenzio da %ds" % self.timeout)


def main():
    ap = argparse.ArgumentParser(description="Frontale WebSocket del relay (davanti a relay.py)")
    ap.add_argument("--port", type=int, default=9001, help="porta TCP per i browser (ws://)")
    ap.add_argument("--bind", default="0.0.0.0", help="indirizzo su cui ascoltare (127.0.0.1 = solo locale)")
    ap.add_argument("--relay", default="127.0.0.1:9000", help="dove sta relay.py (UDP)")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="secondi di silenzio dopo cui una connessione si chiude "
                         "(il pannello manda un PING al secondo)")
    ap.add_argument("--origin", action="append", default=[],
                    help="origine (https://host) ammessa; ripetibile; vuoto = tutte")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    host, _, port = args.relay.partition(":")
    try:
        RelayWs(args.port, (host, int(port or 9000)), bind=args.bind,
                timeout=args.timeout, origins=args.origin, verbose=args.verbose).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
