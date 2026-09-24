"""
client.py - il ponte di UN giocatore fra il gioco e il relay.

    mGBA  --TCP 127.0.0.1--> client.py --UDP--> relay          (--transport tcp)
    GBA fisico --Celio/USB--> client.py --UDP--> relay         (--transport usb)

Dal 2026-08-02 il "gioco" puo' essere anche un GBA vero: --transport usb parla
con l'adattatore Celio via usb_link.py (thread di lettura dedicato + framing di
sio.c), e tutto il resto - dedup, riordino, PING/PONG, stanza, netem - e'
identico nei due casi. Questo file resta l'UNICO punto che conosce il formato
dei 12 byte del gioco - il relay sopra e il payload sotto non devono saperne
niente.

Tre cose che fa e che non sono ovvie:

  1. DEDUPLICA E RIORDINA. Il protocollo del payload e' nato su un trasporto
     ordinato e affidabile (il socket TCP del Lua). Su UDP un PASSO duplicato
     farebbe fare al remoto un passo in piu', e uno fuori ordine glielo farebbe
     sbagliare. Le PERDITE invece vanno bene: un passo perso si riassorbe al
     primo SYNC, perche' il payload riallinea per qualsiasi scostamento quando
     il remoto e' fermo. E' quella correzione che rende UDP accettabile.

  2. MISURA L'RTT con PING/PONG propri, fuori dal protocollo di gioco: i 12 byte
     restano intatti.

  3. SIMULA LA RETE (--delay, --jitter, --loss). E' il motivo per cui provare da
     soli e' meglio che con un amico sulla stessa wifi: quello darebbe 5 ms e
     nasconderebbe ogni difetto, mentre il progetto si e' dato ~50 ms come
     limite e va verificato.

  4. SCEGLIE LA STANZA, che dal 2026-07-30 e' LA PARTITA e non la mappa. Con
     la stanza = mappa il relay smetteva di inoltrare appena i due giocatori si
     trovavano su mappe diverse, e sono proprio quei due casi che la Fase 7 deve
     far funzionare: l'amico nella route accanto e l'amico dentro una porta.

Uso:
    python client.py --listen 8123 --relay 127.0.0.1:9000 --peer-id 1 --room 1
    python client.py --listen 8124 --relay 127.0.0.1:9000 --peer-id 2 --room 1 --delay 60 --jitter 15
"""

import argparse
import heapq
import json
import os
import queue
import random
import select
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ws_link import WsRelay, e_ws_url  # noqa: E402
from protocol import (  # noqa: E402
    CLUB_DATA, CLUB_ENTER, CLUB_LEAVE, CLUB_REQ, CLUB_STATUS,
    EV_CLUB, EV_STATUS, EVENT_SIZE, IMPRONTA_LUA, IMPRONTA_SORGENTI,
    IMPRONTA_WEB, T_BYE, T_CLUB,
    T_EVENT, T_HELLO, T_PING, T_PONG, VERSIONE_CLUB,
    club_block_riga, club_enter, club_leave, club_unpack,
    extract_map_key, make_leave_event, nome_stato, pack, unpack,
)

# Quanti blocchi del club loggare per direzione, per sessione: lo scambio
# dei dati giocatore sta in ~7 comandi per lato, 80 righe bastano a vedere
# anche l'inizio della stanza e non intasano il registro.
CLUB_LOG_BLOCCHI = 80

PING_INTERVAL = 1.0
STATUS_INTERVAL = 5.0
# Da quanto il GBA deve TACERE prima che il battito si porti dietro la nostra
# ultima posizione. Nell'overworld il payload emette un SYNC al secondo da
# solo (anche da fermi): sotto questa soglia la fotografia sarebbe una copia
# inutile su un canale stretto. Sopra, vuol dire che il payload ha mollato la
# porta - club, menu, lotta - ed e' li' che sparivamo per chi entrava dopo.
PRESENZA_SILENZIO_S = 2.0


class Netem:
    """Ritardo, jitter e perdita applicati a cio' che ESCE verso il relay.

    Applicandolo solo in uscita, il ritardo di sola andata e' esattamente
    --delay e l'andata-ritorno e' il doppio: numeri leggibili senza dover fare
    conti. Se entrambi i client lo impostano, ciascuna direzione ha il suo.
    """

    def __init__(self, delay_ms, jitter_ms, loss_pct):
        self.delay = delay_ms / 1000.0
        self.jitter = jitter_ms / 1000.0
        self.loss = loss_pct / 100.0
        self.queue = []          # heap di (istante_di_invio, contatore, dati)
        self.counter = 0
        self.dropped = 0

    @property
    def active(self):
        return self.delay > 0 or self.jitter > 0 or self.loss > 0

    def submit(self, data, now):
        if self.loss and random.random() < self.loss:
            self.dropped += 1
            return

        wait = self.delay
        if self.jitter:
            wait += random.uniform(-self.jitter, self.jitter)
        if wait < 0:
            wait = 0.0

        self.counter += 1
        heapq.heappush(self.queue, (now + wait, self.counter, data))

    def due(self, now):
        out = []
        while self.queue and self.queue[0][0] <= now:
            out.append(heapq.heappop(self.queue)[2])
        return out

    def next_deadline(self):
        return self.queue[0][0] if self.queue else None


