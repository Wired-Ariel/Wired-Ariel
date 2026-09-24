#!/usr/bin/env python3
"""
finto_club.py - il PARTNER FINTO: il Cable Club si prova DA SOLI (procedura V).

Il problema che risolve: per provare il club servivano due persone, due GBA e
una serata; e il banco GBA<->emulatore (procedura Q) e' bloccato dal Lua WIP
di Celio, non da noi (mGBA senza cavo virtuale non si crede mai master, la
signorina emulata "non trova nessuno"). Questo script toglie di mezzo TUTTO
il lato remoto: si collega al relay come peer 2 e recita la parte del GBA
dell'amico, sia a livello del NOSTRO protocollo di club (epoche, sequenze,
riannunci: riusa ClubSession, lo stesso identico codice della partita vera)
sia a livello del protocollo di link della Gen 3 (link.c, letto dalla decomp):

  - peer 2 = ruolo slave del protocollo = GBA MASTER del gioco. Quindi e' il
    finto a condurre: manda SEND_LINK_TYPE (0x2222, linkType 0x1133 = scambio,
    LINKTYPE_TRADE_SETUP), poi il proprio LinkPlayerBlock (60 byte con la
    firma "GameFreak inc.", versione Smeraldo 0x4003, lingua italiana) in
    INIT_BLOCK + 5 CONT_BLOCK;
  - conta i comandi del GBA VERO (INIT + 5 CONT del suo blocco giocatore:
    e' IL criterio della prova, il punto esatto dove le prove in due morivano);
  - ricevuto il blocco intero, chiede le schede (SEND_BLOCK_REQ) e manda la
    propria (vuota ma di misura giusta): il linkup del GBA vero COMPLETA e
    parte il warp verso la saletta;
  - nella saletta fa il minimo sindacale: eco di READY_EXIT_STANDBY, tasti
    IDLE, eco di READY_CLOSE_LINK all'uscita. Il fantasma sta fermo: non e'
    un giocatore, e' un banco di prova.

Cosa dimostra: TUTTA la catena locale (GBA -> cavo -> Pico -> USB -> client
-> relay) nei due versi, con il codice vero. Cosa NON dimostra: il verso
d'ingresso su un secondo GBA fisico e la latenza di internet - ma la latenza
la assorbono le code, e quel verso e' lo stesso codice di questo.

    python finto_club.py --relay 127.0.0.1:9000 --room 58243 --peer-id 2

Oppure doppio clic su prova-club-da-solo.bat (relay e 2-gioca gia' su).
"""

import argparse
import os
import select
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from club_link import ClubSession
from protocol import (
    CLUB_DATA, CLUB_ENTER, CLUB_LEAVE, CLUB_REQ, CLUB_STATUS,
    T_CLUB, T_PING, T_PONG, VERSIONE_CLUB,
    club_block_riga, club_leave, club_unpack, pack, unpack,
)
from usb_link import (
    CMD_CONNECT_LINK, CMD_SET_MODE_MASTER, CMD_SET_MODE_SLAVE,
    CMD_START_HANDSHAKE,
    LINK_ST_AWAIT_MODE, LINK_ST_CLOSED, LINK_ST_CONNECTED,
    LINK_ST_HANDSHAKE_RX, LINK_ST_RECONNECTING,
)

# Il passo della recita: quanto aspetta il finto fra un gradino e l'altro
# della scala (come farebbe un GBA vero che deve arrivarci col gioco), e
# quanto fra un blocco e l'altro della raffica. I test li mettono a 0.
RITMO_PASSO_S = 0.25
RITMO_BURST_S = 0.03
# I 400 ms che il firmware vero aspetta fra una UsbSection e l'altra
# (module/link.cpp:29). Costante perche' i test la azzerano.
RITMO_RICONNESSIONE_S = 0.4

# Il LinkPlayerBlock del finto: 60 byte ESATTI come li costruisce link.c
# (ProcessRecvCmds, caso LINKCMD_SEND_LINK_TYPE). La firma e' controllata
# con strcmp dal ricevente: sbagliarla = CB2_LinkError sul GBA vero.
MAGIC_GAMEFREAK = b"GameFreak inc.\x00\x00"                # char magic[16]
LINKTYPE_TRADE_SETUP = 0x1133                              # scambio, come la signorina
NOME_FINTO = bytes([0xC0, 0xC3, 0xC8, 0xCE, 0xC9,          # "FINTO" nel charset Gen 3
                    0xFF, 0x00, 0x00])                     # terminatore + quiete

