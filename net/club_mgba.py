#!/usr/bin/env python3
"""
club_mgba.py - il banco di prova del Cable Club: GBA FISICO contro EMULATORE.

    GBA (cartuccia, al club) <-cavo-> Pico (modo LINK) <-USB-> questo script
                                                                  |
                                              websocket ws://127.0.0.1:51784
                                                                  |
                                          mGBA + mgba/club-emulatore.lua
                                          (la ROM di Smeraldo dell'amico...
                                           o la STESSA, con un altro save)

Perche' esiste (2026-08-17): il club via internet non e' mai riuscito sul
fisico, e ogni prova costava una serata in due. Questo attrezzo toglie di
mezzo TUTTO il nostro strato - relay, client, payload, rete - e mette il
Pico in modo link contro la controparte di riferimento: il Lua ufficiale di
Celio (repo Celio-mGBA-Link), che trasforma mGBA nel partner del link.

  - Se uno scambio riesce QUI, il modo link del Pico e il gioco sono a
    posto, e il difetto del club via internet sta nel nostro netcode.
  - Se fallisce QUI, il problema e' sotto (firmware/cavo/timing), e lo si
    caccia con la referenza in mano, senza amico e senza rete.

E' il port in Python di MgbaLocalBridgeSession del client ufficiale
(Celio-Client, pages/onlineLink/mgbaLocalBridgeSession.ts), protocollo
letto da li' e da mGBA-server.lua:

  - via websocket i TESTI sono numeri: noi mandiamo LinkStatus (lo stato
    del device fisico), il Lua manda CommandType (cosa vuole che il device
    faccia). I BINARI sono blocchi dati da 64 byte, passati grezzi.
  - il Lua risponde al nostro AwaitMode con SetModeMaster: il GBA fisico
    fa da master, l'emulatore da slave. Non e' negoziabile ed e' comodo
    cosi': una variabile in meno.
  - StartHandshake e ConnectLink verso il device sono CANCELLI, non
    inoltri: StartHandshake parte solo dopo che il device ha detto
    HandshakeReceived (con ripiego a 1 s se il Lua non lo chiede), e
    ConnectLink solo dopo StartHandshake (stesso ripiego). Copiato pari
    pari dal bridge ufficiale, ripieghi compresi.

Niente payload: per questo test il GBA puo' avviare la cartuccia
NORMALMENTE, senza multiboot. Serve solo arrivare al Cable Club.

Uso:
    python club_mgba.py                       # ws://127.0.0.1:51784
    python club_mgba.py --timing 7400 --cable auto
"""

import argparse
import base64
import hashlib
import os
import queue
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from usb_link import (  # noqa: E402
    CMD_CANCEL, CMD_CONNECT_LINK, CMD_SET_MODE_MASTER, CMD_SET_MODE_SLAVE,
    CMD_START_HANDSHAKE, LINK_ST_AWAIT_MODE, LINK_ST_CLOSED,
    LINK_ST_CONNECTED, LINK_ST_HANDSHAKE_OK, LINK_ST_HANDSHAKE_RX,
    LINK_ST_NOMI, LINK_ST_READY, LINK_ST_RECONNECTING, UsbLink,
)

# CommandType del protocollo Celio (linkdevice.service.ts:28-35): sono i
# numeri che il Lua manda come TESTO sul websocket.
CT_CANCEL = 0x01
CT_SET_MODE_MASTER = 0x10
CT_SET_MODE_SLAVE = 0x11
CT_START_HANDSHAKE = 0x12
CT_CONNECT_LINK = 0x13

ST_DEBUG = 0xFFFF
ST_EMU_FINISHED = 0xFF09

RIPIEGO_S = 1.0     # HANDSHAKE_FALLBACK_MS del bridge ufficiale


