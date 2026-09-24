/* app.js - il pannello gen3-poke-multiplayer nel browser (2026-08-23).
 *
 * Tiene insieme i pezzi: device.js (il Pico via WebUSB), multiboot.js (il
 * gioco nel GBA), relay.js + bridge.js (la partita via relay WebSocket), e
 * disegna lo stesso pannello di net/pannello.html - ma qui non c'e' nessun
 * processo dietro: tutto vive nella pagina. Le impostazioni stanno in
 * localStorage; i default li puo' proporre config.js (generato da
 * tools/prepara-sito-web.ps1) cosi' chi prova trova relay e stanza gia'
 * scritti.
 *
 * I tre passi, nell'ordine: 0 Collega il Pico (WebUSB vuole un click),
 * 1 Carica il gioco nel GBA (multiboot, poi F-4 e il canale si riapre da
 * solo), 2 Gioca (relay + bridge). Ogni passo dice nel registro cosa e'
 * successo, con le stesse righe di client.py.
 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var CHIAVE = "passotile.web";
  // IT / EN (2026-09-24): la lingua la decide la pagina (gioca.html o gioca-en.html),
  // qui si sceglie solo quale delle due frasi mostrare. Le righe tecniche che
  // arrivano dai moduli (bridge, device, multiboot) restano in italiano.
  var EN = document.documentElement.lang === "en";
  function L(it, en) { return EN ? en : it; }
  var STATI_EN = { lotta: "battle", dialogo: "dialogue", zaino: "bag", squadra: "party" };
  function nomeStato(v) { var n = GbaSio.nomeStato(v); return EN && STATI_EN[n] ? STATI_EN[n] : n; }
  var DEF = (window.PASSOTILE_DEFAULTS || {});

  var cfg = null;            // {relay, stanza, peer, timing, cavo}
  var dev = null;            // CelioDevice in passthrough (partita)
  var link = null;           // RelayLink
  var bridge = null;         // OwlBridge
  // SPETTATORE (2026-08-24): partita senza Pico e senza GBA. Serve a due cose
  // vere - guardare gli amici sulla mappa live da un PC che il GBA non ce l'ha,
  // e far funzionare QUALCOSA su Firefox, dove WebUSB non esiste e non
  // esistera' (Mozilla l'ha marcata "harmful" nella sua standards position).
  // Il bridge gira identico: con device = null, _deliverOne esce subito.
  var spettatore = false;
  var picoNome = "";
  // Il Pico staccato a partita in corso (2026-08-30): `dev` torna null come
  // quando non si e' mai collegato, ma le due situazioni vanno dette in modo
  // diverso - "non collegato" fa pensare a un passo da fare, "SCOLLEGATO" dice
  // che qualcosa e' successo adesso.
  var picoPerso = false;
  var caricato = false;      // multiboot riuscito in questa pagina
  var multibootInCorso = false;
  var inPartita = false;
  var sondaVista = false;
  var romScelta = null;
  var righe = 0, MAX_RIGHE = 1200;
  // Il registro COMPLETO della sessione: il DOM taglia a MAX_RIGHE per non
  // appesantire la pagina, ma la copia e il download devono avere tutto.
  // Testo puro, una stringa per riga: qualche MB al massimo, il browser regge.
  var registroCompleto = [];
  var ultimeParole = 0, ultimoT = 0, paroleS = 0;
  var ultimoSeq = -1, buchi = 0;

  /* --- registro ------------------------------------------------------------ */
  function log(testo, chi, grave) {
    var el = $("log");
    var d = document.createElement("span");
    var t = new Date();
    var hh = ("0" + t.getHours()).slice(-2) + ":" + ("0" + t.getMinutes()).slice(-2) + ":" + ("0" + t.getSeconds()).slice(-2);
    chi = chi || "gba";
    if (!grave) grave = /!!!|FALLIT|ERRORE|non riesco|NESSUN|CADUTO|NON RAGGIUNGIBILE/i.test(testo);
    d.className = "r " + chi + (grave ? " err" : "");
    var sp = document.createElement("span"); sp.className = "chi"; sp.textContent = hh + " [" + chi + "]";
    d.appendChild(sp);
    d.appendChild(document.createTextNode(" " + testo.replace(/^\[[a-z +]+\]\s*/i, "")));
    el.appendChild(d);
    registroCompleto.push(hh + " [" + chi + "] " + testo.replace(/^\[[a-z +]+\]\s*/i, ""));
    if (++righe > MAX_RIGHE) { el.removeChild(el.firstChild); righe--; }
    if ($("autoscroll").checked) el.scrollTop = el.scrollHeight;
  }
  function logDa(riga) {
    // le righe dei moduli arrivano gia' con il prefisso [usb ] / [mb  ] / [rete ] / [stato ] / [club ]
    var m = /^\[([a-z +]+)\]/i.exec(riga);
    var chi = m ? m[1].trim() : "gba";
    if (chi === "gba+") chi = "gba";
    log(riga, chi);
  }
  function errore(msg) { log(L("ERRORE: ", "ERROR: ") + msg, "usb", true); esito("esito-azione", msg, "male"); }
  function esito(id, testo, classe) { var e = $(id); e.textContent = testo || ""; e.className = "esito" + (classe ? " " + classe : ""); }

  /* --- impostazioni ------------------------------------------------------- */
  function caricaCfg() {
    var salvate = {};
    try { salvate = JSON.parse(localStorage.getItem(CHIAVE) || "{}") || {}; } catch (e) { salvate = {}; }
    cfg = {
      relay: salvate.relay || DEF.relay || "",
      stanza: salvate.stanza || DEF.stanza || 1,
      peer: salvate.peer || (1 + Math.floor(Math.random() * 65000)),
      // IL PEER CON CUI GIOCHI ALTROVE (2026-08-26). Serve solo da
      // spettatore: e' il numero del TUO client - quello dentro mGBA o
      // accanto al GBA - e serve a riconoscerti sulla Mappa live, dove
      // diventi il pallino azzurro "tu" invece di un amico verde qualunque.
      // 0 = non impostato. Non sta nel config.js del sito apposta: e' un dato
      // personale, non una scelta di chi pubblica la pagina.
      peerGioco: salvate.peerGioco || 0,
      timing: salvate.timing || 7400,
      cavo: salvate.cavo || "auto"
    };
    if (!salvate.peer) salvaCfg();   // il numero di peer nasce una volta e resta
    applicaCfg();
  }
  function salvaCfg() { try { localStorage.setItem(CHIAVE, JSON.stringify(cfg)); } catch (e) { /* niente */ } }
  function applicaCfg() {
    $("relay").value = cfg.relay; $("stanza").value = cfg.stanza; $("peer").value = cfg.peer;
    $("peer-gioco").value = cfg.peerGioco || "";
    $("timing").value = cfg.timing; $("cavo").value = cfg.cavo;
  }
  function leggiCfg() {
    var stanza = parseInt($("stanza").value, 10), peer = parseInt($("peer").value, 10), timing = parseInt($("timing").value, 10);
    var grezzoGioco = $("peer-gioco").value.trim();
    var peerGioco = grezzoGioco === "" ? 0 : parseInt(grezzoGioco, 10);
    if (!(stanza >= 1 && stanza <= 65535)) return L("la stanza va da 1 a 65535", "the room must be between 1 and 65535");
    if (!(peer >= 1 && peer <= 65534)) return L("il numero di peer va da 1 a 65534", "the peer number must be between 1 and 65534");
    if (!(peerGioco === 0 || (peerGioco >= 1 && peerGioco <= 65534)))
      return L("il peer di gioco va da 1 a 65534 (oppure vuoto)", "the game peer must be between 1 and 65534 (or empty)");
    // Due ruoli con lo stesso numero sono una trappola: il relay ora sa
    // tenerli separati (spettatore e giocatore non si espellono a vicenda),
    // ma sulla mappa saresti insieme "tu" e "un amico". Meglio non arrivarci.
    if (peerGioco && peerGioco === peer)
      return L("il tuo peer di gioco deve essere DIVERSO da quello di questa pagina", "your game peer must be DIFFERENT from this page's peer");
    if (!(timing >= 1000 && timing <= 60000)) return L("il timing va da 1000 a 60000", "the timing must be between 1000 and 60000");
    cfg = { relay: $("relay").value.trim(), stanza: stanza, peer: peer, peerGioco: peerGioco,
            timing: timing, cavo: $("cavo").value };
    salvaCfg();
    return null;
  }
  function salva() {
    var err = leggiCfg();
    esito("esito-config", err || L("Impostazioni salvate.", "Settings saved."), err ? "male" : "bene");
    if (!err) { disegnaStato(); setTimeout(function () { esito("esito-config", ""); }, 3000); }
  }

  /* --- il Pico ------------------------------------------------------------ */
  function opzioniDevice() {
    return {
      timing: cfg.timing > 0 ? cfg.timing : null,
      cable: cfg.cavo,
      log: logDa,
      onEvent: suEventoDalGba,
      onSonda: function (w) { sondaVista = true; if ($("mostra-sonda").checked) log("[gba ] " + GbaSio.describeSonda(w), "gba"); },
      onDiag: function (w) { if ($("mostra-sonda").checked) log("[gba+] " + GbaSio.describeDiag(w), "gba"); },
      onPing: function (w) { log("[gba ] PING eco " + JSON.stringify(w), "gba"); },
      onStatus: function () {},
      onLost: suPicoPerso
    };
  }

  /* L'ADATTATORE SPARITO (2026-08-30). Tre cose, in quest'ordine: si congeda
   * dagli amici (cosi' il nostro avatar sparisce SUBITO invece che al timeout
   * del relay, che era il motore del va-e-vieni), si dimentica il device
   * cosi' la spia smette di mentire, e si ridisegna lo stato. La stanza
   * NON si chiude: ricollegando il Pico si riprende da dov'era. */
  function suPicoPerso(perche) {
    if (bridge) bridge.congeda(perche || "adattatore scollegato");
    dev = null;
    picoPerso = true;
    inPartita = false;
    esito("esito-azione", L("Il Pico si e' scollegato. Ricollegalo e premi di nuovo "
      + "'Collega il Pico': il GBA non va spento, il programma vive nella sua RAM.",
      "The Pico got disconnected. Plug it back in and press 'Connect the Pico' again: "
      + "don't switch the GBA off, the program lives in its RAM."), "male");
    disegnaStato();
  }
  function suEventoDalGba(e) {
    var seq = e[3];
    if (ultimoSeq >= 0 && seq !== ((ultimoSeq + 1) & 0xFF)) buchi++;
    ultimoSeq = seq;
    if ($("mostra-eventi").checked) log("[gba ] " + GbaSio.describeEvent(e), "gba");
    if (bridge && inPartita) bridge.onDeviceEvent(e);
  }

  // Gli errori WebUSB arrivano in inglese e senza rimedio: qui si traducono
  // I TRE CASI CHE CAPITANO DAVVERO, ognuno con la sua cura. Era il motivo
  // del "il sito non mi rileva": il messaggio grezzo non diceva cosa fare.
  function spiegaErroreUsb(e, fase) {
    var m = (e && e.message) || String(e);
    if (e && e.name === "NotFoundError") {
      return L("nessun Pico scelto. Se la finestra era VUOTA: 1) il cavo USB deve essere " +
             "dati, non solo ricarica; 2) il Pico non deve essere in modalita' BOOTSEL " +
             "(scollega e ricollega senza premere il bottone); 3) su Windows serve il " +
             "driver WinUSB, installato una volta con Zadig (device 2FE3:000A).",
             "no Pico selected. If the window was EMPTY: 1) the USB cable must carry " +
             "data, not just power; 2) the Pico must not be in BOOTSEL mode " +
             "(unplug and replug it without pressing the button); 3) on Windows you need the " +
             "WinUSB driver, installed once with Zadig (device 2FE3:000A).");
    }
    if (e && (e.name === "SecurityError" || e.name === "NetworkError") && fase === "apri") {
      return L("il Pico si vede ma non si apre (" + m + "). Quasi sempre e' una di due: " +
             "1) manca il driver WinUSB - installalo con Zadig (device 2FE3:000A, 'Install driver'); " +
             "2) un altro programma lo sta usando - chiudi il pannello Python, client.py o un'altra " +
             "scheda del sito, poi riprova.",
             "the Pico is visible but won't open (" + m + "). It's almost always one of two things: " +
             "1) the WinUSB driver is missing - install it with Zadig (device 2FE3:000A, 'Install driver'); " +
             "2) another program is using it - close the Python panel, client.py or another " +
             "tab of this site, then try again.");
    }
    if (/claim/i.test(m)) {
      return L("l'interfaccia USB e' gia' occupata: il pannello Python (o un'altra scheda del " +
             "sito) sta parlando col Pico. Chiudilo e ripremi Collega.",
             "the USB interface is already in use: the Python panel (or another tab of this " +
             "site) is talking to the Pico. Close it and press Connect again.");
    }
    return m;
  }

  async function collegaPico() {
    if (!CelioDevice.supportato()) { errore(L("questo browser non ha WebUSB: serve Chrome o Edge.", "this browser has no WebUSB: you need Chrome or Edge.")); return; }
    if (dev) return;
    var fase = "scegli";
    try {
      var d0 = new CelioDevice(opzioniDevice());
      var d = await d0.riusa();
      if (!d) d = await d0.request();
      picoNome = (d.productName || "Pico") + (d.serialNumber ? " · " + d.serialNumber : "");
      log("[usb ] device " + d.productName + " (" + d.manufacturerName + ") serial " + d.serialNumber, "usb");
      fase = "apri";
      await d0.open();
      dev = d0;
      picoPerso = false;
      // Il bridge di una partita gia' avviata tiene il device VECCHIO, quello
      // marcato `perso`: senza questo passaggio la presenza resterebbe
      // soppressa per sempre dopo un ricollega (vedi sorgenteViva).
      if (bridge) bridge.cambiaDevice(d0);
      esito("esito-azione", L("Pico collegato: il canale e' aperto.", "Pico connected: the channel is open."), "bene");
    } catch (e) {
      errore(spiegaErroreUsb(e, fase));
      if (d0) { try { await d0.close(); } catch (e2) { /* niente */ } }
      dev = null;
    }
    disegnaTutto();
  }

  async function scollegaPico() {
    if (inPartita) fermaPartita();
    if (dev) { await dev.close(); dev = null; log("[usb ] scollegato", "usb"); }
    disegnaTutto();
  }

  async function riavviaPico() {
    if (!dev) return;
    if (inPartita) fermaPartita();
    log("[usb ] riavvio del Pico (F-4): sparisce e ricompare, poi riapro il canale da solo", "usb");
    var d = dev; dev = null;
    await d.reboot();
    disegnaTutto();
    await riapriDopoRiavvio("riavvio");
  }

  /* Dopo un F-4 il device rienumera: se il browser ricorda il permesso,
   * getDevices() lo ritrova e si riapre senza click. Altrimenti si chiede. */
  async function riapriDopoRiavvio(motivo) {
    var ultimoErrore = null;
    for (var i = 0; i < 8; i++) {
      await new Promise(function (r) { setTimeout(r, 700); });
      try {
        var d0 = new CelioDevice(opzioniDevice());
        var d = await d0.riusa();
        if (!d) continue;
        await d0.open();
        dev = d0;
        log("[usb ] canale riaperto dopo il " + motivo + " (" + (i + 1) + " tentativi)", "usb");
        disegnaTutto();
        return true;
      } catch (e) { ultimoErrore = e; /* si riprova */ }
    }
    // il MOTIVO dell'ultimo fallimento va nel registro: 8 tentativi muti
    // rendevano il problema indiagnosticabile dal log
    log("[usb ] dopo il " + motivo + " il Pico non e' ricomparso da solo"
        + (ultimoErrore ? " (ultimo errore: " + spiegaErroreUsb(ultimoErrore, "apri") + ")" : "")
        + L(": premi 'Collega il Pico'", ": press 'Connect the Pico'"), "usb", true);
    disegnaTutto();
    return false;
  }

  /* --- il multiboot --------------------------------------------------------- */
  function leggiRom(file) {
    return new Promise(function (ok, ko) {
      var r = new FileReader();
      r.onload = function () { ok(new Uint8Array(r.result)); };
      r.onerror = function () { ko(r.error); };
      r.readAsArrayBuffer(file);
    });
  }
  async function romDelSito() {
    // Lo stub dipende dalla cartuccia (2026-09-24): quello italiano accetta solo
    // BPEI, quello inglese solo BPEE (con la cartuccia sbagliata resta rosso).
    var tentativi = romLua() === "usa"
      ? ["mbstub-usa.gba", "../hw/mbstub/build/mbstub-usa.gba"]
      : [DEF.mbstub || "mbstub.gba", "../hw/mbstub/build/mbstub.gba"];
    for (var i = 0; i < tentativi.length; i++) {
      try {
        var resp = await fetch(tentativi[i], { cache: "no-store" });
        if (resp.ok) {
          var b = new Uint8Array(await resp.arrayBuffer());
          if (b.length > 0x200) { log("[mb  ] programma: " + tentativi[i] + " (" + b.length + " byte)", "mb"); return b; }
        }
      } catch (e) { /* prossimo */ }
    }
    throw new Error(L("mbstub.gba non trovato accanto alla pagina (ne' in ../hw/mbstub/build/): scegli il file a mano con '.gba diverso'", "mbstub.gba not found next to the page (nor in ../hw/mbstub/build/): pick the file by hand with 'Different .gba'"));
  }

  async function caricaGioco() {
    if (multibootInCorso) return;
    if (!CelioDevice.supportato()) { errore(L("questo browser non ha WebUSB: serve Chrome o Edge.", "this browser has no WebUSB: you need Chrome or Edge.")); return; }
    if (inPartita) fermaPartita();
    multibootInCorso = true;
    disegnaTutto();
    var raw = null;
    try {
      if (!romScelta) romScelta = await romDelSito();
      if (dev) { await dev.close(); dev = null; log("[usb ] canale passthrough chiuso: il multiboot vuole il modo parola grezza", "usb"); }
      raw = new CelioDevice({ timing: cfg.timing, cable: cfg.cavo, raw: true, log: logDa });
      var d = await raw.riusa();
      if (!d) d = await raw.request();
      picoNome = (d.productName || "Pico") + (d.serialNumber ? " · " + d.serialNumber : "");
      await raw.open();
      var buttate = await raw.drainRx(500);
      if (buttate) log("[mb  ] " + buttate + " parole di rumore buttate prima di iniziare", "mb");
      var t = cfg.timing || 3700;
      var mb = new GbaMultiboot.Multiboot(raw, {
        timingFast: t, timingWait: Math.max(t, Math.floor(70 * 1000 / 0.54)), waitMs: 70,
        log: function (r) { log("[mb  ] " + r, "mb"); },
        progress: function (pc) { esito("esito-azione", L("trasferimento ", "transfer ") + pc + "%"); }
      });
      esito("esito-azione", L("in attesa del GBA (slot VUOTO, acceso dopo il cavo)...", "waiting for the GBA (EMPTY slot, switched on after plugging the cable)..."));
      var st = await mb.run(romScelta);
      log("[mb  ] byte " + st.byte + " | durata " + st.durata.toFixed(1) + " s | parole " + st.paroleInviate + " | risposte " + st.risposteLette + " (" + st.paroleS.toFixed(0) + " parole/s) | scartate " + st.scartate + " | CRC " + GbaMultiboot.hex(st.crc), "mb");
      caricato = true;
      esito("esito-azione", L("FATTO in " + st.durata.toFixed(1) + " s: schermo del GBA ROSSO. Riavvio il Pico (F-4) e riapro il canale...", "DONE in " + st.durata.toFixed(1) + " s: GBA screen RED. Restarting the Pico (F-4) and reopening the channel..."), "bene");
      // Mezzo secondo di QUIETE prima della F-4 (2026-08-25): il DONE arriva
      // quando noi abbiamo letto il CRC, ma il BIOS del GBA sta ancora
      // saltando nel programma - e il riavvio del Pico fa sobbalzare le linee
      // del cavo. E' il sospettato del "DONE ma niente schermo rosso" (3/3
      // dal sito quella sera; in emulatore lo stesso stub arriva sempre al
      // rosso). Identico a mb_multi.py: si toccano insieme.
      await new Promise(function (r) { setTimeout(r, 500); });
      await raw.reboot(); raw = null;
      log(L("[mb  ] Pico riavviato (F-4). Inserisci la cartuccia: rosso -> giallo -> verde -> gioco.", "[mb  ] Pico restarted (F-4). Insert the cartridge: red -> yellow -> green -> game."), "mb");
      multibootInCorso = false;
      disegnaTutto();
      await riapriDopoRiavvio("multiboot");
      esito("esito-azione", caricato && dev ? L("Programma nel GBA e canale aperto. Inserisci la cartuccia, poi Gioca.", "Program in the GBA and channel open. Insert the cartridge, then Play.") : L("Programma nel GBA. Premi 'Collega il Pico', inserisci la cartuccia, poi Gioca.", "Program in the GBA. Press 'Connect the Pico', insert the cartridge, then Play."), "bene");
    } catch (e) {
      errore("multiboot: " + e.message.split("\n")[0]);
      if (raw) { try { await raw.close(); } catch (e2) { /* niente */ } }
    } finally {
      multibootInCorso = false;
      disegnaTutto();
    }
  }

  /* --- la mappa live -------------------------------------------------------- */
  //
  // Le posizioni vanno alla scheda della mappa (mappa.html) via
  // BroadcastChannel, nello STESSO formato di /api/posizioni del pannello
  // Python - cosi' la mappa non sa e non deve sapere da quale dei due
  // pannelli sta guardando. Niente rete in piu': sono gli stessi eventi che
  // passano gia' di qui.
  var canalePos = null;
  try {
    canalePos = new BroadcastChannel("passotile-posizioni");
    // La mappa aperta DOPO il pannello chiede, e le si risponde subito invece
    // di lasciarla vuota fino al battito successivo.
    canalePos.onmessage = function (ev) { if (ev.data && ev.data.chiedo) inviaPosizioni(); };
  } catch (e) { /* browser senza BroadcastChannel: la mappa restera' vuota */ }

  function inSecondi(p) {
    if (!p) return null;
    var o = {};
    for (var k in p) o[k] = p[k];
    o.t = (p.visto || Date.now()) / 1000;   // il payload del pannello Python usa i secondi
    return o;
  }

  /* --- lo script per chi gioca in emulatore -------------------------------
   *
   * Chi usa mGBA non puo' usare questa pagina per giocare (dal browser non si
   * inietta dentro un emulatore), ma la stanza deve poterla scegliere QUI -
   * altrimenti l'unico modo e' modificargli lo script a mano, ogni volta.
   * Quindi: si prende il template gia' pubblicato accanto al sito, si
   * riscrivono le tre righe della configurazione e lo si scarica pronto.
   *
   * Le tre righe sono un CONTRATTO con build.ps1, che le emette una per riga
   * (RELAY_URL / RELAY_ROOM / RELAY_PEER). Se un giorno cambia il formato, qui
   * si deve FALLIRE RUMOROSAMENTE invece di scaricare un file che sembra
   * giusto e non lo e': un utente non ha modo di accorgersene, e il sintomo
   * sarebbe "non ci vediamo" senza nessuna causa visibile.
   */
  // Un modello per ROM (2026-09-24): Smeraldo italiano (BPEI) o Emerald inglese
  // USA/Europa (BPEE). I due script sono uguali tranne i simboli del gioco, e
  // ognuno rifiuta la ROM dell'altro con un messaggio chiaro.
  var TEMPLATE_LUA = { it: "passotile-emulatore.lua", usa: "passotile-emulatore-usa.lua" };
  var CHIAVE_ROM = "passotile.romlua";
  function romLua() { var s = $("rom-lua"); return s && s.value === "usa" ? "usa" : "it"; }

  function configuraLua(testo, url, stanza, peer) {
    var righe = [
      { nome: "RELAY_URL", re: /^RELAY_URL\s*=.*$/m, val: 'RELAY_URL = "' + url + '"' },
      { nome: "RELAY_ROOM", re: /^RELAY_ROOM\s*=.*$/m, val: "RELAY_ROOM = " + stanza },
      { nome: "RELAY_PEER", re: /^RELAY_PEER\s*=.*$/m, val: "RELAY_PEER = " + peer }
    ];
    for (var i = 0; i < righe.length; i++) {
      if (!righe[i].re.test(testo)) {
        throw new Error("template non compatibile: manca la riga " + righe[i].nome +
                        " (il sito e lo script sono di due versioni diverse)");
      }
      testo = testo.replace(righe[i].re, righe[i].val);
    }
    return testo;
  }

  /* wss:// -> ws://: il Lua di mGBA non ha TLS. Si dice, non si fa di
   * nascosto: chi legge deve sapere che quel canale non e' cifrato. */
  function relayPerLua(url) {
    if (/^wss:\/\//i.test(url)) return { url: "ws://" + url.slice(6), degradato: true };
    if (/^ws:\/\//i.test(url)) return { url: url, degradato: false };
    return { url: "ws://" + url.replace(/^https?:\/\//i, ""), degradato: false };
  }

  async function scaricaLua() {
    var err = leggiCfg();
    if (err) { errore(err); return; }
    var r = relayPerLua(cfg.relay);
    try {
      var rom = romLua();
      var resp = await fetch(TEMPLATE_LUA[rom], { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      var testo = await resp.text();
      // peer 0 = lo sorteggia il Lua all'avvio: nessuno deve coordinare numeri.
      // IL PEER LO FISSA IL SITO, E SE LO RICORDA (2026-08-26).
      //
      // Prima qui si scriveva 0, cioe' "lo sorteggia il Lua all'avvio": comodo
      // per chi gioca, ma rendeva IMPOSSIBILE compilare il campo "il tuo
      // numero di gioco" - quel numero lo sapeva solo il log di mGBA, e senza
      // di esso sulla mappa ti vedevi come "amico" invece che come "tu".
      // (Due comodita' giuste che si annullavano a vicenda: e' successo
      // davvero, ed e' il difetto che Lain ha visto.)
      //
      // Adesso il numero lo sceglie QUESTA pagina: lo scrive nello script E lo
      // salva come peerGioco, cosi' le due cose combaciano per costruzione e
      // non c'e' niente da copiare a mano. Resta modificabile nel campo.
      var peerGioco = cfg.peerGioco;
      if (!peerGioco || peerGioco === cfg.peer) {
        do { peerGioco = 1 + Math.floor(Math.random() * 65000); } while (peerGioco === cfg.peer);
        cfg.peerGioco = peerGioco;
        salvaCfg();
        applicaCfg();
      }
      var out = configuraLua(testo, r.url, cfg.stanza, peerGioco);
      var a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([out], { type: "text/plain" }));
      var nomeFile = "gen3-poke-multiplayer-" + L("stanza", "room") + cfg.stanza + (rom === "usa" ? "-eng" : "-ita") + ".lua";
      a.download = nomeFile;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 10000);
      log("[lua  ] scaricato " + nomeFile + " (ROM " + (rom === "usa" ? "inglese" : "italiana") + "): stanza " +
          cfg.stanza + ", relay " + r.url + ", peer " + peerGioco +
          " (segnato anche qui come 'tuo numero di gioco': sulla mappa sarai «tu»)" +
          (r.degradato ? " (relay IN CHIARO: il Lua di mGBA non ha TLS)" : ""), "rete");
      // L'AVVISO CHE EVITA UN'ORA AL TELEFONO. Il Lua parla solo ws:// in
      // chiaro, e un relay servito da nginx dietro HTTPS di solito risponde
      // 301: da qui non lo possiamo verificare (un browser su https non puo'
      // aprire una connessione in chiaro), quindi lo si DICE, e si dice anche
      // come suona il guasto - cosi' chi lo incontra lo riconosce.
      if (r.degradato) {
        log("[lua  ] ATTENZIONE: il tuo relay e' https, e lo script lo usera' in chiaro (" +
            r.url + "). Se quell'indirizzo non risponde anche in chiaro, in mGBA leggerai " +
            "\"il relay ha rifiutato l'upgrade: HTTP/1.1 301\": in quel caso serve un " +
            "endpoint ws:// aperto sul server, oppure un relay in casa.", "attesa");
      }
      esito("esito-azione", L("Script scaricato: caricalo in mGBA con Tools > Scripting > Load script.", "Script downloaded: load it in mGBA with Tools > Scripting > Load script.") +
            (r.degradato ? L(" (Il relay dev'essere raggiungibile anche in chiaro: vedi il registro.)", " (The relay must also be reachable unencrypted: see the log.)") : ""), "bene");
    } catch (e) {
      // Meglio nessun file che un file sbagliato.
      log("[lua  ] non sono riuscito a preparare lo script: " + e.message, "no");
      errore(L("script per l'emulatore non disponibile: ", "emulator script not available: ") + e.message);
    }
  }

  function inviaPosizioni() {
    if (!canalePos || !bridge) return;
    var s = bridge.snapshot();
    var amici = {};
    s.amici.forEach(function (a) { amici[a.peerId] = inSecondi(a.pos); });
    var ora = Date.now() / 1000;
    var ioPos = inSecondi(s.posIo), ioPeer = cfg.peer;
    // DA SPETTATORE, "TU" SEI IL TUO GIOCATORE. Senza device non esiste una
    // posIo (la scrive solo l'evento che arriva dal GBA), quindi chi gioca in
    // emulatore si vedrebbe come un amico verde qualunque in mezzo agli
    // altri. Se ha dichiarato il suo peer di gioco, quello diventa "io": la
    // mappa lo disegna azzurro senza che mappa.html cambi di una riga (il
    // contratto e' `io` + `peer` nella vista, vedi net/mappa.html).
    if (spettatore && cfg.peerGioco && amici[cfg.peerGioco]) {
      ioPos = amici[cfg.peerGioco];
      ioPeer = cfg.peerGioco;
      delete amici[cfg.peerGioco];   // niente doppione verde sotto l'azzurro
    }
    try {
      canalePos.postMessage({
        t: ora,
        viste: [{ peer: ioPeer, t: ora, io: ioPos, amici: amici }]
      });
    } catch (e) { /* niente */ }
  }

  // La versione GUIDATA DAGLI EVENTI: il bridge la chiama a ogni posizione
  // nuova (onPos). E' lei che tiene viva la Mappa live anche quando questa
  // scheda e' in background - i setInterval li' vengono rallentati fino a un
  // tick al minuto, gli eventi di rete no.
  //
  // IL FRENO NON BUTTA VIA NIENTE (2026-08-26). Prima era `se sono passati
  // meno di 150 ms, esci`: l'aggiornamento veniva SCARTATO e non tornava piu'.
  // Due conseguenze, misurate: correndo (un passo ogni ~133 ms) passava
  // meta' dei passi, in bici (~67 ms) un quarto - e la mappa, che riceve
  // POSIZIONI e non passi, non poteva piu' sapere dei tile intermedi: li
  // mostrava come una scivolata unica, che e' esattamente il "salta come una
  // pedina" di un tempo. E anche a piedi l'ULTIMA posizione prima di fermarsi
  // poteva sparire, lasciando l'avatar indietro di un tile fino al SYNC.
  //
  // Ora il freno RITARDA invece di scartare: se arriva troppo presto si
  // programma l'invio alla scadenza, quindi l'ultimo stato non si perde mai.
  // Il periodo scende a 80 ms, che a 12 messaggi al secondo su un
  // BroadcastChannel non e' niente (sono poche centinaia di byte) e copre
  // anche la corsa; la bici resta sotto-campionata di suo, e per quella la
  // cura sta nella mappa, che ora anima i salti brevi come passi veri.
  var FRENO_POS_MS = 80;
  var ultimoInvioPos = 0, posInCoda = null;
  function inviaPosizioniEvento() {
    var ora = Date.now();
    var manca = FRENO_POS_MS - (ora - ultimoInvioPos);
    if (manca <= 0) {
      if (posInCoda) { clearTimeout(posInCoda); posInCoda = null; }
      ultimoInvioPos = ora;
      inviaPosizioni();
      return;
    }
    if (posInCoda) return;              // gia' programmato: sara' l'ultimo stato
    posInCoda = setTimeout(function () {
      posInCoda = null;
      ultimoInvioPos = Date.now();
      inviaPosizioni();
    }, manca);
  }

  function apriMappa() {
    window.open("mappa.html", "passotile-mappa");
    // La mappa chiede da sola appena parte; questo copre il caso in cui la
    // scheda fosse gia' aperta da prima.
    setTimeout(inviaPosizioni, 500);
  }

  /* --- la partita ----------------------------------------------------------- */
  function avviaPartita(soloGuarda) {
    var err = leggiCfg();
    if (err) { errore(err); return; }
    if (!cfg.relay) { errore(L("scrivi il relay nelle impostazioni (wss://host/ws)", "enter the relay in the settings (wss://host/ws)")); return; }
    spettatore = !!soloGuarda;
    if (!dev && !spettatore) { errore(L("prima collega il Pico (passo 0) - oppure usa 'Guarda soltanto'", "connect the Pico first (step 0) - or use 'Just watch'")); return; }
    if (inPartita) return;
    var url = OwlRelay.normalizzaUrl(cfg.relay);
    url += (url.indexOf("?") >= 0 ? "&" : "?") + "stanza=" + cfg.stanza;
    bridge = new OwlBridge({
      peerId: cfg.peer, room: cfg.stanza, device: dev, log: logDa,
      copies: 2, onStatus: function () { disegnaStato(); },
      onPos: inviaPosizioniEvento, spettatore: spettatore
    });
    link = new OwlRelay.RelayLink(url, {
      onMessage: function (b) { bridge.onRelayMessage(b); },
      onOpen: function () { bridge.sendHello(); disegnaStato(); },
      onClose: function () { disegnaStato(); },
      log: logDa
    });
    bridge.link = link;
    link.connect();
    bridge.start();
    inPartita = true;
    log("[rete ] " + (spettatore ? "SPETTATORE" : "partita") + ": peer " + cfg.peer +
        ", stanza " + cfg.stanza + ", relay " + url, "rete");
    if (spettatore) {
      log(L("[rete ] modo spettatore: nessun GBA collegato. Ricevi le posizioni degli amici " +
          "(guardale con 'Mappa live'), ma non mandi niente.", "[rete ] spectator mode: no GBA connected. You receive your friends' positions " +
          "(watch them with 'Live map'), but you send nothing."), "rete");
      esito("esito-azione", L("Spettatore: apri la Mappa live per vedere gli amici camminare.", "Spectator: open the Live map to watch your friends walk."), "bene");
    } else {
      log("[rete ] Cable Club: supportato anche da qui (versione club " + OwlClub.VERSIONE_CLUB +
          "). Sedetevi al bancone in due e il resto parte da solo; durante lo scambio tieni questa scheda in primo piano.", "rete");
      esito("esito-azione", L("Partita avviata: cammina all'aperto, gli inviati devono salire.", "Game started: walk around outdoors, the Sent counter must go up."), "bene");
    }
    disegnaTutto();
  }
  function fermaPartita() {
    if (!inPartita) return;
    inPartita = false;
    if (bridge) { bridge.stop(); }
    if (link) { link.close(); }
    log("[rete ] partita fermata", "rete");
    bridge = null; link = null;
    disegnaTutto();
  }

  /* "Il relay c'e'?" senza Pico e senza partita: si apre la connessione, si
   * manda UN PING con la stanza delle impostazioni, si misura il PONG e si
   * chiude. E' la domanda che chi prova si fa per prima, e la risposta deve
   * essere un numero, non un'impressione. */
  function provaRelay() {
    var err = leggiCfg();
    if (err) { esito("esito-config", err, "male"); return; }
    if (!cfg.relay) { esito("esito-config", L("scrivi prima il relay (wss://host/ws)", "enter the relay first (wss://host/ws)"), "male"); return; }
    var url = OwlRelay.normalizzaUrl(cfg.relay);
    url += (url.indexOf("?") >= 0 ? "&" : "?") + "stanza=" + cfg.stanza;
    var b = $("btn-prova-relay"); b.disabled = true;
    esito("esito-config", L("provo ", "trying ") + url + " ...");
    var t0 = 0, seq = 0x5A, finito = false;
    var timer = setTimeout(function () { chiudi(L("il relay non risponde entro 5 s: indirizzo sbagliato, porta chiusa, o ws:// su una pagina https", "the relay did not answer within 5 s: wrong address, closed port, or ws:// on an https page"), "male"); }, 5000);
    var l = new OwlRelay.RelayLink(url, {
      onOpen: function () { t0 = performance.now(); l.send(OwlRelay.pack(OwlRelay.T.PING, cfg.peer, cfg.stanza, seq)); },
      onMessage: function (bytes) {
        var p = OwlRelay.unpack(bytes);
        if (p && p.kind === OwlRelay.T.PONG && p.seq === seq) {
          chiudi(L("relay raggiungibile: PONG in ", "relay reachable: PONG in ") + (performance.now() - t0).toFixed(0) + L(" ms (stanza ", " ms (room ") + cfg.stanza + ")", "bene");
        }
      },
      onClose: function (motivo) { if (!finito && motivo !== "chiusa") chiudi(L("relay NON raggiungibile (", "relay NOT reachable (") + motivo + "): " + url, "male"); },
      log: logDa
    });
    function chiudi(testo, classe) {
      if (finito) return;
      finito = true; clearTimeout(timer); l.close(); b.disabled = false;
      esito("esito-config", testo, classe);
      log("[rete ] prova del relay: " + testo, "rete", classe === "male");
    }
    l.connect();
  }

  /* --- disegno -------------------------------------------------------------- */
  function bottone(contenitore, numero, titolo, sotto, sottoVivo, vivo, lavoro, onAvvia, onFerma, disabilitato) {
    var c = $(contenitore); c.innerHTML = "";
    var b = document.createElement("button");
    b.className = "azione" + (vivo ? " viva" : "") + (lavoro ? " lavoro" : "");
    b.disabled = !!disabilitato;
    b.setAttribute("aria-label", (vivo ? L("Ferma: ", "Stop: ") : L("Avvia: ", "Start: ")) + titolo);
    b.onclick = function () { (vivo ? onFerma : onAvvia)(); };
    var num = document.createElement("span"); num.className = "num"; num.textContent = vivo ? "■" : (lavoro ? "…" : numero);
    var testo = document.createElement("span"); testo.className = "testo";
    var bb = document.createElement("b"); bb.textContent = titolo;
    var sm = document.createElement("small"); sm.textContent = vivo ? sottoVivo : sotto;
    testo.appendChild(bb); testo.appendChild(sm);
    var fine = document.createElement("span"); fine.className = "fine"; fine.textContent = vivo ? L("Ferma", "Stop") : (lavoro ? L("In corso", "Working") : L("Avvia", "Start"));
    b.appendChild(num); b.appendChild(testo); b.appendChild(fine);
    c.appendChild(b);
  }

  function disegnaPassi() {
    bottone("riga-pico", "0", L("Collega il Pico", "Connect the Pico"), L("Il browser chiede quale USB: scegli il Pico (2FE3:000A). Serve Chrome o Edge.", "The browser asks which USB device: pick the Pico (2FE3:000A). Needs Chrome or Edge."),
      L("collegato: ", "connected: ") + picoNome, !!dev, false, collegaPico, scollegaPico, multibootInCorso);
    bottone("riga-multiboot", "1", L("Carica il gioco nel GBA", "Load the game into the GBA"), L("Slot vuoto, GBA acceso dopo il cavo. Schermo rosso ➜ cartuccia ➜ verde.", "Empty slot, GBA switched on after the cable. Red screen ➜ cartridge ➜ green."),
      "", false, multibootInCorso, caricaGioco, function () {}, multibootInCorso);
    bottone("riga-gioca", "2", L("Gioca", "Play"), L("Da premere quando sei in partita, all'aperto. Relay e stanza dalle impostazioni.", "Press it once you're in game, outdoors. Relay and room come from the settings."),
      (spettatore ? L("spettatore", "spectator") : L("partita in corso", "game running")) + L(": stanza ", ": room ") + (cfg ? cfg.stanza : "—") + ", peer " + (cfg ? cfg.peer : "—"),
      inPartita, false, function () { avviaPartita(false); }, fermaPartita, multibootInCorso);
    var bs = $("btn-spettatore");
    if (bs) { bs.disabled = inPartita || multibootInCorso; bs.textContent = inPartita && spettatore ? L("Sei spettatore", "You are a spectator") : L("Guarda soltanto (spettatore)", "Just watch (spectator)"); }
    $("btn-riavvia").disabled = !dev || multibootInCorso;
    $("btn-scollega").disabled = !dev || multibootInCorso;
  }

  function spia(id, testo, stato) { $(id).className = "spia" + (stato ? " " + stato : ""); $(id + "-testo").textContent = testo; }
  function led(id, stato) { $(id).className = "led" + (stato ? " " + stato : ""); }

  function disegnaStato() {
    var s = bridge ? bridge.snapshot() : null;
    var mia = s && s.posIo ? s.posIo : null;

    // il tuo GBA
    var mappa = mia ? (mia.gruppo + "." + mia.numero) : null;
    var alClub = !!(bridge && bridge.club);
    $("st-gba").textContent = alClub ? L("Al Cable Club", "At the Cable Club") : (mappa ? L("In partita", "In game") : (caricato || sondaVista ? L("Programma caricato", "Program loaded") : (dev ? L("Canale aperto", "Channel open") : L("Fermo", "Idle"))));
    $("st-mappa").textContent = alClub
      ? L("scambio o lotta in corso: non toccare cavo e Pico, tieni la scheda in primo piano", "trade or battle in progress: don't touch cable or Pico, keep this tab in the foreground")
      : (mappa
        ? (L("mappa ", "map ") + mappa + " (" + mia.x + "," + mia.y + ")" + (mia.stato ? " · " + nomeStato(mia.stato) : ""))
        : (sondaVista ? L("il programma risponde: cammina all'aperto", "the program is answering: walk around outdoors") : (caricato ? L("in attesa che dica dove sei", "waiting for it to report where you are") : L("programma non caricato: premi il passo 1", "program not loaded: press step 1"))));
    spia("spia-gba", alClub ? "club" : (mappa ? L("attivo", "active") : (sondaVista ? L("pronto", "ready") : (dev ? L("canale", "channel") : L("spento", "off")))), (alClub || mappa) ? "ok" : (inPartita ? "no" : (dev ? "attesa" : "")));

    // gli amici
    var amici = s ? s.amici : [];
    $("st-amico").textContent = amici.length ? (amici.length === 1 ? L("1 collegato", "1 connected") : amici.length + L(" collegati", " connected")) : L("In attesa", "Waiting");
    $("st-stanza").textContent = L("stanza ", "room ") + (cfg ? cfg.stanza : "—") + L(" · tu sei il peer ", " · you are peer ") + (cfg ? cfg.peer : "—");
    var ul = $("lista-amici"); ul.innerHTML = "";
    amici.forEach(function (a) {
      var li = document.createElement("li");
      var b = document.createElement("b"); b.textContent = "peer " + a.peerId;
      if (spettatore && cfg && cfg.peerGioco === a.peerId) b.textContent += L(" (il tuo GBA)", " (your GBA)");
      li.appendChild(b);
      // Quanto e' VECCHIA l'ultima notizia di questo amico: un elenco che
      // mostra solo posizioni non dice se sono di adesso o di dieci secondi
      // fa, ed e' la differenza fra "e' li' fermo" e "si e' scollegato".
      var eta = a.pos.visto ? Math.round((Date.now() - a.pos.visto) / 1000) : null;
      li.appendChild(document.createTextNode(L(" mappa ", " map ") + a.pos.gruppo + "." + a.pos.numero + " (" + a.pos.x + "," + a.pos.y + ")" + (a.pos.stato ? " · " + nomeStato(a.pos.stato) : "") + (eta === null ? "" : " · " + (eta <= 1 ? L("adesso", "now") : eta + L(" s fa", " s ago")))));
      ul.appendChild(li);
    });
    spia("spia-amico", amici.length ? L("vivi", "live") : (inPartita ? (link && link.aperta ? L("in attesa", "waiting") : "relay?") : L("in attesa", "waiting")),
         amici.length ? "ok" : (inPartita ? (link && link.aperta ? "attesa" : "no") : ""));

    // canale
    $("st-parole").textContent = dev && paroleS ? paroleS.toFixed(0) : "—";
    $("st-inviati").textContent = s ? s.sent : 0;
    $("st-ricevuti").textContent = s ? s.received : 0;
    $("st-rtt").textContent = s && s.rttUltimo !== null ? (s.rttUltimo.toFixed(0) + " ms") : "—";

    // targhe
    if (!inPartita) { led("led-relay", ""); $("targa-relay").textContent = "relay: " + (cfg && cfg.relay ? cfg.relay : L("non impostato", "not set")); }
    else if (link && link.aperta) { led("led-relay", "ok"); $("targa-relay").textContent = L("relay: collegato", "relay: connected") + (s && s.rttMedio !== null ? " · RTT " + s.rttMedio.toFixed(0) + " ms" : ""); }
    else { led("led-relay", "no"); $("targa-relay").textContent = L("relay: NON raggiungibile, riprovo", "relay: NOT reachable, retrying"); }
    led("led-pico", dev ? "ok" : (multibootInCorso ? "attesa" : (picoPerso ? "no" : "")));
    $("targa-pico").textContent = dev ? ("Pico: " + picoNome)
      : (multibootInCorso ? L("Pico: multiboot in corso", "Pico: multiboot in progress")
        : (picoPerso ? L("Pico: SCOLLEGATO - ricollegalo e premi Collega", "Pico: DISCONNECTED - plug it back and press Connect") : L("Pico: non collegato", "Pico: not connected")));

    $("marchio").style.boxShadow = (inPartita && mappa) ? "0 6px 16px rgba(0,0,0,.35), 0 0 0 2px rgba(63,214,143,.55)" : "0 6px 16px rgba(0,0,0,.35)";
  }

  function disegnaDiagnostica() {
    if (!dev) return;
    var adesso = Date.now();
    var parole = dev.deframer.wordsRx + dev.rawRx;
    if (ultimoT) paroleS = (parole - ultimeParole) * 1000 / (adesso - ultimoT);
    ultimeParole = parole; ultimoT = adesso;
    $("st-stats").textContent = dev.stats() + L(" | buchi di numerazione ", " | sequence gaps ") + buchi +
      (dev.lastStatus !== null ? " | status 0x" + dev.lastStatus.toString(16).padStart(4, "0") : "");
    if (dev.stateWords) {
      var eta = ((adesso - dev.stateStamp) / 1000).toFixed(0);
      $("st-sonda").textContent = L("sonda #", "probe #") + dev.stateCount + " (" + eta + L(" s fa): ", " s ago): ") + GbaSio.describeSonda(dev.stateWords);
    }
    if (dev.diagWords) $("st-diag").textContent = GbaSio.describeDiag(dev.diagWords);
  }

  function disegnaTutto() { disegnaPassi(); disegnaStato(); }

  /* --- attrezzi ------------------------------------------------------------- */
  function copiaLog(bott) {
    // Dal buffer completo, NON dal DOM: il DOM taglia a MAX_RIGHE, e per il
    // debug servono tutte le righe della sessione.
    var testo = registroCompleto.join("\n");
    var originale = bott.textContent;
    function fatto(ok) { bott.textContent = ok ? L("Copiato! (", "Copied! (") + registroCompleto.length + L(" righe)", " lines)") : L("Non ci riesco: usa Scarica", "Couldn't copy: use Download"); setTimeout(function () { bott.textContent = originale; }, 2500); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(testo).then(function () { fatto(true); }, function () { fatto(false); });
    } else {
      var ta = document.createElement("textarea"); ta.value = testo; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select(); var ok = document.execCommand("copy"); document.body.removeChild(ta); fatto(ok);
    }
  }
  function scaricaLog() {
    // Un file .log con TUTTA la sessione: la via sicura quando le righe sono
    // tante o gli appunti fanno i capricci.
    var testo = registroCompleto.join("\n") + "\n";
    var blob = new Blob([testo], { type: "text/plain;charset=utf-8" });
    var a = document.createElement("a");
    var t = new Date();
    var nome = "gen3-poke-multiplayer-" + t.getFullYear() + ("0" + (t.getMonth() + 1)).slice(-2) + ("0" + t.getDate()).slice(-2) +
               "-" + ("0" + t.getHours()).slice(-2) + ("0" + t.getMinutes()).slice(-2) + ("0" + t.getSeconds()).slice(-2) + ".log";
    a.href = URL.createObjectURL(blob);
    a.download = nome;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
  }

  /* --- avvio ---------------------------------------------------------------- */
  window.addEventListener("load", function () {
    caricaCfg();
    $("btn-salva").onclick = salva;
    $("btn-prova-relay").onclick = provaRelay;
    $("btn-riavvia").onclick = riavviaPico;
    $("btn-scollega").onclick = scollegaPico;
    $("btn-copia").onclick = function () { copiaLog(this); };
    $("btn-scarica").onclick = scaricaLog;
    // "Pulisci" sgombra SOLO lo schermo: il buffer completo resta, perche'
    // copia e download devono restituire tutta la sessione anche dopo.
    $("btn-pulisci").onclick = function () { $("log").innerHTML = ""; righe = 0; registroCompleto.push(L("--- (schermo pulito: il registro completo continua) ---", "--- (screen cleared: the full log continues) ---")); };
    $("btn-mappa").onclick = apriMappa;
    $("btn-spettatore").onclick = function () { avviaPartita(true); };
    $("btn-lua").onclick = scaricaLua;
    // La ROM per lo script: ricordata; la prima volta si indovina dalla lingua della pagina.
    var romSalvata = null;
    try { romSalvata = localStorage.getItem(CHIAVE_ROM); } catch (e) { romSalvata = null; }
    if ($("rom-lua")) {
      $("rom-lua").value = (romSalvata === "it" || romSalvata === "usa") ? romSalvata : (EN ? "usa" : "it");
      $("rom-lua").onchange = function () {
        try { localStorage.setItem(CHIAVE_ROM, romLua()); } catch (e) { /* niente */ }
        romScelta = null;   // cambia la cartuccia: lo stub va ripreso
        log(L("[mb  ] versione del gioco: ", "[mb  ] game version: ") + (romLua() === "usa" ? "Emerald (USA/EU)" : "Smeraldo (IT)"), "mb");
      };
    }
    $("mb-file").onchange = async function () {
      var f = $("mb-file").files[0];
      if (!f) return;
      romScelta = await leggiRom(f);
      log("[mb  ] programma scelto a mano: " + f.name + " (" + romScelta.length + " byte)", "mb");
    };
    ["relay", "stanza", "peer", "peer-gioco", "timing", "cavo"].forEach(function (id) { $(id).addEventListener("change", function () { leggiCfg(); disegnaStato(); }); });

    var avv = [];
    if (!CelioDevice.supportato()) {
      // Non e' un difetto della pagina ed e' bene che si legga: Firefox non
      // ha WebUSB e non l'avra' (Mozilla l'ha marcata "harmful" nella sua
      // standards position), Safari nemmeno. Chi arriva qui deve capire in
      // due righe che cosa puo' fare lo stesso.
      avv.push(L("Questo browser non puo' pilotare il GBA: WebUSB esiste solo su Chrome, Edge e derivati " +
               "(anche Android). Su Firefox e Safari non c'e', ed e' una scelta di chi li fa, non un difetto di questa pagina. " +
               "Puoi pero' usare «Guarda soltanto (spettatore)»: ti colleghi al relay e vedi gli amici muoversi sulla Mappa live.",
               "This browser can't drive the GBA: WebUSB only exists in Chrome, Edge and their derivatives " +
               "(Android too). Firefox and Safari don't have it, by their makers' choice, not a bug of this page. " +
               "You can still use «Just watch (spectator)»: you connect to the relay and see your friends move on the Live map."));
    }
    if (typeof isSecureContext !== "undefined" && !isSecureContext) avv.push(L("La pagina non e' in contesto sicuro (serve https:// oppure http://localhost): WebUSB resta spento.", "The page is not in a secure context (it needs https:// or http://localhost): WebUSB stays off."));
    if (location.protocol === "file:") avv.push(L("Pagina aperta da file://: servila con un server (es. python -m http.server) e apri http://127.0.0.1:PORTA/.", "Page opened from file://: serve it with a server (e.g. python -m http.server) and open http://127.0.0.1:PORT/."));
    if (avv.length) { $("avviso-testo").textContent = avv.join(" "); $("avviso").hidden = false; }

    disegnaTutto();
    setInterval(function () { disegnaDiagnostica(); disegnaStato(); inviaPosizioni(); }, 1000);
    log(L("pronto. Passo 0: 'Collega il Pico' apre la finestra del browser per scegliere il Pico (2FE3:000A).", "ready. Step 0: 'Connect the Pico' opens the browser window to pick the Pico (2FE3:000A)."), "usb");
    if (!cfg.relay) log(L("[rete ] nessun relay impostato: scrivilo nelle impostazioni (te lo da' chi ha pubblicato la pagina)", "[rete ] no relay set: enter it in the settings (whoever published the page gives it to you)"), "rete");
    window.addEventListener("beforeunload", function () { if (link) link.close(); });
  });
})();
