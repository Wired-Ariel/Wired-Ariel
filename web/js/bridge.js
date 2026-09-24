/* bridge.js - il ponte di UN giocatore fra il GBA (via WebUSB) e il relay
 * (via WebSocket), nel browser (2026-08-23).
 *
 * E' il porting di net/client.py (classe Bridge), riga per riga dove conta:
 * stessi contatori, stesse righe di log, stesse regole. Chi ha letto i log
 * di client.py legge questi senza imparare niente di nuovo. Dal 2026-08-26
 * c'e' anche il CABLE CLUB (la sessione vive in club.js, qui solo
 * l'orchestrazione, come client.py fa con club_link.py): l'EVENT_CLUB del
 * GBA avvia la sessione, il Pico passa in modo LINK e i blocchi viaggiano
 * come T_CLUB attraverso il relay. Quello che NON c'e': il simulatore di
 * rete (netem).
 *
 * Le tre cose che fa e che non sono ovvie (da client.py):
 *   1. DEDUPLICA E RIORDINA i datagrammi del relay (seq a 8 bit, modulo 256).
 *      Le PERDITE invece vanno bene: un passo perso si riassorbe al primo
 *      SYNC - e prima ancora lo RICUCE questo ponte (heal), in linea retta.
 *   2. MISURA L'RTT con PING/PONG propri, fuori dal protocollo di gioco.
 *   3. MANDA OGNI PASSO/GIRA DUE VOLTE al GBA (il tratto SIO perde ~8%): la
 *      copia la scarta il payload sul seq; un secchiello limita le copie.
 *
 * Dipendenze: OwlRelay (relay.js) per header e WebSocket, GbaSio (sio.js) per
 * la descrizione degli eventi, e un "device" con sendEvent(12 byte) ->
 * Promise (CelioDevice di device.js, o un finto nei test).
 */
