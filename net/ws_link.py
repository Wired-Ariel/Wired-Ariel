# -*- coding: utf-8 -*-
"""
ws_link.py - il relay raggiunto via WebSocket invece che via UDP (2026-08-25).

PERCHE' ESISTE, E NON E' UNA COMODITA'.
--------------------------------------
Il 2026-08-25, provando i pacchetti puntati sulla VPS, e' venuto fuori che
`gbcatrade.wired-ariel.it:9000/udp` **non e' raggiungibile da internet**:
timeout, non rifiuto. Il firewall della macchina (ufw) la permette, il relay
ascolta su 0.0.0.0 - ma il pacchetto non arriva mai al processo, e nel log
del relay non e' MAI comparso un peer che non fosse 127.0.0.1. Il filtro sta
a monte, nella Security List della VCN Oracle, che si apre solo dalla console
cloud. Ecco perche' finora via internet ha funzionato SOLO il browser: quello
passa dalla 443 (`wss://.../passotile/ws` -> relay_ws.py -> relay.py in
locale). Le partite con i client Python erano sempre state fatte col relay
sul PC di casa, con il port forwarding sul router.

Questo modulo da' ai client Python la stessa strada del browser: si collegano
al frontale WebSocket sulla 443. Vantaggi che restano anche dopo un'eventuale
apertura della porta UDP: la 443 passa da qualunque rete (hotspot, wifi
d'ufficio, NAT rigidi) e non chiede niente a nessun router.

COSA NON CAMBIA. Il protocollo e' identico: gli stessi datagrammi OWL1 di
protocol.py viaggiano come frame WebSocket BINARI, uno per datagramma - e'
lo stesso contratto che relay_ws.py ha col browser (web/js/relay.js). Il
relay non sa e non deve sapere da dove arrivano.

RFC 6455, il minimo che serve: handshake, frame mascherati (obbligatorio dal
client), ping/pong, close. E' la stessa implementazione gia' provata in
net/test_relay_ws.py, portata qui e resa capace di TLS e di riaggancio.
Nessuna dipendenza esterna: sui PC degli amici non si installa niente.
"""

import base64
import hashlib
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 8, 9, 10

# Un frame di gioco e' 23 byte (11 di header OWL1 + 12 di evento): un tetto
# di 64 KB e' gia' assurdamente largo, e serve solo a non credere a un
# mittente impazzito.
MAX_FRAME = 65536


def e_ws_url(s):
    """La stringa del --relay e' un URL WebSocket? (ws:// o wss://)"""
    return s.startswith("ws://") or s.startswith("wss://")