def blocco_giocatore(link_type=LINKTYPE_TRADE_SETUP):
    """Il LinkPlayerBlock del finto, 60 byte esatti. Il linkType cambia con
    la meccanica in corso e DEVE combaciare con quello del GBA vero:
    IsLinkPlayerDataExchangeComplete (link.c) confronta i linkType di tutti
    e se non coincidono il gioco dice "scelte diverse"."""
    return (
        MAGIC_GAMEFREAK
        + struct.pack("<HHI", 0x4003, 0x8000, 0x00C1A0DE)  # version (Smeraldo+0x4000),
                                                           # lp_field_2, trainerId
        + NOME_FINTO
        + struct.pack("<BBBBIHH",
                      0x11, 0x00, 0x11, 0x00,              # progressFlags (dex+campione),
                                                           # neverRead, copia, gender
                      link_type, 0x0000, 0x0004)           # linkType, id, lingua ITA
        + MAGIC_GAMEFREAK
    )


BLOCCO_GIOCATORE = blocco_giocatore()
assert len(BLOCCO_GIOCATORE) == 60

# La scheda allenatore che il gioco chiede dopo la conferma: 100 byte
# (BLOCK_REQ_SIZE_100). Vuota: il gioco la copia e basta, e il fantasma non
# ha niente da esibire.
SCHEDA_FINTA = bytes(100)

LINKCMD_SEND_LINK_TYPE = 0x2222
LINKCMD_INIT_BLOCK = 0xBBBB
LINKCMD_CONT_BLOCK = 0x8888
LINKCMD_SEND_BLOCK_REQ = 0xCCCC
LINKCMD_SEND_HELD_KEYS = 0xCAFE
LINKCMD_READY_CLOSE_LINK = 0x5FFF
LINKCMD_READY_EXIT_STANDBY = 0x2FFE
LINK_KEY_CODE_IDLE = 0x1A
LINK_KEY_CODE_EXIT_ROOM = 0x17
# La meccanica VERA: dal secondo giro il gioco non e' piu' "davanti alla
# signorina" (TRADE_SETUP) ma dentro lo scambio (cable_club.c,
# CreateTask_ReestablishCableClubLink, caso USING_TRADE_CENTER).
LINKTYPE_TRADE = 0x1111


def pacchetto(*words):
    """Un blocco del club: UN pacchetto di comando da 8 parole nei primi 16
    byte, zeri a seguire - identico a come lo produce il firmware."""
    w = list(words) + [0] * (8 - len(words))
    return struct.pack("<8H", *w) + bytes(48)


def blocco_in_cont(dati, mittente_id):
    """Un block send del gioco: INIT_BLOCK + i CONT_BLOCK da 14 byte l'uno,
    come LinkCB_BlockSend (link.c)."""
    fuori = [pacchetto(LINKCMD_INIT_BLOCK, len(dati), 0x80 + mittente_id)]
    pos = 0
    while pos < len(dati):
        pezzo = dati[pos:pos + 14].ljust(14, b"\x00")
        fuori.append(pacchetto(LINKCMD_CONT_BLOCK,
                               *struct.unpack("<7H", pezzo)))
        pos += 14
    return fuori


