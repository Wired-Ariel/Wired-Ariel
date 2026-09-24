/* multiboot.js - il multiboot del GBA in modo MultiPlay 16 bit, nel browser
 * (2026-08-21). E' il port riga per riga di net/mb_multi.py (classe
 * Multiboot): stesse costanti (GBATEK + LinkCableMultiboot.hpp), stessa
 * regola "la coda TX non si svuota mai" (a coda vuota il firmware manda 0x7FFF
 * e fa lo scambio lo stesso), stessa sovrapposizione fra blocchi, stessa
 * leva F-1 per l'attesa di 1/16 s, stessi controlli risposta per risposta.
 *
 * Il trasporto e' un oggetto con QUATTRO promesse:
 *   sendWords(words) -> Promise        readWord(timeoutMs) -> Promise<int|null>
 *   drainRx(ms) -> Promise<int>        setTiming(iterations, label) -> Promise
 * (+ restart() opzionale fra un tentativo e l'altro). Lo implementano
 * CelioDevice (device.js, raw=true) e LinkSimulato (mb_sim.js).
 *
 * Aritmetica: tutto a 32 bit senza segno con >>> 0 e Math.imul (il seme si
 * moltiplica modulo 2^32: in JS un `*` normale perderebbe i bit bassi).
 *
 * SCRITTO ALLA CIECA sull'hardware; provato contro lo slave simulato
 * (mb_test.html, lo stesso autotest di mb_multi.py).
 */
