/* club.js - la sessione del Cable Club nel browser (2026-08-26).
 *
 * E' il porting riga per riga di net/club_link.py (che a sua volta e' la porta
 * in Python della sessione Celio): stessa macchina a stati, stessi contatori,
 * stesse righe di log, stesso protocollo sul filo (i corpi T_CLUB di
 * net/protocol.py, byte per byte). Chi ha letto i log del pannello Python
 * legge questi senza imparare niente di nuovo. Se cambia la macchina a stati
 * si cambia QUI e in club_link.py insieme, e si alza VERSIONE_CLUB in
 * ENTRAMBI.
 *
 * L'UNICA differenza vera dal Python: I RUOLI. Il pannello Python collassa la
 * negoziazione su "peer 1 = master, peer 2 = slave" perche' i suoi peer sono
 * scelti a mano. Il sito invece sorteggia il peer (1..65001), quindi la regola
 * si generalizza SENZA rompere quella vecchia:
 *
 *     peer 1  -> sempre master     (come il Python)
 *     peer 2  -> sempre slave      (come il Python)
 *     altri   -> vince il piu' BASSO (simmetrico e deterministico)
 *
 * Cosi' sito<->sito funziona coi numeri sorteggiati, e sito<->pannello Python
 * funziona con i peer 1/2 del pannello (il Python non cambia di una riga).
 * Il peer dell'amico si legge dall'header OWL1 del primo T_CLUB che arriva:
 * se il nostro peer non e' 1 ne' 2, il comando di ruolo al device ASPETTA
 * quel primo pacchetto (il firmware in AwaitMode aspetta comunque il ruolo,
 * e' il suo mestiere - in Celio originale glielo assegnava il server).
 * Limite noto: pannello Python con peer scelto a mano >= 3 contro il sito
 * NON negozia (il Python li' dice sempre "slave") - come gia' prima.
 *
 * La classe non tocca ne' USB ne' WebSocket: parla con due interfacce
 * iniettate (dev e sendNet), ed e' per questo che bridge_test.html la prova a
 * secco, come net/test_club.py - regola del progetto.
 *
 * Script classico (niente moduli ES), come il resto di web/js.
 */
