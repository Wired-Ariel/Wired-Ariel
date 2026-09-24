#!/usr/bin/env python3
"""
test_club_mgba.py - il ponte GBA<->emulatore a secco, senza hardware.

Due cose sotto prova:
  1. il client websocket minimo (handshake RFC6455, frame mascherati,
     ping/pong) contro un serverino scritto qui;
  2. la logica del ponte: ruolo dal Lua, i cancelli StartHandshake e
     ConnectLink coi ripieghi, i blocchi passati grezzi nei due versi, la
     chiusura dal device.

Il "device" e' un finto UsbLink con le stesse code e gli stessi metodi che
il ponte usa; il "Lua" e' un websocket server di 60 righe che parla il
protocollo letto da mGBA-server.lua. Niente mock framework: oggetti veri,
protocollo vero, solo l'hardware e' finto.
"""

import base64
import hashlib
import os
import queue
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import club_mgba  # noqa: E402
from club_mgba import WsClient, bridge  # noqa: E402
from usb_link import (  # noqa: E402
    CMD_CANCEL, CMD_CONNECT_LINK, CMD_SET_MODE_MASTER,
    CMD_START_HANDSHAKE, LINK_ST_CLOSED, LINK_ST_HANDSHAKE_RX,
)


class ServerinoWs:
    """Il minimo indispensabile di un server websocket, per il test."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.conn = None
        self.ricevuti = queue.Queue()   # (opcode, payload) dal client
        threading.Thread(target=self._servi, daemon=True).start()

    def _servi(self):
        self.conn, _ = self.sock.accept()
        dati = b""
        while b"\r\n\r\n" not in dati:
            dati += self.conn.recv(4096)
        testa, _, resto = dati.partition(b"\r\n\r\n")
        chiave = [r.split(b": ", 1)[1] for r in testa.split(b"\r\n")
                  if r.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(
            chiave + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        self.conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                          b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                          b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        buf = resto
        while True:
            try:
                while len(buf) < 2:
                    pezzo = self.conn.recv(4096)
                    if not pezzo:
                        return
                    buf += pezzo
                b0, b1 = buf[0], buf[1]
                n = b1 & 0x7F
                testa_n = 2
                if n == 126:
                    while len(buf) < 4:
                        buf += self.conn.recv(4096)
                    n = struct.unpack(">H", buf[2:4])[0]
                    testa_n = 4
                mask_n = 4 if (b1 & 0x80) else 0
                tot = testa_n + mask_n + n
                while len(buf) < tot:
                    buf += self.conn.recv(4096)
                maschera = buf[testa_n:testa_n + mask_n]
                payload = buf[testa_n + mask_n:tot]
                if maschera:
                    payload = bytes(b ^ maschera[i % 4]
                                    for i, b in enumerate(payload))
                self.ricevuti.put((b0 & 0x0F, payload))
                buf = buf[tot:]
            except OSError:
                return

    def manda_testo(self, testo):
        dati = testo.encode()
        self.conn.sendall(bytes([0x81, len(dati)]) + dati)

    def manda_binario(self, dati):
        testa = bytes([0x82])
        if len(dati) < 126:
            testa += bytes([len(dati)])
        else:
            testa += bytes([126]) + struct.pack(">H", len(dati))
        self.conn.sendall(testa + dati)

    def manda_ping(self):
        self.conn.sendall(bytes([0x89, 0x02]) + b"ci")


class FintoLink:
    """Le code e i metodi di UsbLink che il ponte tocca. Niente di piu'."""

    def __init__(self):
        self.club_statuses = queue.Queue()
        self.club_blocks = queue.Queue()
        self.comandi = []
        self.blocchi_mandati = []

    def club_command(self, cmd, label):
        self.comandi.append(cmd)

    def club_send_block(self, b):
        self.blocchi_mandati.append(bytes(b))
        return True


def aspetta(cond, secondi=2.0):
    fine = time.time() + secondi
    while time.time() < fine:
        if cond():
            return True
        time.sleep(0.01)
    return False


