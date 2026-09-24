"""test_relay_ws.py - il frontale WebSocket davanti al relay, provato a secco.

Avvia relay.py e relay_ws.py in due thread su porte libere, poi parla con
relay_ws da un client WebSocket scritto qui (handshake + frame mascherati,
RFC 6455) e con un client UDP diretto: la prova che conta e' l'ultima,
cioe' che un browser e un client.py nella stessa stanza si vedono.

Uso:  python net\test_relay_ws.py
"""

import base64
import hashlib
import os
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from protocol import (  # noqa: E402
    T_BYE, T_EVENT, T_HELLO, T_PING, T_PONG, pack, unpack,
)
import relay  # noqa: E402
import relay_ws  # noqa: E402


def porta_libera(tipo):
    s = socket.socket(socket.AF_INET, tipo)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class WsClient:
    """Il minimo di RFC 6455 lato client: handshake, frame mascherati, lettura."""

    def __init__(self, port, path="/", mask=True, extra_headers=""):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.mask = mask
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = ("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n%s\r\n" % (path, key, extra_headers))
        self.sock.sendall(req.encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("chiuso durante l'handshake: %r" % head)
            head += chunk
        self.status = head.split(b"\r\n")[0].decode("latin-1")
        hdrs = head.split(b"\r\n\r\n")[0].decode("latin-1").lower()
        atteso = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.accept_ok = ("sec-websocket-accept: " + atteso.lower()) in hdrs
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def send(self, payload, opcode=2, fin=True, split=None):
        """split=n spezza il payload in un frame iniziale di n byte + una
        continuazione: serve al test della frammentazione."""
        if split is not None:
            self._frame(opcode, payload[:split], fin=False)
            self._frame(0, payload[split:], fin=True)
            return
        self._frame(opcode, payload, fin=fin)

    def _frame(self, opcode, payload, fin=True):
        b0 = (0x80 if fin else 0) | opcode
        n = len(payload)
        if n < 126:
            head = bytes([b0, (0x80 if self.mask else 0) | n])
        elif n < 65536:
            head = bytes([b0, (0x80 if self.mask else 0) | 126]) + struct.pack(">H", n)
        else:
            head = bytes([b0, (0x80 if self.mask else 0) | 127]) + struct.pack(">Q", n)
        if self.mask:
            m = os.urandom(4)
            payload = bytes(b ^ m[i & 3] for i, b in enumerate(payload))
            head += m
        self.sock.sendall(head + payload)

    def recv(self, timeout=2.0):
        """Ritorna (opcode, payload) del prossimo frame, o None allo scadere."""
        self.sock.settimeout(timeout)
        fine = time.monotonic() + timeout
        while True:
            fr = self._pop()
            if fr is not None:
                return fr
            if time.monotonic() > fine:
                return None
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buf += chunk

    def _pop(self):
        b = self.buf
        if len(b) < 2:
            return None
        op = b[0] & 0x0F
        ln = b[1] & 0x7F
        pos = 2
        if ln == 126:
            if len(b) < 4:
                return None
            ln = struct.unpack_from(">H", b, 2)[0]
            pos = 4
        if len(b) < pos + ln:
            return None
        payload = b[pos:pos + ln]
        self.buf = b[pos + ln:]
        return op, payload

    def close(self):
        try:
            self._frame(8, b"\x03\xe8")
            self.sock.close()
        except OSError:
            pass


class TestRelayWs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.udp_port = porta_libera(socket.SOCK_DGRAM)
        cls.ws_port = porta_libera(socket.SOCK_STREAM)
        cls.relay = relay.Relay(cls.udp_port, timeout=10, verbose=False)
        cls.t_relay = threading.Thread(target=cls.relay.run, daemon=True)
        cls.t_relay.start()
        cls.front = relay_ws.RelayWs(cls.ws_port, ("127.0.0.1", cls.udp_port),
                                     bind="127.0.0.1", timeout=5.0)
        cls.t_front = threading.Thread(target=cls.front.run, daemon=True)
        cls.t_front.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.front.stop()
        cls.t_front.join(2)

    # --- aiuti ----------------------------------------------------------
    def attendi(self, ws, kind, timeout=2.0):
        """Il prossimo datagramma di quel tipo (scarta gli altri)."""
        fine = time.monotonic() + timeout
        while time.monotonic() < fine:
            fr = ws.recv(timeout=0.3)
            if fr is None:
                continue
            op, payload = fr
            if op != 2:
                continue
            p = unpack(payload)
            if p and p[0] == kind:
                return p
        return None

    def test_01_handshake(self):
        ws = WsClient(self.ws_port)
        self.assertIn("101", ws.status)
        self.assertTrue(ws.accept_ok, "Sec-WebSocket-Accept sbagliato")
        ws.close()

    def test_02_get_senza_upgrade_risponde_json(self):
        s = socket.create_connection(("127.0.0.1", self.ws_port), timeout=2)
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        data = b""
        while True:
            try:
                c = s.recv(4096)
            except socket.timeout:
                break
            if not c:
                break
            data += c
        s.close()
        self.assertIn(b"200 OK", data)
        self.assertIn(b'"relay_ws": true', data)

    def test_03_ping_pong_attraverso_il_relay(self):
        ws = WsClient(self.ws_port)
        ws.send(pack(T_PING, 11, 77, 5, b"\x01\x02\x03"))
        p = self.attendi(ws, T_PONG)
        self.assertIsNotNone(p, "nessun PONG dal relay attraverso il frontale")
        self.assertEqual(p[1], 11)
        self.assertEqual(p[4], b"\x01\x02\x03")
        ws.close()

    def test_04_due_browser_stessa_stanza(self):
        a = WsClient(self.ws_port)
        b = WsClient(self.ws_port)
        a.send(pack(T_HELLO, 21, 300, 0))
        b.send(pack(T_HELLO, 22, 300, 0))
        time.sleep(0.2)
        ev = bytes(range(12))
        a.send(pack(T_EVENT, 21, 300, 1, ev))
        p = self.attendi(b, T_EVENT)
        self.assertIsNotNone(p, "B non ha ricevuto l'evento di A")
        self.assertEqual(p[1], 21)
        self.assertEqual(p[4], ev)
        # A non riceve il proprio evento
        self.assertIsNone(self.attendi(a, T_EVENT, timeout=0.6))
        a.close()
        b.close()

    def test_05_browser_e_udp_si_vedono(self):
        """LA PROVA CHE CONTA: un browser (WS) e un client.py (UDP) nella
        stessa stanza sono la stessa partita."""
        ws = WsClient(self.ws_port)
        ws.send(pack(T_HELLO, 31, 400, 0))
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.settimeout(2)
        udp.sendto(pack(T_HELLO, 32, 400, 0), ("127.0.0.1", self.udp_port))
        time.sleep(0.2)
        # UDP -> WS
        ev1 = b"\x01\x04\x00\x09\x00\x02\x0b\x00\x10\x00\x00\x00"
        udp.sendto(pack(T_EVENT, 32, 400, 9, ev1), ("127.0.0.1", self.udp_port))
        p = self.attendi(ws, T_EVENT)
        self.assertIsNotNone(p, "il browser non vede l'evento del client UDP")
        self.assertEqual((p[1], p[4]), (32, ev1))
        # WS -> UDP
        ev2 = b"\x02\x01\x00\x0a\x00\x02\x0c\x00\x10\x00\x00\x00"
        ws.send(pack(T_EVENT, 31, 400, 10, ev2))
        fine = time.monotonic() + 2
        visto = None
        while time.monotonic() < fine:
            try:
                data, _ = udp.recvfrom(2048)
            except socket.timeout:
                break
            q = unpack(data)
            if q and q[0] == T_EVENT:
                visto = q
                break
        self.assertIsNotNone(visto, "il client UDP non vede l'evento del browser")
        self.assertEqual((visto[1], visto[4]), (31, ev2))
        # chiusura del browser -> T_BYE al client UDP (inventato dal frontale)
        ws.close()
        fine = time.monotonic() + 3
        bye = None
        while time.monotonic() < fine:
            try:
                data, _ = udp.recvfrom(2048)
            except socket.timeout:
                break
            q = unpack(data)
            if q and q[0] == T_BYE and q[1] == 31:
                bye = q
                break
        self.assertIsNotNone(bye, "alla chiusura del browser l'amico non riceve il VIA (T_BYE)")
        udp.close()

    def test_06_frammentato_e_lungo(self):
        a = WsClient(self.ws_port)
        b = WsClient(self.ws_port)
        a.send(pack(T_HELLO, 41, 500, 0))
        b.send(pack(T_HELLO, 42, 500, 0))
        time.sleep(0.2)
        corpo = bytes((i * 7) & 0xFF for i in range(300))   # > 125: lunghezza a 16 bit
        d = pack(T_EVENT, 41, 500, 3, corpo)
        a.send(d, split=13)                                  # due frame: testa + continuazione
        p = self.attendi(b, T_EVENT)
        self.assertIsNotNone(p)
        self.assertEqual(p[4], corpo)
        a.close()
        b.close()

    def test_07_spazzatura_non_uccide(self):
        a = WsClient(self.ws_port)
        a.send(b"ciao, non sono OWL1", opcode=1)
        a.send(b"\x00" * 5)
        # la connessione resta viva: un ping dopo passa ancora
        a.send(pack(T_PING, 51, 600, 1, b"x"))
        self.assertIsNotNone(self.attendi(a, T_PONG))
        a.close()

    def test_08_ping_di_controllo_ws(self):
        a = WsClient(self.ws_port)
        a.send(b"\xaa\xbb", opcode=9)      # PING WebSocket (non il nostro T_PING)
        fr = a.recv(2.0)
        self.assertIsNotNone(fr)
        self.assertEqual(fr, (10, b"\xaa\xbb"))
        a.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
