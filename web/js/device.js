/* device.js - l'adattatore Celio via WebUSB (2026-08-21).
 *
 * E' il gemello di net/usb_link.py (UsbLink): stessa sequenza di apertura
 * misurata sul fisico (Cancel, SetMode passthrough, master, cavo, timing,
 * StartHandshake), stesso framing (sio.js), stessi contatori. Gira SOLO su
 * browser con WebUSB (Chrome/Edge): su Windows serve comunque il driver WinUSB
 * (Zadig), come per il client di Celio.
 *
 * COME SI LEGGE, E PERCHE' (la lezione di usb_link.py e del client di Celio):
 * un endpoint interrupt IN viene interrogato dall'host SOLO se c'e' una
 * richiesta pendente. Qui c'e' SEMPRE una transferIn in volo sull'endpoint
 * dati e una su quello di stato, riarmate appena si risolvono - e' quello che
 * fa il client WebUSB di Celio (linkdevice.service.ts) e che in Python ha
 * richiesto due thread dedicati.
 *
 * SCRITTO ALLA CIECA: il 2026-08-21 nessun browser e' stato collegato al Pico.
 * La prima prova sul fisico e' in web/README.md.
 */
(function (root) {
  "use strict";

  var VID = 0x2FE3, PID = 0x000A;
  var EP_CMD = 1, EP_STATUS = 1, EP_DATA = 2;   // WebUSB: numero senza il bit di direzione
  var CMD = {
    SET_MODE: 0x00, CANCEL: 0x01, SET_MODE_MASTER: 0x10, SET_MODE_SLAVE: 0x11,
    START_HANDSHAKE: 0x12, CONNECT_LINK: 0x13, SET_RAW_TIMING: 0x14,
    SET_CABLE_TYPE: 0x15, HW_REBOOT: 0x43
  };
  var MODE = { PASSTHROUGH: 0x04, ONLINE_LINK: 0x01 };
  var REBOOT_MAGIC = 0xA5;
  var CABLE = { auto: 0, gba: 1, gbc: 2 };

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function CelioDevice(opts) {
    opts = opts || {};
    this.timing = opts.timing || null;     // iterazioni PIO (F-1), null = default firmware
    this.cable = opts.cable || "auto";      // F-3
    this.raw = !!opts.raw;                  // modo parola grezza (multiboot)
    this.log = opts.log || function () {};
    // L'ADATTATORE SPARITO (2026-08-30). Chi ci sta sopra deve saperlo: senza
    // questo avviso il resto del sito continua a comportarsi come se il Pico
    // fosse li' - la spia resta verde e, peggio, il battito continua a
    // rimandare la fotografia della presenza, facendo comparire e sparire il
    // nostro avatar a casa dell'amico ogni mezzo minuto (vedi bridge.js).
    this.onLost = opts.onLost || function () {};
    this.onEvent = opts.onEvent || function () {};
    this.onSonda = opts.onSonda || function () {};
    this.onDiag = opts.onDiag || function () {};
    this.onPing = opts.onPing || function () {};
    this.onStatus = opts.onStatus || function () {};
    this.device = null;
    this.open_ = false;
    this.deframer = new root.GbaSio.SioDeframer();
    this.packetsRx = 0; this.framesTx = 0; this.txErrors = 0;
    this.statusReads = 0; this.lastStatus = null;
    this.eventsRx = 0;
    this.rawRx = 0; this.rawTx = 0;
    this.rawWords = []; this.rawWaiters = [];
    this.rawOdd = -1;
    this.stateWords = null; this.stateStamp = 0; this.stateCount = 0;
    this.diagWords = null; this.diagStamp = 0;
    // Il modo CLUB (Cable Club, 2026-08-26): fra clubEnter() e clubExit() il
    // Pico e' nel modo LINK originale di Celio (0x01) e sul filo USB passano
    // stati (EP_STATUS) e blocchi da 64 byte (EP_DATA), non frame SIO. La
    // logica di sessione NON sta qui (sta in club.js): qui solo il trasporto.
    // I due callback li aggancia il bridge quando apre la sessione.
    this.clubMode = false;
    this.clubReady = false;
    this.onClubStatus = function () {};
    this.onClubBlock = function () {};
    this._clubAwaitOk = null;
    this._clubAwaitVisto = false;
    // Il distacco: `perso` distingue "chiuso da noi" da "sparito sotto i
    // piedi". I due contatori di errori consecutivi sono la seconda strada
    // per accorgersene (la prima e' l'evento 'disconnect' di WebUSB), e si
    // azzerano a ogni lettura riuscita.
    this.perso = false;
    this._errDati = 0;
    this._errStato = 0;
    this._onDisconnect = null;
  }

  /* Quante letture consecutive fallite bastano a dire "non c'e' piu'".
   * Il ciclo dei dati gira ogni 50 ms: 20 tentativi = un secondo di buio.
   * Serve una soglia e non "il primo errore" perche' durante il multiboot e
   * ai cambi di modo qualche transferIn viene rifiutato per un istante senza
   * che il Pico se ne sia andato davvero. */
  var ERR_FATALI = 20;

  CelioDevice.supportato = function () { return !!(navigator.usb); };

  /* Chiede all'utente il Pico (va chiamata da un click: WebUSB lo pretende). */
  CelioDevice.prototype.request = function () {
    var self = this;
    return navigator.usb.requestDevice({ filters: [{ vendorId: VID, productId: PID }] })
      .then(function (d) { self.device = d; return d; });
  };

  /* Riusa un device gia' autorizzato (dopo un reload), se c'e'. */
  CelioDevice.prototype.riusa = function () {
    var self = this;
    return navigator.usb.getDevices().then(function (list) {
      for (var i = 0; i < list.length; i++) {
        if (list[i].vendorId === VID && list[i].productId === PID) { self.device = list[i]; return list[i]; }
      }
      return null;
    });
  };

  CelioDevice.prototype.cmd = function (bytes, pauseMs, label) {
    var self = this;
    return this.device.transferOut(EP_CMD, new Uint8Array(bytes)).then(function (r) {
      if (r.status !== "ok") throw new Error("comando '" + label + "' rifiutato: " + r.status);
      self.log("[usb ] " + label);
      return sleep(pauseMs);
    }, function (e) {
      // Lo stesso difetto noto di usb_link.py: l'endpoint del Pico inceppato
      // dopo una sessione lunga. Il rimedio e' il riavvio F-4, o lo
      // scollega/ricollega.
      throw new Error("il comando '" + label + "' non e' partito (" + e.message + "): endpoint del Pico " +
        "inceppato? Rimedio: bottone 'Riavvia il Pico' (F-4), oppure scollega e ricollega.");
    });
  };

  CelioDevice.prototype.open = function () {
    var self = this;
    if (!this.device) return Promise.reject(new Error("nessun device: prima 'Collega'"));
    return this.device.open()
      .then(function () { return self.device.selectConfiguration(1); })
      .then(function () { return self.device.claimInterface(0); })
      .then(function () { return self.cmd([CMD.CANCEL], 600, "Cancel"); })
      .then(function () { return self.cmd([CMD.SET_MODE, MODE.PASSTHROUGH], 300, "SetMode raw relay (rilevazione cavo ADESSO)"); })
      .then(function () { return self.cmd([CMD.SET_MODE_MASTER], 200, "master"); })
      .then(function () {
        if (self.cable !== "auto") {
          return self.cmd([CMD.SET_CABLE_TYPE, CABLE[self.cable]], 200,
            "cavo forzato: " + self.cable + " (SD su GP" + (self.cable === "gba" ? 3 : 4) + ")");
        }
      })
      .then(function () {
        if (self.timing) return self.setTiming(self.timing, "timing " + self.timing + " iterazioni PIO", 200);
      })
      .then(function () { return self.cmd([CMD.START_HANDSHAKE], 200, "StartHandshake"); })
      .then(function () {
        self.open_ = true;
        self.perso = false;
        self._errDati = 0;
        self._errStato = 0;
        self._ascoltaIlDistacco();
        self._dataLoop();
        self._statusLoop();
        return self;
      });
  };

  /* Rifa' la sequenza di setup senza rienumerare il device (fra un tentativo
   * di multiboot e l'altro: GBATEK vuole che la sessione RICOMINCI). Come
   * UsbLink.restart: NIENTE device.reset(), che su Zephyr fa rienumerare. */
  CelioDevice.prototype.restart = function () {
    var self = this;
    return this.cmd([CMD.CANCEL], 400, "Cancel (riavvio sessione)")
      .then(function () { return self.cmd([CMD.SET_MODE, MODE.PASSTHROUGH], 300, "SetMode"); })
      .then(function () { return self.cmd([CMD.SET_MODE_MASTER], 200, "master"); })
      .then(function () { if (self.cable !== "auto") return self.cmd([CMD.SET_CABLE_TYPE, CABLE[self.cable]], 200, "cavo forzato: " + self.cable); })
      .then(function () { if (self.timing) return self.setTiming(self.timing, "timing " + self.timing, 200); })
      .then(function () { return self.cmd([CMD.START_HANDSHAKE], 200, "StartHandshake"); });
  };

  CelioDevice.prototype.setTiming = function (iterations, label, pauseMs) {
    var v = iterations >>> 0;
    return this.cmd([CMD.SET_RAW_TIMING, v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF],
      pauseMs || 0, label || ("timing " + iterations));
  };

  /* F-4: riavvia il Pico. Dopo, questo oggetto e' da buttare: il device
   * rienumera e va richiesto di nuovo (requestDevice / riusa). */
  CelioDevice.prototype.reboot = function () {
    var self = this;
    return this.cmd([CMD.HW_REBOOT, REBOOT_MAGIC], 0, "riavvio del Pico (F-4)")
      .catch(function () { /* il device sparisce sotto la write: e' il successo */ })
      .then(function () { return self.close(); });
  };

  CelioDevice.prototype.close = function () {
    this.open_ = false;
    this._smettiDiAscoltareIlDistacco();
    var d = this.device; this.device = null;
    if (!d) return Promise.resolve();
    return d.close().catch(function () {});
  };

  /* IL DISTACCO (2026-08-30).
   *
   * Prima di oggi il sito non se ne accorgeva in nessun modo: staccando il
   * Pico, `transferIn` cominciava a fallire e i due cicli di lettura
   * ritentavano PER SEMPRE, 20 volte al secondo, scrivendo una riga di log
   * ognuna. Il device restava non nullo, quindi la spia restava verde e il
   * bridge continuava a mandare il battito con la fotografia della presenza:
   * l'amico vedeva il nostro avatar comparire e sparire ogni ~30 s (il
   * timeout del relay WebSocket) senza che niente, da nessuna parte, dicesse
   * che l'adattatore non c'era piu'.
   *
   * Adesso si chiude una volta sola e lo si dice a chi sta sopra. Due strade
   * per accorgersene, perche' una sola non basta: l'evento 'disconnect' di
   * WebUSB (immediato, ma non arriva se il Pico si impianta senza staccarsi)
   * e il conteggio degli errori consecutivi (piu' lento ma cieco a nulla). */
  CelioDevice.prototype._ascoltaIlDistacco = function () {
    var self = this;
    if (this._onDisconnect || !navigator.usb || !navigator.usb.addEventListener) return;
    this._onDisconnect = function (ev) {
      if (self.device && ev.device === self.device) self.perdi("staccato dalla presa");
    };
    navigator.usb.addEventListener("disconnect", this._onDisconnect);
  };

  CelioDevice.prototype._smettiDiAscoltareIlDistacco = function () {
    if (!this._onDisconnect) return;
    try { navigator.usb.removeEventListener("disconnect", this._onDisconnect); } catch (e) { /* niente */ }
    this._onDisconnect = null;
  };

  /* Una volta sola, comunque ci si arrivi. Chiude e avvisa. */
  CelioDevice.prototype.perdi = function (perche) {
    if (this.perso || !this.open_) return;
    this.perso = true;
    this.open_ = false;
    this.log("[usb ] ADATTATORE SCOLLEGATO (" + perche + "). La partita si ferma " +
      "qui: ricollega il Pico e ricomincia da 'Collega il Pico'. Il GBA NON va " +
      "spento, il programma vive nella sua RAM.");
    this._smettiDiAscoltareIlDistacco();
    var d = this.device; this.device = null;
    if (d) { try { d.close().catch(function () {}); } catch (e) { /* niente */ } }
    try { this.onLost(perche); } catch (e) { /* niente */ }
  };

  CelioDevice.prototype._dataLoop = function () {
    var self = this;
    function giro() {
      if (!self.open_ || !self.device) return;
      self.device.transferIn(EP_DATA, 64).then(function (r) {
        if (r.status === "ok" && r.data && r.data.byteLength) {
          self.packetsRx++;
          self._onData(new Uint8Array(r.data.buffer, r.data.byteOffset, r.data.byteLength));
        }
        self._errDati = 0;
        giro();
      }, function (e) {
        if (!self.open_) return;
        // UNA riga sola, non una ogni 50 ms. Il log alluvionato non era solo
        // brutto da leggere: ogni riga e' un nodo nel DOM e un reflow della
        // pagina, e con 20 righe al secondo il thread principale si strozza
        // al punto che i setInterval del battito saltano - cioe' il rimedio
        // diventava esso stesso una causa del difetto.
        self._errDati++;
        if (self._errDati === 1) {
          self.log("[usb ] lettura dati fallita (" + e.message + "): riprovo in silenzio");
        }
        if (self._errDati >= ERR_FATALI) { self.perdi("nessuna risposta dal Pico"); return; }
        setTimeout(giro, 50);
      });
    }
    giro();
  };

  CelioDevice.prototype._statusLoop = function () {
    var self = this;
    function giro() {
      if (!self.open_ || !self.device) return;
      self.device.transferIn(EP_STATUS, 64).then(function (r) {
        if (r.status === "ok" && r.data && r.data.byteLength >= 2) {
          self.lastStatus = r.data.getUint16(0, true);
          self.statusReads++;
          self.onStatus(self.lastStatus);
          if (self.clubMode) {
            // In modo club gli stati SONO la sessione (LinkStatus di Celio):
            // il lettore qui e' SEMPRE pendente, quindi il buco del buffer
            // unico che in usb_link.py ha richiesto il doppio thread non
            // esiste - e' lo stesso motivo per cui il client web di Celio
            // non soffriva.
            if (self.lastStatus === 0xFF02) {
              self._clubAwaitVisto = true;
              if (self._clubAwaitOk) { var okAwait = self._clubAwaitOk; self._clubAwaitOk = null; okAwait(true); }
            }
            self.onClubStatus(self.lastStatus);
          }
        }
        // Azzerare il conteggio SOLO su una lettura riuscita, e azzerare con
        // esso anche il "l'ho gia' detto": prima _statusErrDetto non tornava
        // mai indietro, quindi un secondo guasto piu' avanti nella sessione
        // restava muto.
        self._errStato = 0;
        self._statusErrDetto = false;
        giro();
      }, function (e) {
        if (!self.open_) return;
        // una volta sola: un loop di stato morto non deve restare muto
        self._errStato++;
        if (!self._statusErrDetto) {
          self._statusErrDetto = true;
          self.log("[usb ] lettura stato fallita (" + e.message + "): riprovo in silenzio");
        }
        // Il ciclo dello stato gira ogni 200 ms: stessa soglia in secondi del
        // ciclo dei dati (un secondo di buio) vuol dire un quarto dei giri.
        if (self._errStato >= ERR_FATALI / 4) { self.perdi("nessuno stato dal Pico"); return; }
        setTimeout(giro, 200);
      });
    }
    giro();
  };

  CelioDevice.prototype._onData = function (bytes) {
    if (this.clubMode) {
      // Un blocco della sessione link e' UN pacchetto USB da 64 byte esatti
      // (32 parole del link), come in usb_link._club_data_loop: il resto e'
      // rumore di transizione e si ignora.
      if (bytes.length === 64) this.onClubBlock(new Uint8Array(bytes));
      return;
    }
    if (this.raw) {
      var buf = bytes, start = 0;
      if (this.rawOdd >= 0) { this._pushRaw(this.rawOdd | (buf[0] << 8)); this.rawOdd = -1; start = 1; }
      var n = buf.length - start;
      if (n & 1) { this.rawOdd = buf[buf.length - 1]; n--; }
      for (var i = start; i < start + n; i += 2) this._pushRaw(buf[i] | (buf[i + 1] << 8));
      return;
    }
    var frames = this.deframer.feed(bytes);
    var S = root.GbaSio.SIO;
    for (var k = 0; k < frames.length; k++) {
      var f = frames[k];
      if (f.type === S.T_EVENT && f.words.length === S.EVENT_WORDS) {
        this.eventsRx++;
        this.onEvent(root.GbaSio.wordsToBytes(f.words));
      } else if (f.type === S.T_STATE && f.words.length === 8) {
        this.stateWords = f.words; this.stateStamp = Date.now(); this.stateCount++;
        this.onSonda(f.words);
      } else if (f.type === S.T_DIAG && f.words.length === 8) {
        this.diagWords = f.words; this.diagStamp = Date.now();
        this.onDiag(f.words);
      } else if (f.type === S.T_PING) {
        this.onPing(f.words);
      }
    }
  };

  CelioDevice.prototype._pushRaw = function (w) {
    this.rawRx++;
    if (this.rawWaiters.length) { var wt = this.rawWaiters.shift(); clearTimeout(wt.t); wt.ok(w); }
    else this.rawWords.push(w);
  };

  /* --- API del bridge: 12 byte dentro, 12 byte fuori ---------------------- */
  CelioDevice.prototype.sendEvent = function (event12) {
    if (event12.length !== 12) throw new Error("un NetEvent e' 12 byte, non " + event12.length);
    return this.sendFrame(root.GbaSio.SIO.T_EVENT, root.GbaSio.bytesToWords(event12));
  };

  CelioDevice.prototype.sendFrame = function (ftype, words) {
    var self = this;
    return this.device.transferOut(EP_DATA, root.GbaSio.sioFrame(ftype, words))
      .then(function () { self.framesTx++; }, function (e) { self.txErrors++; throw e; });
  };

  /* --- API a parola grezza (multiboot, come mb_multi.py) ------------------- */
  CelioDevice.prototype.sendWords = function (words) {
    if (!this.raw) return Promise.reject(new Error("sendWords richiede raw=true"));
    var data = root.GbaSio.wordsToBytes(words);
    var self = this;
    var p = Promise.resolve();
    for (var i = 0; i < data.length; i += 64) {
      (function (chunk) { p = p.then(function () { return self.device.transferOut(EP_DATA, chunk); }); })(data.subarray(i, i + 64));
    }
    return p.then(function () { self.rawTx += words.length; });
  };

  CelioDevice.prototype.readWord = function (timeoutMs) {
    var self = this;
    if (this.rawWords.length) return Promise.resolve(this.rawWords.shift());
    return new Promise(function (ok) {
      var wt = { ok: ok, t: null };
      wt.t = setTimeout(function () {
        var i = self.rawWaiters.indexOf(wt);
        if (i >= 0) self.rawWaiters.splice(i, 1);
        ok(null);
      }, timeoutMs || 2000);
      self.rawWaiters.push(wt);
    });
  };

  CelioDevice.prototype.wordsPending = function () { return this.rawWords.length; };

  CelioDevice.prototype.drainRx = function (ms) {
    var self = this;
    return sleep(ms || 300).then(function () { var n = self.rawWords.length; self.rawWords = []; return n; });
  };

  /* --- il Cable Club (2026-08-26, gemello di usb_link.py club_enter/exit) --- */

  /* Ferma il passthrough e mette il Pico in Online Link Mode. Risolve con
   * true se AwaitMode e' arrivato entro 3 s (il device e' pronto), false se
   * no. Gli stati della transizione fluiscono comunque verso onClubStatus:
   * i residui li filtra la sessione (il "secondo giro di chiave"). */
  CelioDevice.prototype.clubEnter = function () {
    var self = this;
    this.clubMode = true;
    this.clubReady = false;
    this._clubAwaitVisto = false;
    return this.cmd([CMD.CANCEL], 600, "Cancel (fine passthrough)")
      .then(function () { return self.cmd([CMD.SET_MODE, MODE.ONLINE_LINK], 0, "SetMode onlineLink (modo Celio originale)"); })
      .then(function () {
        return new Promise(function (ok) {
          if (self._clubAwaitVisto) return ok(true);
          self._clubAwaitOk = ok;
          setTimeout(function () {
            if (self._clubAwaitOk === ok) { self._clubAwaitOk = null; ok(self._clubAwaitVisto); }
          }, 3000);
        });
      })
      .then(function (pronto) {
        self.clubReady = pronto;
        self.log("[usb ] " + (pronto ? "AwaitMode: il device e' pronto per il club"
          : "!!! AwaitMode NON e' arrivato entro 3 s: il device non e' entrato in modo link"));
        return pronto;
      });
  };

  /* Chiude il modo club e rifa' TUTTA la sequenza passthrough: al ritorno il
   * canale eventi e' quello di sempre. Il de-framer si riallinea da solo
   * (la FSM riparte da WAIT_SYNC, i contatori restano). */
  CelioDevice.prototype.clubExit = function () {
    this.clubMode = false;
    this._clubAwaitOk = null;
    var d = this.deframer;
    d.state = d.W_SYNC; d.data = []; d.bytePending = -1;
    return this.restart();
  };

  CelioDevice.prototype.clubCommand = function (cmd, label) {
    return this.cmd([cmd], 50, label);
  };

  /* Un blocco dati da 64 byte verso il device (32 parole del link).
   * Risolve con true/false invece di rigettare: chi chiama conta, non muore. */
  CelioDevice.prototype.clubSendBlock = function (block64) {
    var self = this;
    return this.device.transferOut(EP_DATA, block64)
      .then(function () { return true; }, function () { self.txErrors++; return false; });
  };

  CelioDevice.prototype.stats = function () {
    var d = this.deframer;
    return "rx pacchetti " + this.packetsRx + " | parole " + d.wordsRx + " (idle " + d.idleWords + ")" +
      " | frame " + d.framesRx + " (errori " + d.frameErr + ", resync " + d.resync + ")" +
      " | eventi " + this.eventsRx + " | tx frame " + this.framesTx + " (errori " + this.txErrors + ")" +
      " | stati letti " + this.statusReads;
  };

  root.CelioDevice = CelioDevice;
  root.CelioDevice.CMD = CMD; root.CelioDevice.MODE = MODE;
})(typeof window !== "undefined" ? window : globalThis);
