/* sio.js - il framing di payload/sio.c, in JavaScript (2026-08-21).
 *
 * E' il gemello di net/usb_link.py (sio_frame + SioDeframer) e di
 * protocol.describe_event: stesse costanti, stessa FSM, stessi contatori. Se
 * cambia il framing sul GBA cambia QUI e in usb_link.py insieme.
 *
 *   [SYNC 0xA55A][HEAD = tipo<<8 | n_parole][dati...][CKSUM]
 *   CKSUM = -(HEAD + somma dati) a 16 bit; il SYNC non entra nella somma.
 *   Tipi: 0x01 EVENT (6 parole = un NetEvent), 0x02 STATE (sonda pagina 1),
 *         0x03 PING, 0x04 DIAG (sonda pagina 2).
 *   Idle: 0x7FFF (GBA child senza niente da dire), 0xFFFF (linea a riposo).
 *
 * Script classico (niente moduli ES): la pagina deve aprirsi anche da file://.
 */
(function (root) {
  "use strict";

  var SIO = {
    SYNC: 0xA55A, IDLE: 0x7FFF, MAX_WORDS: 32,
    T_EVENT: 0x01, T_STATE: 0x02, T_PING: 0x03, T_DIAG: 0x04,
    EVENT_WORDS: 6
  };

  function sioFrame(ftype, words) {
    if (!(ftype > 0 && ftype <= 0xFF)) throw new Error("tipo frame fuori range: " + ftype);
    if (words.length > SIO.MAX_WORDS) throw new Error("troppe parole: " + words.length);
    var head = ((ftype & 0xFF) << 8) | words.length;
    var sum = head;
    for (var i = 0; i < words.length; i++) sum += words[i] & 0xFFFF;
    var cksum = (-sum) & 0xFFFF;
    var all = [SIO.SYNC, head].concat(words, [cksum]);
    var out = new Uint8Array(all.length * 2);
    for (var k = 0; k < all.length; k++) {
      out[2 * k] = all[k] & 0xFF;
      out[2 * k + 1] = (all[k] >> 8) & 0xFF;
    }
    return out;
  }

  /* La FSM di ricezione, parola per parola. feed(bytes) accetta pacchetti USB
   * di qualunque lunghezza (anche a meta' parola) e ritorna i frame completi
   * col checksum giusto: [{type, words}]. Lo stato sopravvive fra una feed e
   * l'altra: e' il punto. */
  function SioDeframer() {
    this.W_SYNC = 0; this.W_HEAD = 1; this.W_DATA = 2; this.W_CKSUM = 3;
    this.state = this.W_SYNC;
    this.ftype = 0; this.count = 0; this.head = 0; this.data = [];
    this.bytePending = -1;
    // Gli stessi contatori di g_sio sul GBA e di SioDeframer in Python.
    this.wordsRx = 0; this.framesRx = 0; this.frameErr = 0; this.resync = 0;
    this.idleWords = 0;
    this.hist = {}; this.histSize = 0;
  }

  SioDeframer.prototype._drop = function () {
    this.frameErr++; this.resync++;
    this.state = this.W_SYNC; this.data = [];
  };

  SioDeframer.prototype.feed = function (bytes) {
    var out = [];
    var buf = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    var start = 0;
    var w;
    if (this.bytePending >= 0) {
      if (buf.length === 0) return out;
      w = this.bytePending | (buf[0] << 8);
      this.bytePending = -1;
      start = 1;
      this._word(w, out);
    }
    var n = buf.length - start;
    if (n & 1) {
      this.bytePending = buf[buf.length - 1];
      n -= 1;
    }
    for (var i = start; i < start + n; i += 2) {
      this._word(buf[i] | (buf[i + 1] << 8), out);
    }
    return out;
  };

  SioDeframer.prototype._word = function (w, out) {
    this.wordsRx++;
    if (this.state === this.W_SYNC) {
      if (this.histSize < 64 || (w in this.hist)) {
        if (!(w in this.hist)) this.histSize++;
        this.hist[w] = (this.hist[w] || 0) + 1;
      }
      if (w === SIO.SYNC) this.state = this.W_HEAD;
      else if (w === SIO.IDLE || w === 0xFFFF) this.idleWords++;
    } else if (this.state === this.W_HEAD) {
      var ftype = w >> 8, count = w & 0xFF;
      if (ftype === 0 || count > SIO.MAX_WORDS) { this._drop(); return; }
      this.ftype = ftype; this.count = count; this.head = w; this.data = [];
      this.state = count ? this.W_DATA : this.W_CKSUM;
    } else if (this.state === this.W_DATA) {
      this.data.push(w);
      if (this.data.length === this.count) this.state = this.W_CKSUM;
    } else {
      var sum = this.head + w;
      for (var i = 0; i < this.data.length; i++) sum += this.data[i];
      if ((sum & 0xFFFF) === 0) {
        this.framesRx++;
        out.push({ type: this.ftype, words: this.data.slice() });
        this.state = this.W_SYNC; this.data = [];
      } else {
        this._drop();
      }
    }
  };

  /* --- i 12 byte di gioco (protocol.py) ---------------------------------- */
  var EV = { STEP: 1, SYNC: 2, TURN: 3, LEAVE: 4, STATUS: 5, CARD: 6, CLUB: 7 };
  var EV_NOMI = { 1: "PASSO", 2: "SYNC", 3: "GIRA", 4: "VIA", 5: "STATO", 6: "CARTA", 7: "CLUB" };
  var DIR_NOMI = { 0: "-", 1: "giu", 2: "su", 3: "sx", 4: "dx" };
  var STATO_NOMI = { 0: "overworld", 1: "lotta", 2: "dialogo", 3: "menu",
                     4: "zaino", 5: "squadra", 6: "pokedex", 7: "pokenav" };

  function wordsToBytes(words) {
    var b = new Uint8Array(words.length * 2);
    for (var i = 0; i < words.length; i++) { b[2 * i] = words[i] & 0xFF; b[2 * i + 1] = (words[i] >> 8) & 0xFF; }
    return b;
  }
  function bytesToWords(b) {
    var w = [];
    for (var i = 0; i + 1 < b.length; i += 2) w.push(b[i] | (b[i + 1] << 8));
    return w;
  }
  function s16(lo, hi) { var v = lo | (hi << 8); return v & 0x8000 ? v - 0x10000 : v; }

  function nomeStato(v) { return STATO_NOMI[v] || ("?" + v); }

  /* Come protocol.describe_event: serve solo al log. */
  function describeEvent(e) {
    if (!e || e.length < 12) return "(evento corto)";
    var kind = e[0], dir = e[1], seq = e[3], mg = e[4], mn = e[5];
    var x = s16(e[6], e[7]), y = s16(e[8], e[9]);
    var pad = function (n) { return ("   " + n).slice(-3); };
    var k = EV_NOMI[kind] || ("?" + kind);
    if (kind === EV.STATUS) return "#" + pad(seq) + " " + k + "  mappa " + mg + "." + mn + " " + nomeStato(dir);
    if (kind === EV.CARD) return "#" + pad(seq) + " " + k + "  mappa " + mg + "." + mn + " chunk " + dir + "/13";
    return "#" + pad(seq) + " " + k + "  mappa " + mg + "." + mn + " (" + x + "," + y + ") " + (DIR_NOMI[dir] || "?");
  }

  /* La sonda pagina 1 (SioSendSonda in payload/main.c), come la stampa client.py. */
  function describeSonda(w) {
    if (!w || w.length < 8) return "";
    var flags = w[4];
    return "vbl " + w[0] + " | emessi " + w[1] +
      " | porta " + (w[2] >> 8) + " prese / " + (w[2] & 0xFF) + " rese" +
      " | latch " + (flags & 1) + " (rese " + (w[3] >> 8) + ", risvegli " + (w[3] & 0xFF) + ")" +
      " | overworld " + ((flags & 2) ? "si" : "NO") + ((flags & 4) ? " (assestamento IN CORSO)" : "") +
      " | stato io " + nomeStato((flags >> 8) & 0xF) + " | sezioni " + ((flags >> 12) & 0xF) +
      " | cb2 0x" + ((w[5] | (w[6] << 16)) >>> 0).toString(16).toUpperCase().padStart(8, "0") +
      // w[7] porta due byte, ENTRAMBI modulo 256 (la sonda e' ferma a 8
      // parole): conta il moto fra una sonda e l'altra, non il totale.
      // SCRUB IE deve salire entrando in lotta - e' la prova che SioShutdown
      // lascia stare i bit del gioco (VCount = motore audio).
      " | RIPARAZIONI " + (w[7] & 0xFF) + " | SCRUB IE " + (w[7] >> 8);
  }

  /* La sonda pagina 2 (SioFillDiag in payload/sio.c). */
  function describeDiag(d) {
    if (!d || d.length < 8) return "";
    var siocnt = d[0], rcnt = d[1], ie = d[2];
    var nostri = ((siocnt & 0x3000) === 0x2000) && (siocnt & 0x4000) && ((rcnt & 0xC000) === 0) && (ie & 0x0080);
    var sveglia = (ie & 0x0040) !== 0;
    var hex = function (v) { return v.toString(16).toUpperCase().padStart(4, "0"); };
    return "SIOCNT " + hex(siocnt) + " RCNT " + hex(rcnt) + " IE " + hex(ie) + " -> " +
      (sveglia ? "!!! IL LINK DEL GIOCO SI E' SVEGLIATO (Timer 3 armato)" : (nostri ? "REGISTRI NOSTRI" : "REGISTRI NON NOSTRI")) +
      " | irq " + d[3] + " | parole " + d[4] + " | errori " + (d[5] >> 8) + ", tx pieno " + (d[5] & 0xFF) +
      " | cb1 riagganci " + d[6] + " | salti hook " + d[7];
  }

  root.GbaSio = {
    SIO: SIO, EV: EV, STATO_NOMI: STATO_NOMI,
    sioFrame: sioFrame, SioDeframer: SioDeframer,
    wordsToBytes: wordsToBytes, bytesToWords: bytesToWords,
    describeEvent: describeEvent, describeSonda: describeSonda, describeDiag: describeDiag,
    nomeStato: nomeStato
  };
})(typeof window !== "undefined" ? window : globalThis);