(function (root) {
  "use strict";

  var R = root.OwlRelay, G = root.GbaSio;
  var EVENT_SIZE = 12;
  var EV = { STEP: 1, SYNC: 2, TURN: 3, LEAVE: 4, STATUS: 5, CARD: 6, CLUB: 7 };
  var HEAL_MAX = 3;
  var DIR_DX = { 1: 0, 2: 0, 3: -1, 4: 1 };   // giu, su, sx, dx (sDirDelta* del payload)
  var DIR_DY = { 1: 1, 2: -1, 3: 0, 4: 0 };
  var COPY_RATE = 8;                          // copie al secondo concesse dal secchiello
  var PING_INTERVAL = 1000, STATUS_INTERVAL = 5000;
  /* Da quanto il GBA deve TACERE prima che il battito porti con se' la nostra
   * ultima posizione: vedi rimandaPresenza. Gemello di PRESENZA_SILENZIO_S in
   * client.py. */
  var PRESENZA_SILENZIO_MS = 2000;
  // Lo spettatore RIPETE il suo WATCH (2026-08-26). Non e' ridondanza inutile:
  // se il relay viene riavviato a partita viva perde tutto lo stato, e il
  // primo pacchetto che riceve da noi e' quasi sempre il PING (1/s) - che ci
  // farebbe rientrare da GIOCATORE, occupando un posto della stanza. Il WATCH
  // ogni 5 s chiude quella finestra restando sotto il timeout del relay (10 s).
  var WATCH_INTERVAL = 5000;

  function s16(lo, hi) { var v = lo | (hi << 8); return v & 0x8000 ? v - 0x10000 : v; }
  function put16(b, off, v) { b[off] = v & 0xFF; b[off + 1] = (v >> 8) & 0xFF; }
  function mapKeyOf(e) { return (e[4] << 8) | e[5]; }
  function now() { return (typeof performance !== "undefined" && performance.now) ? performance.now() : Date.now(); }

  /* I 12 byte che dicono al payload "l'amico se n'e' andato" (make_leave_event). */
  function makeLeaveEvent(mapKey) {
    var b = new Uint8Array(EVENT_SIZE);
    b[0] = EV.LEAVE; b[4] = (mapKey >> 8) & 0xFF; b[5] = mapKey & 0xFF;
    return b;
  }

  /* opts: {peerId, room, device, link (RelayLink, opzionale), log, copies,
   *        onStatus(stato), wireLoss (solo test)} */
  function OwlBridge(opts) {
    opts = opts || {};
    this.peerId = opts.peerId & 0xFFFF;
    this.room = opts.room & 0xFFFF;
    this.device = opts.device || null;
    this.link = opts.link || null;
    this.log = opts.log || function () {};
    this.onStatus = opts.onStatus || function () {};
    // Chiamata a OGNI aggiornamento di posizione (mia o degli amici): serve
    // alla Mappa live. Guidata dagli eventi apposta - un setInterval nella
    // scheda in background il browser lo rallenta a un tick al minuto, e la
    // mappa si congelava; onmessage/transfer invece arrivano sempre.
    this.onPos = opts.onPos || null;
    // SPETTATORE: guarda la stanza senza giocare (niente device, niente
    // eventi). Il bridge lo deve SAPERE perche' cambia il verbo con cui entra
    // in stanza - WATCH invece di HELLO - e quindi se occupa un posto.
    this.spettatore = !!opts.spettatore;
    this.watchInviati = 0;
    this.timerWatch = null;
    this.wireCopies = Math.max(1, opts.copies || 2);
    this.wireLoss = Math.max(0, opts.wireLoss || 0) / 100;
    this.rand = opts.rand || Math.random;

    this.outSeq = 0;
    this.lastSeq = {};        // peerId -> ultimo seq accettato
    // FINO A 4 GIOCATORI (2026-08-25): il payload distingue i remoti dal
    // nibble alto del type (slot 0..2), assegnato QUI, dal ricevente.
    // Identico a client.py: primo evento -> primo slot libero, BYE -> lo
    // slot torna libero, quarto amico -> niente avatar (solo mappa live).
    this.peerSlots = {};      // peerId -> slot avatar 0..2
    this.slotFree = [0, 1, 2];
    this.slotPieniAvvisati = {};
    this.slotScartati = 0;
    this.peerRoom = {};       // peerId -> mappa del suo ultimo evento
    this.peerPos = {};        // peerId -> {map, x, y} (catena dichiarata, per la ricucitura)
    this.peers = {};          // peerId -> {map, x, y, dir, stato, genere, avatar, visto}
    this.mapKey = null;       // la nostra ultima mappa
    this.posIo = null;        // {map, x, y, dir, stato, ...}
    this.statoIo = null;
    this.statoCambi = 0;
    this.pendingPing = {};
    this.rtts = [];
    this.rttUltimo = null; this.rttMin = null; this.rttMax = null; this.rttMedio = null;

    this.sent = 0; this.received = 0;
    this.dupDropped = 0; this.lateDropped = 0;
    this.leaves = 0;
    // La presenza di chi sta fermo: vedi ricordaPresenza/rimandaPresenza.
    // `presenzeSoppresse` e' la contro-prova della correzione del 30/08: deve
    // restare a zero per tutta una partita normale e salire SOLO quando
    // l'adattatore e' stato staccato. Se salisse camminando, il gate sta
    // spegnendo una presenza legittima e l'amico non ci vedrebbe piu' fermi.
    this.lastEvent = null; this.presenzeRimandate = 0; this.ultimoEventoAt = 0;
    this.presenzeSoppresse = 0; this.congedato = false;
    this.healed = 0; this.healFar = 0;
    this.wireSent = 0; this.wireDropped = 0; this.copiesSkipped = 0;
    /* GBA MUTO dopo un club: finche' il payload non torna a parlare, sul filo
     * non si scrive. Il gioco puo' essere ancora fermo alla schermata del
     * club COL LINK APERTO, e ogni frame di camminata scritto li' viene letto
     * dal gioco come dati del link: spazzatura nei blocchi giocatore, e il
     * GBA che conclude "ALLENATORI di un'altra regione" (campo 2026-08-27,
     * 700+ frame iniettati in un gioco appeso ad "attendi"). Il primo evento
     * VALIDO dal GBA e' la prova che il payload ha ripreso la porta. */
    this.gbaMuto = false; this.gbaMutoScartati = 0;
    this.copyTokens = COPY_RATE; this.copyStamp = now();
    this.clubAvvisi = 0;
    // Il Cable Club integrato (2026-08-26): quando il payload avvisa che il
    // GBA e' al club (EV_CLUB), il bridge passa il Pico in modo LINK e fa da
    // ponte fra il device e il relay. Specchio dei campi di client.py.
    this.club = null;              // OwlClub.ClubSession, o null
    this.clubPartner = null;       // il peer dell'amico nella sessione in corso
    this.clubDrop = 0;             // eventi di gioco buttati durante il club
    this.clubSessions = 0;         // -1 = "qui il club non si puo'" gia' detto
    this.clubMalformati = 0;       // corpi T_CLUB illeggibili (versioni diverse?)
    this.clubStantii = 0;          // EVENT_CLUB in quarantena, ignorati
    this.clubAltrui = 0;           // T_CLUB di un terzo peer, ignorati
    this.clubRiagganciofino = 0;   // secondi: prima di questo, niente riaperture
    this.clubVersioneDetta = false;
    this.clubTimer = null;
    this.deviceErrori = 0;
    this.timerPing = null; this.timerStatus = null;
    this.avviato = false;
  }

  /* --- verso il relay ----------------------------------------------------- */

  OwlBridge.prototype.nextSeq = function () { this.outSeq = (this.outSeq + 1) & 0xFF; return this.outSeq; };

  OwlBridge.prototype.sendRelay = function (bytes) {
    if (this.link) return this.link.send(bytes);
    return false;
  };

  /* Il verbo con cui si entra in stanza DICHIARA il ruolo: HELLO = gioco,
   * WATCH = guardo soltanto. Il nome della funzione resta sendHello perche'
   * il chiamante e' uno solo e conta il momento, non il verbo: l'apertura
   * del WebSocket (e ogni sua riapertura, che e' proprio quando il relay
   * potrebbe non ricordarsi piu' di noi). */
  OwlBridge.prototype.sendHello = function () {
    if (this.spettatore) {
      this.sendRelay(R.pack(R.T.WATCH, this.peerId, this.room, 0));
      this.watchInviati++;
      return;
    }
    this.sendRelay(R.pack(R.T.HELLO, this.peerId, this.room, 0));
    // L'HELLO iscrive alla stanza ma non porta la posizione, e il relay non
    // conserva niente: se abbiamo gia' una fotografia la si rimanda subito.
    this.rimandaPresenza();
  };

  /* LA PRESENZA DI CHI STA FERMO (2026-08-28).
   *
   * Il relay non inoltra gli HELLO: in stanza si ESISTE solo quando si parla.
   * Al Cable Club, nei menu e in lotta il GBA molla la porta seriale e tace,
   * quindi chi entra dopo non sa che ci siamo - ne' come avatar ne' come
   * partner di scambio. Si conserva una FOTOGRAFIA dell'ultima posizione
   * assoluta (tipo forzato a SYNC: rimandare un PASSO farebbe camminare il
   * nostro avatar una seconda volta a casa dell'amico) e la si rimanda sul
   * battito, all'ingresso in stanza e al primo evento di un peer mai visto.
   * Stesso rimedio che il Lua dell'emulatore ha gia' (ws.lastEvent). */
  OwlBridge.prototype.ricordaPresenza = function (e) {
    var k = e[0] & 0x0F;
    if (k < 1 || k > 3) return;
    var f = new Uint8Array(e.subarray(0, EVENT_SIZE));
    f[0] = (e[0] & 0xF0) | 2;
    this.lastEvent = f;
  };

  /* SOLO QUANDO IL GBA TACE: nell'overworld il payload emette gia' un SYNC
   * assoluto al secondo anche da fermi, e il canale verso il gioco e' stretto
   * (226 parole/s). Il buco vero e' l'altro: al club, nei menu e in lotta il
   * payload molla la porta seriale. `sempre` salta il gate ed e' la risposta
   * al primo evento di un peer appena entrato, che va vista subito. */
  /* «IL GBA TACE» E «L'ADATTATORE NON C'E' PIU'» NON SONO LA STESSA COSA
   * (2026-08-30). La presenza e' nata per il primo caso - club, menu, lotta:
   * il payload molla la porta seriale ma la console e' li' e fra un attimo
   * ricomincia. Il gate era pero' solo «tace da 2 s», che e' una condizione
   * che un adattatore staccato soddisfa per sempre: il battito continuava a
   * ripubblicare la fotografia, l'amico ci vedeva ricomparire, e trenta
   * secondi dopo il relay ci buttava fuori di nuovo. Da qui il va-e-vieni
   * dell'avatar visto nel registro del 30/08.
   *
   * Adesso la fotografia parte solo se la SORGENTE e' viva. Per chi guarda e
   * basta (spettatore) la sorgente e' il suo emulatore, non c'e' device: li'
   * la regola vecchia resta giusta. */
  OwlBridge.prototype.sorgenteViva = function () {
    if (this.spettatore) return true;
    if (!this.device) return true;         // pannello/collaudo senza device
    return !this.device.perso;
  };

  OwlBridge.prototype.rimandaPresenza = function (sempre) {
    if (!this.lastEvent) return;
    if (!this.sorgenteViva()) { this.presenzeSoppresse++; return; }
    if (!sempre && (now() - this.ultimoEventoAt) < PRESENZA_SILENZIO_MS) return;
    this.sendRelay(R.pack(R.T.EVENT, this.peerId, this.room, this.nextSeq(), this.lastEvent));
    this.presenzeRimandate++;
  };

  /* Il Pico ricollegato dopo un distacco: il bridge deve smettere di guardare
   * il device morto, o `sorgenteViva` resterebbe falsa per sempre e l'amico
   * non ci vedrebbe piu' fermi nemmeno a partita ripresa. Si riapre anche il
   * congedo: da qui in poi si esiste di nuovo. */
  OwlBridge.prototype.cambiaDevice = function (device) {
    this.device = device || null;
    if (device && !device.perso) this.congedato = false;
  };

  /* IL CONGEDO (2026-08-30). Nessun client diceva mai T_BYE: si spariva
   * tacendo, e l'amico lo scopriva dal timeout del relay 10-30 s dopo. Con
   * l'adattatore staccato quel ritardo era il motore del va-e-vieni. Detto
   * subito, invece, l'avatar sparisce una volta e resta sparito. */
  OwlBridge.prototype.congeda = function (perche) {
    if (this.congedato) return;
    this.congedato = true;
    this.lastEvent = null;          // niente piu' fotografie, per nessuna strada
    try {
      this.sendRelay(R.pack(R.T.BYE, this.peerId, this.room, this.nextSeq()));
      this.log("[rete ] congedo mandato agli amici (" + perche + "): "
        + "il tuo avatar sparisce dalle loro partite.");
    } catch (e) { /* il WebSocket puo' essere gia' morto: pazienza */ }
  };

  /* Un evento di 12 byte DAL GBA: verso il relay (forward_event). */
  OwlBridge.prototype.onDeviceEvent = function (event) {
    if (!event || event.length < EVENT_SIZE) return;
    var e = event instanceof Uint8Array ? event : new Uint8Array(event);

    // Il GBA e' tornato a parlare: il payload ha ripreso la porta, si puo'
    // tornare a scrivergli (vedi gbaMuto nel costruttore).
    if (this.gbaMuto) {
      this.gbaMuto = false;
      this.log("[club ] il GBA e' tornato a parlare (" + this.gbaMutoScartati +
        " frame trattenuti nel frattempo): riprendo a scrivergli");
      this.gbaMutoScartati = 0;
    }

    if (e[0] === EV.CLUB) {
      // L'avviso del club non e' un evento di gioco e non si inoltra come
      // tale: diventa l'avvio della sessione (arriva in 3 copie, clubStart
      // e' idempotente). Per 10 s dopo la chiusura di un club anche
      // l'avviso del PROPRIO GBA e' in quarantena: il payload ri-latcha e
      // accoda EVENT_CLUB che non possono uscire finche' il Pico e' in modo
      // LINK, e all'uscita arrivano tutti insieme (client.py, 2026-08-21).
      this.clubAvvisi++;
      if (now() / 1000 < this.clubRiagganciofino) {
        this.clubStantii++;
        if (this.clubStantii === 1) this.log("[club ] EVENT_CLUB stantio del club appena chiuso: in quarantena");
        return;
      }
      this.clubStart("il TUO GBA e' entrato al Cable Club", true);
      return;
    }
    if (e[0] === EV.STATUS) {
      if (e[1] !== this.statoIo) {
        this.statoIo = e[1];
        this.statoCambi++;
        this.log("[stato ] io -> " + G.nomeStato(e[1]) + "  (dal GBA, #" + e[3] + ")");
      }
    }
    this.posIo = this._aggiornaPos(this.posIo, e);
    this.mapKey = mapKeyOf(e);
    this.ricordaPresenza(e);
    this.ultimoEventoAt = now();
    this.sendRelay(R.pack(R.T.EVENT, this.peerId, this.room, this.nextSeq(), e.subarray(0, EVENT_SIZE)));
    this.sent++;
    if (this.onPos) this.onPos();
  };

  OwlBridge.prototype.sendPing = function () {
    var seq = this.nextSeq();
    this.pendingPing[seq] = now();
    this.sendRelay(R.pack(R.T.PING, this.peerId, this.room, seq));
    // Il battito porta con se' la presenza: e' il solo pacchetto che parte
    // anche quando il GBA tace (club, menu, lotta).
    this.rimandaPresenza();
  };

  /* --- dal relay ---------------------------------------------------------- */

  OwlBridge.prototype.acceptSeq = function (peerId, seq) {
    var last = this.lastSeq[peerId];
    if (last === undefined) { this.lastSeq[peerId] = seq; return true; }
    var delta = (seq - last) & 0xFF;
    if (delta === 0) { this.dupDropped++; return false; }
    if (delta >= 128) { this.lateDropped++; return false; }
    this.lastSeq[peerId] = seq;
    return true;
  };

  OwlBridge.prototype.onRelayMessage = function (bytes) {
    var p = R.unpack(bytes);
    if (!p) return;
    var t = now();

    if (p.kind === R.T.PONG) {
      var sentAt = this.pendingPing[p.seq];
      if (sentAt !== undefined) {
        delete this.pendingPing[p.seq];
        var rtt = t - sentAt;
        this.rtts.push(rtt);
        this.rttUltimo = rtt;
      }
      return;
    }

    if (p.kind === R.T.BYE) {
      var room = this.peerRoom[p.peerId];
      delete this.peerRoom[p.peerId];
      delete this.lastSeq[p.peerId];
      delete this.peerPos[p.peerId];
      delete this.peers[p.peerId];
      // Lo slot avatar torna libero, e il VIA parte TIMBRATO con lo slot che
      // l'amico occupava (identico a client.py). Senza slot, niente despawn.
      var byeSlot = this.peerSlots[p.peerId];
      if (byeSlot !== undefined) {
        delete this.peerSlots[p.peerId];
        this.slotFree.push(byeSlot);
        this.slotFree.sort();
        this.slotPieniAvvisati = {};
      }
      if (room === undefined) { this.log("peer " + p.peerId + " se n'e' andato (mai visto un suo evento)"); return; }
      this.log("peer " + p.peerId + " se n'e' andato (era su mappa " + (room >> 8) + "." + (room & 0xFF) + "): despawn");
      this.leaves++;
      if (byeSlot !== undefined) this.deliver(makeLeaveEvent(room), byeSlot);
      return;
    }

    if (p.kind === R.T.CLUB) {
      // La sessione del club ha il SUO dedup (numeri di serie degli stati,
      // sequenze dei blocchi): il dedup dei datagrammi qui sotto la
      // affamerebbe. Identico a client.py.
      this.handleClubNet(p.peerId, p.body);
      return;
    }

    if (p.kind !== R.T.EVENT || p.body.length < EVENT_SIZE) return;
    // Il primo evento di un peer mai visto vale come "ci sono, e tu?": va
    // guardato PRIMA di acceptSeq, che e' la funzione che registra il peer.
    var peerNuovo = this.lastSeq[p.peerId] === undefined;
    if (!this.acceptSeq(p.peerId, p.seq)) return;

    var event = new Uint8Array(p.body.subarray(0, EVENT_SIZE));
    this.peerRoom[p.peerId] = mapKeyOf(event);
    this.received++;
    this.healAndDeliver(p.peerId, event);
    if (peerNuovo) this.rimandaPresenza(true);
  };

  /* --- la ricucitura dei passi persi (heal_and_deliver, identica a client.py) --- */

  OwlBridge.prototype.slotFor = function (peerId) {
    var slot = this.peerSlots[peerId];
    if (slot === undefined) {
      if (this.slotFree.length) {
        slot = this.slotFree.shift();
        this.peerSlots[peerId] = slot;
        this.log("amico " + peerId + " -> avatar slot " + slot);
      } else {
        if (!this.slotPieniAvvisati[peerId]) {
          this.slotPieniAvvisati[peerId] = true;
          this.log("amico " + peerId + " SENZA avatar: 3 amici gia' a schermo (resta sulla mappa live)");
        }
        return null;
      }
    }
    return slot;
  };

  OwlBridge.prototype.healAndDeliver = function (peerId, event) {
    var slot = this.slotFor(peerId);
    var kind = event[0];
    if (kind !== EV.STEP && kind !== EV.SYNC && kind !== EV.TURN) {
      if (kind === EV.STATUS) {
        var pr = this.peers[peerId];
        if (!pr || pr.stato !== event[1]) {
          this.log("[stato ] amico " + peerId + " -> " + G.nomeStato(event[1]) + "  (dalla rete, verso il GBA)");
        }
      }
      if (kind === EV.LEAVE) delete this.peers[peerId];
      else this.peers[peerId] = this._aggiornaPos(this.peers[peerId], event);
      this.deliver(event, slot);
      if (this.onPos) this.onPos();
      return;
    }
    this.peers[peerId] = this._aggiornaPos(this.peers[peerId], event);
    if (this.onPos) this.onPos();

    var mapKey = mapKeyOf(event);
    var x = s16(event[6], event[7]), y = s16(event[8], event[9]);
    var last = this.peerPos[peerId];
    this.peerPos[peerId] = { map: mapKey, x: x, y: y };

    if (!last || last.map !== mapKey) { this.deliver(event, slot); return; }
    var cx = last.x, cy = last.y, tx, ty;

    if (kind === EV.STEP) {
      if (Math.abs(x - cx) + Math.abs(y - cy) <= 1) { this.deliver(event, slot); return; }
      var ddx = DIR_DX[event[1]];
      if (ddx === undefined) { this.deliver(event, slot); return; }
      var ddy = DIR_DY[event[1]];
      tx = x - ddx; ty = y - ddy;
      if (ddx !== 0 && (ty !== cy || (tx - cx) * ddx < 0)) { this.deliver(event, slot); return; }
      if (ddy !== 0 && (tx !== cx || (ty - cy) * ddy < 0)) { this.deliver(event, slot); return; }
    } else {
      if (x !== cx && y !== cy) { this.deliver(event, slot); return; }
      tx = x; ty = y;
    }

    var gap = Math.abs(tx - cx) + Math.abs(ty - cy);
    if (gap === 0) { this.deliver(event, slot); return; }
    if (gap > HEAL_MAX) { this.healFar++; this.deliver(event, slot); return; }

    var synthSeq = (event[3] - gap + 256) & 0xFF;
    while (cx !== tx || cy !== ty) {
      var stepDir;
      if (Math.abs(tx - cx) >= Math.abs(ty - cy)) { stepDir = tx > cx ? 4 : 3; cx += tx > cx ? 1 : -1; }
      else { stepDir = ty > cy ? 1 : 2; cy += ty > cy ? 1 : -1; }
      var synth = new Uint8Array(event);
      synth[0] = EV.STEP; synth[1] = stepDir; synth[3] = synthSeq;
      synthSeq = (synthSeq + 1) & 0xFF;
      put16(synth, 6, cx); put16(synth, 8, cy);
      this.deliver(synth, slot);
      this.healed++;
    }
    this.deliver(event, slot);
  };

  /* --- verso il GBA: copie e secchiello ----------------------------------- */

  OwlBridge.prototype._copyAllowed = function () {
    var t = now();
    this.copyTokens = Math.min(COPY_RATE, this.copyTokens + (t - this.copyStamp) / 1000 * COPY_RATE);
    this.copyStamp = t;
    if (this.copyTokens < 1) { this.copiesSkipped++; return false; }
    this.copyTokens -= 1;
    return true;
  };

  OwlBridge.prototype.deliver = function (event, slot) {
    if (slot === null) { this.slotScartati++; return; }
    var copies = 1;
    if (event[0] === EV.STEP || event[0] === EV.TURN) {
      while (copies < this.wireCopies && this._copyAllowed()) copies++;
    }
    // IL TIMBRO DELLO SLOT nel nibble alto del type (contratto EVENT_SLOT
    // del payload). Dopo la decisione sulle copie, una volta per tutte.
    if (slot) {
      event = new Uint8Array(event);
      event[0] |= (slot << 4);
    }
    for (var i = 0; i < copies; i++) {
      if (this.wireLoss && this.rand() < this.wireLoss) { this.wireDropped++; continue; }
      this.wireSent++;
      this._deliverOne(event);
    }
  };

  OwlBridge.prototype._deliverOne = function (event) {
    if (this.club) {
      // Durante il club l'endpoint dati porta i blocchi della sessione link:
      // scriverci un evento di gioco vorrebbe dire iniettare 12 byte di
      // camminata in mezzo a uno scambio. Il payload dorme comunque: si
      // butta e si conta (identico a client.py).
      this.clubDrop++;
      return;
    }
    if (this.gbaMuto) { this.gbaMutoScartati++; return; }
    if (!this.device) return;
    var self = this;
    try {
      var p = this.device.sendEvent(event);
      if (p && p.then) p.then(null, function (e) {
        self.deviceErrori++;
        if (self.deviceErrori <= 3) self.log("invio al GBA fallito (" + e.message + "), continuo");
      });
    } catch (e) {
      this.deviceErrori++;
      if (this.deviceErrori <= 3) this.log("invio al GBA fallito (" + e.message + "), continuo");
    }
  };

  /* --- il Cable Club integrato (2026-08-26) --------------------------------
   *
   * La sessione vive in club.js (OwlClub.ClubSession); qui c'e' solo
   * l'orchestrazione locale, specchio di client.py: entrare (Pico in modo
   * LINK), pompare stati e blocchi fra device e relay, uscire (Pico di nuovo
   * in passthrough). Tutto da solo: il giocatore si siede al bancone e il
   * resto parte, come col pannello Python.
   */

  OwlBridge.prototype.sendClub = function (body) {
    this.sendRelay(R.pack(R.T.CLUB, this.peerId, this.room, this.nextSeq(), body));
  };

  /* Il primo T_CLUB che arriva dice CHI e' il partner (header OWL1): serve
   * al ruolo coi peer sorteggiati, e a ignorare un eventuale terzo peer. */
  OwlBridge.prototype._clubLatch = function (peerId) {
    if (this.clubPartner === null) {
      this.clubPartner = peerId;
      if (this.club) this.club.setPartner(peerId);
    }
  };

  OwlBridge.prototype._clubDopo = function () {
    if (this.club && this.club.finita) this.clubEnd();
  };

  OwlBridge.prototype.clubStart = function (motivo, dalGba) {
    if (this.club) return;
    if (!dalGba && now() / 1000 < this.clubRiagganciofino) {
      // Sessione appena chiusa: gli annunci dell'amico ancora in volo non
      // devono farci rimbalzare dentro. L'EV_CLUB del NOSTRO GBA invece
      // passa (il giocatore e' DAVVERO alla signorina; la sua quarantena
      // sta in onDeviceEvent).
      return;
    }
    var dev = this.device;
    if (this.spettatore || !dev || typeof dev.clubEnter !== "function") {
      // Da spettatore, o senza Pico, il club non si puo' fare: lo si dice
      // una volta e basta (specchio del ramo "emulatore" di client.py).
      if (this.clubSessions === 0) {
        this.clubSessions = -1;
        this.log("[club ] " + motivo + " - ma il club dal browser vuole il Pico collegato " +
          "via WebUSB: " + (this.spettatore ? "da spettatore" : "senza device") + " si ignora");
      }
      return;
    }
    var C = root.OwlClub;
    if (!C) {
      this.log("[club ] " + motivo + " - ma club.js non e' caricato: il sito e' monco, ricaricalo", null, true);
      return;
    }
    var self = this;
    this.log("============================================================");
    this.log("[club ] " + motivo);
    this.log("[club ] passo il Pico in modo LINK (Celio) e faccio io da ponte: NON toccare " +
      "niente, gioca pure - e tieni questa scheda in PRIMO PIANO (in background il browser rallenta i timer)");
    // La sessione nasce PRIMA del giro sul Pico: cosi' l'ENTER parte subito
    // con l'epoca giusta e i pacchetti dell'amico arrivati nel frattempo
    // finiscono nella sessione, che e' gia' quella vera (come client.py).
    var devAdapter = {
      errori: 0, consegnati: 0,
      command: function (cmd, label) {
        var me = this;
        dev.clubCommand(cmd, label).then(function () {
          self.log("[club ] -> device: " + label);
        }, function (e) {
          me.errori++;
          self.log("[club ] comando al device FALLITO (" + e.message + ")");
        });
      },
      sendBlock: function (block64) {
        var me = this;
        // Il contenuto va nel log PRIMA dell'invio: se il canale muore qui,
        // sapere COSA stava passando vale piu' del contatore.
        me.consegnati++;
        if (me.consegnati <= C.CLUB_LOG_BLOCCHI) self.log("[club ] >>GBA " + C.proto.blockRiga(block64));
        dev.clubSendBlock(block64).then(function (ok) { if (!ok) me.errori++; });
      }
    };
    var sess = new C.ClubSession({
      myPeer: this.peerId,
      dev: devAdapter,
      sendNet: function (b) { self.sendClub(b); },
      log: function (r) { self.log(r); }
    });
    this.club = sess;
    if (this.clubSessions < 0) this.clubSessions = 0;
    this.clubSessions++;
    if (this.clubPartner !== null) sess.setPartner(this.clubPartner);
    this.sendClub(C.proto.clubEnter(sess.epoca));
    dev.onClubStatus = function (st) {
      if (self.club === sess) { sess.onDeviceStatus(st); self._clubDopo(); }
    };
    dev.onClubBlock = function (b) {
      if (self.club !== sess) return;
      if (sess.blocchiTx < C.CLUB_LOG_BLOCCHI) self.log("[club ] GBA>> " + C.proto.blockRiga(b));
      sess.onDeviceBlock(b);
    };
    // Il battito della sessione (rilanci, riannunci, watchdog). In una scheda
    // in BACKGROUND il browser rallenta i setInterval: i dati continuano a
    // fluire (device e rete sono a eventi), ma i rilanci no - da qui l'invito
    // a tenere la scheda davanti durante uno scambio.
    this.clubTimer = setInterval(function () {
      if (self.club === sess) { sess.tick(); self._clubDopo(); }
    }, 100);
    dev.clubEnter().then(function (pronto) {
      if (!pronto && self.club === sess) {
        self.log("[club ] il device non e' entrato in modo link: se la sessione non parte, " +
          "'Riavvia il Pico' (F-4) e risiediti al bancone", null, true);
      }
    }, function (e) {
      // L'endpoint inceppato, o il Pico sparito: qui non c'e' la F-4
      // automatica del pannello Python - si dice il rimedio e si smonta.
      self.log("[club ] ingresso nel modo link FALLITO (" + e.message + "): premi 'Riavvia il " +
        "Pico' (F-4), poi risiediti al bancone. Il GBA NON va spento.", null, true);
      if (self.club === sess) {
        self.sendClub(C.proto.clubLeave(sess.epoca));
        sess.abortita = true;
        sess.motivoFine = "device non entrato in modo link";
        sess.finita = true;
        self.clubEnd();
      }
    });
  };

  OwlBridge.prototype.handleClubNet = function (peerId, body) {
    var C = root.OwlClub;
    if (!C) return;
    var P = C.proto;
    var p = P.clubUnpack(body);
    if (!p) {
      // Un corpo che non si legge e' quasi sempre l'altra parte con una
      // versione diversa: va detto FORTE, una volta.
      this.clubMalformati++;
      if (this.clubMalformati === 1) {
        this.log("[club ] PACCHETTO CLUB ILLEGGIBILE dall'amico: probabilmente sito e " +
          "pacchetto sono di versioni diverse - aggiornateli su ENTRAMBI i lati", null, true);
      }
      return;
    }
    if (this.club && this.clubPartner !== null && peerId !== this.clubPartner) {
      // Il club e' a DUE: in una stanza da 3-4, il terzo che parlasse di
      // club non deve inquinare la sessione in corso.
      this.clubAltrui++;
      if (this.clubAltrui === 1) {
        this.log("[club ] il peer " + peerId + " parla di club mentre la sessione e' col peer " +
          this.clubPartner + ": lo ignoro (il club e' a due)");
      }
      return;
    }
    if (p.sub === P.CLUB_ENTER) {
      this.clubStart("l'AMICO (peer " + peerId + ") e' entrato al Cable Club");
      if (this.club) {
        this._clubLatch(peerId);
        this.club.notaPartner(p.epoca);
      }
      return;
    }
    if (p.sub === P.CLUB_LEAVE) {
      if (this.club) { this.club.onNetLeave(p.epoca); this._clubDopo(); }
      return;
    }
    if (!this.club) {
      // Un CLUB_STATUS senza sessione = il club era gia' in corso quando
      // questa pagina e' partita (ricarica a meta'): ci si aggancia. I
      // DATA/REQ da soli non bastano ad aprire (arrivano anche da sessioni
      // zombie); gli stati invece vengono riannunciati apposta.
      if (p.sub === P.CLUB_STATUS) {
        this.clubStart("club gia' in corso dall'altra parte: mi aggancio in ritardo");
      }
      if (!this.club) return;
    }
    this._clubLatch(peerId);
    if (p.sub === P.CLUB_STATUS) {
      this.controllaVersioneClub(p.versione, p.impronta);
      this.club.onNetStatus(p.epoca, p.sseq, p.status);
    } else if (p.sub === P.CLUB_DATA) {
      this.club.onNetBlock(p.epoca, p.seq, p.block);
    } else if (p.sub === P.CLUB_REQ) {
      this.club.onNetReq(p.epoca, p.seqs);
    }
    this._clubDopo();
  };

  /* Le due parti devono parlare la STESSA versione del club. L'impronta dei
   * file qui non e' confrontabile (il sito non ha i .py): si controlla il
   * numero di versione e si DICE chi c'e' dall'altra parte, una volta. */
  OwlBridge.prototype.controllaVersioneClub = function (versione, impronta) {
    if (this.clubVersioneDetta) return;
    this.clubVersioneDetta = true;
    var C = root.OwlClub;
    if (versione === 0) {
      this.log("[club ] L'AMICO HA I FILE VECCHI (i suoi pacchetti non portano nemmeno il " +
        "numero di versione): IL CLUB NON PUO' FUNZIONARE, aggiornate il suo pacchetto", null, true);
    } else if (versione !== C.VERSIONE_CLUB) {
      this.log("[club ] VERSIONI DIVERSE: lui ha la " + versione + ", questo sito la " +
        C.VERSIONE_CLUB + ". IL CLUB NON PUO' FUNZIONARE COSI': aggiornate il lato vecchio", null, true);
    } else if (impronta === C.IMPRONTA_LUA) {
      this.log("[club ] l'amico gioca in EMULATORE (mGBA con lo script gen3-poke-multiplayer, " +
        "versione club " + versione + "): compatibile");
    } else if (impronta === C.IMPRONTA_WEB) {
      this.log("[club ] l'amico gioca anche lui dal browser (stessa versione club " + versione + "): ok");
    } else {
      this.log("[club ] l'amico usa il pannello Python (versione club " + versione + "): compatibile");
    }
  };

  OwlBridge.prototype.clubEnd = function () {
    var sess = this.club;
    if (!sess) return;
    this.club = null;
    this.clubPartner = null;
    if (this.clubTimer) { clearInterval(this.clubTimer); this.clubTimer = null; }
    var esito = sess.abortita ? "ABBANDONATA (" + (sess.motivoFine || "watchdog") + ")" : "conclusa";
    this.log("[club ] sessione " + esito + " | " + sess.riassunto());
    if (this.clubDrop) {
      this.log("[club ] eventi di camminata buttati durante il club: " + this.clubDrop +
        " (normale: il payload dormiva)");
      this.clubDrop = 0;
    }
    // Niente riagganci automatici per qualche secondo: gli annunci
    // dell'amico ancora in volo appartengono alla sessione appena morta.
    this.clubRiagganciofino = now() / 1000 + 10.0;
    this.sendClub(root.OwlClub.proto.clubLeave(sess.epoca));
    var dev = this.device, self = this;
    if (dev && typeof dev.clubExit === "function") {
      dev.onClubStatus = function () {};
      dev.onClubBlock = function () {};
      // Fino al primo evento valido dal GBA il filo verso di lui resta muto:
      // il gioco potrebbe essere ancora alla schermata del club col link
      // aperto, e i frame di camminata li' dentro sono veleno.
      this.gbaMuto = true;
      this.gbaMutoScartati = 0;
      dev.clubExit().then(function () {
        self.log("[club ] Pico di nuovo in passthrough: aspetto che il GBA torni a parlare prima di scrivergli");
      }, function (e) {
        self.log("[club ] rientro nel passthrough FALLITO (" + e.message + "): premi 'Riavvia " +
          "il Pico' (F-4). Il GBA NON va spento: il programma vive in RAM.", null, true);
      });
    }
  };

  /* --- la posizione (per le schede e per la mappa) ------------------------ */
  OwlBridge.prototype._aggiornaPos = function (pos, e) {
    var kind = e[0];
    if (kind !== 1 && kind !== 2 && kind !== 3 && kind !== EV.STATUS) return pos;
    var nuovo = {};
    if (pos) for (var k in pos) nuovo[k] = pos[k];
    nuovo.map = mapKeyOf(e);
    nuovo.gruppo = e[4]; nuovo.numero = e[5];
    nuovo.x = s16(e[6], e[7]); nuovo.y = s16(e[8], e[9]);
    nuovo.genere = e[10]; nuovo.avatar = e[11];
    nuovo.visto = Date.now();
    if (kind === EV.STATUS) nuovo.stato = e[1];
    else {
      nuovo.dir = e[1]; nuovo.stato = 0;
      // La VELOCITA' (byte 2, SPEED_*) serve alla mappa live per distinguere
      // la corsa dalla camminata: avatarState non la distingue, resta NORMAL
      // in entrambi i casi. Solo da PASSO/SYNC/GIRA: gli eventi di STATO
      // portano speed 0 fisso e azzererebbero la corsa a ogni battito.
      nuovo.speed = e[2];
    }
    return nuovo;
  };

  /* --- ciclo: ping e riga di stato ---------------------------------------- */

  OwlBridge.prototype.start = function () {
    var self = this;
    if (this.avviato) return;
    this.avviato = true;
    this.timerPing = setInterval(function () { self.sendPing(); }, PING_INTERVAL);
    this.timerStatus = setInterval(function () { self.status(); }, STATUS_INTERVAL);
    // Il PING resta anche da spettatore: e' lui a misurare l'RTT, e il WATCH
    // non ha un PONG apposta - due canali, due scopi, nessuna ambiguita'.
    if (this.spettatore) {
      this.timerWatch = setInterval(function () { self.sendHello(); }, WATCH_INTERVAL);
    }
  };

  OwlBridge.prototype.stop = function () {
    this.avviato = false;
    if (this.club) {
      // Partita fermata a meta' sessione: si chiude pulito (LEAVE all'amico,
      // Pico di nuovo in passthrough) invece di lasciare una sessione zombie.
      this.club.abortita = true;
      this.club.motivoFine = "partita fermata";
      this.club.finita = true;
      this.clubEnd();
    }
    if (this.timerPing) clearInterval(this.timerPing);
    if (this.timerStatus) clearInterval(this.timerStatus);
    if (this.timerWatch) clearInterval(this.timerWatch);
    this.timerPing = this.timerStatus = this.timerWatch = null;
  };

  /* Un amico e' "vivo" se ha parlato negli ultimi 12 s (il relay lo toglie a 10). */
  OwlBridge.prototype.amiciVivi = function () {
    var out = [], t = Date.now();
    for (var id in this.peers) {
      var p = this.peers[id];
      if (t - p.visto < 12000) out.push({ peerId: parseInt(id, 10), pos: p });
    }
    return out;
  };

  OwlBridge.prototype.status = function () {
    var rtt;
    if (this.rtts.length) {
      var lo = Infinity, hi = -Infinity, sum = 0;
      for (var i = 0; i < this.rtts.length; i++) { var v = this.rtts[i]; if (v < lo) lo = v; if (v > hi) hi = v; sum += v; }
      this.rttMin = lo; this.rttMax = hi; this.rttMedio = sum / this.rtts.length;
      rtt = "RTT min " + lo.toFixed(1) + " / medio " + this.rttMedio.toFixed(1) + " / max " + hi.toFixed(1) + " ms";
    } else {
      rtt = "RTT non ancora misurato";
      this.rttMin = this.rttMax = this.rttMedio = null;
    }
    this.rtts = [];
    var where = this.mapKey === null ? "mappa non ancora nota" : ("io su mappa " + (this.mapKey >> 8) + "." + (this.mapKey & 0xFF));
    this.log(rtt + " | inviati " + this.sent + " | ricevuti " + this.received +
      " | scartati " + this.dupDropped + " dup + " + this.lateDropped + " arretrati" +
      " | ricuciti " + this.healed + " (+" + this.healFar + " larghi) | VIA " + this.leaves +
      " | presenza " + this.presenzeRimandate +
      " | stanza " + this.room + " | " + where);
    // "presenza" = le fotografie rispedite mentre il GBA taceva. A zero e'
    // legittimo (sempre nell'overworld, dove il payload parla da solo); deve
    // salire ogni volta che si passa dai menu, da una lotta o dal club.
    if (this.club && !this.presenzeRimandate) {
      this.log("      !!! sei al Cable Club e non e' partita nessuna " +
        "presenza: chi entra ora non sa che ci sei.");
    }
    if (this.spettatore) {
      // Il contatore che sale e' la prova che la ritrasmissione del WATCH e'
      // viva: se restasse fermo, dopo un riavvio del relay conteremmo come
      // giocatori e ruberemmo un posto senza che nessuno se ne accorga.
      this.log("  spettatore: guardo e basta (WATCH inviati " + this.watchInviati +
        ", uno ogni " + (WATCH_INTERVAL / 1000) + " s) - non occupo un posto in stanza");
    } else {
      this.log("  filo verso il gioco: " + this.wireSent + " frame scritti (copie x" + this.wireCopies + "), " +
        this.copiesSkipped + " copie saltate per non intasare" +
        (this.wireLoss ? (", " + this.wireDropped + " buttati dal simulatore di perdita sul filo") : ""));
    }
    if (this.club) this.log("[club ] " + this.club.riassunto());
    this.onStatus(this.snapshot());
  };

  OwlBridge.prototype.snapshot = function () {
    return {
      peerId: this.peerId, room: this.room, mapKey: this.mapKey, posIo: this.posIo, statoIo: this.statoIo,
      sent: this.sent, received: this.received, dupDropped: this.dupDropped, lateDropped: this.lateDropped,
      healed: this.healed, healFar: this.healFar, leaves: this.leaves,
      presenzeRimandate: this.presenzeRimandate,
      wireSent: this.wireSent, copiesSkipped: this.copiesSkipped,
      rttUltimo: this.rttUltimo, rttMin: this.rttMin, rttMedio: this.rttMedio, rttMax: this.rttMax,
      club: !!this.club, clubSessions: Math.max(0, this.clubSessions),
      amici: this.amiciVivi()
    };
  };

  root.OwlBridge = OwlBridge;
  root.OwlBridge.EV = EV;
  root.OwlBridge.makeLeaveEvent = makeLeaveEvent;
})(typeof window !== "undefined" ? window : globalThis);
