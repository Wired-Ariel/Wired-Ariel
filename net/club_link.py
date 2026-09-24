"""
club_link.py - la sessione del Cable Club sopra il NOSTRO relay (Consegna D).

E' la porta in Python della sessione Celio, letta dai sorgenti il 2026-08-09
(Celio-Server/src/session.ts per la negoziazione, Celio-Client
linkdeviceExchangeSession.ts per l'affidabilita' - entrambi GPL-3.0). Cosa
resta e cosa cambia:

- STESSI comandi e stati del device (usb_link.LINK_ST_* / CMD_*): il firmware
  sul Pico E' Celio, e questo modulo lo pilota come farebbe il client web.
- La negoziazione dei ruoli del server COLLASSA: peer 1 = master, peer 2 =
  slave, sempre. Con due GBA veri il ruolo e' arbitrario (serviva per i casi
  GBA<->mGBA di Celio), e senza preferenze non serve nessuna autorita'.
- L'affidabilita' cambia trasporto: Celio sta su Socket.IO (TCP, niente
  perdite), noi su UDP. Quindi: gli STATI viaggiano in 3 copie con un numero
  di serie per il dedup (uno stato perso appende la sessione: un
  LinkConnected mai arrivato = il partner non manda mai ConnectLink); i
  BLOCCHI dati hanno sequenza, buffer di riordino, richiesta dei mancanti
  (come Celio) piu' il rilancio periodico dell'ULTIMO blocco (il buco che
  Celio non ha: se si perde il blocco finale nessun successivo lo denuncia).

LE EPOCHE (2026-08-19, dalla prova sul campo con l'amico). Celio sta su una
connessione: se un client cade, cade la sessione. Noi stiamo su UDP senza
connessione, e il campo ha mostrato il buco: un client riavviato apre una
sessione NUOVA (sequenze da 0) mentre il partner tiene la VECCHIA (sequenze a
700+), e i blocchi finiscono tutti in "fuori-ordine"/"dup" - il device non
riceve piu' NIENTE, il GBA resta appeso alla schermata di conferma e l'unica
uscita e' scollegare il cavo. Log reale: `fuori-ordine 46 richiesti 736`.
La cura, in tre pezzi:

1. Ogni sessione nasce con un'EPOCA (u32 a caso) che viaggia in OGNI corpo.
   Il partner che vede un'epoca nuova sa che il client di la' e' ripartito:
   RIAGGANCIA IN CORSA (azzera il lato-partner del proprio stato, riannuncia
   il proprio) invece di macinare sequenze di due mondi diversi. Le epoche
   abbandonate finiscono in una lista nera: i pacchetti in volo della vita
   precedente non fanno ping-pong.
2. RIANNUNCIO PERIODICO: l'ingresso al club (finche' il partner non si fa
   vivo) e l'ultimo stato del ladder (finche' il partner non e' connesso)
   ripartono ogni ANNUNCIO_S. Prima l'ENTER partiva UNA volta: il client
   dell'amico riavviato dopo quell'attimo non veniva mai a sapere del club
   (campo, 2026-08-19: Lain "in attesa" per 10 minuti con l'amico che
   camminava normalmente).
3. WATCHDOG VERO: "progresso" non e' piu' qualunque pacchetto (i dup dei
   rilanci tenevano vive sessioni zombie all'infinito), e un partner MUTO
   per PARTNER_MUTO_S chiude la sessione da solo - si torna a camminare.

La classe non tocca ne' USB ne' socket: parla con due interfacce iniettate
(il device e la rete). E' il motivo per cui net/test_club.py la prova a secco,
senza hardware - regola del progetto: niente consegna senza procedura, e la
procedura a secco e' l'unica eseguibile alla cieca.

La macchina, vista dal lato di UN client (l'altro e' speculare):

    device: AwaitMode      -> comando SetModeMaster/Slave (per peer-id)
    device: HandshakeRx    -> annuncio al partner; quando ANCHE il partner
                              e' HandshakeRx -> comando StartHandshake
    device: LinkConnected  -> annuncio al partner (che manda ConnectLink
                              al SUO device)
    rete:   LinkConnected  -> comando ConnectLink al mio device
    device: blocco dati    -> sequenza + storia (512) -> al partner
    rete:   blocco dati    -> riordino/dedup -> al device, in ordine
    rete:   richiesta seq  -> rispedizione dalla storia
    device: LinkClosed     -> annuncio; quando ENTRAMBI chiusi -> finita
    device: Reconnecting   -> scala da capo, MA con un conto alla rovescia
                              (RIAPERTURA_S): se la scala nuova non si
                              completa, il gioco ha chiuso per davvero
                              (frullatore, annulli - niente EXIT_ROOM,
                              quindi niente LinkClosed) -> finita
"""

import os
import struct
import time

from protocol import (
    CLUB_DATA, CLUB_REQ, CLUB_STATUS,
    club_data, club_enter, club_req, club_status,
)
from usb_link import (
    CMD_CONNECT_LINK, CMD_SET_MODE_MASTER, CMD_SET_MODE_SLAVE,
    CMD_START_HANDSHAKE,
    LINK_ST_AWAIT_MODE, LINK_ST_CLOSED, LINK_ST_CONNECTED,
    LINK_ST_DEBUG, LINK_ST_HANDSHAKE_OK, LINK_ST_HANDSHAKE_RX,
    LINK_ST_NOMI, LINK_ST_READY, LINK_ST_RECONNECTING,
)