class TestPonte(unittest.TestCase):
    def setUp(self):
        self.server = ServerinoWs()
        self.link = FintoLink()
        self.ws = WsClient("ws://127.0.0.1:%d" % self.server.port)
        self.esito = {}
        self.t = threading.Thread(
            target=lambda: self.esito.update(
                zip(("al_lua", "al_gba"),
                    bridge(self.link, self.ws, log=lambda *a: None))),
            daemon=True)
        self.t.start()
        self.assertTrue(aspetta(lambda: self.server.conn is not None))

    def chiudi(self):
        self.link.club_statuses.put(LINK_ST_CLOSED)
        self.t.join(timeout=3)
        self.assertFalse(self.t.is_alive(), "il ponte non e' uscito")

    def test_ruolo_e_cancelli_nel_flusso_completo(self):
        # Il Lua assegna il ruolo (come fa davvero: master al fisico).
        self.server.manda_testo(str(club_mgba.CT_SET_MODE_MASTER))
        self.assertTrue(aspetta(
            lambda: CMD_SET_MODE_MASTER in self.link.comandi))
        self.assertNotIn(CMD_START_HANDSHAKE, self.link.comandi)

        # Il device dice HandshakeReceived: viene INOLTRATO al Lua...
        self.link.club_statuses.put(LINK_ST_HANDSHAKE_RX)
        self.assertTrue(aspetta(lambda: not self.server.ricevuti.empty()))
        op, dati = self.server.ricevuti.get(timeout=1)
        self.assertEqual((op, dati), (0x1, str(LINK_ST_HANDSHAKE_RX).encode()))

        # ...e quando il Lua chiede StartHandshake, il cancello si apre.
        self.server.manda_testo(str(club_mgba.CT_START_HANDSHAKE))
        self.assertTrue(aspetta(
            lambda: CMD_START_HANDSHAKE in self.link.comandi))

        # ConnectLink dal Lua: passa perche' StartHandshake e' gia' partito.
        self.server.manda_testo(str(club_mgba.CT_CONNECT_LINK))
        self.assertTrue(aspetta(
            lambda: CMD_CONNECT_LINK in self.link.comandi))
        self.chiudi()

    def test_start_non_parte_senza_handshake_del_device(self):
        # Il Lua chiede StartHandshake ma il device NON ha ancora fatto
        # l'handshake: il cancello deve restare chiuso.
        self.server.manda_testo(str(club_mgba.CT_START_HANDSHAKE))
        time.sleep(0.3)
        self.assertNotIn(CMD_START_HANDSHAKE, self.link.comandi)
        # Arriva l'handshake: ora si'.
        self.link.club_statuses.put(LINK_ST_HANDSHAKE_RX)
        self.assertTrue(aspetta(
            lambda: CMD_START_HANDSHAKE in self.link.comandi))
        self.chiudi()

    def test_ripiego_senza_lua(self):
        # Handshake del device e il Lua che tace: dopo ~1 s il ripiego
        # manda StartHandshake e poi ConnectLink da solo (bridge ufficiale).
        self.link.club_statuses.put(LINK_ST_HANDSHAKE_RX)
        self.assertTrue(aspetta(
            lambda: CMD_START_HANDSHAKE in self.link.comandi, 2.5))
        self.assertTrue(aspetta(
            lambda: CMD_CONNECT_LINK in self.link.comandi, 2.5))
        self.chiudi()

    def test_blocchi_grezzi_nei_due_versi(self):
        blocco = bytes(range(64))
        self.link.club_blocks.put(blocco)
        self.assertTrue(aspetta(lambda: not self.server.ricevuti.empty()))
        op, dati = self.server.ricevuti.get(timeout=1)
        self.assertEqual((op, dati), (0x2, blocco))

        self.server.manda_binario(blocco * 2)   # due blocchi in un frame
        self.assertTrue(aspetta(
            lambda: len(self.link.blocchi_mandati) == 2))
        self.assertEqual(self.link.blocchi_mandati, [blocco, blocco])
        self.chiudi()

    def test_ping_del_server_non_disturba(self):
        self.server.manda_ping()
        self.server.manda_testo(str(club_mgba.CT_SET_MODE_MASTER))
        self.assertTrue(aspetta(
            lambda: CMD_SET_MODE_MASTER in self.link.comandi))
        self.chiudi()

    def test_cancel_dal_lua_chiude(self):
        self.server.manda_testo(str(club_mgba.CT_CANCEL))
        self.t.join(timeout=3)
        self.assertFalse(self.t.is_alive())
        self.assertIn(CMD_CANCEL, self.link.comandi)


if __name__ == "__main__":
    unittest.main(verbosity=1)