class WsClient:
    """Client websocket minimo (RFC 6455), solo libreria standard.

    Fa il minimo che serve per parlare col server Lua di mGBA: handshake
    HTTP, frame testo/binari MASCHERATI (obbligo del lato client), risposta
    ai ping, niente frammentazione (il Lua manda frame interi). Non e' una
    libreria generica e non vuole diventarlo.
    """

    def __init__(self, url, subprotocol="celio", timeout=5.0):
        if not url.startswith("ws://"):
            raise ValueError("solo ws:// (niente TLS verso un Lua locale)")
        resto = url[5:]
        host, _, path = resto.partition("/")
        self.path = "/" + path
        h, _, p = host.partition(":")
        self.host, self.port = h, int(p or 80)
        self.sock = socket.create_connection((self.host, self.port), timeout)
        self.sock.settimeout(timeout)
        self._buf = b""

        chiave = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\n"
               "Host: %s:%d\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Protocol: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n"
               % (self.path, self.host, self.port, chiave, subprotocol))
        self.sock.sendall(req.encode())

        risposta = b""
        while b"\r\n\r\n" not in risposta:
            pezzo = self.sock.recv(4096)
            if not pezzo:
                raise ConnectionError("il server ws ha chiuso nell'handshake")
            risposta += pezzo
        testa, _, self._buf = risposta.partition(b"\r\n\r\n")
        if b" 101 " not in testa.split(b"\r\n", 1)[0]:
            raise ConnectionError("handshake ws rifiutato: %r"
                                  % testa.split(b"\r\n", 1)[0])
        attesa = base64.b64encode(hashlib.sha1(
            (chiave + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest())
        if attesa not in testa:
            raise ConnectionError("Sec-WebSocket-Accept sbagliato")

        # Da qui in poi il socket e' BLOCCANTE: il timeout serviva solo a non
        # restare appesi su un mGBA spento. A sessione stabilita il silenzio
        # e' lo stato NORMALE (si aspetta che i giocatori parlino con la
        # signorina), e il primo collaudo sul fisico (2026-08-17) e' morto
        # esattamente cosi': 5 s di quiete scambiati per un collegamento
        # caduto, proprio dopo l'assegnazione del ruolo.
        self.sock.settimeout(None)

    def _send_frame(self, opcode, payload):
        maschera = os.urandom(4)
        n = len(payload)
        testa = bytes([0x80 | opcode])
        if n < 126:
            testa += bytes([0x80 | n])
        elif n < 65536:
            testa += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            testa += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mascherato = bytes(b ^ maschera[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(testa + maschera + mascherato)

    def send_text(self, testo):
        self._send_frame(0x1, testo.encode())

    def send_binary(self, dati):
        self._send_frame(0x2, dati)

    def _leggi(self, n):
        while len(self._buf) < n:
            pezzo = self.sock.recv(4096)
            if not pezzo:
                raise ConnectionError("il server ws ha chiuso")
            self._buf += pezzo
        via, self._buf = self._buf[:n], self._buf[n:]
        return via

    def recv(self):
        """Ritorna (opcode, payload) del prossimo frame dati; gestisce ping e
        close da se'. Blocca finche' non arriva qualcosa: il silenzio e'
        lo stato normale di una sessione in attesa dei giocatori."""
        while True:
            b0, b1 = self._leggi(2)
            opcode = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._leggi(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._leggi(8))[0]
            maschera = self._leggi(4) if (b1 & 0x80) else None
            payload = self._leggi(n)
            if maschera:
                payload = bytes(b ^ maschera[i % 4]
                                for i, b in enumerate(payload))
            if opcode == 0x9:                      # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:                      # close
                raise ConnectionError("close dal server ws")
            if opcode in (0x1, 0x2):
                return opcode, payload
            # continuation/pong/altro: per questo server non esistono


def bridge(link, ws, log=print):
    """Il ponte, fedele a MgbaLocalBridgeSession: gira finche' la sessione
    non finisce (LinkClosed dal device) o il ws non cade."""
    usb_handshake = False
    lua_start = False
    lua_connect = False
    start_mandato = False
    connect_mandato = False
    start_scadenza = None      # ripiego: StartHandshake senza richiesta Lua
    connect_scadenza = None
    blocchi_al_lua = 0
    blocchi_al_gba = 0

    ws_in = queue.Queue()

    def lettore_ws():
        try:
            while True:
                ws_in.put(ws.recv())
        except Exception as e:
            ws_in.put(("morto", str(e)))

    threading.Thread(target=lettore_ws, daemon=True,
                     name="club_mgba-ws").start()

    def manda_start(perche):
        nonlocal start_mandato, connect_scadenza
        if start_mandato or not usb_handshake:
            return
        start_mandato = True
        link.club_command(CMD_START_HANDSHAKE, "StartHandshake (%s)" % perche)
        if lua_connect:
            manda_connect("il Lua l'aveva gia' chiesto")
        else:
            connect_scadenza = time.time() + RIPIEGO_S

    def manda_connect(perche):
        nonlocal connect_mandato
        if connect_mandato or not start_mandato:
            return
        connect_mandato = True
        link.club_command(CMD_CONNECT_LINK, "ConnectLink (%s)" % perche)

    log("[ponte] in ascolto: parla con la signorina sul GBA e nell'emulatore")
    while True:
        mosso = False

        # 1. stati dal device fisico -> Lua (testo). Ready/Debug/EmuFinished
        #    restano locali, come nel bridge ufficiale.
        try:
            st = link.club_statuses.get_nowait()
            mosso = True
        except queue.Empty:
            st = None
        if st is not None:
            log("[ponte] device: %s" % LINK_ST_NOMI.get(st, "0x%04X" % st))
            if st not in (LINK_ST_READY, ST_DEBUG, ST_EMU_FINISHED):
                ws.send_text(str(st))
            if st == LINK_ST_HANDSHAKE_RX:
                usb_handshake = True
                if lua_start:
                    manda_start("il Lua l'aveva gia' chiesto")
                elif start_scadenza is None and not start_mandato:
                    start_scadenza = time.time() + RIPIEGO_S
            elif st == LINK_ST_CLOSED:
                log("[ponte] il device ha chiuso: fine della sessione")
                return blocchi_al_lua, blocchi_al_gba

        # 2. blocchi dal device fisico -> Lua (binario, grezzi)
        try:
            blocco = link.club_blocks.get_nowait()
            mosso = True
        except queue.Empty:
            blocco = None
        if blocco is not None:
            ws.send_binary(bytes(blocco))
            blocchi_al_lua += 1

        # 3. messaggi dal Lua
        try:
            genere, contenuto = ws_in.get_nowait()
            mosso = True
        except queue.Empty:
            genere = None
        if genere == "morto":
            log("[ponte] websocket caduto: %s" % contenuto)
            return blocchi_al_lua, blocchi_al_gba
        if genere == 0x1:                          # testo: un CommandType
            try:
                cmd = int(contenuto.decode().strip())
            except ValueError:
                cmd = -1
            if cmd in (CT_SET_MODE_MASTER, CT_SET_MODE_SLAVE):
                log("[ponte] il Lua assegna il ruolo: %s"
                    % ("master" if cmd == CT_SET_MODE_MASTER else "slave"))
                link.club_command(
                    CMD_SET_MODE_MASTER if cmd == CT_SET_MODE_MASTER
                    else CMD_SET_MODE_SLAVE, "ruolo dal Lua")
            elif cmd == CT_START_HANDSHAKE:
                lua_start = True
                manda_start("richiesto dal Lua")
            elif cmd == CT_CONNECT_LINK:
                lua_connect = True
                manda_connect("richiesto dal Lua")
            elif cmd == CT_CANCEL:
                log("[ponte] il Lua chiede Cancel: fine della sessione")
                link.club_command(CMD_CANCEL, "Cancel dal Lua")
                return blocchi_al_lua, blocchi_al_gba
        elif genere == 0x2:                        # binario: blocchi da 64
            dati = bytes(contenuto)
            for i in range(0, len(dati), 64):
                pezzo = dati[i:i + 64]
                if len(pezzo) == 64:
                    link.club_send_block(pezzo)
                    blocchi_al_gba += 1

        # 4. i ripieghi a 1 s del bridge ufficiale
        adesso = time.time()
        if (start_scadenza is not None and adesso >= start_scadenza
                and not start_mandato):
            start_scadenza = None
            manda_start("ripiego: il device e' pronto e il Lua tace")
        if (connect_scadenza is not None and adesso >= connect_scadenza
                and not connect_mandato):
            connect_scadenza = None
            manda_connect("ripiego dopo StartHandshake")

        if not mosso:
            time.sleep(0.002)


def main():
    ap = argparse.ArgumentParser(description="Cable Club: GBA vs emulatore")
    ap.add_argument("--ws", default="ws://127.0.0.1:51784",
                    help="il server Lua dentro mGBA (default %(default)s)")
    ap.add_argument("--timing", type=int, default=7400)
    ap.add_argument("--cable", default="auto", choices=["auto", "gba", "gbc"])
    args = ap.parse_args()

    print("=" * 64)
    print("CABLE CLUB: GBA FISICO contro EMULATORE - banco di prova")
    print("=" * 64)
    print("""
Prima di lanciare, in quest'ordine:
  1. mGBA aperto con la ROM di Smeraldo (dump dell'amico o un altro save),
     partita caricata, personaggio DAVANTI alla signorina del Cable Club
     (Centro Pokemon, piano di sopra).
  2. In mGBA: Tools > Scripting > Load script > mgba\\club-emulatore.lua
     (nella console deve comparire "Starting websocket server").
  3. GBA fisico acceso con la cartuccia (multiboot NON necessario per
     questo test), anche lui al Cable Club.
  4. Questo script. Poi parla con la signorina su TUTTI E DUE.
""")

    print("[ponte] apro il device (passthrough di servizio)...")
    # Costruire NON apre: il device si aggancia con open(), come fa client.py.
    # Dimenticarla produce un 'NoneType has no attribute write' travestito
    # da endpoint inceppato (successo il 2026-08-17).
    link = UsbLink(timing=args.timing, quiet=False, cable=args.cable)
    link.open()
    print("[ponte] passo in modo LINK...")
    link.club_enter()
    if not getattr(link, "club_ready", False):
        raise SystemExit(
            "[ponte] il device non e' entrato in modo link (niente "
            "AwaitMode): riprova dopo un 3-sblocca.bat (F-4).")

    print("[ponte] mi collego al Lua di mGBA su %s ..." % args.ws)
    try:
        ws = WsClient(args.ws)
    except Exception as e:
        raise SystemExit(
            "[ponte] websocket KO: %s\n"
            "        mGBA e' aperto? Lo script club-emulatore.lua e' "
            "caricato? La console Lua dice 'Starting websocket server'?"
            % e)
    print("[ponte] collegato. La sessione e' viva.")

    try:
        al_lua, al_gba = bridge(link, ws)
    except KeyboardInterrupt:
        print("\n[ponte] interrotto: chiudo con ordine")
        try:
            ws.send_text(str(LINK_ST_CLOSED))
        except Exception:
            pass
        link.club_command(CMD_CANCEL, "Cancel (chiusura)")
        al_lua = al_gba = -1

    print("[ponte] blocchi GBA->emulatore: %s | emulatore->GBA: %s"
          % (al_lua, al_gba))
    print("[ponte] fine. Il Pico resta in modo link: per una nuova prova "
          "rilancia lo script;\n[ponte] per tornare alla partita normale "
          "usa il pannello (che rifa' il multiboot).")


if __name__ == "__main__":
    main()