# Quanti blocchi tenere per le rispedizioni. Celio ne tiene 512 per client.
STORIA_BLOCCHI = 512
# Copie di ogni stato (dedup sul numero di serie dall'altra parte).
COPIE_STATO = 3
# Rilancio dell'ultimo blocco se il device non ne produce di nuovi: copre la
# perdita del blocco FINALE, che nessun successivo puo' denunciare.
RILANCIO_ULTIMO_S = 0.3

# ConnectLink DISTANZIATO da StartHandshake. "Dopo" non basta: il firmware deve
# avere TRASMESSO la parola di slave (packetLayer.hpp:120, isHandshakeEnabled
# legge m_transmitedHandShake), e quella esce solo con un trasferimento nello
# stato "enabled". ConnectLink nello stesso millisecondo salta lo stato a
# "connect" prima del primo trasferimento: 0xB9A0 non esce mai e
# establishConncection resta appeso - niente `cavo collegato`, mai. Con due GBA
# veri il distacco lo mette la rete; con l'EMULATORE (annuncia "collegato"
# all'istante) i due comandi partivano insieme: campo del 2026-08-27.
CONNECT_RITARDO_S = 0.35

# IL CONGEDO. L'amico esce dalla porta: staccare qui il cavo lascia il NOSTRO
# gioco dentro la coreografia d'uscita, senza partner - schermata nera di
# errore e riavvio (campo 2026-08-27). Il firmware fa l'opposto
# (usbSection.cpp:41-79): visto EXIT_ROOM continua a fare da cavo finche' i due
# si sono scambiati READY_CLOSE_LINK. Qui si fa uguale: si manda al device la
# PORTA e poi l'ECO di chiusura, e si aspetta che sia il device a dire
# "link chiuso".
CONGEDO_MAX_S = 6.0
LINKCMD_HELD_KEYS = 0xCAFE
LINKCMD_READY_CLOSE_LINK = 0x5FFF
LINK_KEY_IDLE = 0x11
LINK_KEY_EXIT_ROOM = 0x17


def blocco_da_cmd(parole):
    """Un blocco da 64 byte: 8 parole e 48 zeri (come `pacchetto()` del finto
    e `bloccoDaCmd` del Lua)."""
    b = bytearray(64)
    for i in range(8):
        w = parole[i] if i < len(parole) else 0
        b[i * 2] = w & 0xFF
        b[i * 2 + 1] = (w >> 8) & 0xFF
    return bytes(b)
# Riannuncio periodico: ENTER finche' il partner non si e' fatto vivo, ultimo
# stato del ladder finche' il partner non e' connesso. E' cio' che permette a
# un client riavviato di riagganciarsi a un club gia' in corso.
ANNUNCIO_S = 2.0
# Senza nessun progresso per questo tempo, la sessione si dichiara morta e si
# torna al passthrough: meglio riprendere a camminare che restare appesi.
WATCHDOG_S = 120.0
# Partner che non manda NIENTE (nemmeno un rilancio) per questo tempo: o non
# e' mai entrato in sessione o il suo client e' morto. Si chiude.
PARTNER_MUTO_S = 120.0
# Sessione in cui NESSUN GBA si e' mai presentato (nessun handshake, ne'
# nostro ne' del partner): e' un club nato da un avviso stantio, non da un
# giocatore alla signorina. Nel flusso vero ALMENO un lato fa l'handshake
# entro pochi secondi (chi ha parlato con la signorina); l'altro puo'
# metterci minuti ad arrivare al bancone, e infatti qui basta UNO dei due.
FANTASMA_S = 90.0
# Dopo una "riconnessione" (il gioco ha chiuso il link) la scala nuova deve
# COMPLETARSI entro questo tempo. Nelle riaperture vere (sedersi alla
# macchina degli scambi, cominciare la lotta) i due giochi riaprono
# nell'istante in cui chiudono - la chiusura e' un accordo a due (doppio
# READY_CLOSE_LINK) - e la scala torna su in pochi secondi, ben dentro i
# ~10 s di pazienza del gioco. Se non torna su, la comunicazione e' FINITA:
# il frullatore, l'annullo al bancone e la fine di uno scambio/lotta
# chiudono il link SENZA passare dalla porta della saletta (CAFE 0017,
# EXIT_ROOM), che per il firmware e' l'unica chiusura vera
# (usbSection.cpp:54-62) - quindi LinkClosed non arrivera' mai. Senza
# questo timer il Pico restava in modo link per sempre e la camminata non
# tornava piu' (campo 2026-08-27: frullatore finito, overworld morto fino
# al riavvio). 30 s = 3 volte la pazienza del gioco: un falso positivo qui
# spegnerebbe una comunicazione viva, meglio larghi.
RIAPERTURA_S = 30.0
# Epoche del partner gia' abbandonate da ricordare (i pacchetti in volo della
# vita precedente vanno scartati, non riadottati).
EPOCHE_MORTE_MAX = 8
# Blocchi da tenere da parte mentre il device e' fra due sezioni. La raffica
# dei dati giocatore e' di 7 comandi: il tetto e' largo dieci volte tanto e
# serve solo a non gonfiare all'infinito se la sezione non tornasse mai.
ATTESA_DEV_MAX = 64