class FintoGba:
    """Il 'device + GBA' finto che ClubSession pilota come fosse un Pico.

    Interfaccia identica a client._ClubDev (command, send_block); in piu'
    produce stati e blocchi che il ciclo principale travasa nella sessione
    con on_device_status/on_device_block, come fa club_pump col Pico vero."""

    def __init__(self, log):
        self.log = log
        self.stati = []            # verso la sessione (LINK_ST_*)
        self.blocchi = []          # verso la sessione (il "GBA" parla)
        self._coda = []            # (quando, funzione): la recita a tempo
        # il flusso in ARRIVO dal GBA vero
        self._rx_size = 0
        self._rx_pos = 0
        self._giocatore_ok = False
        self._scheda_chiesta = False
        self.vittoria = False
        self._tasti_da = 0.0       # 0 = non ancora in saletta
        self._eco_standby_quando = 0.0
        self._chiusura_fatta = False
        self._uscita_fatta = False
        self.contatori = {}
        # I GIRI. Il link non e' uno solo: entrare nella saletta e' il giro
        # 0, sedersi alla macchina degli scambi ne apre un altro. Vedi
        # _riconnessione: e' il pezzo che riproduce da soli il difetto del
        # 2026-08-20 (i due seduti, "un momento attendi", errore).
        self.giri = 0
        self.riaperture_ok = 0
        self._link_type = LINKTYPE_TRADE_SETUP

    def avvia(self):
        """La sessione e' nata: il device 'entra in modo link'."""
        self.stati.append(LINK_ST_AWAIT_MODE)

    # -- interfaccia dev per ClubSession -----------------------------------

    def command(self, cmd, label):
        self.log("[finto] <- %s" % label)
        if cmd == CMD_SET_MODE_SLAVE:
            # il GBA finto "arriva dalla signorina" e manda 0xB9A0
            self._dopo(RITMO_PASSO_S,
                       lambda: self.stati.append(LINK_ST_HANDSHAKE_RX))
        elif cmd == CMD_START_HANDSHAKE:
            # da master di gioco si stabilisce da solo (il trigger dei 5
            # frame di Task_TriggerHandshake), poi parte lo scambio dati
            self._dopo(RITMO_PASSO_S, self._stabilisci)
        elif cmd == CMD_SET_MODE_MASTER:
            self.log("[finto] !!! mi e' arrivato il ruolo MASTER: il finto "
                     "recita il peer 2 (slave). Lancialo con --peer-id 2.")
        elif cmd == CMD_CONNECT_LINK:
            pass                   # per lo slave e' un no-op anche sul Pico

    def send_block(self, block64):
        """Un blocco CONSEGNATO dal partner (il GBA vero, via relay)."""
        w = struct.unpack("<8H", block64[:16])
        self.contatori[w[0]] = self.contatori.get(w[0], 0) + 1
        if w[0] == LINKCMD_INIT_BLOCK:
            self._rx_size = w[1]
            self._rx_pos = 0
            self.log("[finto] il TUO GBA inizia un blocco da %d byte" % w[1])
        elif w[0] == LINKCMD_CONT_BLOCK:
            self._rx_pos += 14
            if self._rx_size and self._rx_pos >= self._rx_size:
                self._blocco_completo()
        elif w[0] == LINKCMD_READY_EXIT_STANDBY:
            adesso = time.monotonic()
            if adesso >= self._eco_standby_quando:
                self._eco_standby_quando = adesso + 0.5
                self.blocchi.append(pacchetto(LINKCMD_READY_EXIT_STANDBY))
                self.log("[finto] standby: eco")
        elif w[0] == LINKCMD_SEND_HELD_KEYS:
            if w[1] == LINK_KEY_CODE_EXIT_ROOM and not self._uscita_fatta:
                # Uscire dalla PORTA e' l'unica vera fine: nel firmware
                # spegne keepAlive e la sessione muore (usbSection.cpp:54-57).
                self._uscita_fatta = True
                self.blocchi.append(pacchetto(LINKCMD_SEND_HELD_KEYS,
                                              LINK_KEY_CODE_EXIT_ROOM))
                self._dopo(RITMO_PASSO_S,
                           lambda: self.stati.append(LINK_ST_CLOSED))
                self.log("[finto] il TUO GBA esce dalla saletta: chiudo")
            elif not self._tasti_da:
                self._tasti_da = time.monotonic()
                self.log("[finto] SALETTA RAGGIUNTA: tasti in scambio. Il "
                         "fantasma sta fermo; la prova e' gia' vinta.")
        elif w[0] == LINKCMD_READY_CLOSE_LINK and not self._chiusura_fatta:
            # NON e' la fine: e' il gioco che chiude il link per cambiare
            # meccanica (ci si e' seduti alla macchina degli scambi).
            self._chiusura_fatta = True
            self.blocchi.append(pacchetto(LINKCMD_READY_CLOSE_LINK, w[1]))
            self._dopo(RITMO_PASSO_S, self._riconnessione)

    # -- la recita ---------------------------------------------------------

    def _dopo(self, ritardo, fn):
        self._coda.append((time.monotonic() + ritardo, fn))

    def _riconnessione(self):
        """Il GBA vero ha chiuso il link per cambiare meccanica. Il Pico
        vero, in quel momento, fa ESATTAMENTE questo: la UsbSection finisce,
        parte LinkReconnecting, e dopo 400 ms ne nasce un altra che
        ricomincia da HandshakeReceived e riaspetta StartHandshake e
        ConnectLink (module/link.cpp:26-30, usbSection.cpp:6-39).

        E' il pezzo che rende riproducibile DA SOLI il difetto del campo:
        se il client non rifa' la scala, da qui in poi il canale e' morto e
        il gioco va in errore dopo "un momento attendi"."""
        self.giri += 1
        self._link_type = LINKTYPE_TRADE
        self._rx_size = 0
        self._rx_pos = 0
        self._giocatore_ok = False
        self._chiusura_fatta = False
        self._tasti_da = 0.0
        self.log("[finto] ==============================================")
        self.log("[finto] il TUO GBA ha chiuso il link per cambiare "
                 "meccanica: lo riapro come il Pico vero (giro %d)"
                 % (self.giri + 1))
        self.stati.append(LINK_ST_RECONNECTING)
        self._dopo(RITMO_RICONNESSIONE_S,
                   lambda: self.stati.append(LINK_ST_HANDSHAKE_RX))

    def _stabilisci(self):
        self.stati.append(LINK_ST_CONNECTED)
        self._dopo(RITMO_PASSO_S, self._raffica_giocatore)

    def _raffica_giocatore(self):
        """Il master apre: LINK_TYPE, poi il proprio blocco giocatore.
        E' la raffica di 7 comandi SENZA RITENTI di link.c: se sul tuo lato
        ne arrivano meno di 7, il ladro e' ancora vivo."""
        fuori = [pacchetto(LINKCMD_SEND_LINK_TYPE, self._link_type)]
        fuori += blocco_in_cont(blocco_giocatore(self._link_type), 0)
        self.log("[finto] mando la raffica del master: LINK_TYPE + blocco "
                 "giocatore (%d comandi)" % len(fuori))
        for i, b in enumerate(fuori):
            self._dopo(RITMO_BURST_S * i, lambda b=b: self.blocchi.append(b))

    def _blocco_completo(self):
        size, self._rx_size = self._rx_size, 0
        if self.giri and not self._giocatore_ok:
            # Dal secondo giro il gioco non chiede le schede: gli basta lo
            # scambio dei dati giocatore (cable_club.c,
            # Task_ReestablishLinkAwaitConfirmation).
            self._giocatore_ok = True
            self.riaperture_ok += 1
            self.log("[finto] ==============================================")
            self.log("[finto] LA RIAPERTURA HA FUNZIONATO (giro %d): il TUO "
                     "GBA ha rifatto la scala e i dati passano. E' la prova "
                     "della correzione del 2026-08-20." % (self.giri + 1))
        elif not self.giri and not self._giocatore_ok:
            self._giocatore_ok = True
            self.log("[finto] ==============================================")
            self.log("[finto] BLOCCO GIOCATORE del TUO GBA completo (%d "
                     "byte): la raffica e' passata TUTTA. Chiedo le schede."
                     % size)
            self._dopo(RITMO_PASSO_S, self._chiedi_schede)
        elif not self.vittoria:
            self.vittoria = True
            self.log("[finto] ==============================================")
            self.log("[finto] SCHEDA del TUO GBA ricevuta per intera: LINKUP "
                     "COMPLETO. Il tuo GBA ora entra nella saletta.")

    def _chiedi_schede(self):
        fuori = [pacchetto(LINKCMD_SEND_BLOCK_REQ, 2)]      # BLOCK_REQ_SIZE_100
        fuori += blocco_in_cont(SCHEDA_FINTA, 0)
        for i, b in enumerate(fuori):
            self._dopo(RITMO_BURST_S * i, lambda b=b: self.blocchi.append(b))

    def tick(self):
        adesso = time.monotonic()
        pronte = [fn for (q, fn) in self._coda if q <= adesso]
        self._coda = [(q, fn) for (q, fn) in self._coda if q > adesso]
        for fn in pronte:
            fn()
        if self._tasti_da and adesso - self._tasti_da >= 0.1:
            self._tasti_da = adesso
            self.blocchi.append(pacchetto(LINKCMD_SEND_HELD_KEYS,
                                          LINK_KEY_CODE_IDLE))

    def riepilogo(self):
        from protocol import LINKCMD_NOMI
        voci = ["%s x%d" % (LINKCMD_NOMI.get(k, "0x%04X" % k), v)
                for k, v in sorted(self.contatori.items())]
        riga = "comandi ricevuti dal TUO GBA: " + (", ".join(voci) or "nessuno")
        if self.giri:
            riga += " | riaperture del link %d, riuscite %d" % (
                self.giri, self.riaperture_ok)
        return riga


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--relay", default="127.0.0.1:9000")
    ap.add_argument("--room", type=int, default=58243)
    ap.add_argument("--peer-id", type=int, default=2)
    ap.add_argument("--esci-dopo", type=float, default=0.0, metavar="SECONDI",
                    help="esce dalla saletta (CLUB_LEAVE) dopo N secondi dal "
                         "collegamento: serve a provare il CONGEDO dell'altro "
                         "lato - chi resta dentro deve uscire dalla porta, non "
                         "andare in errore (campo 2026-08-27)")
    args = ap.parse_args()

    host, porta = args.relay.rsplit(":", 1)
    relay = (host, int(porta))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    def log(riga):
        print(riga, flush=True)

    seq = [0]

    def next_seq():
        seq[0] = (seq[0] + 1) & 0xFF
        return seq[0]

    def send_club(body):
        sock.sendto(pack(T_CLUB, args.peer_id, args.room, next_seq(), body),
                    relay)

    log("[finto] partner finto per la stanza %d su %s (peer %d)"
        % (args.room, args.relay, args.peer_id))
    log("[finto] aspetto che il TUO GBA parli con la signorina (scegli "
        "SCAMBIO): il resto lo faccio io. Ctrl+C per uscire.")

    sess = None
    finto = None
    riaggancio_dopo = 0.0
    ping_quando = 0.0
    blocchi_log = [0]
    esci_quando = 0.0    # si arma al LINKUP: prima non c'e' da chi uscire
    uscito = False

    while True:
        pronti, _, _ = select.select([sock], [], [], 0.02)
        adesso = time.monotonic()

        # L'USCITA COMANDATA (--esci-dopo): si va via come chi passa dalla
        # porta della saletta. Chi resta deve essere ACCOMPAGNATO fuori dal suo
        # lato, non trovarsi il cavo staccato.
        if (args.esci_dopo and not esci_quando and finto is not None
                and getattr(finto, "vittoria", False)):
            esci_quando = adesso + args.esci_dopo
            log("[finto] esco fra %.0f s (--esci-dopo)" % args.esci_dopo)
        if esci_quando and not uscito and adesso >= esci_quando:
            uscito = True
            send_club(club_leave(sess.epoca if sess else 0))
            log("[finto] ESCO dalla saletta (LEAVE): l'altro lato deve "
                "accompagnare fuori il suo gioco dalla porta")

        for _ in pronti:
            try:
                data, _addr = sock.recvfrom(2048)
            except OSError:
                break
            parsed = unpack(data)
            if parsed is None:
                continue
            kind, _peer, _room, _seq, body = parsed
            if kind == T_PONG or kind != T_CLUB:
                continue
            club = club_unpack(body)
            if club is None:
                log("[finto] pacchetto club illeggibile: versioni diverse "
                    "dei .py?")
                continue
            sub, epoca, payload = club
            if sess is None:
                if sub not in (CLUB_ENTER, CLUB_STATUS):
                    continue
                if adesso < riaggancio_dopo:
                    continue
                log("[finto] " + "=" * 50)
                log("[finto] il TUO GBA e' al Cable Club: entro in scena")
                finto = FintoGba(log)
                sess = ClubSession(master=(args.peer_id == 1), dev=finto,
                                   send_net=send_club, log=log)
                blocchi_log[0] = 0
                finto.avvia()
            if sub == CLUB_ENTER:
                sess.nota_partner(epoca)
            elif sub == CLUB_LEAVE:
                sess.on_net_leave(epoca)
            elif sub == CLUB_STATUS:
                if payload[2] and payload[2] != VERSIONE_CLUB:
                    log("[finto] ATTENZIONE: il client dall'altra parte ha "
                        "la versione %d, questo banco la %d"
                        % (payload[2], VERSIONE_CLUB))
                sess.on_net_status(epoca, payload[0], payload[1])
            elif sub == CLUB_DATA:
                if blocchi_log[0] < 40:
                    blocchi_log[0] += 1
                    log("[finto] GBA>> %s" % club_block_riga(payload[1]))
                sess.on_net_block(epoca, payload[0], payload[1])
            elif sub == CLUB_REQ:
                sess.on_net_req(epoca, payload)

        if sess is not None:
            finto.tick()
            while finto.stati:
                sess.on_device_status(finto.stati.pop(0))
            while finto.blocchi:
                sess.on_device_block(finto.blocchi.pop(0))
            sess.tick()
            if sess.finita:
                log("[finto] sessione finita | %s" % sess.riassunto())
                log("[finto] %s" % finto.riepilogo())
                log("[finto] %s" % ("VITTORIA PIENA: schede scambiate."
                                    if finto.vittoria else
                                    "prova NON completata: guarda quale "
                                    "comando manca qui sopra."))
                log("[finto] pronto per un altro giro (riparla con la "
                    "signorina quando vuoi).")
                sess = None
                finto = None
                riaggancio_dopo = adesso + 3.0

        if adesso >= ping_quando:
            ping_quando = adesso + 1.0
            sock.sendto(pack(T_PING, args.peer_id, args.room, next_seq(),
                             struct.pack("<d", time.time())), relay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[finto] uscita.")
