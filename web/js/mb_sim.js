/* mb_sim.js - il GBA simulato e il trasporto simulato per il multiboot,
 * port di SlaveSimulato e LinkSimulato di net/mb_multi.py (2026-08-21).
 *
 * Lo slave decifra DAVVERO i dati con lo stesso seme, li confronta con la ROM
 * e calcola il CRC per conto suo: se una costante multiplay fosse sbagliata
 * si vede qui, non dopo 30 s sull'hardware. Il trasporto modella il firmware
 * vero: E' LA LETTURA A CONSUMARE UNO SCAMBIO, e a coda TX vuota parte 0x7FFF
 * (rawRelaySection.cpp:147); fra un push e l'altro il master fa `buco`
 * scambi da solo. Stesso modello di Python, stesse trappole. */
(function (root) {
  "use strict";
  var K = root.GbaMultiboot.K, crcStep = root.GbaMultiboot.crcStep;

  function SlaveSimulato(rom, opts) {
    opts = opts || {};
    this.rom = rom;
    this.cc = opts.cc === undefined ? 0x5A : opts.cc;
    this.uu = opts.uu === undefined ? 0x11 : opts.uu;
    this.rr = opts.rr === undefined ? 0xC3 : opts.rr;
    this.ritardoDetect = opts.ritardoDetect === undefined ? 3 : opts.ritardoDetect;
    this.stato = "detect";
    this.header = [];
    this.headerRimasti = K.HEADER_PARTS;
    this.post = 0;
    this.palette = null; this.hh = null; this.llll = null;
    this.seed = 0; this.crc = K.CRCC_START_MULTI;
    this.parte = (K.HEADER_SIZE / 4) | 0;
    this.metaAlta = false; this.bassa = 0;
    this.ricevuti = [];
    this.endAttese = 2;
    this.crcFinale = null;
    this.errori = [];
  }

  SlaveSimulato.prototype.exchange = function (w) {
    var s = this.stato;
    if (s === "morto") return 0xFFFF;
    if (s === "detect") {
      if (w === K.CMD_HANDSHAKE) {
        if (this.ritardoDetect > 0) { this.ritardoDetect--; return this.ritardoDetect ? 0xFFFF : 0x0000; }
        return K.ACK_HANDSHAKE | K.CLIENT_BIT;
      }
      if (w === (K.CMD_CONFIRM_CLIENTS | K.CLIENT_BIT)) { this.stato = "header"; return K.ACK_HANDSHAKE | K.CLIENT_BIT; }
      return 0xFFFF;
    }
    if (s === "header") {
      this.header.push(w & 0xFF, (w >> 8) & 0xFF);
      var r = (this.headerRimasti << 8) | K.CLIENT_BIT;
      this.headerRimasti--;
      if (this.headerRimasti === 0) this.stato = "post";
      return r;
    }
    if (s === "post") {
      this.post++;
      if (this.post === 1) return K.CLIENT_BIT;
      this.stato = "palette";
      return K.ACK_HANDSHAKE | K.CLIENT_BIT;
    }
    if (s === "palette") {
      if ((w & 0xFF00) === K.CMD_SEND_PALETTE) {
        this.palette = w & 0xFF;
        this.cc = (this.cc * 73 + 41) & 0xFF;       // rigenerato a OGNI 0x63pp, come il GBA vero
        return K.ACK_RESPONSE | this.cc;
      }
      if ((w & 0xFF00) === K.CMD_CONFIRM_HANDSHAKE) {
        this.hh = w & 0xFF;
        var atteso = (K.HANDSHAKE_DATA + this.cc + 0xFF + 0xFF) & 0xFF;
        if (this.hh !== atteso) this.errori.push("hh sbagliato: " + this.hh.toString(16) + " invece di " + atteso.toString(16));
        this.stato = "lunghezza";
        return K.ACK_RESPONSE | this.uu;
      }
      this.stato = "morto";
      return 0xFFFF;
    }
    if (s === "lunghezza") {
      this.llll = w;
      var att = ((this.rom.length - 0x190) / 4) | 0;
      if (w !== att) this.errori.push("lunghezza sbagliata: " + w + " invece di " + att);
      this.seed = (this.palette | (this.cc << 8) | (0xFF << 16) | (0xFF << 24)) >>> 0;
      this.stato = "dati";
      return K.ACK_RESPONSE | this.rr;
    }
    if (s === "dati") {
      if (!this.metaAlta) {
        this.seed = (Math.imul(this.seed, K.SEED_MULTIPLIER) + 1) >>> 0;
        this.bassa = w; this.metaAlta = true;
        return (this.parte << 2) & 0xFFFF;
      }
      var enc = (this.bassa | (w << 16)) >>> 0;
      var plain = (enc ^ ((0xFE000000 - (this.parte << 2)) >>> 0) ^ this.seed ^ K.DATA_XOR_MULTI) >>> 0;
      this.ricevuti.push(plain & 0xFF, (plain >>> 8) & 0xFF, (plain >>> 16) & 0xFF, (plain >>> 24) & 0xFF);
      this.crc = crcStep(this.crc, plain);
      var r2 = ((this.parte << 2) + 2) & 0xFFFF;
      this.parte++; this.metaAlta = false;
      if (this.parte >= ((this.rom.length / 4) | 0)) {
        this.stato = "fine";
        this.crc &= 0xFFFF;
        this.crc = crcStep(this.crc, this._crcB()) & 0xFFFF;
      }
      return r2;
    }
    if (s === "fine") {
      if (w === K.CMD_ROM_END) { if (this.endAttese > 0) { this.endAttese--; return K.ACK_ROM_END_WAIT; } return K.ACK_ROM_END; }
      if (w === K.CMD_FINAL_CRC) { this.stato = "crc"; return K.ACK_ROM_END; }
      return K.ACK_ROM_END;
    }
    if (s === "crc") { this.crcFinale = w; return this.crc; }
    return 0xFFFF;
  };

  SlaveSimulato.prototype._crcB = function () {
    return (this.hh | (this.rr << 8) | (0xFF << 16) | (0xFF << 24)) >>> 0;
  };

  SlaveSimulato.prototype.verifica = function () {
    var att = this.rom.subarray(K.HEADER_SIZE);
    var ok = this.ricevuti.length === att.length, primo = -1;
    if (ok) { for (var i = 0; i < att.length; i++) if (this.ricevuti[i] !== att[i]) { ok = false; primo = i; break; } }
    if (!ok) this.errori.push("dati diversi: " + this.ricevuti.length + " byte ricevuti, " + att.length + " attesi, primo scostamento a " + (primo < 0 ? "?" : primo));
    var hok = this.header.length === K.HEADER_SIZE;
    if (hok) { for (var j = 0; j < K.HEADER_SIZE; j++) if (this.header[j] !== this.rom[j]) { hok = false; break; } }
    if (!hok) this.errori.push("header diverso dall'originale");
    if (this.crcFinale !== this.crc) this.errori.push("CRC: master " + this.crcFinale + ", slave " + this.crc);
    return this.errori;
  };

  function LinkSimulato(slave, opts) {
    opts = opts || {};
    this.slave = slave;
    this.iniettaOgni = opts.iniettaOgni || 0;
    this.timingRif = opts.timingRif || 3700;
    this.periodo = this.timingRif;
    this.buco = opts.buco === undefined ? 1 : opts.buco;
    this.tx = []; this.risposte = [];
    this.scambi = 0; this.idleMandati = 0; this.avviato = false;
  }

  LinkSimulato.prototype._scambia = function (w) { this.scambi++; this.risposte.push(this.slave.exchange(w)); };

  LinkSimulato.prototype.sendWords = function (words) {
    if (this.avviato) {
      var n = Math.floor(this.buco * this.timingRif / this.periodo);
      for (var i = 0; i < n; i++) {
        if (this.tx.length) this._scambia(this.tx.shift());
        else { this.idleMandati++; this._scambia(0x7FFF); }
      }
    }
    for (var k = 0; k < words.length; k++) this.tx.push(words[k] & 0xFFFF);
    this.avviato = true;
    return Promise.resolve();
  };

  LinkSimulato.prototype.readWord = function () {
    if (!this.risposte.length) {
      if (this.iniettaOgni && (this.scambi + 1) % this.iniettaOgni === 0) this._scambia(0x7FFF);
      else if (this.tx.length) this._scambia(this.tx.shift());
      else { this.idleMandati++; this._scambia(0x7FFF); }
    }
    return Promise.resolve(this.risposte.shift());
  };

  LinkSimulato.prototype.drainRx = function () { var n = this.risposte.length; this.risposte = []; return Promise.resolve(n); };
  LinkSimulato.prototype.setTiming = function (it) { this.periodo = Math.max(1, it | 0); return Promise.resolve(); };

  root.GbaMbSim = { SlaveSimulato: SlaveSimulato, LinkSimulato: LinkSimulato };
})(typeof window !== "undefined" ? window : globalThis);