def ruolo_master(mio, suo):
    """True = master, False = slave, None = non ancora decidibile.

    LA REGOLA E' SIMMETRICA: i due lati arrivano SEMPRE a ruoli complementari,
    qualunque coppia di numeri abbiano. Gemella di `ruoloMaster` in
    web/js/club.js e di `decidiRuolo` in mgba/club_lua.lua - i tre devono dare
    lo stesso verdetto o al bancone restano due slave e non parte nessuno.

    Fino al 2026-08-28 QUI la regola non c'era: il client Python decideva
    `master = (peer_id == 1)`, cioe' guardava solo il proprio numero. Con una
    partita a 3-4 o un peer scelto a mano, questo lato era SEMPRE slave e
    dall'altra parte il sito poteva concludere slave anche lui: nessuno apriva
    le danze e i due schermi restavano su "in attesa" per sempre.
    """
    if mio == 1:
        return True
    if mio == 2:
        return False
    if suo is None:
        return None
    if suo == 1:
        return False
    if suo == 2:
        return True
    return mio < suo


class ClubSession:
    """Una sessione di Cable Club. dev: oggetto con command(cmd, label) e
    send_block(bytes64). send_net: callable che spedisce un corpo T_CLUB al
    partner (via relay). log: callable per una riga di log.

    `master` puo' essere None: vuol dire "il ruolo lo decidera' il confronto
    con il peer dell'amico, appena si annuncia" (vedi set_master).
    """

    def __init__(self, master, dev, send_net, log, epoca=None):
        self.master = master
        # Il ruolo: AwaitMode visto ma partner ancora ignoto = comando in
        # attesa. Specchio di _awaitVisto/_ruoloInviato in club.js.
        self._await_mode_visto = False
        self._ruolo_inviato = False
        self._ruolo_atteso = False
        self.dev = dev
        self.send_net = send_net
        self.log = log

        self.finita = False
        self.abortita = False
        self.motivo_fine = ""

        # l'identita' di QUESTA sessione: viaggia in ogni corpo di rete
        if epoca is None:
            epoca = struct.unpack("<I", os.urandom(4))[0] or 1
        self.epoca = epoca
        self.epoca_partner = None
        self._epoche_morte = []

        # lato stati
        self._sseq = 0
        self._visti_sseq = set()
        self._io_hs = False
        self._lui_hs = False
        self._hs_avviato = False
        self._io_chiuso = False
        self._lui_chiuso = False
        self._lui_connesso = False
        self._connect_inviato = False
        self._io_connesso = False      # il NOSTRO device e' "cavo collegato"
        self._ultimo_stato = None      # l'ultimo stato annunciato (riannunci)
        self._lui_stato_visto = None   # l'ultimo stato VISTO dal partner:
                                       # un riannuncio identico non e' progresso
        # Il timer della riapertura: armato da _nuovo_giro, spento quando la
        # scala del giro nuovo e' completa (_forse_riapertura_ok). Se resta
        # armato oltre RIAPERTURA_S, il link non riapre piu': fine sessione.
        self._riaperto_quando = 0.0
        # Una SEZIONE del firmware e' viva sul nostro device: dal suo
        # HandshakeReceived fino alla riconnessione. Fuori da questa
        # finestra i comandi non si mandano (vedi _forse_connect_link).
        self._sezione_viva = False
        # La sezione e' COLLEGATA (cavo collegato): e' il cancello dei BLOCCHI.
        # Consegnarli prima - anche solo all'handshake - li fa cadere nel vuoto:
        # e' il campo del 2026-08-27 (GBA vs emulatore), 7 blocchi persi e il
        # GBA appeso ad "attendi" per sempre.
        self._sezione_pronta = False
        # Il congedo (vedi CONGEDO_MAX_S): l'amico e' uscito, accompagniamo
        # fuori il nostro gioco invece di staccargli il cavo.
        self.congedo = False
        self._congedo_da = 0.0
        # LA SESSIONE COMINCIA CON AwaitMode, NON CON IL PRIMO STATO CHE PASSA
        # (2026-08-16). Smontare il passthrough lascia residui nell'endpoint
        # di stato, LinkClosed compreso: senza questo flag un residuo chiudeva
        # una sessione mai nata, ed e' cio' che ha fatto fallire ogni scambio
        # nella prova sul campo (`device: link chiuso` come prima riga,
        # `blocchi tx 0 rx 0`). usb_link.club_enter() ora filtra gia' a monte;
        # questo e' il secondo giro di chiave, perche' il costo di sbagliarsi
        # e' un'altra serata persa in due.
        self._avviata = False

        # lato dati
        self._tx_seq = 0
        self._storia = {}            # seq -> blocco (per le rispedizioni)
        self._ultimo_tx = None       # (seq, blocco) per il rilancio periodico
        self._ultimo_tx_quando = 0.0
        self._rx_attesa = 0          # prossima sequenza da consegnare
        self._rx_buffer = {}         # fuori ordine, in attesa del buco
        self._in_attesa_dev = []     # gia' in ordine, aspettano la sezione
        adesso = time.monotonic()
        self._nata = adesso
        self._progresso = adesso
        self._partner_quando = adesso
        self._annuncio_quando = 0.0  # 0 = annuncia al primo tick

        # contatori (finiscono nel log di stato del client)
        self.blocchi_tx = 0
        self.blocchi_rx = 0
        self.duplicati = 0
        self.fuori_ordine = 0
        self.richiesti = 0
        self.rilanci = 0
        self.riannunci = 0
        self.partner_riavvii = 0     # epoche nuove adottate in corsa
        self.epoca_scarti = 0        # pacchetti di epoche morte, buttati
        self.salti_numerazione = 0   # adozioni della numerazione del partner
        self.giri = 0                # riaperture del link (scambi, lotte...)
        self.blocchi_tenuti = 0      # consegnati alla sezione nuova
        self.blocchi_persi_dev = 0   # buttati: attesa piena (non deve mai)

    # -- helpers -----------------------------------------------------------

    def _tocca(self):
        self._progresso = time.monotonic()

    def _annuncia_stato(self, status):
        corpo = club_status(self.epoca, self._sseq, status)
        self._sseq = (self._sseq + 1) & 0xFFFF
        for _ in range(COPIE_STATO):
            self.send_net(corpo)

    def set_master(self, m):
        """Il ruolo deciso da fuori (il client ha visto il peer dell'amico).

        Se AwaitMode e' gia' passato, il comando parte adesso; se deve ancora
        arrivare, sara' lui a mandarlo. In entrambi i casi UNA volta sola.
        """
        if self._ruolo_inviato or m is None:
            return
        self.master = m
        self._forse_ruolo()

    def _forse_ruolo(self):
        """Manda SetMode se il ruolo e' noto E il device lo sta aspettando."""
        if self._ruolo_inviato or not self._await_mode_visto:
            return
        if self.master is None:
            if not self._ruolo_atteso:
                self._ruolo_atteso = True
                self.log("[club ] ruolo in attesa: lo decide il confronto dei "
                         "peer-id appena l'amico si annuncia")
            return
        self._ruolo_inviato = True
        self.dev.command(CMD_SET_MODE_MASTER if self.master else CMD_SET_MODE_SLAVE,
                         "ruolo: %s" % ("master" if self.master else "slave"))

    def _nuovo_giro(self, motivo):
        """Il link e' stato chiuso e riaperto: la scala ricomincia da capo.

        Non e' un guasto, e' come funziona il Cable Club. Si entra nella
        saletta con una sessione; ci si siede alla macchina degli scambi (o
        si comincia una lotta, o si mixano i record) e il gioco ne apre una
        NUOVA: cable_club.c, Task_ReestablishLink -> OpenLink(), con
        gLinkType che passa a TRADE/BATTLE. Il firmware fa lo stesso: la
        UsbSection finisce, lui manda LinkReconnecting e dopo 400 ms ne crea
        un altra (module/link.cpp:26-30), che rimette in coda TUTTI i
        comandi della scala.

        Prima di questa correzione i gate erano per SESSIONE: _hs_avviato
        restava alzato e StartHandshake non ripartiva piu'. Letto sul campo
        il 2026-08-20: i due si siedono, "un momento attendi", i blocchi si
        congelano (tx fermo, solo rilanci) e il gioco va in errore per
        timeout. Il giro nuovo lo decide SEMPRE il nostro device via USB,
        mai la rete: l'USB non perde pacchetti, la rete si'."""
        self.giri += 1
        self._io_hs = False
        self._hs_avviato = False
        self._sezione_viva = False
        self._sezione_pronta = False
        # _lui_hs NON si azzera, e non e' una dimenticanza. I due lati si
        # riaprono nello stesso istante (il gioco chiude il link su
        # entrambi) e l'ordine e' una corsa: se l'annuncio del partner
        # arriva un attimo PRIMA del nostro reset, azzerarlo lo
        # cancellerebbe e resteremmo fermi fino al riannuncio, due secondi
        # buoni su un timeout di dieci. Tenerlo non costa sicurezza: il
        # flag che protegge il device e' _io_hs (dall'USB, sempre vero) e
        # StartHandshake e' proprio cio' che la sezione nuova aspetta.
        #
        # _lui_connesso INVECE si azzera, e li' e' obbligatorio: un
        # ConnectLink mandato prima che il Pico abbia trasmesso almeno una
        # volta la parola di slave sostituisce quella parola e la sezione
        # resta appesa per sempre (usbSection.cpp:15-27). Quel comando deve
        # nascere da un LinkConnected del giro NUOVO, non da uno vecchio.
        self._lui_connesso = False
        self._connect_inviato = False
        self._io_connesso = False
        self._ultimo_stato = None
        self._annuncio_quando = 0.0
        # Da qui parte il conto alla rovescia: o la scala nuova si completa
        # entro RIAPERTURA_S, o il gioco ha chiuso per davvero (frullatore,
        # annullo, fine meccanica) e la sessione va chiusa da noi - il
        # firmware LinkClosed non lo dira' mai (vedi RIAPERTURA_S).
        self._riaperto_quando = time.monotonic()
        self.log("[club ] %s: la scala del link si rifa' da capo (giro %d)"
                 % (motivo, self.giri))

    def _al_device(self, blocco64):
        """L unica porta verso il device, ed esiste per un motivo preciso.

        Quando il firmware apre una sezione nuova installa il gestore dei
        comandi, e quel gestore comincia con k_msgq_purge: TUTTO cio' che
        abbiamo scritto durante il buco fra le due sezioni viene buttato
        via (usbLinkCommand.cpp, init). E cio' che arriva proprio in quel
        momento e' la raffica dei dati giocatore del partner - sette
        comandi che il gioco non ritenta MAI (link.c, LinkCB_BlockSend).
        Perderne uno li' e' il gioco appeso a "un momento attendi".

        Quindi finche' la sezione non c'e' i blocchi si tengono da parte,
        in ordine, e si consegnano tutti insieme appena esiste."""
        if self._sezione_pronta:
            self.dev.send_block(blocco64)
            return
        if len(self._in_attesa_dev) < ATTESA_DEV_MAX:
            self._in_attesa_dev.append(blocco64)
        else:
            self.blocchi_persi_dev += 1

    def _consegna_arretrato(self):
        """La sezione nuova c'e': si svuota cio' che si era tenuto."""
        if not self._in_attesa_dev:
            return
        arretrato, self._in_attesa_dev = self._in_attesa_dev, []
        self.blocchi_tenuti += len(arretrato)
        self.log("[club ] %d blocchi tenuti da parte durante la riapertura: "
                 "li consegno adesso alla sezione nuova" % len(arretrato))
        for blocco in arretrato:
            self.dev.send_block(blocco)

    def _epoca_ok(self, epoca):
        """Il filtro d'ingresso di TUTTO cio' che arriva dalla rete.
        Ritorna True se il pacchetto appartiene alla sessione corrente del
        partner; adotta in corsa un'epoca nuova (= il suo client e' ripartito);
        scarta le epoche gia' abbandonate."""
        if epoca == self.epoca_partner:
            self._partner_quando = time.monotonic()
            return True
        if epoca in self._epoche_morte:
            self.epoca_scarti += 1
            return False
        self._partner_quando = time.monotonic()
        if self.epoca_partner is None:
            self.epoca_partner = epoca
            self.log("[club ] partner agganciato (epoca 0x%08X)" % epoca)
            return True
        # Epoca nuova a sessione viva: il client dell'amico e' RIPARTITO.
        # Il suo device ricomincia da AwaitMode e le sue sequenze da 0: il
        # lato-partner del nostro stato non vale piu' niente. Si riazzera
        # QUELLO (il nostro device non si tocca: e' avanti e va bene cosi')
        # e si riannuncia subito il nostro ultimo stato, cosi' il suo ladder
        # fresco puo' salire.
        self._epoche_morte.append(self.epoca_partner)
        del self._epoche_morte[:-EPOCHE_MORTE_MAX]
        vecchia = self.epoca_partner
        self.epoca_partner = epoca
        self.partner_riavvii += 1
        self._visti_sseq.clear()
        self._lui_hs = False
        self._lui_chiuso = False
        self._lui_connesso = False
        self._connect_inviato = False
        self._lui_stato_visto = None   # gli stati del client fresco contano
        self._rx_buffer.clear()
        self._rx_attesa = 0            # il partner fresco riparte da 0
        self._annuncio_quando = 0.0    # riannuncio al primo tick
        self._tocca()
        self.log("[club ] IL CLIENT DELL'AMICO E' RIPARTITO (epoca 0x%08X -> "
                 "0x%08X): riaggancio in corsa, riazzero il suo lato e "
                 "riannuncio il mio" % (vecchia, epoca))
        return True

    def nota_partner(self, epoca):
        """Un CLUB_ENTER: nessun payload, ma l'epoca va agganciata/adottata."""
        if not self.finita:
            self._epoca_ok(epoca)

    # -- eventi dal DEVICE (il mio Pico) -----------------------------------

    def on_device_status(self, status):
        if self.finita:
            return
        self._tocca()
        nome = LINK_ST_NOMI.get(status, "0x%04X" % status)

        # Niente che arrivi PRIMA di AwaitMode appartiene a questa sessione:
        # sono i resti del passthrough appena smontato. Non si processano e
        # soprattutto non si ANNUNCIANO al partner, o il residuo passerebbe
        # dalla porta di servizio e chiuderebbe la sessione da casa sua.
        if not self._avviata and status != LINK_ST_AWAIT_MODE:
            self.log("[club ] device: %s (residuo del passthrough, ignorato)"
                     % nome)
            return

        self.log("[club ] device: %s" % nome)

        # IL GIOCO CHIUDE E RIAPRE IL LINK A META' SESSIONE, ed e' il caso
        # NORMALE di ogni meccanica: sedersi alla macchina degli scambi,
        # cominciare una lotta, mixare i record. Il firmware lo annuncia con
        # LinkReconnecting. Da qui la scala va rifatta da capo.
        if status == LINK_ST_RECONNECTING:
            self._nuovo_giro("il gioco ha riaperto il link")

        # Come Celio: Ready e Debug restano locali, il resto si annuncia.
        if status not in (LINK_ST_READY, LINK_ST_DEBUG):
            # RECONNECTING si annuncia una volta (e' informativo) ma NON
            # diventa "l'ultimo stato": riannunciato ogni 2 s, il partner se
            # lo rileggerebbe per sempre come notizia nuova.
            if status != LINK_ST_RECONNECTING:
                self._ultimo_stato = status
            self._annuncia_stato(status)

        if status == LINK_ST_AWAIT_MODE:
            self._avviata = True
            # La negoziazione collassata: il ruolo lo decide il confronto dei
            # peer-id. Se l'amico non si e' ancora annunciato il comando NON
            # parte: aspetta set_master. Il ruolo NON si ripete a ogni giro -
            # il firmware lo tiene nel modulo e la sezione nuova nasce gia'
            # col modo giusto (link.cpp:17-22) - quindi _ruolo_inviato.
            self._await_mode_visto = True
            self._forse_ruolo()
        elif status == LINK_ST_HANDSHAKE_RX:
            # E' la prova che una sezione NUOVA esiste ed e' arrivata al
            # punto in cui aspetta i nostri comandi. Prima di qui il
            # firmware puo' avere m_currentSection a nullptr: vedi
            # _forse_connect_link, il comando che ci finirebbe dentro.
            self._sezione_viva = True
            self._io_hs = True
            self._forse_start_handshake()
            self._forse_connect_link()   # se il partner era gia' pronto
        elif status == LINK_ST_CONNECTED:
            # Il partner, ricevendolo, mandera' ConnectLink al SUO device
            # (server.ts:170-173, emitToOppositeSocket). Per noi e' meta'
            # della prova che una riapertura si e' completata.
            #
            # E SOLO ADESSO i blocchi si consegnano: il campo del 2026-08-27
            # (GBA vs emulatore) ha mostrato che i blocchi spinti nel device
            # DURANTE l'handshake si perdono - `cavo collegato` non arrivava
            # nemmeno - e la raffica dei dati giocatore il gioco non la
            # ritenta mai.
            self._sezione_pronta = True
            self._consegna_arretrato()
            self._io_connesso = True
            self._forse_riapertura_ok()
        elif status == LINK_ST_CLOSED:
            self._io_chiuso = True
            if self.congedo:
                # Il congedo e' riuscito: il gioco e' uscito DALLA PORTA, non
                # per un errore del cavo. E' la riga che distingue le due cose.
                self.finita = True
                self.log("[club ] il gioco e' uscito dalla saletta dalla "
                         "porta: congedo riuscito")
                return
            self._forse_finita()

    def on_device_block(self, blocco64):
        if self.finita:
            return
        self._tocca()
        if self.congedo:
            # Il gioco chiede di chiudere: gli si risponde di si'. E' la
            # condizione che il firmware aspetta per uscire dal suo giro
            # (partnerReadyCloseLink && readyCloseLink, usbSection.cpp:70-73).
            cmd = blocco64[0] | (blocco64[1] << 8)
            if cmd == LINKCMD_READY_CLOSE_LINK:
                self._al_device(blocco_da_cmd([LINKCMD_READY_CLOSE_LINK]))
            else:
                self._al_device(blocco_da_cmd([LINKCMD_HELD_KEYS, LINK_KEY_IDLE]))
            return   # durante il congedo non si parla piu' con la rete
        seq = self._tx_seq
        self._tx_seq += 1
        self._storia[seq] = blocco64
        if len(self._storia) > STORIA_BLOCCHI:
            del self._storia[min(self._storia)]
        self._ultimo_tx = (seq, blocco64)
        self._ultimo_tx_quando = time.monotonic()
        self.send_net(club_data(self.epoca, seq, blocco64))
        self.blocchi_tx += 1

    # -- eventi dalla RETE (il partner, via relay) -------------------------

    def on_net_status(self, epoca, sseq, status):
        if self.finita or not self._epoca_ok(epoca):
            return
        if sseq in self._visti_sseq:
            return                       # copia del triplo invio / riannuncio
        self._visti_sseq.add(sseq)
        # PROGRESSO e' uno stato NUOVO, non un riannuncio: i riannunci hanno
        # sseq freschi (il dedup non li ferma) e tenevano vivo il watchdog
        # all'infinito nelle sessioni gia' morte ("partner: cavo collegato"
        # ogni 2 s per sempre, campo 2026-08-27). Lo stato ripetuto si
        # PROCESSA comunque - i flag sono idempotenti, e dopo una riapertura
        # il riannuncio identico e' proprio cio' che fa risalire la scala -
        # ma il watchdog conta solo le novita'.
        if status != self._lui_stato_visto:
            self._lui_stato_visto = status
            self._tocca()
        self.log("[club ] partner: %s"
                 % LINK_ST_NOMI.get(status, "0x%04X" % status))

        if status == LINK_ST_HANDSHAKE_RX:
            self._lui_hs = True
            self._forse_start_handshake()
        elif status == LINK_ST_CONNECTED:
            # server.ts: LinkConnected di la' -> ConnectLink di qua. Il
            # comando pero' non parte sempre subito: le condizioni le decide
            # _forse_connect_link, e finche' non ci sono il comando resta
            # SEGNATO (questo flag) e parte appena diventano vere.
            self._lui_connesso = True
            self._forse_connect_link()
        elif status == LINK_ST_RECONNECTING:
            # Informativo. Il giro nuovo lo decide il NOSTRO device, non la
            # rete: cosi' un annuncio perso non ci lascia indietro e uno
            # ripetuto non ci fa ricominciare in eterno.
            pass
        elif status == LINK_ST_CLOSED:
            if not self._avviata:
                self.log("[club ] (il partner riporta un residuo: ignorato)")
                return
            self._lui_chiuso = True
            # Se il NOSTRO gioco e' ancora dentro, non si stacca: lo si
            # accompagna fuori. Se ha gia' chiuso lui, si finisce come sempre.
            if self._io_chiuso:
                self._forse_finita()
            else:
                self._congeda("il gioco dell'amico ha chiuso il link")

    def on_net_block(self, epoca, seq, blocco64):
        if self.finita or not self._epoca_ok(epoca):
            return
        self.blocchi_rx += 1
        if seq < self._rx_attesa:
            # Duplicato (rilancio, o copia riordinata): NON e' progresso,
            # non tocca il watchdog - i dup tenevano vive sessioni zombie.
            self.duplicati += 1
            return
        self._tocca()
        if seq > self._rx_attesa + STORIA_BLOCCHI:
            # Un buco piu' largo della storia del partner e' irrecuperabile
            # PER COSTRUZIONE (lui tiene 512 blocchi): chiedere i mancanti
            # sarebbe chiedere l'impossibile per sempre. Succede in un caso
            # solo: ci si e' agganciati a una sessione del partner gia'
            # vecchia. Si adotta la sua numerazione e si va.
            self.salti_numerazione += 1
            self.log("[club ] numerazione del partner adottata al volo: "
                     "salto da seq %d a %d (aggancio a sessione gia' in "
                     "corso)" % (self._rx_attesa, seq))
            self._rx_buffer.clear()
            self._rx_attesa = seq
        if seq > self._rx_attesa:
            self.fuori_ordine += 1
            self._rx_buffer[seq] = blocco64
            self._chiedi_mancanti(seq)
            self._svuota_buffer()
            return
        self._al_device(blocco64)
        self._rx_attesa += 1
        self._svuota_buffer()

    def on_net_req(self, epoca, seqs):
        if self.finita or not self._epoca_ok(epoca):
            return
        self._tocca()
        for seq in seqs:
            blocco = self._storia.get(seq)
            if blocco is not None:
                self.send_net(club_data(self.epoca, seq, blocco))

    def on_net_leave(self, epoca):
        if self.finita or not self._epoca_ok(epoca):
            return
        self._congeda("l'amico e' uscito dalla saletta")

    # -- il battito --------------------------------------------------------

    def tick(self):
        """Da chiamare spesso (il ciclo del client va gia' a ~20 Hz)."""
        if self.finita:
            return
        adesso = time.monotonic()

        # Il ConnectLink lasciato in attesa dal distanziamento.
        self._forse_connect_link()

        # Il congedo non puo' durare per sempre: se il device non dice "link
        # chiuso" entro CONGEDO_MAX_S si stacca comunque - ma avendoci
        # provato, che e' la differenza fra uscire e andare in errore.
        if self.congedo and adesso - self._congedo_da > CONGEDO_MAX_S:
            self.abortita = True
            self.finita = True
            self.log("[club ] il gioco non ha chiuso il link entro %d s: stacco"
                     % CONGEDO_MAX_S)
            return

        # Il rilancio dell'ultimo blocco: se il device tace, l'ultima cosa
        # detta viene ripetuta finche' la sessione vive. Il dedup di la'
        # (seq < attesa) la scarta gratis.
        if (self._ultimo_tx is not None
                and adesso - self._ultimo_tx_quando >= RILANCIO_ULTIMO_S):
            seq, blocco = self._ultimo_tx
            self.send_net(club_data(self.epoca, seq, blocco))
            self._ultimo_tx_quando = adesso
            self.rilanci += 1

        # I riannunci: presenza e ultimo stato. Coprono il partner che si
        # riavvia (deve poter risalire il ladder anche se gli annunci
        # originali sono passati prima che nascesse) e le copie perse.
        if adesso >= self._annuncio_quando:
            self._annuncio_quando = adesso + ANNUNCIO_S
            if self.epoca_partner is None:
                self.send_net(club_enter(self.epoca))
                self.riannunci += 1
            if self._io_chiuso and not self._lui_chiuso:
                self._annuncia_stato(LINK_ST_CLOSED)
                self.riannunci += 1
            elif not self._lui_connesso and self._ultimo_stato is not None:
                self._annuncia_stato(self._ultimo_stato)
                self.riannunci += 1

        # Il club fantasma: nato da un EVENT_CLUB stantio, nessun GBA e'
        # davvero al bancone. Senza questo, i due client restavano ad
        # "attesa ruolo" per sempre e la camminata non ripartiva piu'
        # (campo, 2026-08-21).
        if (not self._io_hs and not self._lui_hs
                and adesso - self._nata > FANTASMA_S):
            self.log("[club ] NESSUN GBA si e' presentato al club in %d s: "
                     "avvio fantasma, si torna a camminare" % int(FANTASMA_S))
            self.motivo_fine = "fantasma"
            self.abortita = True
            self.finita = True
            return

        # Il gioco ha chiuso il link e la riapertura non si e' mai completata:
        # non era una meccanica che riapre, era la FINE della comunicazione
        # (frullatore, annullo al bancone, fine scambio/lotta). Il firmware
        # LinkClosed non lo dira' mai (aspetta EXIT_ROOM dalla porta): si
        # chiude da qui e si torna al passthrough. Vedi RIAPERTURA_S.
        if (self._riaperto_quando
                and adesso - self._riaperto_quando > RIAPERTURA_S):
            self.log("[club ] IL GIOCO HA CHIUSO IL LINK e in %d s la "
                     "riapertura non si e' completata: comunicazione finita "
                     "(fine o annullo di scambio/lotta/frullatore), si torna "
                     "a camminare" % int(RIAPERTURA_S))
            self.motivo_fine = "link chiuso senza riapertura"
            self.abortita = True
            self.finita = True
            return

        # Partner muto: mai visto, o client di la' morto senza LEAVE.
        if adesso - self._partner_quando > PARTNER_MUTO_S:
            self.log("[club ] L'AMICO NON SI FA SENTIRE da %d s: sessione "
                     "abbandonata, si torna a camminare"
                     % int(PARTNER_MUTO_S))
            self.motivo_fine = "partner muto"
            self.abortita = True
            self.finita = True
            return

        if adesso - self._progresso > WATCHDOG_S:
            self.log("[club ] NESSUN PROGRESSO da %d s: sessione abbandonata, "
                     "si torna a camminare" % int(WATCHDOG_S))
            self.motivo_fine = "watchdog"
            self.abortita = True
            self.finita = True

    # -- i pezzi interni ---------------------------------------------------

    def _forse_start_handshake(self):
        # server.ts:157-163: quando TUTTI hanno HandshakeReceived, il server
        # manda StartHandshake a entrambi. Qui ogni lato valuta da se' la
        # stessa condizione sul proprio device.
        if (self._io_hs and self._lui_hs and self._sezione_viva
                and not self._hs_avviato):
            self._hs_avviato = True
            self._hs_quando = time.monotonic()
            self.dev.command(CMD_START_HANDSHAKE, "StartHandshake (entrambi pronti)")
            self._forse_connect_link()   # il ConnectLink in attesa, se c'e'

    def _forse_connect_link(self):
        """ConnectLink, ma solo quando e' sicuro mandarlo. Due condizioni, e
        nessuna delle due e' pignoleria:

        - DOPO StartHandshake, e DISTANZIATO (CONNECT_RITARDO_S). Il
          firmware aspetta di vedere la parola di SLAVE trasmessa
          (usbSection.cpp:15, isHandshakeEnabled), e connectHandshake() la
          sostituisce con quella di master: arrivare prima che un
          trasferimento sia partito brucia il gradino e la sezione resta
          appesa per sempre. E' successo DAVVERO col partner in emulatore,
          che annuncia "collegato" all'istante (campo 2026-08-27).
        - Con una SEZIONE VIVA. Fra una sezione e l'altra il firmware tiene
          m_currentSection a nullptr per 400 ms (link.cpp:24-30) e
          receiveCommand lo dereferenzia senza controllare (link.cpp:46-47,
          l'autore stesso commenta "kinda sketch"): un ConnectLink capitato
          in quella finestra e' un puntatore nullo sul Pico.

        Se le condizioni non ci sono il comando non si perde: resta segnato
        in _lui_connesso e parte da se' appena diventano vere."""
        if (self._lui_connesso and self._sezione_viva and self._hs_avviato
                and not self._connect_inviato
                and time.monotonic() - self._hs_quando >= CONNECT_RITARDO_S):
            self._connect_inviato = True
            self.dev.command(CMD_CONNECT_LINK, "ConnectLink (partner pronto)")
            self._forse_riapertura_ok()

    def _forse_riapertura_ok(self):
        """La scala del giro nuovo e' completa - il nostro device e' connesso
        E il ConnectLink e' partito (= anche il partner e' connesso). Era una
        riapertura vera: il conto alla rovescia si spegne. Serve la coppia:
        nel guasto del 2026-08-27 un lato arrivava a "cavo collegato" da solo
        (il gioco riprovava al bancone) mentre l'altro non c'era piu'."""
        if (self._riaperto_quando and self._io_connesso
                and self._connect_inviato):
            self._riaperto_quando = 0.0

    def _chiedi_mancanti(self, fino_a):
        mancanti = [s for s in range(self._rx_attesa, fino_a)
                    if s not in self._rx_buffer][:16]
        if mancanti:
            self.richiesti += len(mancanti)
            self.send_net(club_req(self.epoca, mancanti))

    def _svuota_buffer(self):
        while self._rx_attesa in self._rx_buffer:
            self._al_device(self._rx_buffer.pop(self._rx_attesa))
            self._rx_attesa += 1

    def _congeda(self, motivo):
        """L'amico se n'e' andato. NON si stacca: si accompagna fuori anche il
        nostro gioco, con la danza del cavo vero (PORTA, poi l'eco di
        READY_CLOSE_LINK). Chiudera' il device dicendo "link chiuso"."""
        if self.finita or self.congedo:
            return
        if not self._sezione_pronta:   # cavo mai collegato: niente da fare
            self.motivo_fine = motivo
            self.abortita = True
            self.finita = True
            return
        self.congedo = True
        self._congedo_da = time.monotonic()
        self.motivo_fine = motivo
        self.log("[club ] %s: accompagno fuori il gioco dalla porta della "
                 "saletta (niente strappo al cavo)" % motivo)
        self._al_device(blocco_da_cmd([LINKCMD_HELD_KEYS, LINK_KEY_EXIT_ROOM]))

    def _forse_finita(self):
        # server.ts:179-189: tutti chiusi -> sessione chiusa.
        if self._io_chiuso and self._lui_chiuso:
            self.finita = True
            self.motivo_fine = "chiusa da entrambi"
            self.log("[club ] sessione chiusa da ENTRAMBI i lati: si torna "
                     "al passthrough")

    def riassunto(self):
        riga = ("blocchi tx %d rx %d | dup %d fuori-ordine %d richiesti %d "
                "rilanci %d | riannunci %d"
                % (self.blocchi_tx, self.blocchi_rx, self.duplicati,
                   self.fuori_ordine, self.richiesti, self.rilanci,
                   self.riannunci))
        # I contatori delle patologie si stampano solo se > 0, ma se ci sono
        # devono GRIDARE (regola del progetto: un contatore muto nel log e'
        # una diagnosi persa).
        if self.giri:
            riga += " | riaperture del link %d" % self.giri
        if self.blocchi_tenuti:
            riga += " | blocchi salvati dalla riapertura %d" % self.blocchi_tenuti
        if self.blocchi_persi_dev:
            riga += " | BLOCCHI PERSI (attesa piena) %d" % self.blocchi_persi_dev
        if self.partner_riavvii:
            riga += " | RIAVVII DEL PARTNER %d" % self.partner_riavvii
        if self.epoca_scarti:
            riga += " | scarti epoca morta %d" % self.epoca_scarti
        if self.salti_numerazione:
            riga += " | SALTI DI NUMERAZIONE %d" % self.salti_numerazione
        return riga