class WsRelay:
    """Il relay via WebSocket, con la stessa faccia di una socket UDP:
    send(datagram) e recv() -> datagram | None.

    Il riaggancio e' automatico e SILENZIOSO fino a che non riesce: una
    partita non deve morire perche' e' caduta la linea per due secondi. Ogni
    tentativo e' distanziato (RETRY_S) per non fare flood verso il server;
    il chiamante se ne accorge dai contatori, non da un'eccezione.
    """

    RETRY_S = 2.0

    def __init__(self, url, log=None, timeout=10.0):
        self.url = url
        self.log = log or (lambda msg: None)
        self.timeout = timeout
        self.sock = None
        self.buf = b""
        self.next_try = 0.0
        self.riagganci = 0
        self.errori = 0
        self.connesso = False
        u = urlparse(url)
        self.tls = (u.scheme == "wss")
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or (443 if self.tls else 80)
        self.path = u.path or "/"
        if u.query:
            self.path += "?" + u.query

    # --- connessione -------------------------------------------------------

    def connect(self):
        """Un tentativo. Ritorna True se il canale e' pronto."""
        self.close(quiet=True)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
            if self.tls:
                ctx = ssl.create_default_context()
                raw = ctx.wrap_socket(raw, server_hostname=self.host)
            req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n"
                   % (self.path, self.host, key))
            raw.sendall(req.encode("ascii"))

            head = b""
            raw.settimeout(self.timeout)
            while b"\r\n\r\n" not in head:
                chunk = raw.recv(4096)
                if not chunk:
                    raise RuntimeError("connessione chiusa durante l'handshake")
                head += chunk
                if len(head) > 16384:
                    raise RuntimeError("handshake troppo lungo")

            testa, resto = head.split(b"\r\n\r\n", 1)
            righe = testa.decode("latin-1").split("\r\n")
            if "101" not in righe[0]:
                # Il caso tipico: l'URL punta a una pagina invece che al
                # frontale. Dirlo per intero, che e' la meta' della diagnosi.
                raise RuntimeError("il server non ha accettato l'upgrade: %s" % righe[0])
            atteso = base64.b64encode(
                hashlib.sha1((key + GUID).encode()).digest()).decode()
            hdrs = testa.decode("latin-1").lower()
            if ("sec-websocket-accept: " + atteso.lower()) not in hdrs:
                raise RuntimeError("Sec-WebSocket-Accept sbagliato: non e' un WebSocket")

            raw.setblocking(False)
            self.sock = raw
            self.buf = resto
            self.connesso = True
            # LO DICE. Senza questa riga, "collegato" e "muto" hanno lo stesso
            # aspetto nel log - il client stampa comunque che aspetta il gioco
            # - e chi prova a casa non sa distinguere un relay irraggiungibile
            # da un emulatore non ancora avviato.
            self.log("[ws   ] collegato al relay %s (%s)"
                     % (self.url, "TLS" if self.tls else "in chiaro"))
            return True
        except Exception as exc:
            self.errori += 1
            self.close(quiet=True)
            self.next_try = time.monotonic() + self.RETRY_S
            self.log("[ws   ] collegamento a %s fallito: %s" % (self.url, exc))
            return False

    def ensure(self, now=None):
        """Da chiamare nel ciclo: riaggancia se serve, senza bloccare."""
        if self.connesso:
            return True
        if (now or time.monotonic()) < self.next_try:
            return False
        if self.connect():
            self.riagganci += 1
            if self.riagganci > 1:
                self.log("[ws   ] riagganciato al relay (%d volta/e)" % self.riagganci)
            return True
        return False

    def close(self, quiet=False):
        if self.sock is not None:
            try:
                if self.connesso and not quiet:
                    self._send_frame(OP_CLOSE, b"\x03\xe8")
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.connesso = False
        self.buf = b""

    def fileno(self):
        """Per select(): -1 quando non c'e' canale (il chiamante lo salta)."""
        return self.sock.fileno() if self.sock is not None else -1

    # --- frame -------------------------------------------------------------

    def _send_frame(self, opcode, payload):
        b0 = 0x80 | opcode
        n = len(payload)
        if n < 126:
            head = bytes([b0, 0x80 | n])
        elif n < 65536:
            head = bytes([b0, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([b0, 0x80 | 127]) + struct.pack(">Q", n)
        # La maschera e' OBBLIGATORIA per i frame che partono dal client
        # (RFC 6455 5.3): senza, un server conforme chiude la connessione.
        m = os.urandom(4)
        payload = bytes(b ^ m[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(head + m + payload)

    def send(self, datagram):
        """Un datagramma OWL1 = un frame binario. False se il canale non c'e'
        (il chiamante non deve morire: al giro dopo si riaggancia)."""
        if not self.connesso:
            return False
        try:
            self._send_frame(OP_BIN, datagram)
            return True
        except OSError as exc:
            self.log("[ws   ] invio fallito (%s): riaggancio" % exc)
            self.close(quiet=True)
            self.next_try = time.monotonic() + self.RETRY_S
            return False

    def recv(self):
        """Il prossimo datagramma di gioco, o None. Non blocca mai: i frame
        di controllo (ping/close) si consumano qui dentro."""
        if not self.connesso:
            return None
        try:
            while True:
                fr = self._pop()
                if fr is not None:
                    op, payload = fr
                    if op == OP_BIN:
                        return payload
                    if op == OP_PING:
                        try:
                            self._send_frame(OP_PONG, payload)
                        except OSError:
                            pass
                        continue
                    if op == OP_CLOSE:
                        self.log("[ws   ] il relay ha chiuso il canale: riaggancio")
                        self.close(quiet=True)
                        self.next_try = time.monotonic() + self.RETRY_S
                        return None
                    continue          # TEXT/PONG/continuazioni: non ci servono
                try:
                    chunk = self.sock.recv(65536)
                except (BlockingIOError, ssl.SSLWantReadError):
                    return None
                except OSError as exc:
                    self.log("[ws   ] lettura fallita (%s): riaggancio" % exc)
                    self.close(quiet=True)
                    self.next_try = time.monotonic() + self.RETRY_S
                    return None
                if not chunk:
                    self.log("[ws   ] canale chiuso dal server: riaggancio")
                    self.close(quiet=True)
                    self.next_try = time.monotonic() + self.RETRY_S
                    return None
                self.buf += chunk
        except Exception as exc:
            self.errori += 1
            self.log("[ws   ] errore di lettura: %s" % exc)
            self.close(quiet=True)
            self.next_try = time.monotonic() + self.RETRY_S
            return None

    def _pop(self):
        """Un frame intero dal buffer, o None se non e' ancora arrivato.
        I frame del server NON sono mascherati (RFC 6455 5.1)."""
        b = self.buf
        if len(b) < 2:
            return None
        op = b[0] & 0x0F
        masked = bool(b[1] & 0x80)
        ln = b[1] & 0x7F
        pos = 2
        if ln == 126:
            if len(b) < 4:
                return None
            ln = struct.unpack_from(">H", b, 2)[0]
            pos = 4
        elif ln == 127:
            if len(b) < 10:
                return None
            ln = struct.unpack_from(">Q", b, 2)[0]
            pos = 10
        if ln > MAX_FRAME:
            raise RuntimeError("frame di %d byte: il relay non manda roba cosi'" % ln)
        if masked:
            if len(b) < pos + 4:
                return None
            mask = b[pos:pos + 4]
            pos += 4
        if len(b) < pos + ln:
            return None
        payload = b[pos:pos + ln]
        if masked:
            payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
        self.buf = b[pos + ln:]
        return op, payload