(function (root) {
  "use strict";

  /* --- costanti della sessione (patchabili dai test, come in Python) ------- */
  var K = {
    STORIA_BLOCCHI: 512,     // blocchi tenuti per le rispedizioni (Celio: 512)
    COPIE_STATO: 3,          // copie di ogni stato (dedup sul serial di la')
    RILANCIO_ULTIMO_S: 0.3,  // rilancio dell'ultimo blocco se il device tace
    /* ConnectLink DISTANZIATO da StartHandshake. Il firmware trasmette 0xB9A0
     * solo nello stato "enabled" (packetLayer.hpp:188) e `isHandshakeEnabled`
     * pretende di AVERLO TRASMESSO (riga 120): se ConnectLink arriva prima che
     * un trasferimento sia partito, lo stato salta a "connect", 0xB9A0 non esce
     * mai e `establishConncection` (usbSection.cpp) resta appeso per sempre -
     * niente `cavo collegato`, mai. Con due GBA veri il distacco lo mette la
     * rete; con l'EMULATORE (che annuncia "collegato" all'istante) i due
     * comandi partivano nello stesso millisecondo: campo del 2026-08-27. */
    CONNECT_RITARDO_S: 0.35, // ConnectLink mai prima di cosi' da StartHandshake
    /* IL CONGEDO. L'amico esce dalla porta: staccare qui il cavo lascia il
     * NOSTRO gioco dentro la coreografia d'uscita, senza partner - schermata
     * nera di errore e riavvio (campo 2026-08-27). Il firmware fa l'opposto
     * (usbSection.cpp:41-79): visto EXIT_ROOM continua a fare da cavo finche'
     * i due si sono scambiati READY_CLOSE_LINK. Qui si fa uguale: si manda al
     * device la PORTA e poi l'ECO di chiusura, e si aspetta che sia il device
     * a dire "link chiuso". */
    CONGEDO_MAX_S: 6.0,      // poi si stacca comunque, ma avendoci provato
    ANNUNCIO_S: 2.0,         // riannuncio periodico (ENTER / ultimo stato)
    WATCHDOG_S: 120.0,       // nessun progresso -> sessione morta
    PARTNER_MUTO_S: 120.0,   // partner mai visto o morto senza LEAVE
    FANTASMA_S: 90.0,        // nessun GBA al bancone -> club fantasma
    // Dopo una "riconnessione" la scala nuova deve COMPLETARSI entro questo
    // tempo. Le riaperture vere (macchina degli scambi, lotta) tornano su in
    // pochi secondi; il frullatore, l'annullo al bancone e la fine di uno
    // scambio/lotta invece chiudono il link SENZA passare dalla porta della
    // saletta (CAFE 0017, EXIT_ROOM) - l'unica chiusura che il firmware
    // riconosca (usbSection.cpp:54-62) - quindi LinkClosed non arrivera'
    // mai e senza questo timer il Pico restava in modo link per sempre
    // (campo 2026-08-27: frullatore finito, overworld morto fino al
    // riavvio). 30 s = 3 volte la pazienza del gioco: meglio larghi.
    RIAPERTURA_S: 30.0,
    EPOCHE_MORTE_MAX: 8,     // epoche abbandonate da ricordare
    ATTESA_DEV_MAX: 64       // blocchi tenuti da parte fra due sezioni
  };

  /* Gli stati che il firmware emette in modo onlineLink (usb_link.py). */
  var ST = {
    AWAIT_MODE: 0xFF02, HANDSHAKE_RX: 0xFF03, HANDSHAKE_OK: 0xFF04,
    CONNECTED: 0xFF05, RECONNECTING: 0xFF06, CLOSED: 0xFF07,
    READY: 0xFF08, DEBUG: 0xFFFF
  };
  var ST_NOMI = {
    0xFF02: "attesa ruolo", 0xFF03: "handshake col GBA",
    0xFF04: "handshake finito", 0xFF05: "cavo collegato",
    0xFF06: "riconnessione", 0xFF07: "link chiuso",
    0xFF08: "pronto", 0xFFFF: "debug"
  };
  function nomeStato(s) { return ST_NOMI[s] || ("0x" + s.toString(16).toUpperCase()); }

  /* I comandi del device (usb_link.py / device.js: stessi numeri). */
  var CMD = { SET_MODE_MASTER: 0x10, SET_MODE_SLAVE: 0x11, START_HANDSHAKE: 0x12, CONNECT_LINK: 0x13 };

  /* --- il protocollo dei corpi T_CLUB (gemello di net/protocol.py) ---------
   * 4 = fine sessione su riapertura mai completata + watchdog che non conta
   *     i riannunci (2026-08-27, il frullatore lasciava il club appeso). */
  var VERSIONE_CLUB = 4;
  /* L'impronta del CLIENT WEB: una costante riservata al posto dell'md5 dei
   * .py (che qui non esistono). Il pannello Python la riconosce e dice
   * "l'amico gioca dal browser" invece di urlare al pacchetto vecchio.
   * Stessa costante in net/protocol.py (IMPRONTA_WEB): si toccano insieme. */
  var IMPRONTA_WEB = 0x57454221;
  /* L'impronta dell'EMULATORE (mgba/club_lua.lua, 2026-08-27): come il sito,
   * il Lua di mGBA non ha i .py da hashare. Serve a non urlare "FILE DIVERSI"
   * a chi gioca in emulatore - che col Brick e' il caso normale. Stessa
   * costante in net/protocol.py (IMPRONTA_LUA): si toccano insieme. */
  var IMPRONTA_LUA = 0x4C554131;
  /* I comandi della danza d'uscita (link.h, identici nel firmware). */
  var LINKCMD_HELD_KEYS = 0xCAFE, LINKCMD_READY_CLOSE_LINK = 0x5FFF;
  var LINK_KEY_IDLE = 0x11, LINK_KEY_EXIT_ROOM = 0x17;

  var CLUB_STATUS = 1, CLUB_DATA = 2, CLUB_REQ = 3, CLUB_ENTER = 4, CLUB_LEAVE = 5;

  function put16(b, off, v) { b[off] = v & 0xFF; b[off + 1] = (v >> 8) & 0xFF; }
  function put32(b, off, v) { put16(b, off, v); put16(b, off + 2, (v >>> 16)); }
  function get16(b, off) { return b[off] | (b[off + 1] << 8); }
  function get32(b, off) { return (get16(b, off) | (get16(b, off + 2) << 16)) >>> 0; }

  function clubStatus(epoca, sseq, status, versione, impronta) {
    /* [sub u8][epoca u32][sseq u16][status u16][versione u16][impronta u32]
     * Versione e impronta in CODA, come in Python: un client vecchio legge i
     * primi 9 byte come sempre e ignora il resto. */
    if (versione === undefined) versione = VERSIONE_CLUB;
    if (impronta === undefined) impronta = IMPRONTA_WEB;
    var b = new Uint8Array(15);
    b[0] = CLUB_STATUS; put32(b, 1, epoca); put16(b, 5, sseq); put16(b, 7, status);
    put16(b, 9, versione); put32(b, 11, impronta);
    return b;
  }
  function clubData(epoca, seq, block64) {
    var b = new Uint8Array(9 + 64);
    b[0] = CLUB_DATA; put32(b, 1, epoca); put32(b, 5, seq);
    b.set(block64.subarray(0, 64), 9);
    return b;
  }
  function clubReq(epoca, seqs) {
    var b = new Uint8Array(6 + 4 * seqs.length);
    b[0] = CLUB_REQ; put32(b, 1, epoca); b[5] = seqs.length & 0xFF;
    for (var i = 0; i < seqs.length; i++) put32(b, 6 + 4 * i, seqs[i]);
    return b;
  }
  function clubEnter(epoca) { var b = new Uint8Array(5); b[0] = CLUB_ENTER; put32(b, 1, epoca); return b; }
  function clubLeave(epoca) { var b = new Uint8Array(5); b[0] = CLUB_LEAVE; put32(b, 1, epoca); return b; }

  /* Ritorna un oggetto per sub, oppure null se malformato (come club_unpack). */
  function clubUnpack(body) {
    var b = body instanceof Uint8Array ? body : new Uint8Array(body);
    if (b.length < 5) return null;
    var sub = b[0], epoca = get32(b, 1);
    if (sub === CLUB_STATUS && b.length >= 9) {
      var out = { sub: sub, epoca: epoca, sseq: get16(b, 5), status: get16(b, 7), versione: 0, impronta: 0 };
      if (b.length >= 15) { out.versione = get16(b, 9); out.impronta = get32(b, 11); }
      return out;
    }
    if (sub === CLUB_DATA && b.length >= 9 + 64) {
      return { sub: sub, epoca: epoca, seq: get32(b, 5), block: new Uint8Array(b.subarray(9, 9 + 64)) };
    }
    if (sub === CLUB_REQ && b.length >= 6) {
      var n = b[5];
      if (b.length >= 6 + 4 * n) {
        var seqs = [];
        for (var i = 0; i < n; i++) seqs.push(get32(b, 6 + 4 * i));
        return { sub: sub, epoca: epoca, seqs: seqs };
      }
    }
    if (sub === CLUB_ENTER || sub === CLUB_LEAVE) return { sub: sub, epoca: epoca };
    return null;
  }

  /* I comandi del protocollo di link della Gen 3 (per il log dei blocchi).
   * I nomi sono quelli VERI della decomp (pokeemerald include/link.h), come
   * in net/protocol.py: la tabella vecchia veniva da Celio ed era sbagliata
   * su piu' voci - 0x4444 etichettato "READY_CLOSE" quando la chiusura vera
   * e' 0x5FFF, e il 2026-08-27 quel nome ha depistato una diagnosi intera. */
  var LINKCMD_NOMI = {
    0x0000: "(vuoto)", 0x1111: "BLENDER_STOP", 0x2222: "SEND_LINK_TYPE",
    0x2FFE: "READY_EXIT_STANDBY", 0x2FFF: "SEND_PACKET",
    0x4444: "BLENDER_SEND_KEYS", 0x5555: "DUMMY_1", 0x5566: "DUMMY_2",
    0x5FFF: "READY_CLOSE_LINK", 0x6666: "SEND_EMPTY", 0x7777: "SEND_0xEE",
    0x7FFF: "COUNTDOWN", 0x8888: "CONT_BLOCK",
    0xAAAA: "BLENDER_NO_PBLOCK_SPACE", 0xAAAB: "SEND_ITEM",
    0xAABB: "READY_TO_TRADE", 0xABCD: "READY_FINISH_TRADE",
    0xBBBB: "INIT_BLOCK", 0xBBCC: "READY_CANCEL_TRADE",
    0xCAFE: "SEND_HELD_KEYS", 0xCCCC: "SEND_BLOCK_REQ",
    0xCCDD: "START_TRADE", 0xDCBA: "CONFIRM_FINISH_TRADE",
    0xDDDD: "SET_MONS_TO_TRADE", 0xDDEE: "PLAYER_CANCEL_TRADE",
    0xEEAA: "REQUEST_CANCEL", 0xEEBB: "BOTH_CANCEL_TRADE",
    0xEECC: "PARTNER_CANCEL_TRADE", 0xEFFF: "LINKCMD_NONE"
  };
  /* La meccanica della sessione: la parola 1 di SEND_LINK_TYPE e' gLinkType
   * (LINKTYPE_* della decomp). Stamparla dice subito COSA stavano facendo. */
  var LINKTYPE_NOMI = {
    0x1111: "scambio, alla macchina", 0x1122: "scambio, aggancio",
    0x1133: "scambio, al bancone", 0x1144: "scambio, staccato",
    0x2211: "lotta", 0x2233: "lotta singola", 0x2244: "lotta doppia",
    0x2255: "lotta multi", 0x2266: "torre lotta L50",
    0x2277: "torre lotta libera", 0x2288: "torre lotta",
    0x3311: "mix record, prima", 0x3322: "mix record, dopo",
    0x4411: "frullatore, al bancone", 0x4422: "frullatore, in corso",
    0x5501: "mystery event", 0x5502: "e-reader RF/VF",
    0x5503: "e-reader smeraldo", 0x6601: "gara, modo G", 0x6602: "gara, modo E"
  };
  function blockRiga(block64) {
    var w = [], parole = [];
    for (var i = 0; i < 8; i++) { w.push(get16(block64, 2 * i)); parole.push(("000" + w[i].toString(16).toUpperCase()).slice(-4)); }
    var nome = LINKCMD_NOMI[w[0]] || ("0x" + parole[0] + "?");
    if (w[0] === 0x2222 && LINKTYPE_NOMI[w[1]]) nome += " = " + LINKTYPE_NOMI[w[1]];
    return parole.join(" ") + " | " + nome;
  }

  /* Un blocco da 64 byte a partire dalle parole del comando: 8 u16 e 48 zeri,
   * come `bloccoDaCmd` del Lua e `blocco_da_cmd` di club_link.py. */
  function bloccoDaCmd(parole) {
    var b = new Uint8Array(64);
    for (var i = 0; i < 8; i++) {
      var w = parole[i] || 0;
      b[i * 2] = w & 0xFF; b[i * 2 + 1] = (w >>> 8) & 0xFF;
    }
    return b;
  }

  /* --- i ruoli ------------------------------------------------------------- */
  /* true = master, false = slave, null = non ancora decidibile (serve il peer
   * del partner). La regola e' simmetrica: i due lati arrivano SEMPRE a ruoli
   * complementari, qualunque coppia di numeri abbiano. */
  function ruoloMaster(mio, suo) {
    if (mio === 1) return true;
    if (mio === 2) return false;
    if (suo === null || suo === undefined) return null;
    if (suo === 1) return false;
    if (suo === 2) return true;
    return mio < suo;
  }

  function nowS() { return (typeof performance !== "undefined" && performance.now ? performance.now() : Date.now()) / 1000; }

  function epocaCasuale() {
    var v = 0;
    if (typeof crypto !== "undefined" && crypto.getRandomValues) {
      var a = new Uint32Array(1); crypto.getRandomValues(a); v = a[0];
    } else {
      v = Math.floor(Math.random() * 0x100000000);
    }
    return (v >>> 0) || 1;
  }

  /* --- la sessione ---------------------------------------------------------
   * opts: {myPeer, dev, sendNet, log, epoca?}
   *   dev:     oggetto con command(cmd, label) e sendBlock(bytes64)
   *   sendNet: callable che spedisce un corpo T_CLUB al partner (via relay)
   *   log:     callable per una riga di log
   */
  function ClubSession(opts) {
    this.myPeer = opts.myPeer;
    this.partnerPeer = null;
    this.master = null;            // deciso da ruoloMaster, magari in ritardo
    this.dev = opts.dev;
    this.sendNet = opts.sendNet;
    this.log = opts.log || function () {};

    this.finita = false;
    this.abortita = false;
    this.motivoFine = "";

    // l'identita' di QUESTA sessione: viaggia in ogni corpo di rete
    this.epoca = opts.epoca !== undefined ? (opts.epoca >>> 0) || 1 : epocaCasuale();
    this.epocaPartner = null;
    this._epocheMorte = [];

    // lato stati
    this._sseq = 0;
    this._vistiSseq = {};          // set dei serial gia' visti
    this._ioHs = false;
    this._luiHs = false;
    this._hsAvviato = false;
    this._hsQuando = 0;
    this._ioChiuso = false;
    this._luiChiuso = false;
    this._luiConnesso = false;
    this._connectInviato = false;
    this._ioConnesso = false;      // il NOSTRO device e' "cavo collegato"
    this._ultimoStato = null;      // l'ultimo stato annunciato (riannunci)
    this._luiStatoVisto = null;    // l'ultimo stato VISTO dal partner:
                                   // un riannuncio identico non e' progresso
    // Il timer della riapertura: armato da _nuovoGiro, spento quando la
    // scala del giro nuovo e' completa (_forseRiaperturaOk). Se resta
    // armato oltre RIAPERTURA_S, il link non riapre piu': fine sessione.
    this._riapertoQuando = 0;
    // Una SEZIONE del firmware e' viva sul nostro device: dal suo
    // HandshakeReceived fino alla riconnessione (vedi club_link.py).
    this._sezioneViva = false;
    /* La sezione e' COLLEGATA (cavo collegato): e' il cancello dei BLOCCHI.
     * Consegnarli prima - anche solo all'handshake - li fa cadere nel vuoto:
     * campo del 2026-08-27 (GBA vs emulatore), 7 blocchi persi e il GBA
     * appeso ad "attendi" per sempre. */
    this._sezionePronta = false;
    /* Il congedo (vedi CONGEDO_MAX_S): l'amico e' uscito, accompagniamo fuori
     * il nostro gioco invece di staccargli il cavo. */
    this.congedo = false; this.congedoDa = 0; this.congedoPorta = false;
    // LA SESSIONE COMINCIA CON AwaitMode, NON COL PRIMO STATO CHE PASSA:
    // i residui del passthrough smontato non devono chiudere una sessione
    // mai nata (campo 2026-08-16, secondo giro di chiave).
    this._avviata = false;
    // Il ruolo: AwaitMode visto ma partner ignoto = comando in attesa.
    this._awaitVisto = false;
    this._ruoloInviato = false;
    this._ruoloAtteso = false;

    // lato dati
    this._txSeq = 0;
    this._storia = {};             // seq -> blocco (per le rispedizioni)
    this._storiaOrdine = [];       // i seq in ordine, per lo sfratto del piu' vecchio
    this._ultimoTx = null;         // [seq, blocco] per il rilancio periodico
    this._ultimoTxQuando = 0;
    this._rxAttesa = 0;            // prossima sequenza da consegnare
    this._rxBuffer = {};           // fuori ordine, in attesa del buco
    this._inAttesaDev = [];        // gia' in ordine, aspettano la sezione
    var adesso = nowS();
    this._nata = adesso;
    this._progresso = adesso;
    this._partnerQuando = adesso;
    this._annuncioQuando = 0;      // 0 = annuncia al primo tick

    // contatori (finiscono nel log di stato del bridge)
    this.blocchiTx = 0;
    this.blocchiRx = 0;
    this.duplicati = 0;
    this.fuoriOrdine = 0;
    this.richiesti = 0;
    this.rilanci = 0;
    this.riannunci = 0;
    this.partnerRiavvii = 0;       // epoche nuove adottate in corsa
    this.epocaScarti = 0;          // pacchetti di epoche morte, buttati
    this.saltiNumerazione = 0;     // adozioni della numerazione del partner
    this.giri = 0;                 // riaperture del link (scambi, lotte...)
    this.blocchiTenuti = 0;        // consegnati alla sezione nuova
    this.blocchiPersiDev = 0;      // buttati: attesa piena (non deve mai)
  }

  /* -- helpers ------------------------------------------------------------ */

  ClubSession.prototype._tocca = function () { this._progresso = nowS(); };

  ClubSession.prototype._annunciaStato = function (status) {
    var corpo = clubStatus(this.epoca, this._sseq, status);
    this._sseq = (this._sseq + 1) & 0xFFFF;
    for (var i = 0; i < K.COPIE_STATO; i++) this.sendNet(corpo);
  };

  /* Il peer del partner, letto dall'header del primo T_CLUB: sblocca il
   * comando di ruolo se era in attesa. */
  ClubSession.prototype.setPartner = function (peer) {
    if (this.partnerPeer !== null || peer === undefined || peer === null) return;
    this.partnerPeer = peer;
    this._forseRuolo();
  };

  ClubSession.prototype._forseRuolo = function () {
    if (this._ruoloInviato || !this._awaitVisto) return;
    var m = ruoloMaster(this.myPeer, this.partnerPeer);
    if (m === null) {
      if (!this._ruoloAtteso) {
        this._ruoloAtteso = true;
        this.log("[club ] ruolo in attesa: lo decide il confronto dei peer-id appena l'amico si annuncia");
      }
      return;
    }
    this.master = m;
    this._ruoloInviato = true;
    var perche = this.myPeer === 1 || this.myPeer === 2 || this.partnerPeer === 1 || this.partnerPeer === 2
      ? "dal peer-id, come da protocollo"
      : "peer " + this.myPeer + " contro " + this.partnerPeer + ": vince il piu' basso";
    this.dev.command(m ? CMD.SET_MODE_MASTER : CMD.SET_MODE_SLAVE,
      "ruolo: " + (m ? "MASTER" : "SLAVE") + " (" + perche + ")");
  };

  ClubSession.prototype._nuovoGiro = function (motivo) {
    /* Il link e' stato chiuso e riaperto: la scala ricomincia da capo.
     * Non e' un guasto, e' come funziona il Cable Club (sedersi alla macchina
     * degli scambi, cominciare una lotta, mixare i record): vedi club_link.py
     * per il resoconto completo del campo 2026-08-20. */
    this.giri++;
    this._ioHs = false;
    this._hsAvviato = false;
    this._sezioneViva = false;
    this._sezionePronta = false;
    // _luiHs NON si azzera (la corsa fra i due reset: vedi club_link.py);
    // _luiConnesso INVECE si azzera, ed e' obbligatorio (un ConnectLink del
    // giro vecchio brucia la parola di slave della sezione nuova).
    this._luiConnesso = false;
    this._connectInviato = false;
    this._ioConnesso = false;
    this._ultimoStato = null;
    this._annuncioQuando = 0;
    // Da qui parte il conto alla rovescia: o la scala nuova si completa
    // entro RIAPERTURA_S, o il gioco ha chiuso per davvero (frullatore,
    // annullo, fine meccanica) e la sessione va chiusa da noi - il
    // firmware LinkClosed non lo dira' mai (vedi K.RIAPERTURA_S).
    this._riapertoQuando = nowS();
    this.log("[club ] " + motivo + ": la scala del link si rifa' da capo (giro " + this.giri + ")");
  };

  ClubSession.prototype._alDevice = function (blocco64) {
    /* L'unica porta verso il device: fra due sezioni il firmware PURGA la
     * coda dei comandi, e cio' che arriva li' e' la raffica dei dati
     * giocatore che il gioco non ritenta mai. Quindi finche' la sezione non
     * c'e' i blocchi si tengono da parte, in ordine. */
    if (this._sezionePronta) { this.dev.sendBlock(blocco64); return; }
    if (this._inAttesaDev.length < K.ATTESA_DEV_MAX) this._inAttesaDev.push(blocco64);
    else this.blocchiPersiDev++;
  };

  ClubSession.prototype._consegnaArretrato = function () {
    if (!this._inAttesaDev.length) return;
    var arretrato = this._inAttesaDev;
    this._inAttesaDev = [];
    this.blocchiTenuti += arretrato.length;
    this.log("[club ] " + arretrato.length + " blocchi tenuti da parte durante la riapertura: li consegno adesso alla sezione nuova");
    for (var i = 0; i < arretrato.length; i++) this.dev.sendBlock(arretrato[i]);
  };

  ClubSession.prototype._epocaOk = function (epoca) {
    /* Il filtro d'ingresso di TUTTO cio' che arriva dalla rete: adotta in
     * corsa un'epoca nuova (= il client di la' e' ripartito), scarta le
     * epoche gia' abbandonate. Identico a club_link.py. */
    if (epoca === this.epocaPartner) { this._partnerQuando = nowS(); return true; }
    if (this._epocheMorte.indexOf(epoca) >= 0) { this.epocaScarti++; return false; }
    this._partnerQuando = nowS();
    if (this.epocaPartner === null) {
      this.epocaPartner = epoca;
      this.log("[club ] partner agganciato (epoca 0x" + epoca.toString(16).toUpperCase() + ")");
      return true;
    }
    this._epocheMorte.push(this.epocaPartner);
    if (this._epocheMorte.length > K.EPOCHE_MORTE_MAX) this._epocheMorte.splice(0, this._epocheMorte.length - K.EPOCHE_MORTE_MAX);
    var vecchia = this.epocaPartner;
    this.epocaPartner = epoca;
    this.partnerRiavvii++;
    this._vistiSseq = {};
    this._luiHs = false;
    this._luiChiuso = false;
    this._luiConnesso = false;
    this._connectInviato = false;
    this._luiStatoVisto = null;    // gli stati del client fresco contano
    this._rxBuffer = {};
    this._rxAttesa = 0;            // il partner fresco riparte da 0
    this._annuncioQuando = 0;      // riannuncio al primo tick
    this._tocca();
    this.log("[club ] IL CLIENT DELL'AMICO E' RIPARTITO (epoca 0x" + vecchia.toString(16).toUpperCase() +
      " -> 0x" + epoca.toString(16).toUpperCase() + "): riaggancio in corsa, riazzero il suo lato e riannuncio il mio");
    return true;
  };

  ClubSession.prototype.notaPartner = function (epoca) {
    if (!this.finita) this._epocaOk(epoca);
  };

  /* -- eventi dal DEVICE (il mio Pico) ------------------------------------- */

  ClubSession.prototype.onDeviceStatus = function (status) {
    if (this.finita) return;
    this._tocca();
    var nome = nomeStato(status);

    // Niente che arrivi PRIMA di AwaitMode appartiene a questa sessione.
    if (!this._avviata && status !== ST.AWAIT_MODE) {
      this.log("[club ] device: " + nome + " (residuo del passthrough, ignorato)");
      return;
    }

    this.log("[club ] device: " + nome);

    if (status === ST.RECONNECTING) this._nuovoGiro("il gioco ha riaperto il link");

    // Come Celio: Ready e Debug restano locali, il resto si annuncia.
    if (status !== ST.READY && status !== ST.DEBUG) {
      if (status !== ST.RECONNECTING) this._ultimoStato = status;
      this._annunciaStato(status);
    }

    if (status === ST.AWAIT_MODE) {
      this._avviata = true;
      this._awaitVisto = true;
      this._forseRuolo();
    } else if (status === ST.HANDSHAKE_RX) {
      this._sezioneViva = true;
      this._ioHs = true;
      this._forseStartHandshake();
      this._forseConnectLink();   // se il partner era gia' pronto
    } else if (status === ST.CONNECTED) {
      // Meta' della prova che una riapertura si e' completata.
      // E SOLO ADESSO i blocchi si consegnano: spinti nel device DURANTE
      // l'handshake si perdono (campo 2026-08-27, GBA vs emulatore: 7 blocchi
      // nel vuoto, `cavo collegato` mai arrivato, GBA appeso ad "attendi").
      this._sezionePronta = true;
      this._consegnaArretrato();
      this._ioConnesso = true;
      this._forseRiaperturaOk();
    } else if (status === ST.CLOSED) {
      this._ioChiuso = true;
      if (this.congedo) {
        // Il congedo e' riuscito: il gioco e' uscito DALLA PORTA, non per un
        // errore del cavo. E' la riga che distingue le due cose nel log.
        this.finita = true;
        this.log("[club ] il gioco e' uscito dalla saletta dalla porta: " +
          "congedo riuscito");
        return;
      }
      this._forseFinita();
    }
  };

  ClubSession.prototype.onDeviceBlock = function (blocco64) {
    if (this.finita) return;
    this._tocca();
    if (this.congedo) {
      // Il gioco chiede di chiudere: gli si risponde di si'. E' la condizione
      // che il firmware aspetta per uscire dal suo giro
      // (partnerReadyCloseLink && readyCloseLink, usbSection.cpp:70-73).
      var cmd = blocco64[0] | (blocco64[1] << 8);
      if (cmd === LINKCMD_READY_CLOSE_LINK) {
        this._alDevice(bloccoDaCmd([LINKCMD_READY_CLOSE_LINK]));
      } else {
        this._alDevice(bloccoDaCmd([LINKCMD_HELD_KEYS, LINK_KEY_IDLE]));
      }
      return;   // durante il congedo non si parla piu' con la rete
    }
    var seq = this._txSeq;
    this._txSeq++;
    this._storia[seq] = blocco64;
    this._storiaOrdine.push(seq);
    if (this._storiaOrdine.length > K.STORIA_BLOCCHI) delete this._storia[this._storiaOrdine.shift()];
    this._ultimoTx = [seq, blocco64];
    this._ultimoTxQuando = nowS();
    this.sendNet(clubData(this.epoca, seq, blocco64));
    this.blocchiTx++;
  };

  /* -- eventi dalla RETE (il partner, via relay) ---------------------------- */

  ClubSession.prototype.onNetStatus = function (epoca, sseq, status) {
    if (this.finita || !this._epocaOk(epoca)) return;
    if (this._vistiSseq[sseq]) return;    // copia del triplo invio / riannuncio
    this._vistiSseq[sseq] = true;
    // PROGRESSO e' uno stato NUOVO, non un riannuncio: i riannunci hanno
    // sseq freschi (il dedup non li ferma) e tenevano vivo il watchdog
    // all'infinito nelle sessioni gia' morte ("partner: cavo collegato"
    // ogni 2 s per sempre, campo 2026-08-27). Lo stato ripetuto si PROCESSA
    // comunque - i flag sono idempotenti, e dopo una riapertura il
    // riannuncio identico e' proprio cio' che fa risalire la scala - ma il
    // watchdog conta solo le novita'.
    if (status !== this._luiStatoVisto) {
      this._luiStatoVisto = status;
      this._tocca();
    }
    this.log("[club ] partner: " + nomeStato(status));

    if (status === ST.HANDSHAKE_RX) {
      this._luiHs = true;
      this._forseStartHandshake();
    } else if (status === ST.CONNECTED) {
      this._luiConnesso = true;
      this._forseConnectLink();
    } else if (status === ST.RECONNECTING) {
      // Informativo: il giro nuovo lo decide il NOSTRO device, non la rete.
    } else if (status === ST.CLOSED) {
      if (!this._avviata) { this.log("[club ] (il partner riporta un residuo: ignorato)"); return; }
      this._luiChiuso = true;
      // Se il NOSTRO gioco e' ancora dentro, non si stacca: lo si accompagna
      // fuori. Se ha gia' chiuso lui, _forseFinita chiude come sempre.
      if (this._ioChiuso) this._forseFinita();
      else this._congeda("il gioco dell'amico ha chiuso il link");
    }
  };

  ClubSession.prototype.onNetBlock = function (epoca, seq, blocco64) {
    if (this.finita || !this._epocaOk(epoca)) return;
    this.blocchiRx++;
    if (seq < this._rxAttesa) {
      // Duplicato (rilancio, o copia riordinata): NON e' progresso.
      this.duplicati++;
      return;
    }
    this._tocca();
    if (seq > this._rxAttesa + K.STORIA_BLOCCHI) {
      // Aggancio a una sessione gia' vecchia: si adotta la numerazione.
      this.saltiNumerazione++;
      this.log("[club ] numerazione del partner adottata al volo: salto da seq " + this._rxAttesa +
        " a " + seq + " (aggancio a sessione gia' in corso)");
      this._rxBuffer = {};
      this._rxAttesa = seq;
    }
    if (seq > this._rxAttesa) {
      this.fuoriOrdine++;
      this._rxBuffer[seq] = blocco64;
      this._chiediMancanti(seq);
      this._svuotaBuffer();
      return;
    }
    this._alDevice(blocco64);
    this._rxAttesa++;
    this._svuotaBuffer();
  };

  ClubSession.prototype.onNetReq = function (epoca, seqs) {
    if (this.finita || !this._epocaOk(epoca)) return;
    this._tocca();
    for (var i = 0; i < seqs.length; i++) {
      var blocco = this._storia[seqs[i]];
      if (blocco !== undefined) this.sendNet(clubData(this.epoca, seqs[i], blocco));
    }
  };

  ClubSession.prototype.onNetLeave = function (epoca) {
    if (this.finita || !this._epocaOk(epoca)) return;
    this._congeda("l'amico e' uscito dalla saletta");
  };

  /* -- il battito ----------------------------------------------------------- */

  ClubSession.prototype.tick = function () {
    if (this.finita) return;
    var adesso = nowS();

    // Il ConnectLink lasciato in attesa dal distanziamento (CONNECT_RITARDO_S).
    this._forseConnectLink();

    // Il congedo non puo' durare per sempre: se il device non dice "link
    // chiuso" entro CONGEDO_MAX_S si stacca comunque - ma avendoci provato,
    // che e' la differenza fra uscire dalla porta e andare in errore.
    if (this.congedo && adesso - this.congedoDa > K.CONGEDO_MAX_S) {
      this.abortita = true;
      this.finita = true;
      this.log("[club ] il gioco non ha chiuso il link entro " +
        Math.round(K.CONGEDO_MAX_S) + " s: stacco");
      return;
    }

    // Il rilancio dell'ultimo blocco: il dedup di la' lo scarta gratis.
    if (this._ultimoTx !== null && adesso - this._ultimoTxQuando >= K.RILANCIO_ULTIMO_S) {
      this.sendNet(clubData(this.epoca, this._ultimoTx[0], this._ultimoTx[1]));
      this._ultimoTxQuando = adesso;
      this.rilanci++;
    }

    // I riannunci: presenza e ultimo stato (coprono il partner riavviato).
    if (adesso >= this._annuncioQuando) {
      this._annuncioQuando = adesso + K.ANNUNCIO_S;
      if (this.epocaPartner === null) {
        this.sendNet(clubEnter(this.epoca));
        this.riannunci++;
      }
      if (this._ioChiuso && !this._luiChiuso) {
        this._annunciaStato(ST.CLOSED);
        this.riannunci++;
      } else if (!this._luiConnesso && this._ultimoStato !== null) {
        this._annunciaStato(this._ultimoStato);
        this.riannunci++;
      }
    }

    // Il club fantasma: nessun GBA si e' mai presentato al bancone.
    if (!this._ioHs && !this._luiHs && adesso - this._nata > K.FANTASMA_S) {
      this.log("[club ] NESSUN GBA si e' presentato al club in " + Math.round(K.FANTASMA_S) +
        " s: avvio fantasma, si torna a camminare");
      this.motivoFine = "fantasma";
      this.abortita = true;
      this.finita = true;
      return;
    }

    // Il gioco ha chiuso il link e la riapertura non si e' mai completata:
    // non era una meccanica che riapre, era la FINE della comunicazione
    // (frullatore, annullo al bancone, fine scambio/lotta). Il firmware
    // LinkClosed non lo dira' mai (aspetta EXIT_ROOM dalla porta): si
    // chiude da qui e si torna al passthrough. Vedi K.RIAPERTURA_S.
    if (this._riapertoQuando && adesso - this._riapertoQuando > K.RIAPERTURA_S) {
      this.log("[club ] IL GIOCO HA CHIUSO IL LINK e in " + Math.round(K.RIAPERTURA_S) +
        " s la riapertura non si e' completata: comunicazione finita (fine o " +
        "annullo di scambio/lotta/frullatore), si torna a camminare");
      this.motivoFine = "link chiuso senza riapertura";
      this.abortita = true;
      this.finita = true;
      return;
    }

    // Partner muto: mai visto, o client di la' morto senza LEAVE.
    if (adesso - this._partnerQuando > K.PARTNER_MUTO_S) {
      this.log("[club ] L'AMICO NON SI FA SENTIRE da " + Math.round(K.PARTNER_MUTO_S) +
        " s: sessione abbandonata, si torna a camminare");
      this.motivoFine = "partner muto";
      this.abortita = true;
      this.finita = true;
      return;
    }

    if (adesso - this._progresso > K.WATCHDOG_S) {
      this.log("[club ] NESSUN PROGRESSO da " + Math.round(K.WATCHDOG_S) +
        " s: sessione abbandonata, si torna a camminare");
      this.motivoFine = "watchdog";
      this.abortita = true;
      this.finita = true;
    }
  };

  /* -- i pezzi interni ------------------------------------------------------ */

  ClubSession.prototype._forseStartHandshake = function () {
    if (this._ioHs && this._luiHs && this._sezioneViva && !this._hsAvviato) {
      this._hsAvviato = true;
      this._hsQuando = nowS();
      this.dev.command(CMD.START_HANDSHAKE, "StartHandshake (entrambi pronti)");
      this._forseConnectLink();   // il ConnectLink in attesa, se c'e'
    }
  };

  ClubSession.prototype._forseConnectLink = function () {
    /* ConnectLink SOLO dopo StartHandshake e con una sezione viva: i motivi
     * (parola di slave bruciata, puntatore nullo sul Pico) sono a verbale in
     * club_link.py. Se le condizioni non ci sono il comando resta segnato in
     * _luiConnesso e parte da se' appena diventano vere. */
    if (this._luiConnesso && this._sezioneViva && this._hsAvviato && !this._connectInviato
        && nowS() - this._hsQuando >= K.CONNECT_RITARDO_S) {
      this._connectInviato = true;
      this.dev.command(CMD.CONNECT_LINK, "ConnectLink (partner pronto)");
      this._forseRiaperturaOk();
    }
  };

  ClubSession.prototype._forseRiaperturaOk = function () {
    /* La scala del giro nuovo e' completa - il nostro device e' connesso E
     * il ConnectLink e' partito (= anche il partner e' connesso). Era una
     * riapertura vera: il conto alla rovescia si spegne. Serve la COPPIA:
     * nel guasto del 2026-08-27 un lato arrivava a "cavo collegato" da solo
     * (il gioco riprovava al bancone) mentre l'altro non c'era piu'. */
    if (this._riapertoQuando && this._ioConnesso && this._connectInviato) {
      this._riapertoQuando = 0;
    }
  };

  ClubSession.prototype._chiediMancanti = function (finoA) {
    var mancanti = [];
    for (var s = this._rxAttesa; s < finoA && mancanti.length < 16; s++) {
      if (this._rxBuffer[s] === undefined) mancanti.push(s);
    }
    if (mancanti.length) {
      this.richiesti += mancanti.length;
      this.sendNet(clubReq(this.epoca, mancanti));
    }
  };

  ClubSession.prototype._svuotaBuffer = function () {
    while (this._rxBuffer[this._rxAttesa] !== undefined) {
      var b = this._rxBuffer[this._rxAttesa];
      delete this._rxBuffer[this._rxAttesa];
      this._alDevice(b);
      this._rxAttesa++;
    }
  };

  /* L'amico se n'e' andato. NON si stacca: si accompagna fuori anche il nostro
   * gioco, con la danza del cavo vero (PORTA, poi l'eco di READY_CLOSE_LINK).
   * Chiudera' il device dicendo "link chiuso", e li' finisce la sessione. */
  ClubSession.prototype._congeda = function (motivo) {
    if (this.finita || this.congedo) return;
    if (!this._sezionePronta) {   // cavo mai collegato: niente da accompagnare
      this.motivoFine = motivo; this.abortita = true; this.finita = true;
      return;
    }
    this.congedo = true;
    this.congedoDa = nowS();
    this.congedoPorta = false;
    this.motivoFine = motivo;
    this.log("[club ] " + motivo + ": accompagno fuori il gioco dalla porta " +
      "della saletta (niente strappo al cavo)");
    this._alDevice(bloccoDaCmd([LINKCMD_HELD_KEYS, LINK_KEY_EXIT_ROOM]));
    this.congedoPorta = true;
  };

  ClubSession.prototype._forseFinita = function () {
    if (this._ioChiuso && this._luiChiuso) {
      this.finita = true;
      this.motivoFine = "chiusa da entrambi";
      this.log("[club ] sessione chiusa da ENTRAMBI i lati: si torna al passthrough");
    }
  };

  ClubSession.prototype.riassunto = function () {
    var riga = "blocchi tx " + this.blocchiTx + " rx " + this.blocchiRx +
      " | dup " + this.duplicati + " fuori-ordine " + this.fuoriOrdine +
      " richiesti " + this.richiesti + " rilanci " + this.rilanci +
      " | riannunci " + this.riannunci;
    // Le patologie si stampano solo se > 0, ma se ci sono devono GRIDARE.
    if (this.giri) riga += " | riaperture del link " + this.giri;
    if (this.blocchiTenuti) riga += " | blocchi salvati dalla riapertura " + this.blocchiTenuti;
    if (this.blocchiPersiDev) riga += " | BLOCCHI PERSI (attesa piena) " + this.blocchiPersiDev;
    if (this.partnerRiavvii) riga += " | RIAVVII DEL PARTNER " + this.partnerRiavvii;
    if (this.epocaScarti) riga += " | scarti epoca morta " + this.epocaScarti;
    if (this.saltiNumerazione) riga += " | SALTI DI NUMERAZIONE " + this.saltiNumerazione;
    return riga;
  };

  root.OwlClub = {
    K: K, ST: ST, ST_NOMI: ST_NOMI, CMD: CMD,
    VERSIONE_CLUB: VERSIONE_CLUB, IMPRONTA_WEB: IMPRONTA_WEB,
    IMPRONTA_LUA: IMPRONTA_LUA,
    K: K,                       // le costanti, per i test (bridge_test.html)
    CLUB_LOG_BLOCCHI: 80,
    proto: {
      CLUB_STATUS: CLUB_STATUS, CLUB_DATA: CLUB_DATA, CLUB_REQ: CLUB_REQ,
      CLUB_ENTER: CLUB_ENTER, CLUB_LEAVE: CLUB_LEAVE,
      clubStatus: clubStatus, clubData: clubData, clubReq: clubReq,
      clubEnter: clubEnter, clubLeave: clubLeave, clubUnpack: clubUnpack,
      blockRiga: blockRiga
    },
    ruoloMaster: ruoloMaster,
    ClubSession: ClubSession
  };
})(typeof window !== "undefined" ? window : globalThis);