class Bridge:
    @staticmethod
    def _refuse_if_port_busy(port, peer_id):
        """Rifiuta di partire se qualcun altro e' gia' in ascolto su questa porta.

        NON si puo' lasciar fare al bind, ed e' stato misurato: su Windows un
        bind su 127.0.0.1:P riesce **anche** quando un altro processo ha gia'
        legato 0.0.0.0:P, e nessuna opzione di socket lo impedisce -
        SO_EXCLUSIVEADDRUSE compreso, provato. Le due socket coesistono e quella
        con l'indirizzo piu' specifico si prende le connessioni in arrivo.

        E' esattamente quello che e' successo il 2026-07-30: mGBA in modalita'
        diretta aveva la 8123 in wildcard, questo bridge ci si e' legato sopra,
        e la connessione del secondo emulatore e' finita sul bridge sbagliato.
        Nessun errore da nessuna parte.

        L'unica verifica che funziona e' positiva: provare a collegarsi e vedere
        se qualcuno risponde.
        """
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.3)
        try:
            busy = probe.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            busy = False
        finally:
            probe.close()

        if busy:
            raise SystemExit(
                "[client %d] la porta TCP %d e' GIA' IN ASCOLTO.\n"
                "            C'e' un altro bridge acceso, oppure un emulatore avviato\n"
                "            in modalita' diretta (build.ps1 -LinkRole server) sulla\n"
                "            stessa porta. Su Windows i due bind convivono in silenzio\n"
                "            e le connessioni finiscono su quello sbagliato: mi fermo."
                % (peer_id, port))

    def __init__(self, args):
        self.peer_id = args.peer_id
        self.listen_port = args.listen
        self.transport = args.transport
        self.netem = Netem(args.delay, args.jitter, args.loss)

        # IL RELAY SI RAGGIUNGE IN DUE MODI (2026-08-25).
        #
        # UDP diretto (host:porta) e' quello di sempre. Ma la VPS in
        # produzione NON e' raggiungibile in UDP da internet - il filtro sta
        # nella Security List della VCN, non su ufw - mentre la 443 passa da
        # qualunque rete. Quindi un --relay che sia un URL ws:// o wss://
        # apre lo stesso canale del browser: WebSocket verso relay_ws.py, che
        # gira accanto al relay e gli parla in UDP locale. Il protocollo e i
        # 12 byte non cambiano di una virgola.
        self.ws = None
        if e_ws_url(args.relay):
            self.relay_addr = (args.relay, 0)      # solo per i log
            self.udp = None
            self.ws = WsRelay(args.relay, log=self.log)
        else:
            host, _, port = args.relay.partition(":")
            self.relay_addr = (host, int(port))
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp.setblocking(False)

        self.listener = None      # solo transport tcp
        self.game = None          # socket verso mGBA (solo transport tcp)
        self.inbox = b""          # byte dal gioco non ancora divisi in eventi
        self.usb = None           # UsbLink verso il GBA fisico (solo usb)

        if self.transport == "tcp":
            self._refuse_if_port_busy(args.listen, args.peer_id)

            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # NIENTE SO_REUSEADDR: su Windows non significa "riusa una porta in
            # TIME_WAIT" come su Linux, significa "RUBA la porta a chi ce l'ha gia'".
            try:
                self.listener.bind(("127.0.0.1", args.listen))
            except OSError as exc:
                raise SystemExit(
                    "[client %d] porta TCP %d gia' occupata (%s)."
                    % (args.peer_id, args.listen, exc))
            self.listener.listen(1)
        else:
            # Il GBA fisico. L'import sta qui e non in testa al file: chi usa
            # il transport tcp non deve aver bisogno di pyusb.
            from usb_link import UsbLink
            self.usb = UsbLink(timing=args.usb_timing, quiet=False,
                               cable=args.usb_cable)

        # LA STANZA E' LA PARTITA, NON LA MAPPA (decisione del 2026-07-30).
        #
        # Finche' la stanza era l'ID mappa, il relay smetteva di inoltrare appena
        # i due erano su mappe diverse - cioe' proprio nei due casi che la Fase 7
        # deve far funzionare: l'amico nella route accanto e l'amico dentro una
        # porta. Con 2-4 giocatori il traffico e' irrisorio (~12 byte x ~10/s per
        # peer), quindi si inoltra tutto e il filtro "cosa disegno" lo fa il
        # payload, che gia' lo faceva con il mapKey dell'evento.
        #
        # Il relay NON e' stato toccato: per lui le stanze sono id opachi.
        # Conseguenza voluta: il T_BYE ora arriva solo per disconnessione o
        # timeout veri, non piu' a ogni cambio mappa.
        self.room = args.room

        # Il Cable Club integrato (Consegna D): quando il payload avvisa che
        # il GBA e' al club (EV_CLUB), il client passa il Pico in modo LINK e
        # fa lui da ponte per la sessione del gioco, via relay. None = si
        # cammina normalmente.
        self.club = None
        self.club_drop = 0        # eventi di gioco buttati durante il club
        # GBA MUTO dopo un club: finche' il payload non torna a parlare, sul
        # filo non si scrive. Il gioco puo' essere ancora fermo alla schermata
        # del club COL LINK APERTO, e i frame di camminata scritti li' vengono
        # letti dal gioco come dati del link: spazzatura nei blocchi giocatore
        # -> "ALLENATORI di un'altra regione" (campo 2026-08-27, dal sito).
        # Il primo evento VALIDO dal GBA prova che il payload ha la porta.
        self.gba_muto = False
        self.gba_muto_scartati = 0
        self.club_sessions = 0
        self.club_malformati = 0  # corpi T_CLUB illeggibili (versioni diverse?)
        # Dopo una sessione finita, per qualche secondo NON ci si riaggancia
        # da soli agli annunci dell'amico: i suoi pacchetti in volo non devono
        # farci rimbalzare dentro la sessione appena chiusa. L'EV_CLUB del
        # NOSTRO GBA invece riapre sempre.
        self.club_riaggancio_dopo = 0.0
        self.club_stantii = 0              # EVENT_CLUB in quarantena, ignorati
        # Il peer dell'amico al bancone, per la regola dei ruoli. Vale per LA
        # SESSIONE: si azzera quando finisce, o al bancone dopo si deciderebbe
        # il ruolo contro chi c'era la volta scorsa.
        self.club_partner_peer = None
        # Le TRANSIZIONI di stato, mie e dell'amico (2026-08-21): una riga
        # per cambio, non per evento. Sul fisico e' l'unico modo di
        # vedere se il GBA emette lo stato giusto e se all'altro arriva.
        self.stato_io = None
        self.stato_amici = {}
        self.stato_cambi = 0
        # LA MAPPA LIVE (2026-08-21): le posizioni (mie e degli amici) vanno
        # in net/posizioni-<peer>.json, riscritto atomicamente al massimo 4
        # volte al secondo; il pannello le fonde in /api/posizioni e
        # mappa.html le disegna. Niente rete in piu': e' una vista locale
        # degli stessi eventi che gia' passano di qui.
        self.pos_io = None
        self.pos_amici = {}
        self.pos_sporche = False
        self.pos_prossima = 0.0
        self.pos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posizioni")
        self.pos_path = os.path.join(self.pos_dir, "posizioni-%d.json" % self.peer_id)
        self.pos_ultima_scrittura = 0.0
        self.club_versione_detta = False   # la denuncia si fa UNA volta

        self.out_seq = 0
        self.last_seq = {}        # peer_id -> ultimo seq accettato
        self.peer_room = {}       # peer_id -> mappa del suo ultimo evento
        # LA PRESENZA DI CHI STA FERMO (2026-08-28).
        #
        # Il relay non inoltra gli HELLO e non conserva niente (relay.py,
        # ramo T_HELLO): in stanza si ESISTE solo quando si parla. Finche' si
        # cammina va bene - il payload emette un SYNC assoluto al secondo - ma
        # al Cable Club, nei menu e in lotta il GBA MOLLA la porta seriale e
        # tace: chi entra dopo non sa che siamo qui, ne' come avatar ne' come
        # partner di scambio. Era il difetto "mi collego prima dell'amico e
        # non ci troviamo mai".
        #
        # Rimedio identico a quello che il Lua dell'emulatore ha gia'
        # (inject_body.lua, ws.lastEvent): si conserva una FOTOGRAFIA
        # dell'ultima posizione assoluta e la si rimanda sul battito, quando
        # si (ri)entra in stanza e come risposta al primo evento di un peer
        # mai visto. Il relay non si tocca: la presenza si ripara ai bordi.
        self.last_event = None    # 12 byte, tipo forzato a SYNC
        self.presenze_rimandate = 0
        self.ultimo_evento_at = 0.0   # quando il GBA ha parlato l'ultima volta
        # FINO A 4 GIOCATORI (2026-08-25): il payload distingue i remoti dal
        # nibble alto del type (slot 0..2), e ad assegnarlo e' QUESTO client,
        # l'unico che conosce i peer. Primo evento -> primo slot libero;
        # T_BYE -> lo slot torna libero. Il quarto amico in su non ha avatar
        # sul GBA (16 object event per mappa: il tetto sta nel motore) ma
        # resta visibile sulla mappa live.
        self.peer_slots = {}      # peer_id -> slot avatar 0..2
        self.slot_free = [0, 1, 2]
        self.slot_pieni_avvisati = set()
        self.slot_scartati = 0    # eventi non consegnati al GBA: niente slot
        self.map_key = None       # mappa del nostro ultimo evento, per il log
        self.pending_ping = {}    # seq -> istante di invio
        self.leaves = 0
        # Il VA-E-VIENI (2026-08-30): quando ogni peer se n'e' andato l'ultima
        # volta, e quante volte e' tornato troppo in fretta. `flap` deve stare
        # a ZERO in una partita sana - se sale, dall'altra parte c'e' un
        # adattatore staccato o una pagina che non riesce a parlare col relay.
        self.peer_andato_at = {}  # peer_id -> time.monotonic() del suo T_BYE
        self.flap = 0

        self.sent = 0
        self.received = 0
        self.dup_dropped = 0
        self.late_dropped = 0
        self.rtts = []

        # Ricucitura dei passi persi (2026-08-02): per ogni peer, l'ultima
        # posizione DICHIARATA dal suo flusso di eventi. Non e' la posizione
        # disegnata dal gioco: e' la catena dei suoi PASSO/GIRA/SYNC, quindi un
        # buco qui dentro significa "un evento e' andato perso", mai "il passo
        # e' ancora in volo".
        self.peer_pos = {}        # peer_id -> (map_key, x, y)
        self.healed = 0           # passi sintetizzati per chiudere i buchi
        self.heal_far = 0         # buchi troppo larghi, lasciati al resync
        self.no_heal = args.niente_ricucitura

        # DOPPIO INVIO SULL'ULTIMO TRATTO (2026-08-02, blocco N).
        #
        # La ricucitura qui sopra copre i buchi che ARRIVANO dal relay. Ma il
        # tratto che perde di piu' e' quello DOPO, dal client al gioco: sul GBA
        # fisico e' il SIO, misurato all'8% di frame rotti nella direzione
        # opposta. Nessuno lo ricuce, perche' quando l'evento sparisce li' e'
        # gia' uscito da questo processo, e il payload se ne accorge solo al
        # passo dopo (rammendo, cioe' fermo e poi scivolata).
        #
        # Per quel tratto non serve cucire: serve non perdere. Ogni PASSO e ogni
        # GIRA si mandano due volte, la copia la scarta il payload sul seq
        # (ConsumeRemoteEvents, blocco DOPPIO INVIO). Con l'8% di perdita per
        # copia, perderle entrambe e' lo 0,6%.
        #
        # Solo PASSO e GIRA: un SYNC ripetuto e' idempotente e ne arriva uno al
        # secondo comunque, lo STATO ha gia' le sue ripetizioni, il VIA lo
        # fabbrica questo client con seq 0 e il payload non lo deduplica
        # apposta - mandarlo doppio significherebbe applicarlo due volte.
        # LE COPIE HANNO UN TETTO, e non e' pignoleria: il canale SIO porta 226
        # parole/s e un frame ne pesa 9, cioe' 25 frame/s in tutto. A piedi si
        # producono ~4 passi/s (36 parole/s: raddoppiare non si sente), ma con
        # la mach bike si arriva a ~15 passi/s, e li' il doppio invio da solo
        # sfonderebbe il canale - la coda crescerebbe senza fine e il ritardo
        # con lei. Il secchiello limita le SOLE copie a COPY_RATE al secondo:
        # sotto quella soglia la ridondanza c'e' tutta, sopra decade da sola
        # invece di intasare. Le copie saltate si contano: un rimedio che si
        # spegne in silenzio e' peggio di nessun rimedio.
        self.wire_copies = max(1, args.copie)
        self.wire_loss = max(0.0, args.perdita_filo) / 100.0
        self.wire_sent = 0        # frame effettivamente scritti verso il gioco
        self.wire_dropped = 0     # buttati dal simulatore di perdita sul filo
        self.copies_skipped = 0   # copie non mandate per non intasare il filo
        self.copy_tokens = float(self.COPY_RATE)
        self.copy_stamp = time.monotonic()

        # Come si legge il relay nel log: l'URL intero se e' WebSocket, host e
        # porta se e' UDP. Serve a saperlo a colpo d'occhio quando qualcosa
        # non arriva: i due percorsi hanno guasti diversi.
        dove = (args.relay if self.ws is not None
                else "%s:%d" % (self.relay_addr[0], self.relay_addr[1]))
        if self.transport == "tcp":
            self.log("in ascolto su TCP 127.0.0.1:%d, relay %s, peer %d, stanza %d"
                     % (args.listen, dove, self.peer_id, self.room))
        else:
            self.log("transport USB (Celio), relay %s, peer %d, stanza %d"
                     % (dove, self.peer_id, self.room))
        # IL PEER-ID DEVE ESSERE UNICO, e sbagliarlo non da' nessun errore.
        # Due client con lo stesso peer-id si espellono a vicenda dalla stanza
        # a ogni battito (relay.py, move_to_room: stesso peer_id da un altro
        # indirizzo = il vecchio viene tolto), quindi il sintomo e' "non ci
        # vediamo mai", identico a mille altre cause. Meglio dirlo prima.
        self.log("peer-id %d - DEVE essere diverso da quello di OGNI amico "
                 "nella stanza, o vi buttate fuori a vicenda" % self.peer_id)
        # I DUE NUMERI DA CONFRONTARE A VOCE prima di provare il club. Se
        # non combaciano fra i due PC, il club non puo' funzionare: e' cio'
        # che e' successo il 2026-08-20, quando la correzione era su un lato
        # solo e nessuno poteva accorgersene se non dai log affiancati.
        self.log("versione club %d | impronta dei file %08X  (deve combaciare "
                 "con quella dell'amico)" % (VERSIONE_CLUB, IMPRONTA_SORGENTI))
        if self.netem.active:
            self.log("simulatore attivo: ritardo %d ms, jitter %d ms, perdita %g%%"
                     % (args.delay, args.jitter, args.loss))
        if self.transport == "tcp":
            self.log("ORA avvia mGBA: socket.connect del Lua e' bloccante e "
                     "l'emulatore si ferma finche' non trova questa porta aperta")

    def log(self, msg):
        print("[client %d] %s" % (self.peer_id, msg), flush=True)

    # --- verso il relay ---------------------------------------------------

    def send_udp(self, datagram, now, simulate=True):
        if simulate and self.netem.active:
            self.netem.submit(datagram, now)
        else:
            self._flush_one(datagram)

    def _flush_one(self, datagram):
        try:
            if self.ws is not None:
                self.ws.send(datagram)
            else:
                self.udp.sendto(datagram, self.relay_addr)
        except OSError as exc:
            self.log("invio al relay fallito: %s" % exc)

    def next_seq(self):
        self.out_seq = (self.out_seq + 1) & 0xFF
        return self.out_seq

    def forward_event(self, event, now):
        # Il GBA e' tornato a parlare: il payload ha ripreso la porta, si puo'
        # tornare a scrivergli (vedi gba_muto nel costruttore).
        if self.gba_muto:
            self.gba_muto = False
            self.log("[club ] il GBA e' tornato a parlare (%d frame trattenuti "
                     "nel frattempo): riprendo a scrivergli"
                     % self.gba_muto_scartati)
            self.gba_muto_scartati = 0

        # L'AVVISO DEL CLUB non e' un evento di gioco e non si inoltra come
        # tale: il payload dell'amico non saprebbe che farsene (e peggio,
        # tenterebbe di interpretarlo). Diventa l'avvio della sessione club.
        # Arriva in tre copie: club_start e' idempotente.
        if event and event[0] == EV_CLUB:
            # ANCHE l'avviso del NOSTRO GBA rispetta la quarantena dopo un
            # club appena chiuso. Non e' un caso raro: il payload ri-latcha
            # a ogni riapertura del link (macchina degli scambi, lotta) e
            # accoda EVENT_CLUB che non possono uscire finche' il Pico e' in
            # modo link; alla fine della sessione arrivano tutti insieme e
            # senza questo filtro riaprivano un club dove nessuno era al
            # bancone - i personaggi restavano congelati (campo 2026-08-21).
            # Ripresentarsi DAVVERO alla signorina entro 10 s dall'uscita
            # dalla saletta e' fisicamente impossibile: c'e' il warp.
            if time.monotonic() < self.club_riaggancio_dopo:
                self.club_stantii += 1
                if self.club_stantii == 1:
                    self.log("[club ] EVENT_CLUB stantio del club appena "
                             "chiuso: ignorato (il payload li accoda a ogni "
                             "riapertura del link)")
                return
            self.club_start("il TUO GBA e' entrato al Cable Club",
                            dal_gba=True)
            return

        # LA TRANSIZIONE DI STATO VA NEL LOG (2026-08-21). Tre sessioni di
        # "scheda bianca" sono passate senza che nessuna riga dicesse se il
        # GBA EMETTE zaino/squadra/..., se il PC lo riceve, o se si perde
        # dopo: la riga [stato] esisteva solo nel Lua dell'emulatore. Qui si
        # scrive ogni CAMBIO (le 4 copie e i battiti restano muti).
        if event and event[0] == EV_STATUS:
            if event[1] != self.stato_io:
                self.stato_io = event[1]
                self.stato_cambi += 1
                self.log("[stato ] io -> %s  (dal GBA, #%d)"
                         % (nome_stato(event[1]), event[3]))
        self.pos_io = self._aggiorna_pos(self.pos_io, event)

        # La stanza e' fissa: si inoltra tutto, mappa compresa. Chi decide se
        # quell'evento e' disegnabile e' il payload, che conosce le connessioni
        # fra le mappe - cosa che qui non sapremmo comunque.
        #
        # La mappa dell'evento serve ancora, ma solo per il tracking locale:
        # vedi peer_room in handle_udp.
        self.map_key = extract_map_key(event)

        self.ricorda_presenza(event)
        self.ultimo_evento_at = now
        self.send_udp(pack(T_EVENT, self.peer_id, self.room, self.next_seq(),
                           event), now)
        self.sent += 1

    def ricorda_presenza(self, event):
        """La fotografia dell'ultima posizione assoluta, da rispedire.

        Si conserva una FOTOGRAFIA, non un passo da ripetere: rimandare un
        PASSO farebbe muovere il nostro avatar una seconda volta a casa
        dell'amico. PASSO (1), SYNC (2) e GIRA (3) portano tutti mappa e
        coordinate, quindi si forza il tipo a SYNC lasciando intatto il nibble
        alto (in uscita e' 0 - lo slot lo timbra il client di chi riceve - ma
        il codice non ci fa affidamento). Stessa scelta di relaySend nel Lua.
        """
        if len(event) < EVENT_SIZE:
            return
        if 1 <= (event[0] & 0x0F) <= 3:
            self.last_event = bytes([(event[0] & 0xF0) | 2]) + event[1:EVENT_SIZE]

    def rimanda_presenza(self, now, sempre=False):
        """"Ci sono anche io, e sono qui": la fotografia rispedita.

        SOLO QUANDO IL GBA TACE, e non e' un'ottimizzazione: nell'overworld il
        payload emette gia' un SYNC assoluto al secondo anche da fermi, quindi
        li' una copia in piu' sarebbe solo banda - e la banda verso il GBA e'
        stretta davvero (226 parole/s, ~25 frame/s in tutto). Il buco che
        questo rimedio chiude e' l'altro: al Cable Club, nei menu e in lotta
        il payload MOLLA la porta seriale e non dice piu' niente.

        `sempre` salta il gate: e' la risposta al primo evento di un peer mai
        visto, dove il punto e' farsi vedere SUBITO da chi e' appena entrato,
        non fra un secondo.
        """
        if self.last_event is None:
            return
        if not sempre and now - self.ultimo_evento_at < PRESENZA_SILENZIO_S:
            return
        self.send_udp(pack(T_EVENT, self.peer_id, self.room, self.next_seq(),
                           self.last_event), now)
        self.presenze_rimandate += 1

    def send_ping(self, now):
        seq = self.next_seq()
        self.pending_ping[seq] = now
        # Il PING non passa dal simulatore: misuriamo la rete VERA. Il ritardo
        # finto lo conosciamo gia', non serve misurarlo.
        self.send_udp(pack(T_PING, self.peer_id, self.room, seq), now,
                      simulate=False)
        # Il battito porta con se' la presenza: e' il solo pacchetto che parte
        # anche quando il GBA tace (club, menu, lotta).
        self.rimanda_presenza(now)

    # --- dal relay --------------------------------------------------------

    def accept_seq(self, peer_id, seq):
        """Vero se questo datagramma e' nuovo e non e' arretrato.

        Il seq e' un byte e riavvolge, quindi non si confronta con `>`: si
        guarda la distanza in modulo 256. Fino a 127 avanti = piu' recente,
        oltre = arretrato. Con ~8 eventi al secondo e pochi in volo, il margine
        e' enorme.
        """
        last = self.last_seq.get(peer_id)
        if last is None:
            self.last_seq[peer_id] = seq
            return True

        delta = (seq - last) & 0xFF
        if delta == 0:
            self.dup_dropped += 1
            return False
        if delta >= 128:
            self.late_dropped += 1
            return False

        self.last_seq[peer_id] = seq
        return True

    def handle_udp(self, data, now):
        parsed = unpack(data)
        if parsed is None:
            return

        kind, peer_id, room_id, seq, body = parsed

        if kind == T_PONG:
            sent_at = self.pending_ping.pop(seq, None)
            if sent_at is not None:
                self.rtts.append((now - sent_at) * 1000.0)
            return

        if kind == T_BYE:
            # Con la stanza fissa questo non arriva piu' a ogni cambio mappa:
            # arriva per una disconnessione o un timeout VERI. Il despawn al
            # cambio mappa lo governa ora il payload, che sa quali mappe sono
            # connesse alla nostra e quali no.
            #
            # Il gioco non ha nessun modo di dire "me ne vado", quindi qui il
            # T_BYE diventa un evento nel formato che il payload gia' capisce,
            # senza toccare il relay.
            #
            # Si dimentica il seq: se lo stesso peer torna, ripartira' da un
            # numero qualsiasi e senza questo lo scarteremmo come arretrato.
            room = self.peer_room.pop(peer_id, None)
            self.last_seq.pop(peer_id, None)
            # Anche la catena delle posizioni: se torna, riparte da zero e il
            # primo evento non va "ricucito" contro una posizione di ieri.
            self.peer_pos.pop(peer_id, None)
            # Lo slot avatar torna libero, e il VIA parte TIMBRATO con lo
            # slot che l'amico occupava: il payload deve togliere il SUO
            # avatar, non quello dello slot 0. Un amico senza slot non ha
            # niente da despawnare. Il set degli avvisi si svuota: con uno
            # slot appena liberato il prossimo quarto amico lo prende.
            slot = self.peer_slots.pop(peer_id, None)
            if slot is not None:
                self.slot_free.append(slot)
                self.slot_free.sort()
                self.slot_pieni_avvisati.clear()
            # Quando se n'e' andato: serve a slot_for per riconoscere il
            # VA-E-VIENI (2026-08-30). Il commento qui sopra dice "timeout
            # VERI", ed e' quell'assunzione che il difetto del 30/08 ha rotto:
            # un amico col Pico staccato spariva e ricompariva ogni ~30 s,
            # perche' il suo sito continuava a ripubblicare la presenza. Il
            # rimedio sta dalla sua parte (bridge.js), ma senza una spia qui
            # nessuno se ne sarebbe accorto leggendo il registro.
            #
            # L'ISTANTE SE LO LEGGE DA SOLO, invece di usare il `now` che
            # arriva da fuori: questi due timbri (qui e in slot_for) si
            # confrontano SOLO fra loro, e prendendoli dalla stessa sorgente
            # non possono divergere. Con il `now` del chiamante funzionava in
            # produzione - li' e' monotonic - e taceva sotto test, dove il
            # tempo lo passa il test: cioe' proprio dove doveva parlare.
            self.peer_andato_at[peer_id] = time.monotonic()
            if room is None:
                self.log("peer %d se n'e' andato (mai visto un suo evento)" % peer_id)
                return
            self.log("peer %d se n'e' andato (era su mappa %d.%d): despawn"
                     % (peer_id, room >> 8, room & 0xFF))
            self.leaves += 1
            if slot is not None:
                self.deliver(make_leave_event(room), slot)
            return

        if kind == T_CLUB:
            # La sessione del club ha il SUO dedup (numeri di serie degli
            # stati, sequenze dei blocchi): il filtro accept_seq qui farebbe
            # solo danno - su UDP un datagramma riordinato verrebbe buttato
            # come "arretrato" e la rispedizione lo ripagherebbe.
            self.handle_club_net(peer_id, body, now)
            return

        if kind != T_EVENT or len(body) < EVENT_SIZE:
            return

        # Il primo evento di un peer mai visto vale come "ci sono, e tu?": gli
        # si risponde con la nostra ultima posizione assoluta. Va guardato
        # PRIMA di accept_seq, che e' proprio la funzione che registra il peer.
        peer_nuovo = peer_id not in self.last_seq

        if not self.accept_seq(peer_id, seq):
            return

        event = body[:EVENT_SIZE]
        # La mappa dell'ultimo evento di questo peer: serve al T_BYE, che deve
        # dire al payload SU QUALE mappa l'avatar da togliere si trovava.
        peer_map = extract_map_key(event)
        if peer_map is not None:
            self.peer_room[peer_id] = peer_map

        self.received += 1
        self.heal_and_deliver(peer_id, event)
        if peer_nuovo:
            self.rimanda_presenza(now, sempre=True)

    # --- ricucitura dei passi persi ---------------------------------------
    #
    # IL DIFETTO CHE CHIUDE (misurato il 2026-08-02, prima sessione a due
    # giocatori sul fisico): il canale USB GBA->PC perde ~4% dei frame, quindi
    # ogni tanto un PASSO sparisce. Il PASSO successivo arriva a DUE tile
    # dall'ultima posizione nota, la regola del payload "un passo e' un tile"
    # lo degrada a SYNC, e SYNC = TeleportRemote: l'amico scatta di un tile.
    # Contatori di quella sessione: passi implausibili 9, correzioni 13 - uno
    # scatto visibile ogni pochi secondi di camminata.
    #
    # LA CURA STA QUI E NON NEL PAYLOAD per un motivo di bilancio: il payload
    # -WithSio ha 56 byte liberi in EWRAM, il PC ne ha a piacere. E il posto e'
    # anche concettualmente giusto: la perdita e' del TRASPORTO (USB oggi, UDP
    # con l'amico remoto domani), quindi la ripara il livello di trasporto.
    # Il payload resta il backstop per cio' che si perde DOPO il client
    # (PC->GBA via SIO), con la stessa regola di prima.
    #
    # Come: se un evento con coordinate arriva a 1..HEAL_MAX tile dall'ultima
    # posizione dichiarata sulla STESSA mappa, i tile mancanti diventano PASSO
    # sintetici (stessa velocita' e stato dell'evento vero) e il payload anima
    # una camminata invece di teletrasportare. Su mappa diversa non si tocca
    # niente: il confine ha la sua logica nel payload (bordo, porte, resync) e
    # ricucire li' significherebbe combatterla. Oltre HEAL_MAX nemmeno: un buco
    # largo e' un warp o una disconnessione, e il resync e' la risposta giusta.

    EV_STEP, EV_SYNC, EV_TURN = 1, 2, 3
    HEAL_MAX = 3
    _DIR_DX = {1: 0, 2: 0, 3: -1, 4: 1}   # giu, su, sx, dx (sDirDelta* del payload)
    _DIR_DY = {1: 1, 2: -1, 3: 0, 4: 0}

    # Quanto in fretta deve tornare un amico perche' sia un VA-E-VIENI e non
    # una riconnessione normale. Trenta secondi e' il timeout del relay
    # WebSocket: sotto quella soglia il giro "sparisco per timeout, ricompaio
    # alla riconnessione" e' l'unica spiegazione plausibile.
    FLAP_S = 60.0

    def slot_for(self, peer_id):
        """Lo slot avatar (0..2) di questo amico presso il NOSTRO GBA, o None
        se i tre slot sono occupati. L'assegnazione e' locale al ricevente:
        ognuno numera i propri amici per conto suo, e il numero viaggia solo
        sull'ultimo tratto client -> GBA (nibble alto del type)."""
        slot = self.peer_slots.get(peer_id)
        if slot is None:
            if self.slot_free:
                slot = self.slot_free.pop(0)
                self.peer_slots[peer_id] = slot
                # IL VA-E-VIENI (2026-08-30). Se lo stesso amico riprende un
                # avatar poco dopo essersene andato, non e' una riconnessione:
                # e' un ciclo. Nel registro del 30/08 questo e' andato avanti
                # dieci minuti e nessuna riga lo diceva - si vedevano solo un
                # "avatar slot" e un "se n'e' andato" alternati, che presi uno
                # per uno sembrano normali. A TRANSIZIONE: la prima volta e
                # poi ogni dieci, o il rimedio diventerebbe rumore.
                andato = self.peer_andato_at.pop(peer_id, None)
                if andato is not None and (time.monotonic() - andato) < self.FLAP_S:
                    self.flap += 1
                    if self.flap == 1 or self.flap % 10 == 0:
                        self.log("amico %d VA E VIENE (n. %d): sparisce e torna in "
                                 "meno di %ds. Di solito e' il suo adattatore "
                                 "scollegato, o la sua pagina che non arriva al relay."
                                 % (peer_id, self.flap, int(self.FLAP_S)))
                else:
                    self.log("amico %d -> avatar slot %d" % (peer_id, slot))
            elif peer_id not in self.slot_pieni_avvisati:
                self.slot_pieni_avvisati.add(peer_id)
                self.log("amico %d SENZA avatar: 3 amici gia' a schermo "
                         "(resta sulla mappa live)" % peer_id)
        return slot

    def heal_and_deliver(self, peer_id, event):
        slot = self.slot_for(peer_id)
        if self.no_heal:
            # Solo per il collaudo del RAMMENDO nel payload: con la ricucitura
            # del client accesa i buchi non arrivano mai al gioco, e il ramo
            # del payload resterebbe non esercitato (la regola dei contatori:
            # un ramo mai acceso e' un ramo mai provato).
            self.deliver(event, slot)
            return
        kind = event[0]
        if kind not in (self.EV_STEP, self.EV_SYNC, self.EV_TURN):
            # VIA e STATO non portano una posizione: non aggiornano la catena
            # e non si ricuciono.
            if kind == EV_STATUS and self.stato_amici.get(peer_id) != event[1]:
                self.stato_amici[peer_id] = event[1]
                self.log("[stato ] amico %d -> %s  (dalla rete, verso il GBA)"
                         % (peer_id, nome_stato(event[1])))
            if kind == self.EV_LEAVE_POS:
                self.pos_amici.pop(peer_id, None)
                self.pos_sporche = True
            else:
                self.pos_amici[peer_id] = self._aggiorna_pos(
                    self.pos_amici.get(peer_id), event)
            self.deliver(event, slot)
            return

        self.pos_amici[peer_id] = self._aggiorna_pos(self.pos_amici.get(peer_id), event)
        map_key = extract_map_key(event)
        x, y = struct.unpack_from("<hh", event, 6)
        last = self.peer_pos.get(peer_id)
        self.peer_pos[peer_id] = (map_key, x, y)

        if last is None or last[0] != map_key:
            self.deliver(event, slot)
            return

        cx, cy = last[1], last[2]

        # SI RICUCE SOLO IN LINEA RETTA, ED E' UNA REGOLA DI SICUREZZA, non di
        # semplicita' (2026-08-02, sera): le movement action del remoto NON
        # controllano le collisioni (il pass-through e' obbligatorio), quindi
        # un passo sintetico puo' attraversare qualunque muro. Un buco DRITTO
        # e' sicuro per costruzione - il giocatore vero ha camminato proprio
        # su quei tile, che quindi sono calpestabili. Un buco a L no: il
        # giocatore puo' aver AGGIRATO un angolo, e la linea d'aria ci passa
        # ATTRAVERSO - e' il "remoto che passa nei muri" visto in M-1. I buchi
        # a L si consegnano com'e': il payload li degrada a riposizionamento,
        # uno scatto raro ma mai un muro attraversato.
        #
        # Fin dove ricucire: per un PASSO fino al tile di PARTENZA del passo
        # vero (l'ultimo tratto lo anima l'evento vero, con la sua direzione);
        # per SYNC e GIRA fino al tile dichiarato.
        if kind == self.EV_STEP:
            if abs(x - cx) + abs(y - cy) <= 1:
                # Un passo normale, o un GIRA travestito: si consegna com'e'.
                self.deliver(event, slot)
                return
            ddx = self._DIR_DX.get(event[1])
            if ddx is None:
                self.deliver(event, slot)
                return
            ddy = self._DIR_DY[event[1]]
            tx, ty = x - ddx, y - ddy
            # Il tratto da ricucire (ultima posizione -> partenza del passo
            # vero) deve stare TUTTO nella direzione del passo: e' il caso
            # "camminava dritto e ho perso un passo", l'unico sicuro.
            if ddx != 0 and (ty != cy or (tx - cx) * ddx < 0):
                self.deliver(event, slot)
                return
            if ddy != 0 and (tx != cx or (ty - cy) * ddy < 0):
                self.deliver(event, slot)
                return
        else:
            # SYNC/GIRA: si ricuce solo se il buco e' su un asse solo.
            if x != cx and y != cy:
                self.deliver(event, slot)
                return
            tx, ty = x, y

        gap = abs(tx - cx) + abs(ty - cy)
        if gap == 0:
            self.deliver(event, slot)
            return
        if gap > self.HEAL_MAX:
            self.heal_far += 1
            self.deliver(event, slot)
            return

        # OGNI PASSO SINTETICO PRENDE UN SUO seq, e non e' cosmetico: da quando
        # il payload scarta le copie confrontando il seq con quello dell'ultimo
        # evento consumato (DOPPIO INVIO), tre eventi consecutivi con lo stesso
        # numero verrebbero visti come un evento e due copie - cioe' la
        # ricucitura sparirebbe in silenzio, proprio nel caso che deve curare.
        # I numeri giusti sono quelli che sono andati persi: S-gap ... S-1.
        synth_seq = (event[3] - gap) % 256

        while (cx, cy) != (tx, ty):
            # Un asse alla volta, prima quello col residuo piu' grande: per i
            # buchi da un tile e' indifferente, per quelli a L produce l'angolo
            # piu' naturale.
            if abs(tx - cx) >= abs(ty - cy):
                step_dir = 4 if tx > cx else 3
                cx += 1 if tx > cx else -1
            else:
                step_dir = 1 if ty > cy else 2
                cy += 1 if ty > cy else -1
            synth = bytearray(event)
            synth[0] = self.EV_STEP
            synth[1] = step_dir
            synth[3] = synth_seq
            synth_seq = (synth_seq + 1) % 256
            struct.pack_into("<hh", synth, 6, cx, cy)
            self.deliver(bytes(synth), slot)
            self.healed += 1

        self.deliver(event, slot)

    # I TIPI CHE SI MANDANO IN COPIA. Devono restare allineati con il dedup del
    # payload (ConsumeRemoteEvents): duplicare un tipo che il payload NON
    # deduplica significa applicarlo due volte.
    DUP_TYPES = (EV_STEP, EV_TURN)

    # Copie al secondo concesse dal secchiello (vedi __init__). 8 = il doppio
    # invio integrale fino a poco oltre l'andatura di corsa.
    COPY_RATE = 8

    def _copy_allowed(self):
        now = time.monotonic()
        self.copy_tokens = min(float(self.COPY_RATE),
                               self.copy_tokens
                               + (now - self.copy_stamp) * self.COPY_RATE)
        self.copy_stamp = now
        if self.copy_tokens < 1.0:
            self.copies_skipped += 1
            return False
        self.copy_tokens -= 1.0
        return True

    # --- la mappa live ---------------------------------------------------
    EV_LEAVE_POS = 4

    def _aggiorna_pos(self, pos, event):
        """Da un evento di 12 byte alla posizione: PASSO/SYNC/GIRA portano
        mappa, griglia e direzione; STATO porta l'ultima posizione nota e
        lo stato (campo dir). Le coordinate restano di GRIGLIA (mappa + 7):
        e' mappa.html a sottrarre MAP_OFFSET, come fa il payload."""
        if not event or len(event) < EVENT_SIZE:
            return pos
        kind = event[0]
        if kind not in (1, 2, 3, EV_STATUS):
            return pos
        x, y = struct.unpack_from("<hh", event, 6)
        nuovo = dict(pos or {})
        nuovo.update({"gruppo": event[4], "numero": event[5], "x": x, "y": y,
                      "genere": event[10], "avatar": event[11],
                      "t": time.time()})
        if kind == EV_STATUS:
            nuovo["stato"] = event[1]
        else:
            nuovo["dir"] = event[1]
            nuovo["stato"] = 0
            # La VELOCITA' (byte 2, SPEED_*) serve alla mappa live per
            # distinguere la corsa dalla camminata: avatarState non la
            # distingue, resta NORMAL in entrambi i casi (payload main.c:954).
            # Solo da PASSO/SYNC/GIRA: EmitStatus manda speed 0 fisso
            # (main.c:1095) e azzererebbe la corsa a ogni battito di stato.
            nuovo["speed"] = event[2]
        self.pos_sporche = True
        return nuovo

    def scrivi_posizioni(self, now):
        """Al massimo 10 volte al secondo, solo se e' cambiato qualcosa,
        scrittura atomica (tmp + replace): il pannello puo' leggere in
        qualunque istante e non trova mai un file a meta'."""
        # Battito di vita: anche senza novita' il file si riscrive ogni 2 s,
        # cosi' il suo `t` dice "questo client e' vivo" e il pannello puo'
        # ignorare le viste di client chiusi (o dei test) senza confonderle
        # con un giocatore fermo.
        if self.pos_io is None and not self.pos_amici:
            return
        if now < self.pos_prossima:
            return
        if not self.pos_sporche and now - self.pos_ultima_scrittura < 2.0:
            return
        # 0,1 s (era 0,25 fino al 2026-08-26). La mappa riceve POSIZIONI, non
        # passi: campionando ogni quarto di secondo, di una corsa (un tile ogni
        # ~133 ms) le arrivava un tile su due e di una pedalata (~66 ms) uno su
        # quattro, e i tile intermedi erano persi per sempre. La mappa ora anima
        # anche i salti brevi come passi veri, ma con campioni piu' fitti il
        # movimento e' piu' vicino a quello vero e il ritardo scende (misurato
        # in bici: da 2,9 tile a meno di 1). Il costo e' una scrittura di poche
        # centinaia di byte 10 volte al secondo, sostituita atomicamente.
        self.pos_prossima = now + 0.10
        self.pos_sporche = False
        self.pos_ultima_scrittura = now
        dati = {"peer": self.peer_id, "io": self.pos_io,
                "amici": {str(k): v for k, v in self.pos_amici.items()},
                "t": time.time()}
        tmp = self.pos_path + ".tmp"
        try:
            os.makedirs(self.pos_dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dati, f)
            os.replace(tmp, self.pos_path)
        except OSError as exc:
            # Non e' un motivo per fermare la partita: si ritenta al giro dopo.
            self.pos_sporche = True
            if not getattr(self, "_pos_avvisato", False):
                self._pos_avvisato = True
                self.log("[mappa] non riesco a scrivere %s: %s" % (self.pos_path, exc))

    def deliver(self, event, slot=0):
        # Niente slot = niente avatar sul GBA: l'evento si ferma qui (la
        # mappa live e' gia' stata aggiornata da chi ci ha chiamato).
        if slot is None:
            self.slot_scartati += 1
            return
        copies = 1
        if event[0] in self.DUP_TYPES:
            while copies < self.wire_copies and self._copy_allowed():
                copies += 1
        # IL TIMBRO DELLO SLOT, nel nibble alto del type: e' il contratto con
        # EVENT_SLOT del payload. Si timbra DOPO la decisione sulle copie
        # (DUP_TYPES confronta il tipo nudo) e una volta sola per le copie.
        if slot:
            event = bytes((event[0] | (slot << 4),)) + event[1:]
        for _ in range(copies):
            # Il simulatore di perdita SUL FILO. Non e' --loss, che agisce in
            # uscita verso il relay ed e' quindi curabile dalla ricucitura del
            # peer: questo modella il tratto client->gioco, l'unico che nessuno
            # ricuce, ed e' il banco di prova in emulatore del SIO del GBA.
            if self.wire_loss and random.random() < self.wire_loss:
                self.wire_dropped += 1
                continue
            self.wire_sent += 1
            self._deliver_one(event)

    # --- il Cable Club integrato (Consegna D) -----------------------------
    #
    # La sessione vive in club_link.ClubSession; qui c'e' solo l'orchestrazione
    # locale: entrare (Pico in modo LINK), pompare stati/blocchi fra device e
    # relay, uscire (Pico di nuovo in passthrough). Tutto da solo: niente
    # celi0.link, niente .bat, niente mani sul cavo - richiesta di Lain del
    # 2026-08-09.

    class _ClubDev:
        """L'adattatore fra ClubSession e UsbLink: i comandi non devono poter
        uccidere il processo (l'endpoint inceppato alza SystemExit in _cmd)."""

        def __init__(self, usb, log):
            self.usb = usb
            self.log = log
            self.errori = 0
            self.consegnati = 0

        def command(self, cmd, label):
            try:
                self.usb.club_command(cmd, label)
                self.log("[club ] -> device: %s" % label)
            except SystemExit as exc:
                self.errori += 1
                self.log("[club ] comando al device FALLITO (%s)" % exc)

        def send_block(self, block64):
            # Il contenuto va nel log PRIMA dell'invio: se il canale muore
            # qui, sapere COSA stava passando vale piu' del contatore.
            self.consegnati += 1
            if self.consegnati <= CLUB_LOG_BLOCCHI:
                self.log("[club ] >>GBA %s" % club_block_riga(block64))
            if not self.usb.club_send_block(block64):
                self.errori += 1

    def send_club(self, body):
        self.send_udp(pack(T_CLUB, self.peer_id, self.room, self.next_seq(),
                           body), time.time())

    def club_start(self, motivo, dal_gba=False):
        if self.club is not None:
            return
        if not dal_gba and time.monotonic() < self.club_riaggancio_dopo:
            # Sessione appena chiusa: gli annunci dell'amico ancora in volo
            # non devono farci rimbalzare dentro. L'EV_CLUB del NOSTRO GBA
            # invece passa sempre (il giocatore e' DAVVERO alla signorina).
            return
        if self.usb is None:
            # In emulatore il club integrato non esiste: non c'e' un Pico da
            # pilotare. Lo si dice una volta e basta.
            if self.club_sessions == 0:
                self.club_sessions = -1
                self.log("[club ] %s - ma il club integrato vale solo sul GBA "
                         "fisico: in emulatore si ignora" % motivo)
            return

        from club_link import ClubSession, ruolo_master

        self.log("=" * 60)
        self.log("[club ] %s" % motivo)
        self.log("[club ] passo il Pico in modo LINK (Celio) e faccio io da "
                 "ponte: NON toccare niente, gioca pure")
        # La sessione nasce PRIMA del giro sul Pico: cosi' l'ENTER parte
        # subito con l'epoca giusta e l'amico non aspetta i ~2 s del
        # Cancel/SetMode. I suoi pacchetti arrivati nel frattempo finiscono
        # nella sessione, che e' gia' quella vera.
        sess = ClubSession(master=ruolo_master(self.peer_id,
                                               self.club_partner_peer),
                           dev=self._ClubDev(self.usb, self.log),
                           send_net=self.send_club,
                           log=self.log)
        self.send_club(club_enter(sess.epoca))
        try:
            self.usb.club_enter()
        except SystemExit as exc:
            # L'endpoint inceppato: il rimedio F-4 parte DA SOLO.
            self.log("[club ] endpoint inceppato all'ingresso: riavvio il "
                     "Pico da solo (F-4) e riprovo")
            if not self._club_reopen_usb():
                self.log("[club ] non ci sono riuscito: %s" % exc)
                self.send_club(club_leave(sess.epoca))
                return
            try:
                self.usb.club_enter()
            except SystemExit as exc2:
                self.log("[club ] fallito anche dopo il riavvio: %s" % exc2)
                self.send_club(club_leave(sess.epoca))
                return

        self.club = sess
        if self.club_sessions < 0:
            self.club_sessions = 0
        self.club_sessions += 1
        if sess.master is None:
            ruolo = "IN ATTESA (peer %d contro un amico non ancora annunciato)"\
                    % self.peer_id
        elif self.peer_id in (1, 2) or self.club_partner_peer in (1, 2):
            ruolo = "%s (dal peer-id, come da protocollo)" \
                    % ("MASTER" if sess.master else "SLAVE")
        else:
            ruolo = "%s (peer %d contro %d: vince il piu' basso)" \
                    % ("MASTER" if sess.master else "SLAVE",
                       self.peer_id, self.club_partner_peer)
        self.log("[club ] ruolo: %s | epoca 0x%08X" % (ruolo, sess.epoca))

    def handle_club_net(self, peer_id, body, now):
        # IL PEER DELL'AMICO, dall'header del primo T_CLUB della sessione: e'
        # meta' della regola dei ruoli (l'altra meta' e' il nostro peer-id).
        # Si fissa una volta e si dimentica a fine sessione, come _clubLatch
        # nel browser: un peer di ieri farebbe decidere il ruolo contro
        # qualcuno che non e' al bancone.
        if self.club_partner_peer is None and peer_id is not None:
            self.club_partner_peer = peer_id
            if self.club is not None:
                from club_link import ruolo_master
                self.club.set_master(ruolo_master(self.peer_id, peer_id))

        parsed = club_unpack(body)
        if parsed is None:
            # Un corpo che non si legge e' quasi sempre un client con i .py
            # di una versione diversa: va detto FORTE, una volta.
            self.club_malformati += 1
            if self.club_malformati == 1:
                self.log("[club ] PACCHETTO CLUB ILLEGGIBILE dall'amico: "
                         "probabilmente i suoi .py sono di una versione "
                         "diversa - aggiornate il pacchetto su ENTRAMBI i PC")
            return
        sub, epoca, payload = parsed

        if sub == CLUB_ENTER:
            self.club_start("l'AMICO e' entrato al Cable Club")
            if self.club is not None:
                self.club.nota_partner(epoca)
            return
        if sub == CLUB_LEAVE:
            if self.club is not None:
                self.club.on_net_leave(epoca)
            return
        if self.club is None:
            # Un CLUB_STATUS senza sessione = il club era gia' in corso
            # quando questo client e' partito (riavvio a meta'): ci si
            # aggancia. I DATA/REQ da soli non bastano ad aprire (arrivano
            # anche da sessioni zombie); gli stati invece vengono
            # riannunciati apposta.
            if sub == CLUB_STATUS:
                self.club_start("club gia' in corso dall'altra parte: "
                                "mi aggancio in ritardo")
            if self.club is None:
                return
        if sub == CLUB_STATUS:
            self.controlla_versione(payload[2], payload[3])
            self.club.on_net_status(epoca, payload[0], payload[1])
        elif sub == CLUB_DATA:
            self.club.on_net_block(epoca, payload[0], payload[1])
        elif sub == CLUB_REQ:
            self.club.on_net_req(epoca, payload)

    def controlla_versione(self, versione, impronta):
        """I due PC devono avere gli STESSI file. Sembra ovvio e invece e'
        costato una serata: il 2026-08-20 la correzione della riapertura era
        solo su un lato, il club e' morto identico a prima, e per capirlo
        sono serviti i due log affiancati e un contatore che mancava. Adesso
        lo dice il programma, subito e a caratteri cubitali."""
        if self.club_versione_detta:
            return
        if versione == VERSIONE_CLUB and impronta == IMPRONTA_SORGENTI:
            self.club_versione_detta = True
            return
        if versione == VERSIONE_CLUB and impronta == IMPRONTA_LUA:
            # L'amico gioca in EMULATORE (mgba/club_lua.lua): il Lua fa da
            # cavo dentro mGBA, stessa macchina a stati, nessun .py da
            # confrontare.
            self.club_versione_detta = True
            self.log("[club ] l'amico gioca in EMULATORE (mGBA, script "
                     "gen3-poke-multiplayer, stessa versione club %d): compatibile" % versione)
            return
        if versione == VERSIONE_CLUB and impronta == IMPRONTA_WEB:
            # L'amico gioca dal SITO (web/js/club.js): stessa macchina a
            # stati, nessun .py da confrontare. Non e' un pacchetto vecchio.
            self.club_versione_detta = True
            self.log("[club ] l'amico gioca dal BROWSER (sito gen3-poke-multiplayer, "
                     "stessa versione club %d): compatibile" % versione)
            return
        self.club_versione_detta = True
        self.log("!" * 64)
        if versione == 0:
            self.log("[club ] L'AMICO HA I FILE VECCHI (i suoi pacchetti non")
            self.log("[club ] portano nemmeno il numero di versione).")
        elif versione != VERSIONE_CLUB:
            self.log("[club ] VERSIONI DIVERSE: lui ha la %d, tu la %d."
                     % (versione, VERSIONE_CLUB))
        else:
            self.log("[club ] STESSA VERSIONE MA FILE DIVERSI (impronta sua")
            self.log("[club ] %08X, tua %08X): uno dei due ha un pacchetto"
                     % (impronta, IMPRONTA_SORGENTI))
            self.log("[club ] piu' vecchio.")
        self.log("[club ] IL CLUB NON PUO' FUNZIONARE COSI': rifate il")
        self.log("[club ] pacchetto e copiatelo su ENTRAMBI i PC.")
        self.log("!" * 64)

    def club_pump(self):
        """Nel ciclo principale: device -> sessione, e la fine della sessione."""
        if self.club is None:
            return
        try:
            while True:
                self.club.on_device_status(self.usb.club_statuses.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                blocco = self.usb.club_blocks.get_nowait()
                if self.club.blocchi_tx < CLUB_LOG_BLOCCHI:
                    self.log("[club ] GBA>> %s" % club_block_riga(blocco))
                self.club.on_device_block(blocco)
        except queue.Empty:
            pass
        self.club.tick()
        if self.club.finita:
            self.club_end()

    def club_end(self):
        sess = self.club
        self.club = None
        # Il partner vale per LA SESSIONE: la prossima decidera' il ruolo con
        # chi sara' al bancone allora, non con chi c'era adesso.
        self.club_partner_peer = None
        if sess.abortita:
            esito = "ABBANDONATA (%s)" % (sess.motivo_fine or "watchdog")
        else:
            esito = "conclusa"
        self.log("[club ] sessione %s | %s" % (esito, sess.riassunto()))
        if self.club_drop:
            self.log("[club ] eventi di camminata buttati durante il club: %d "
                     "(normale: il payload dormiva)" % self.club_drop)
            self.club_drop = 0
        # Niente riagganci automatici per qualche secondo: gli annunci
        # dell'amico ancora in volo appartengono alla sessione appena morta.
        self.club_riaggancio_dopo = time.monotonic() + 10.0
        # Fino al primo evento valido dal GBA il filo verso di lui resta muto:
        # il gioco potrebbe essere ancora alla schermata del club col link
        # aperto, e i frame di camminata li' dentro sono veleno.
        self.gba_muto = True
        self.gba_muto_scartati = 0
        self.send_club(club_leave(sess.epoca))
        try:
            self.usb.club_exit()
            self.log("[club ] Pico di nuovo in passthrough: aspetto che il GBA "
                     "torni a parlare prima di scrivergli")
        except SystemExit:
            self.log("[club ] endpoint inceppato al rientro: riavvio il Pico "
                     "da solo (F-4)")
            if self._club_reopen_usb():
                self.log("[club ] canale ripartito: si ricammina")
            else:
                self.log("[club ] NON sono riuscito a ripartire: scollega e "
                         "ricollega il Pico (senza BOOTSEL) e rilancia 2-gioca. "
                         "Il GBA NON va spento: il programma vive in RAM.")

    def _club_reopen_usb(self):
        """F-4 automatico + riapertura completa del canale passthrough.
        Ritorna True se il canale e' di nuovo su."""
        from usb_link import UsbLink, riavvia_pico
        try:
            try:
                self.usb.reboot_pico()
            except Exception:
                riavvia_pico(quiet=False)
            self.usb = UsbLink(timing=self.usb.timing, quiet=False,
                               cable=self.usb.cable)
            self.usb.open()
            return True
        except Exception as exc:
            self.log("[club ] riapertura USB fallita: %s" % exc)
            return False

    def _deliver_one(self, event):
        if self.club is not None:
            # Durante il club l'endpoint dati porta i blocchi della sessione
            # link: scriverci un evento di gioco vorrebbe dire iniettare 12
            # byte di camminata in mezzo a uno scambio. Il payload dorme
            # comunque: si butta e si conta.
            self.club_drop += 1
            return
        if self.gba_muto:
            self.gba_muto_scartati += 1
            return
        if self.usb is not None:
            try:
                self.usb.send_event(event)
            except Exception as exc:
                # Il canale USB non muore per un errore di write: il GBA puo'
                # essere in mezzo a un riaggancio (M-7). Si conta e si va avanti.
                self.log("invio al GBA fallito (%s), continuo" % exc)
            return
        if self.game is None:
            return
        try:
            self.game.sendall(event)
        except OSError as exc:
            self.log("il gioco si e' disconnesso in invio: %s" % exc)
            self.close_game()

    # --- verso il gioco ---------------------------------------------------

    def close_game(self):
        if self.game is not None:
            try:
                self.game.close()
            except OSError:
                pass
        self.game = None
        self.inbox = b""

    def handle_game(self, now):
        try:
            chunk = self.game.recv(4096)
        except OSError as exc:
            self.log("lettura dal gioco fallita: %s" % exc)
            self.close_game()
            return

        if not chunk:
            self.log("il gioco ha chiuso la connessione")
            self.close_game()
            return

        self.inbox += chunk
        while len(self.inbox) >= EVENT_SIZE:
            event, self.inbox = self.inbox[:EVENT_SIZE], self.inbox[EVENT_SIZE:]
            self.forward_event(event, now)

    # --- diagnostica ------------------------------------------------------

    def status(self):
        # DURANTE IL CLUB il canale passthrough e' fermo APPOSTA: la
        # diagnostica di sotto ("CANALE FERMO", "DIREZIONE ROTTA") mentirebbe
        # su tutta la linea. Una riga sola, quella giusta.
        if self.club is not None:
            self.log("[club ] sessione viva | %s" % self.club.riassunto())
            return

        # Il bridge 2 del primo test ha stampato tredici righe di "inviati 0"
        # senza mai dire la cosa che contava: che nessun emulatore si era
        # collegato. Quel silenzio e' costato un test intero.
        if self.usb is not None:
            # La riga [usb] e' l'equivalente dei contatori del banco: se
            # "rx frame" non sale, il GBA non sta parlando (cavo, verso,
            # firmware) - e va gridato, non taciuto.
            self.log("[usb] %s | status 0x%s"
                     % (self.usb.stats(),
                        "%04x" % self.usb.last_status
                        if self.usb.last_status is not None else "----"))
            # LA SONDA DEL PAYLOAD (SIO_T_STATE, 2026-08-15). Tre diagnosi di
            # fila sono toccate ai log del PC perche' i contatori del payload
            # erano invisibili sul fisico. Questa riga li mostra: vbl che sale
            # = payload vivo; emessi fermo = non emette; porta prese/rese =
            # il giro del SIO; latch = la resa al Cable Club; cb2 = in quale
            # callback sta il gioco. E se la sonda e' VECCHIA, anche quello
            # e' un verdetto: la porta e' chiusa (o il payload e' fermo) da
            # quel momento in poi.
            if self.usb.state_words is not None:
                w = self.usb.state_words
                eta = time.time() - self.usb.state_stamp
                flags = w[4]
                # Dal 2026-08-21 la sonda dice anche lo STATO che il payload
                # crede di avere (bit 8-11) e quante SEZIONI di menu ha
                # riconosciuto (bit 12-15, modulo 16): e' il contatore che
                # decide il caso "scheda bianca" - Zaino aperto da piu' di
                # 1 s e `sezioni` fermo = il riconoscimento non scatta;
                # `stato io zaino` qui e niente riga [stato] io -> zaino =
                # gli EVENT_STATUS non escono dal cavo.
                self.log("[gba ] sonda #%d (%.0f s fa): vbl %d | emessi %d | "
                         "porta %d prese / %d rese | latch %d (rese %d, "
                         "risvegli %d) | overworld %s%s | stato io %s | "
                         "sezioni %d | cb2 0x%08X | RIPARAZIONI %d | SCRUB IE %d"
                         % (self.usb.state_count, eta, w[0], w[1],
                            w[2] >> 8, w[2] & 0xFF,
                            flags & 1, w[3] >> 8, w[3] & 0xFF,
                            "si" if flags & 2 else "NO",
                            " (assestamento IN CORSO)" if flags & 4 else "",
                            nome_stato((flags >> 8) & 0xF),
                            (flags >> 12) & 0xF,
                            # w[7] porta due byte, ENTRAMBI modulo 256 (il
                            # frame della sonda e' fermo a 8 parole): conta il
                            # moto fra una sonda e l'altra, non il totale.
                            # SCRUB IE deve salire entrando in lotta: e' la
                            # prova che SioShutdown lascia stare i bit del
                            # gioco (VCount = motore audio) invece di
                            # riscriverli da una fotografia vecchia.
                            w[5] | (w[6] << 16), w[7] & 0xFF, w[7] >> 8))
            # LA PAGINA 2: i registri, che non si deducono piu'. Il giudizio
            # sta qui e non nella testa di chi legge: se SIOCNT ha perso il
            # modo Multi o il bit IRQ, o IE il bit 7, la porta non e' piu'
            # nostra e la riga lo DICE.
            if self.usb.diag_words is not None:
                d = self.usb.diag_words
                siocnt, rcnt, ie = d[0], d[1], d[2]
                nostri = ((siocnt & 0x3000) == 0x2000 and (siocnt & 0x4000)
                          and (rcnt & 0xC000) == 0 and (ie & 0x0080))
                # IL NUMERO CHE DECIDE, dal 2026-08-16: il bit 6 di IE e' il
                # Timer 3. Il gioco lo arma SOLO quando crede di essere in una
                # sessione di link (InitTimer, link.c) - e se ci arriva senza
                # che nessuno abbia aperto il Cable Club, vuol dire che gli
                # abbiamo svegliato la macchina di link con i NOSTRI interrupt.
                # E' il guasto che bloccava il gioco a intermittenza.
                sveglia = (ie & 0x0040) != 0
                self.log("[gba+] SIOCNT %04X RCNT %04X IE %04X -> %s | irq %d "
                         "| parole %d | RIPARAZIONI %d, RACCOLTI %d | cb1 "
                         "riagganci %d | salti hook %d"
                         % (siocnt, rcnt, ie,
                            ("!!! IL LINK DEL GIOCO SI E' SVEGLIATO (Timer 3 "
                             "armato): se non siete al Cable Club e' il "
                             "guasto che blocca il gioco a scatti")
                            if sveglia
                            else "REGISTRI NOSTRI" if nostri
                            else "!!! REGISTRI NON NOSTRI: qualcuno ce li ha "
                                 "riconfigurati",
                            d[3], d[4], d[5] >> 8, d[5] & 0xFF, d[6], d[7]))
            # Il primo T-5 (2026-08-02) e' stato riportato come "funziona tutto"
            # mentre questa meta' della catena era ferma a zero: i numeri per
            # accorgersene c'erano tutti, sparsi su due terminali, e nessuna
            # riga li metteva insieme. Adesso il giudizio lo dà il log.
            # IL CANALE SI E' FERMATO DOPO essere partito. Caso reale del
            # 2026-08-02, prima partita via internet: il client di Lain ha
            # letto UN frame (la sua mappa), poi "0 parole/s" per novanta
            # secondi - e nessun allarme, perche' le due guardie qui sotto
            # cercano lo zero assoluto: frames_rx era 1, non 0, e sent era 1,
            # non 0. La prova che il canale era morto stava in venti righe di
            # log e nessuna la gridava. Un contatore che smette di salire e'
            # un guasto quanto uno che non parte mai.
            words = self.usb.deframer.words_rx
            prev = getattr(self, "_prev_words_rx", None)
            stalled = getattr(self, "_stalled_prints", 0)
            stalled = stalled + 1 if (prev is not None and words == prev) else 0
            self._prev_words_rx = words
            self._stalled_prints = stalled

            # Il filo puo' essere vivo (parole che scorrono) mentre il GBA non
            # manda piu' NESSUN frame: sono due cose diverse e vanno contate
            # separatamente, altrimenti "canale fermo" e "porta chiusa"
            # finiscono sotto lo stesso silenzio.
            frames = self.usb.deframer.frames_rx
            prev_frames = getattr(self, "_prev_frames_rx", None)
            frames_frozen = prev_frames is not None and frames == prev_frames
            self._prev_frames_rx = frames
            if not frames_frozen:
                self._mute_prints = 0

            prev_received = getattr(self, "_prev_received", 0)
            self._prev_received = self.received

            if stalled >= 2:
                self.log("      !!! CANALE CON IL GBA FERMO: nessuna parola "
                         "nuova sul filo da %d letture (parole totali ferme a "
                         "%d). L'adattatore e' inceppato: lancia 3-sblocca.bat "
                         "(usb_link.py --riavvia) e rilancia questo, SENZA "
                         "spegnere il GBA - il programma vive in RAM."
                         % (stalled + 1, words))
            elif self.usb.frames_tx > 20 and self.usb.deframer.frames_rx == 0:
                self.log("      !!! DIREZIONE GBA->PC ROTTA: mando frame al GBA e "
                         "non ne torna NESSUNO. Il GBA non parla (cavo/verso/"
                         "programma), oppure il firmware non ha il fix della "
                         "back-pressure (serve celio-f1f2b.uf2 o successivo).")
            elif frames_frozen and frames > 0 and self.received > prev_received:
                # LA PORTA CHIUSA, non un guasto. Il payload possiede il SIO
                # solo dentro l'overworld (main.c: cb2 == CB2_Overworld ->
                # SioInit, altrimenti SioShutdown): in menu, in casa, in lotta
                # e nelle transizioni la porta torna al gioco e il GBA smette
                # di parlare, pur restando il filo attivo a 226 parole/s.
                # Conseguenza che NESSUNA riga diceva, ed e' quella che conta:
                # gli eventi dell'amico che arrivano adesso NON vengono messi
                # in coda da nessuno - si perdono, e il suo sprite resta fermo
                # finche' non si rientra. Il 2026-08-02 questo ha prodotto 40
                # secondi di "sprite fermo" letti come guasto di rete.
                self.log("      il TUO GBA ha chiuso la porta seriale: sei "
                         "fuori dall'overworld (menu, casa, lotta, "
                         "transizione). Gli eventi che arrivano ADESSO si "
                         "perdono e l'altro ti vede fermo. Torna all'aperto.")
            elif frames_frozen and frames > 0:
                # IL GBA E' MUTO E NESSUNO STA RICEVENDO. E' la firma del
                # congelamento del 2026-08-15: entrambi i lati fermi, canale
                # a 226 parole/s perfetto, e NESSUNA delle sirene qui sopra
                # che scattava - questa riga esiste perche' quel silenzio non
                # si ripeta. Conteggio a intervalli: uno solo puo' essere una
                # transizione lunga, tre di fila (15 s) no.
                muto = getattr(self, "_mute_prints", 0) + 1
                self._mute_prints = muto
                if muto >= 3:
                    # La sonda trasforma questo allarme da ipotesi a verdetto:
                    # sonda fresca = payload vivo che non emette (leggere la
                    # riga [gba ]); sonda vecchia = porta mai ripresa o
                    # payload fermo, e l'ultimo [gba ] dice da dove.
                    if self.usb.state_words is not None:
                        eta = time.time() - self.usb.state_stamp
                        verdetto = ("la sonda pero' PARLA (%.0f s fa): il "
                                    "payload e' vivo, leggi la riga [gba ]."
                                    % eta) if eta < 7 else (
                                    "anche la sonda TACE da %.0f s: porta "
                                    "seriale mai ripresa o payload fermo - "
                                    "l'ultima riga [gba ] dice dov'era."
                                    % eta)
                    else:
                        verdetto = ("la sonda non ha MAI parlato: payload "
                                    "vecchio senza sonda, o mai partito.")
                    self.log("      !!! GBA MUTO da %d s con canale vivo. %s"
                             % (muto * 5, verdetto))
            elif self.usb.frames_tx > 20 and frames < 3:
                self.log("      !!! il GBA ha parlato %d volte e poi ha smesso, "
                         "mentre io gli mando roba: il payload non sta girando "
                         "(multiboot da rifare) oppure e' fermo in un menu."
                         % frames)
            elif self.received and not self.sent:
                self.log("      !!! nulla dal GBA verso il relay: gli eventi "
                         "arrivano solo nell'altro verso")
        elif self.game is None:
            self.log("NESSUN EMULATORE COLLEGATO su 127.0.0.1:%d - hai caricato lo "
                     "script giusto in questa istanza?" % self.listen_port)
            return

        if self.rtts:
            lo, hi = min(self.rtts), max(self.rtts)
            avg = sum(self.rtts) / len(self.rtts)
            rtt = "RTT min %.1f / medio %.1f / max %.1f ms" % (lo, avg, hi)
        else:
            rtt = "RTT non ancora misurato"
        self.rtts = []

        if self.map_key is None:
            where = "mappa non ancora nota"
            # Il silenzio del 2026-08-02 sera: due GBA sani, payload seminato,
            # e un'ora persa perche' i client erano stati lanciati col gioco
            # sul TITOLO - dove il payload dorme per progetto. La riga sotto
            # trasforma quel silenzio in una domanda esplicita dopo ~30 s.
            self._no_map_prints = getattr(self, "_no_map_prints", 0) + 1
            if self._no_map_prints >= 6 and self._no_map_prints % 6 == 0:
                self.log("      il GBA non ha ancora detto su che mappa e': "
                         "sei NELL'OVERWORLD a camminare? Sul titolo e nei "
                         "menu il payload tace apposta.")
        else:
            self._no_map_prints = 0
            where = "io su mappa %d.%d" % (self.map_key >> 8,
                                           self.map_key & 0xFF)

        self.log("%s | inviati %d | ricevuti %d | scartati %d dup + %d arretrati"
                 " | ricuciti %d (+%d larghi) | persi dal simulatore %d | VIA %d"
                 " | presenza %d | va-e-vieni %d | stanza %d | %s"
                 % (rtt, self.sent, self.received, self.dup_dropped,
                    self.late_dropped, self.healed, self.heal_far,
                    self.netem.dropped, self.leaves, self.presenze_rimandate,
                    self.flap, self.room, where))

        # "presenza" = le fotografie rispedite mentre il GBA taceva. A zero e'
        # legittimo (sei sempre stato nell'overworld, dove il payload parla da
        # solo); deve invece SALIRE ogni volta che si passa dai menu, da una
        # lotta o dal Cable Club. Se resti fermo al bancone e resta a zero,
        # l'amico che arriva dopo non ti vedra': e' il difetto del 2026-08-28.
        if self.club is not None and not self.presenze_rimandate:
            self.log("      !!! sei al Cable Club e non e' partita nessuna "
                     "presenza: chi entra ora non sa che ci sei.")

        # Il filo verso il gioco. "scritti" comprende le copie, quindi con
        # --copie 2 deve essere circa il doppio dei passi ricevuti: se non lo
        # e', la ridondanza non sta partendo da qui e non ha senso cercarla
        # dall'altra parte.
        self.log("  filo verso il gioco: %d frame scritti (copie x%d), "
                 "%d copie saltate per non intasare%s"
                 % (self.wire_sent, self.wire_copies, self.copies_skipped,
                    (", %d buttati dal simulatore di perdita sul filo"
                     % self.wire_dropped) if self.wire_loss else ""))

        # Gli slot avatar (fino a 4 giocatori): chi sta a schermo su quale
        # slot, e quanti eventi sono rimasti fuori per stanza piena. Se
        # `senza avatar` sale con 3 o meno amici, l'assegnazione e' rotta.
        if self.peer_slots or self.slot_scartati:
            assegnati = ", ".join("amico %d -> slot %d" % (p, s)
                                  for p, s in sorted(self.peer_slots.items()))
            self.log("  avatar: %s | senza avatar: %d eventi"
                     % (assegnati or "nessuno", self.slot_scartati))

    # --- ciclo principale -------------------------------------------------

    def run(self):
        next_ping = 0.0
        next_status = time.monotonic() + STATUS_INTERVAL

        if self.usb is not None:
            # Il canale USB si apre subito (Cancel -> SetMode -> master ->
            # timing -> StartHandshake: la sequenza a verbale del 2026-08-02)
            # e il thread di lettura parte qui dentro.
            self.usb.open()
            self.send_udp(pack(T_HELLO, self.peer_id, self.room, 0),
                          time.monotonic(), simulate=False)
            # L'HELLO iscrive alla stanza ma non porta la posizione: se
            # abbiamo gia' una fotografia (riaggancio), la si rimanda subito.
            self.rimanda_presenza(time.monotonic())
            self.log("GBA collegato via Celio: guarda la barra del banco, "
                     "deve essere VERDE")

        while True:
            now = time.monotonic()

            for datagram in self.netem.due(now):
                self._flush_one(datagram)

            if now >= next_ping:
                self.send_ping(now)
                next_ping = now + PING_INTERVAL

            if now >= next_status:
                self.status()
                next_status = now + STATUS_INTERVAL
            self.scrivi_posizioni(now)

            # Gli eventi del GBA arrivano da un thread, non da una socket:
            # select non puo' svegliarci per loro. Si drena la coda a ogni
            # giro, e il timeout del select qui sotto e' tenuto corto apposta
            # (50 ms massimi di latenza aggiunta, contro i ~250 del caso tcp).
            if self.usb is not None:
                for event in self.usb.poll_events():
                    self.forward_event(event, now)
                # La sessione del Cable Club, se c'e': stati e blocchi dal
                # device verso il partner, e la sua fine.
                self.club_pump()

            if self.ws is not None:
                # Riaggancio silenzioso: una linea caduta per due secondi non
                # deve uccidere la partita (vedi WsRelay.ensure).
                self.ws.ensure(now)
                watch = [self.ws.sock] if self.ws.sock is not None else []
            else:
                watch = [self.udp]
            if self.listener is not None:
                watch.append(self.listener)
            if self.game is not None:
                watch.append(self.game)

            deadline = min(x for x in (next_ping, next_status,
                                       self.netem.next_deadline()) if x is not None)
            # LA LATENZA DELLE LOTTE VIVE QUI (campo, 2026-08-21). Ogni
            # blocco del GBA aspetta che select si svegli prima di partire
            # verso il relay: con il cap a 50 ms, un giro di battuta pagava
            # fino a ~100 ms di sonno (25 ms medi per lato, due lati) SOPRA
            # la rete. Durante il club il cap scende a 3 ms: il costo e' un
            # select a ~300 Hz - niente - e il guadagno e' quasi tutto il
            # ritardo evitabile. La rete (~31 ms di RTT) e il passo del filo
            # (un pacchetto ogni ~17-25 ms, e' il ritmo del gioco stesso)
            # sono il pavimento fisico: sotto quello non si scende da qui.
            if self.club is not None:
                cap = 0.003
            else:
                cap = 0.05 if self.usb is not None else 0.25
            timeout = max(0.0, min(deadline - now, cap))

            try:
                ready, _, _ = select.select(watch, [], [], timeout)
            except KeyboardInterrupt:
                self.log("chiusura")
                return

            now = time.monotonic()

            for sock in ready:
                if sock is self.listener:
                    conn, addr = self.listener.accept()
                    if self.game is not None:
                        # Ricarica dello script in mGBA: la vecchia connessione
                        # e' morta anche se il sistema non ce l'ha ancora detto.
                        self.log("nuova connessione dal gioco, chiudo la vecchia")
                        self.close_game()
                    conn.setblocking(False)
                    self.game = conn
                    self.last_seq.clear()
                    self.peer_room.clear()
                    self.log("gioco collegato da %s:%d" % addr)
                    self.send_udp(pack(T_HELLO, self.peer_id, self.room, 0), now,
                                  simulate=False)
                    self.rimanda_presenza(now)

                elif self.ws is not None and sock is self.ws.sock:
                    while True:
                        data = self.ws.recv()
                        if data is None:
                            break
                        self.handle_udp(data, now)

                elif sock is self.udp:
                    while True:
                        try:
                            data, _ = self.udp.recvfrom(2048)
                        except BlockingIOError:
                            break
                        except OSError:
                            break
                        self.handle_udp(data, now)

                elif sock is self.game:
                    self.handle_game(now)


def main():
    ap = argparse.ArgumentParser(description="Ponte fra il gioco e il relay")
    ap.add_argument("--transport", choices=("tcp", "usb"), default="tcp",
                    help="tcp = mGBA via Lua (com'e' sempre stato); "
                         "usb = GBA fisico via adattatore Celio (usb_link.py, "
                         "richiede il firmware con F-2)")
    ap.add_argument("--usb-timing", type=int, default=None, metavar="ITER",
                    help="solo --transport usb: periodo del master in "
                         "iterazioni PIO (F-1). Vuoto = default del firmware "
                         "(15370 ~= 8.3 ms)")
    ap.add_argument("--usb-cable", choices=("auto", "gba", "gbc"), default="auto",
                    metavar="TIPO",
                    help="solo --transport usb: forza il cablaggio (F-3). "
                         "'gba' = SD su GP3, ed e' quello da usare col cavo "
                         "DMG/GBC su questa board: l'autodetect leggerebbe GP1 "
                         "alto, sceglierebbe GP4 (non cablato) e il canale "
                         "resterebbe muto senza errori")
    ap.add_argument("--listen", type=int, default=8123,
                    help="porta TCP su cui si collega mGBA (solo --transport tcp)")
    ap.add_argument("--relay", default="127.0.0.1:9000")
    ap.add_argument("--peer-id", type=int, default=1)
    ap.add_argument("--room", type=int, default=1,
                    help="stanza = PARTITA, non mappa. Tutti i giocatori della "
                         "stessa sessione usano lo stesso numero. Lo 0 non si "
                         "usa: il relay lo ignora apposta.")
    ap.add_argument("--delay", type=float, default=0,
                    help="ritardo di sola andata, ms")
    ap.add_argument("--jitter", type=float, default=0, help="ms, +/- sul ritardo")
    ap.add_argument("--loss", type=float, default=0, help="perdita, percentuale")
    ap.add_argument("--copie", type=int, default=2, metavar="N",
                    help="quante volte mandare al gioco ogni PASSO/GIRA. "
                         "L'ultimo tratto (SIO sul GBA fisico) perde ~8%% dei "
                         "frame e nessuno lo ricuce: la copia e' cio' che evita "
                         "la pausa-e-scivolata. 1 = come prima del 2026-08-02, "
                         "serve solo per il confronto A/B")
    ap.add_argument("--perdita-filo", type=float, default=0, metavar="PCT",
                    help="butta questa percentuale di frame SUL TRATTO verso il "
                         "gioco (dopo le copie). E' il banco di prova in "
                         "emulatore del SIO del GBA: --perdita-filo 8 riproduce "
                         "su mGBA la perdita misurata sul fisico. Non e' --loss, "
                         "che agisce verso il relay ed e' curabile dalla "
                         "ricucitura dell'altro")
    ap.add_argument("--niente-ricucitura", action="store_true",
                    help="spegne la ricucitura dei passi persi in questo client. "
                         "Serve SOLO a collaudare il rammendo del payload (con "
                         "--loss sul mittente): in esercizio non va mai usato")
    args = ap.parse_args()

    try:
        Bridge(args).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