(function (root) {
  "use strict";

  var K = {
    CMD_HANDSHAKE: 0x6200, ACK_HANDSHAKE: 0x7200, CMD_CONFIRM_CLIENTS: 0x6100,
    CMD_SEND_PALETTE: 0x6300, CMD_CONFIRM_HANDSHAKE: 0x6400, ACK_RESPONSE: 0x7300,
    ACK_RESPONSE_MASK: 0xFF00, HANDSHAKE_DATA: 0x11, CMD_ROM_END: 0x0065,
    ACK_ROM_END_WAIT: 0x0074, ACK_ROM_END: 0x0075, CMD_FINAL_CRC: 0x0066,
    HEADER_SIZE: 0xC0, HEADER_PARTS: 0x60, PALETTE_DATA: 0x93,
    CRCC_START_MULTI: 0xFFF8, CRCC_XOR_MULTI: 0xA517, DATA_XOR_MULTI: 0x6465646F,
    SEED_MULTIPLIER: 0x6F646573, MIN_ROM_SIZE: 0x100 + 0xC0, MAX_ROM_SIZE: 256 * 1024,
    CLIENT_BIT: 0x02, DETECTION_TRIES: 64, MAX_ROM_END_TRIES: 300
  };

  function MultibootError(msg) { this.name = "MultibootError"; this.message = msg; }
  MultibootError.prototype = Object.create(Error.prototype);

  function hex(v, n) { return "0x" + (v >>> 0).toString(16).toUpperCase().padStart(n || 4, "0"); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  /* Un passo del checksum di GBATEK, bit per bit su 32 bit (identico a
   * crc_step in Python, a LinkCableMultiboot e al multiboot.py di lorenzooone). */
  function crcStep(crc, data32, xorVal) {
    if (xorVal === undefined) xorVal = K.CRCC_XOR_MULTI;
    crc = crc >>> 0; data32 = data32 >>> 0;
    for (var i = 0; i < 32; i++) {
      var bit = (crc ^ data32) & 1;
      data32 = data32 >>> 1;
      crc = crc >>> 1;
      if (bit) crc = (crc ^ xorVal) >>> 0;
    }
    return crc >>> 0;
  }

  /* Allinea la ROM a 0x10 e controlla i limiti (GBATEK: multiplo di 10h). */
  function preparaRom(data) {
    var len = data.length;
    if (len % 0x10) len += 0x10 - (len % 0x10);
    if (len < K.MIN_ROM_SIZE) throw new MultibootError("ROM troppo piccola: " + data.length + " byte, minimo " + K.MIN_ROM_SIZE);
    if (len > K.MAX_ROM_SIZE) throw new MultibootError("ROM troppo grande: " + data.length + " byte, massimo " + K.MAX_ROM_SIZE);
    var out = new Uint8Array(len);
    out.set(data);
    return out;
  }

  function u32le(rom, off) { return (rom[off] | (rom[off + 1] << 8) | (rom[off + 2] << 16) | (rom[off + 3] << 24)) >>> 0; }

  function Multiboot(link, opts) {
    opts = opts || {};
    this.link = link;
    this.palette = (opts.palette === undefined ? K.PALETTE_DATA : opts.palette) & 0xFF;
    this.waitMs = opts.waitMs === undefined ? 70 : opts.waitMs;
    this.timingFast = opts.timingFast || null;
    this.timingWait = opts.timingWait || null;
    this.log = opts.log || function () {};
    this.progress = opts.progress || function () {};
    this.maxAttempts = opts.maxAttempts || 3;
    this.detectS = opts.detectS || 5.0;
    this.sovrapponi = opts.sovrapponi === undefined ? true : !!opts.sovrapponi;
    this.scorta = opts.scorta || 24;
    this.resetCounters();
  }

  Multiboot.prototype.resetCounters = function () {
    this.sent = 0; this.recv = 0; this.skipped = 0; this.desync = 0; this.restarts = 0;
    this.trace = []; this.rr = 0; this.coda73 = []; this.nonc = []; this._diag = null;
    this._diagHh = 0; this._diagCc = 0;
  };

  Multiboot.prototype._push = function (words) {
    var self = this;
    return this.link.sendWords(words).then(function () { self.sent += words.length; });
  };

  Multiboot.prototype._pull = function (timeoutS, fase) {
    var self = this;
    return this.link.readWord((timeoutS || 3.0) * 1000).then(function (w) {
      if (w === null || w === undefined) {
        throw new MultibootError("nessuna risposta dal GBA entro " + (timeoutS || 3) + " s (fase " + fase + "). " +
          "Il master ha smesso di clockare, oppure il canale e' caduto.");
      }
      self.recv++;
      self.trace.push([fase, w]);
      if (self.trace.length > 60) self.trace.shift();
      return w;
    });
  };

  Multiboot.prototype.dumpTrace = function () {
    if (!this.trace.length) return "";
    var righe = ["ultime risposte del GBA (fase: valore):"], riga = [];
    for (var i = 0; i < this.trace.length; i++) {
      riga.push(this.trace[i][0] + ":" + this.trace[i][1].toString(16).toUpperCase().padStart(4, "0"));
      if (riga.length === 6) { righe.push("  " + riga.join("  ")); riga = []; }
    }
    if (riga.length) righe.push("  " + riga.join("  "));
    return righe.join("\n");
  };

  Multiboot.prototype.diagnosiCrc = function (gba) {
    var d = this._diag;
    if (!d) return "";
    var crcDati = d[0], hh = d[1], cc = d[2];
    var righe = ["diagnosi: crc dei dati in chiaro " + hex(crcDati) + ", cc letto " + hex(cc, 2) + ", hh " + hex(hh, 2) + ", rr letto " + hex(this.rr, 2)];
    var buoni = [];
    for (var rr = 0; rr < 256; rr++) {
      if ((crcStep(crcDati, (hh | (rr << 8) | 0xFF000000 | 0xFF0000) >>> 0) & 0xFFFF) === gba) buoni.push(hex(rr, 2));
    }
    righe.push(buoni.length
      ? "-> con rr = [" + buoni.join(", ") + "] il CRC sarebbe tornato: i DATI erano giusti, e' rr a essere stato letto male."
      : "-> nessun rr avrebbe fatto tornare il CRC: i dati decifrati dal GBA sono diversi dai nostri, quindi e' il SEME (cioe' cc) a essere sbagliato, non rr.");
    return righe.join("\n");
  };

  /* Rimette `word` in coda finche' ce ne sono `target` in volo: e' la riga
   * che tiene in piedi tutto il resto (vedi mb_multi.py, _keep_fed). */
  Multiboot.prototype._keepFed = function (word, target) {
    if (target === undefined) target = this.scorta;
    var inVolo = this.sent - this.recv;
    if (inVolo > (target >> 1)) return Promise.resolve();
    var n = target - inVolo, arr = [];
    for (var i = 0; i < n; i++) arr.push(word);
    return this._push(arr);
  };

  Multiboot.prototype._pullUntil = function (pred, fase, maxSkip, timeoutS) {
    var self = this, tentativi = 0, ultimo = 0;
    function giro() {
      return self._pull(timeoutS, fase).then(function (w) {
        if (pred(w)) return w;
        self.skipped++; ultimo = w;
        if (++tentativi > maxSkip) {
          throw new MultibootError("fase " + fase + ": " + maxSkip + " risposte di fila non attese (ultima " + hex(ultimo) + "). Il flusso e' fuori sincrono.");
        }
        return giro();
      });
    }
    return giro();
  };

  /* 1. 0x6200 ripetuto finche' il child risponde 0x720x. */
  Multiboot.prototype._detect = function () {
    var self = this, scadenza = Date.now() + this.detectS * 1000, visti = {};
    function giro() {
      if (Date.now() >= scadenza) {
        var chiavi = Object.keys(visti);
        if (chiavi.length) {
          var tot = 0; chiavi.forEach(function (k) { tot += visti[k]; });
          chiavi.sort(function (a, b) { return visti[b] - visti[a]; });
          var top = chiavi.slice(0, 4);
          self.log("risposte viste al detect: " + top.map(function (k) { return hex(+k) + " x" + visti[k] + " (" + Math.floor(visti[k] * 100 / tot) + "%)"; }).join(", "));
          var dom = +top[0], ndom = visti[top[0]];
          if ((dom === 0xFFFF || dom === 0x7FFF) && ndom * 10 >= tot * 9) self.log("  -> linea alta e viva: NESSUNO risponde (non e' il pin: e' il GBA che non e' in attesa)");
          else if (dom === 0 && ndom * 10 >= tot * 9) self.log("  -> linea a massa: SD sul pin sbagliato o filo assente");
        }
        throw new MultibootError("il GBA non ha mai risposto 0x7202 al detect.\n" +
          "       Le cause, in ordine di probabilita':\n" +
          "       - il GBA non e' in attesa di multiboot (slot cartuccia VUOTO, e acceso DOPO aver collegato il cavo);\n" +
          "       - il cavo e' nel verso sbagliato (il verso e' marcato);\n" +
          "       - SW1 non e' su 3,3 V.");
      }
      return self._keepFed(K.CMD_HANDSHAKE).then(function () { return self._pull(3.0, "detect"); }).then(function (w) {
        if ((w & 0xFFF0) === K.ACK_HANDSHAKE && (w & 0xF) === K.CLIENT_BIT) {
          self.log("client 1 rilevato (" + hex(w) + ")");
          return;
        }
        visti[w] = (visti[w] || 0) + 1;
        return giro();
      });
    }
    return giro();
  };

  /* 2-4. 0x610y, i 96 half-word di header, 0x6200, 0x620y, n palette, coda. */
  Multiboot.prototype._header = function (rom, nPalette, coda) {
    var self = this;
    var blocco = [K.CMD_CONFIRM_CLIENTS | K.CLIENT_BIT];
    for (var i = 0; i < K.HEADER_PARTS; i++) blocco.push(rom[i * 2] | (rom[i * 2 + 1] << 8));
    blocco.push(K.CMD_HANDSHAKE);
    blocco.push(K.CMD_HANDSHAKE | K.CLIENT_BIT);
    for (var p = 0; p < nPalette; p++) blocco.push(K.CMD_SEND_PALETTE | this.palette);
    blocco = blocco.concat(coda || []);
    var inVolo = this.sent - this.recv;
    return this._push(blocco).then(function () {
      var atteso = (K.HEADER_PARTS << 8) | K.CLIENT_BIT;   // 0x6002
      return self._pullUntil(function (w) { return w === atteso; }, "header", inVolo + 16);
    }).then(function () {
      var remaining = K.HEADER_PARTS - 1;
      function passo() {
        if (remaining <= 0) return Promise.resolve();
        var atteso = (remaining << 8) | K.CLIENT_BIT;
        return self._pull(3.0, "header").then(function (w) {
          if (w !== atteso) throw new MultibootError("header: alla parola " + (K.HEADER_PARTS - remaining) + " il GBA ha risposto " + hex(w) + " invece di " + hex(atteso));
          var p = Promise.resolve();
          // Il periodo del master si allunga PRIMA della coda del blocco (il
          // timing viaggia col PIO insieme alla parola): da qui in poi ogni
          // scambio lascia ~70 ms all'host.
          if (remaining === 10 && self.timingWait) {
            p = self.link.setTiming(self.timingWait, "periodo lungo (" + self.waitMs + " ms) per la coda dell'iniziazione");
          }
          remaining--;
          return p.then(passo);
        });
      }
      return passo();
    }).then(function () {
      return self._pull(3.0, "fine header");
    }).then(function (w) {
      if (w !== K.CLIENT_BIT) throw new MultibootError("fine header: " + hex(w) + " invece di " + hex(K.CLIENT_BIT));
      return self._pull(3.0, "fine header");
    }).then(function (w) {
      if (w !== (K.ACK_HANDSHAKE | K.CLIENT_BIT)) throw new MultibootError("fine header: " + hex(w) + " invece di " + hex(K.ACK_HANDSHAKE | K.CLIENT_BIT));
      // La palette sta nello stesso blocco, in numero FISSO: vale l'ULTIMA
      // risposta 0x73cc (il GBA rigenera client_data a ogni 0x63pp).
      var cc = null, i = 0;
      function letta() {
        if (i >= nPalette) return Promise.resolve();
        i++;
        return self._pull(5.0, "palette").then(function (w) {
          if ((w & K.ACK_RESPONSE_MASK) === K.ACK_RESPONSE) cc = w & 0xFF;
          return letta();
        });
      }
      return letta().then(function () {
        if (cc === null) throw new MultibootError("nessuna delle " + nPalette + " risposte alla palette era 0x73cc: il GBA non era pronto. Alzare n_palette.");
        return cc;
      });
    });
  };

  /* 6-8. 0x64hh, l'attesa di 1/16 s (via F-1), la lunghezza, e le prime
   * parole di dati gia' in coda (sovrapposizione). */
  Multiboot.prototype._handshakeELunghezza = function (cc, romSize, coda, primoDato) {
    var self = this;
    var hh = (K.HANDSHAKE_DATA + cc + 0xFF + 0xFF) & 0xFF;
    var llll = ((romSize - 0x190) / 4) | 0;
    var uu, rrW;
    return this._push([K.CMD_CONFIRM_HANDSHAKE | hh, llll].concat(coda || [])).then(function () {
      return self._pull(5.0, "0x64hh");
    }).then(function (w) {
      uu = w; return self._pull(5.0, "lunghezza");
    }).then(function (w) {
      rrW = w;
      var etichette = [["0x64hh", uu], ["lunghezza", rrW]];
      for (var i = 0; i < 2; i++) {
        if ((etichette[i][1] & K.ACK_RESPONSE_MASK) !== K.ACK_RESPONSE) {
          throw new MultibootError(etichette[i][0] + ": risposta " + hex(etichette[i][1]) + ", attesa 0x73xx. Il flusso e' sfasato: la prossima risposta dei dati non sarebbe " + hex(primoDato) + ".");
        }
      }
      return self.timingFast ? self.link.setTiming(self.timingFast, "ritorno al timing normale") : null;
    }).then(function () {
      var rr = rrW & 0xFF;
      self.rr = rr; self.coda73 = [uu, rrW]; self.nonc = [uu, rrW];
      self._diagHh = hh; self._diagCc = cc;
      self.log("cc " + hex(cc, 2) + "  hh " + hex(hh, 2) + "  uu " + hex(uu) + "  rr " + hex(rr, 2));
      var crcB = (hh | (rr << 8) | (0xFF << 16) | (0xFF << 24)) >>> 0;
      return { hh: hh, crcB: crcB, giaLette: 0 };
    });
  };

  /* Cifra tutta la ROM e calcola il checksum PRIMA di spedire. */
  Multiboot.prototype.preparaDati = function (rom, cc) {
    var parti = (rom.length / 4) | 0;
    var seed = (this.palette | (cc << 8) | (0xFF << 16) | (0xFF << 24)) >>> 0;
    var crcC = K.CRCC_START_MULTI;
    var daMandare = [], attesi = [];
    for (var i = (K.HEADER_SIZE / 4) | 0; i < parti; i++) {
      seed = (Math.imul(seed, K.SEED_MULTIPLIER) + 1) >>> 0;
      var plain = u32le(rom, i * 4);
      var enc = (plain ^ ((0xFE000000 - (i << 2)) >>> 0) ^ seed ^ K.DATA_XOR_MULTI) >>> 0;
      daMandare.push(enc & 0xFFFF);
      daMandare.push(enc >>> 16);
      attesi.push((i << 2) & 0xFFFF);
      attesi.push(((i << 2) + 2) & 0xFFFF);
      crcC = crcStep(crcC, plain);
    }
    return { daMandare: daMandare, attesi: attesi, crcC: crcC };
  };

  /* 9-10. I dati cifrati, poi il CRC finale. */
  Multiboot.prototype._rom = function (daMandare, attesi, crcC, crcB, giaSpedite, giaLette, avanti) {
    var self = this;
    avanti = avanti || 160;
    var totale = daMandare.length, spedite = giaSpedite || 0, lette = giaLette || 0;
    var ultimoAvviso = 0, codaFinale = false;

    function rifornisci() {
      if (spedite < totale && (spedite - lette) < avanti) {
        var blocco = daMandare.slice(spedite, spedite + 32);
        return self._push(blocco).then(function () { spedite += blocco.length; return rifornisci(); });
      }
      if (spedite >= totale && !codaFinale && self.sovrapponi) {
        codaFinale = true;
        var arr = []; for (var i = 0; i < self.scorta; i++) arr.push(K.CMD_ROM_END);
        return self._push(arr);
      }
      return Promise.resolve();
    }

    function giro() {
      if (lette >= totale) return Promise.resolve();
      return rifornisci().then(function () { return self._pull(3.0, "dati"); }).then(function (w) {
        if (w !== attesi[lette]) {
          throw new MultibootError("dati: alla half-word " + lette + " di " + totale + " il GBA ha risposto " + hex(w) + " invece di " + hex(attesi[lette]) + " (scostamento nel flusso: una parola in piu' o in meno)");
        }
        lette++;
        if (lette - ultimoAvviso >= ((totale / 10) | 0) + 1) { ultimoAvviso = lette; self.progress(Math.floor(lette * 100 / totale)); }
        return giro();
      });
    }

    return giro().then(function () {
      self.progress(100);
      crcC = crcC & 0xFFFF;
      self._diag = [crcC, self._diagHh, self._diagCc];
      crcC = crcStep(crcC, crcB) & 0xFFFF;
      var pronto = false, tentativi = 0;
      function fine() {
        if (pronto || tentativi >= K.MAX_ROM_END_TRIES) return Promise.resolve();
        tentativi++;
        return self._keepFed(K.CMD_ROM_END).then(function () { return self._pull(5.0, "fine dati"); }).then(function (w) {
          if (w === K.ACK_ROM_END) pronto = true;
          return fine();
        });
      }
      return fine().then(function () {
        if (!pronto) throw new MultibootError("il GBA non ha mai risposto 0x0075: non si e' mai dichiarato pronto per il CRC finale");
        var inVolo = self.sent - self.recv;
        return self._push([K.CMD_FINAL_CRC, crcC]).then(function () {
          var max = inVolo + 3 * self.scorta + 8, n = 0;
          function crc() {
            if (n >= max) {
              if (crcC === K.ACK_ROM_END) return crcC;
              throw new MultibootError("il GBA non ha mai mandato il suo CRC (solo " + hex(K.ACK_ROM_END) + ")");
            }
            n++;
            return self._pull(5.0, "CRC").then(function (w) {
              if (w === K.ACK_ROM_END) { self.skipped++; return crc(); }
              if (w !== crcC) {
                var d = self.diagnosiCrc(w);
                if (d) self.log(d);
                throw new MultibootError("CRC finale diverso: il GBA dice " + hex(w) + ", noi " + hex(crcC) + ". I dati sono arrivati ma non sono quelli che abbiamo mandato.");
              }
              return crcC;
            });
          }
          return crc();
        });
      });
    });
  };

  /* La corsa: fino a maxAttempts tentativi, ognuno dal detect. */
  Multiboot.prototype.run = function (romBytes) {
    var self = this;
    var rom = preparaRom(romBytes);
    var tentativo = 0, ultimo = null;

    function corsa() {
      tentativo++;
      if (tentativo > self.maxAttempts) {
        throw new MultibootError("multiboot fallito dopo " + self.maxAttempts + " tentativi. Ultimo errore: " + (ultimo ? ultimo.message : "?"));
      }
      self.resetCounters();
      self.restarts = tentativo - 1;
      var pre = Promise.resolve();
      if (tentativo > 1) {
        self.log("tentativo " + tentativo + " di " + self.maxAttempts);
        if (typeof self.link.restart === "function") pre = pre.then(function () { return self.link.restart(); });
        pre = pre.then(function () { return sleep(500); });
      }
      var t0, cc, dati, testa, hh, crcB, giaLette;
      return pre.then(function () { return self.link.drainRx(300); })
        .then(function () { t0 = Date.now(); return self._detect(); })
        .then(function () { return self._header(rom, self.sovrapponi ? 4 : 1, []); })
        .then(function (c) {
          cc = c;
          self.log("handshake ok (client_data " + hex(cc, 2) + ")");
          dati = self.preparaDati(rom, cc);
          testa = self.sovrapponi ? dati.daMandare.slice(0, 4 * self.scorta) : [];
          return self._handshakeELunghezza(cc, rom.length, testa, dati.attesi[0]);
        })
        .then(function (r) {
          hh = r.hh; crcB = r.crcB; giaLette = r.giaLette;
          return self._rom(dati.daMandare, dati.attesi, dati.crcC, crcB, testa.length, giaLette);
        })
        .then(function (crcC) {
          var durata = (Date.now() - t0) / 1000;
          self.log("DONE!  CRC " + hex(crcC));
          return {
            byte: rom.length, durata: durata, paroleInviate: self.sent, risposteLette: self.recv,
            scartate: self.skipped, desync: self.desync, riavvii: self.restarts,
            paroleS: durata ? self.recv / durata : 0, crc: crcC
          };
        })
        .catch(function (e) {
          if (!(e instanceof MultibootError)) throw e;
          self.desync++;
          ultimo = e;
          self.log("FALLITO: " + e.message);
          var tr = self.dumpTrace();
          if (tr) self.log(tr);
          return corsa();
        });
    }
    return corsa();
  };

  root.GbaMultiboot = { K: K, Multiboot: Multiboot, MultibootError: MultibootError, crcStep: crcStep, preparaRom: preparaRom, hex: hex };
})(typeof window !== "undefined" ? window : globalThis);
