/* relay.js - il relay visto dal browser (2026-08-23).
 *
 * Due cose, entrambe gemelle di net/protocol.py e di net/client.py:
 *
 *   1. l'header OWL1 dei datagrammi (pack/unpack): magic "OWL1", versione 1,
 *      tipo, peerId u16, stanza u16, seq u8 - 11 byte little endian, e poi il
 *      corpo. Il relay non guarda dentro il corpo, e nemmeno questo file;
 *   2. RelayLink: la connessione WebSocket verso relay_ws.py (o il Worker su
 *      Cloudflare), con riconnessione automatica. I browser non fanno UDP:
 *      il messaggio WebSocket (binario) E' il datagramma, byte per byte, e
 *      dall'altra parte relay_ws.py lo gira su UDP a relay.py. Cosi' chi gioca
 *      dal browser e chi gioca con client.py stanno nella stessa stanza.
 *
 * Script classico (niente moduli ES), come il resto di web/js.
 */
(function (root) {
  "use strict";

  var MAGIC = [0x4F, 0x57, 0x4C, 0x31];   // "OWL1"
  var VERSION = 1;
  var HEADER_SIZE = 11;
  // WATCH (5): "voglio guardare questa stanza" - entra da SPETTATORE, non
  // conta nel tetto dei 4 giocatori. Questa tabella e' la gemella di
  // net/protocol.py: si toccano insieme.
  var T = { HELLO: 0, EVENT: 1, PING: 2, PONG: 3, BYE: 4, WATCH: 5, CLUB: 6 };
  var T_NOMI = { 0: "HELLO", 1: "EVENT", 2: "PING", 3: "PONG", 4: "BYE",
                 5: "WATCH", 6: "CLUB" };

  function pack(kind, peerId, roomId, seq, body) {
    var n = body ? body.length : 0;
    var out = new Uint8Array(HEADER_SIZE + n);
    out[0] = MAGIC[0]; out[1] = MAGIC[1]; out[2] = MAGIC[2]; out[3] = MAGIC[3];
    out[4] = VERSION;
    out[5] = kind & 0xFF;
    out[6] = peerId & 0xFF; out[7] = (peerId >> 8) & 0xFF;
    out[8] = roomId & 0xFF; out[9] = (roomId >> 8) & 0xFF;
    out[10] = seq & 0xFF;
    if (n) out.set(body, HEADER_SIZE);
    return out;
  }

  /* Ritorna {kind, peerId, roomId, seq, body} oppure null se non e' roba nostra. */
  function unpack(bytes) {
    var b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    if (b.length < HEADER_SIZE) return null;
    if (b[0] !== MAGIC[0] || b[1] !== MAGIC[1] || b[2] !== MAGIC[2] || b[3] !== MAGIC[3]) return null;
    if (b[4] !== VERSION) return null;
    return {
      kind: b[5],
      peerId: b[6] | (b[7] << 8),
      roomId: b[8] | (b[9] << 8),
      seq: b[10],
      body: b.subarray(HEADER_SIZE)
    };
  }

  /* Da "host:porta" o da un URL qualsiasi a un URL WebSocket. "wss" se la
   * pagina e' https (il browser lo pretende), altrimenti "ws". */
  function normalizzaUrl(testo) {
    var t = (testo || "").trim();
    if (!t) return "";
    if (/^wss?:\/\//i.test(t)) return t;
    if (/^https:\/\//i.test(t)) return "wss://" + t.slice(8);
    if (/^http:\/\//i.test(t)) return "ws://" + t.slice(7);
    var schema = (typeof location !== "undefined" && location.protocol === "https:") ? "wss://" : "ws://";
    return schema + t;
  }

  /* La connessione al relay. opts: {onMessage(bytes), onOpen(), onClose(motivo), log(riga)}.
   * Si riconnette da sola (1, 2, 4 ... 10 s); chi la usa rimanda l'HELLO in onOpen. */
  function RelayLink(url, opts) {
    opts = opts || {};
    this.url = url;
    this.onMessage = opts.onMessage || function () {};
    this.onOpen = opts.onOpen || function () {};
    this.onClose = opts.onClose || function () {};
    this.log = opts.log || function () {};
    this.ws = null;
    this.voluta = false;       // l'utente vuole stare collegato
    this.aperta = false;
    this.tentativi = 0;
    this.riconnessioni = 0;
    this.inviati = 0; this.ricevuti = 0; this.nonNostri = 0; this.erroriInvio = 0;
    this.timerRiconn = null;
  }

  RelayLink.prototype.connect = function () {
    var self = this;
    this.voluta = true;
    if (this.ws) return;
    var ws;
    try {
      ws = new WebSocket(this.url);
    } catch (e) {
      this.log("[rete ] URL del relay non valido: " + this.url + " (" + e.message + ")");
      this.voluta = false;
      this.onClose("url non valido");
      return;
    }
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = function () {
      self.aperta = true;
      if (self.tentativi) self.riconnessioni++;
      self.tentativi = 0;
      self.log("[rete ] collegato al relay " + self.url + (self.riconnessioni ? " (riconnessione n. " + self.riconnessioni + ")" : ""));
      self.onOpen();
    };
    ws.onmessage = function (ev) {
      if (!(ev.data instanceof ArrayBuffer)) { self.nonNostri++; return; }
      self.ricevuti++;
      self.onMessage(new Uint8Array(ev.data));
    };
    ws.onerror = function () { /* il close che segue dice tutto */ };
    ws.onclose = function (ev) {
      var eraAperta = self.aperta;
      self.aperta = false;
      self.ws = null;
      var motivo = (ev && ev.code) ? ("codice " + ev.code + (ev.reason ? " " + ev.reason : "")) : "";
      if (!self.voluta) { self.onClose("chiusa"); return; }
      self.tentativi++;
      var attesa = Math.min(10000, 1000 * Math.pow(2, Math.min(self.tentativi - 1, 4)));
      self.log("[rete ] relay " + (eraAperta ? "CADUTO" : "NON RAGGIUNGIBILE") + " (" + (motivo || self.url) + "): riprovo fra " + (attesa / 1000) + " s");
      self.onClose(eraAperta ? "caduto" : "non raggiungibile");
      self.timerRiconn = setTimeout(function () { self.timerRiconn = null; if (self.voluta) self.connect(); }, attesa);
    };
  };

  RelayLink.prototype.close = function () {
    this.voluta = false;
    if (this.timerRiconn) { clearTimeout(this.timerRiconn); this.timerRiconn = null; }
    var ws = this.ws;
    this.ws = null;
    this.aperta = false;
    if (ws) { try { ws.close(1000, "chiusura"); } catch (e) { /* niente */ } }
  };

  RelayLink.prototype.send = function (bytes) {
    if (!this.ws || this.ws.readyState !== 1) { this.erroriInvio++; return false; }
    try { this.ws.send(bytes); this.inviati++; return true; }
    catch (e) { this.erroriInvio++; return false; }
  };

  root.OwlRelay = {
    T: T, T_NOMI: T_NOMI, HEADER_SIZE: HEADER_SIZE,
    pack: pack, unpack: unpack, normalizzaUrl: normalizzaUrl,
    RelayLink: RelayLink
  };
})(typeof window !== "undefined" ? window : globalThis);
