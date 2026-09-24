#!/usr/bin/env python3
"""
mb_multi.py - multiboot del GBA in modo **MultiPlay 16 bit**, sopra Celio.

PERCHE' ESISTE QUESTO FILE
--------------------------
Fino al 2026-08-02 il progetto dava per scontato che multiboot = SIO Normal
32 bit, che vuole SO<->SI nei due sensi. Il cavo GBA ha SI a massa in un capo
(misurato il 2026-08-01, GP1 = 0/0, batte il pull-up da 10 kOhm), quindi da li'
il verdetto "sul cavo GBA il multiboot non passa" e le 42 prove mute.

E' falso, perche' il multiboot ha tre modi. GBATEK, SWI 25h - MultiBoot,
parametro r1 "Transfer Mode":

    0 = 256 KHz, 32 bit, Normal mode    (veloce e stabile)
    1 = 115 KHz, 16 bit, MultiPlay mode (DEFAULT, lento, fino a tre slave)
    2 = 2 MHz,   32 bit, Normal mode    (il piu' veloce, forse instabile)

Il modo 1 e' esattamente cio' che Celio gia' implementa: Multi-Player 16 bit a
115200, adattatore master, GBA child - misurato su questo hardware a 226
parole/s con 0 errori di frame su 60 s. E' anche il modo del single-pak link
di Nintendo, che gira sul cavo GBA originale con lo slot vuoto.

Conseguenza: **niente secondo firmware e niente secondo cavo**. Il multiboot
diventa una macchina a stati in Python sopra il passthrough che c'e' gia'.

Riferimenti usati per scriverlo, entrambi verificati riga per riga:
  - GBATEK "BIOS Multi Boot (Single Game Pak)" (protocollo, pseudocodice di
    checksum/cifratura, costanti normal vs multiplay);
  - afska/gba-link-connection, lib/LinkCableMultiboot.hpp v8.0.3 (la stessa
    cosa, funzionante su hardware vero: e' da li' che vengono i dettagli che
    GBATEK non fissa - palette 0x93, attesa di 4 frame prima del trasferimento
    principale, crcC &= 0xFFFF prima del contributo di crcB).

L'UNICO NODO DEL TRASPORTO
--------------------------
A coda TX vuota il firmware manda 0x7FFF e fa lo scambio LO STESSO
(rawRelaySection.cpp:147). In una sessione di link e' giusto, in un multiboot
e' una parola spuria in mezzo al protocollo. Qui la regola e': **la coda non si
svuota mai**. Le fasi deterministiche (comando + 96 half-word di header, e i
dati della ROM) si accodano in blocco; le fasi che il protocollo stesso ripete
(0x6200, 0x63pp, 0x0065) si tengono piene a batch. L'unico punto in cui il
protocollo vuole SILENZIO - l'attesa di 1/16 s prima della lunghezza - si fa
senza smettere di clockare, allungando il periodo del master con F-1
(SetRawTiming): il valore di timing accompagna la parola nel PIO e vale come
ritardo DOPO di essa, quindi 0x64hh esce con timing lungo e la parola dopo
arriva 70 ms piu' tardi. Nessun buco, nessuna parola in piu'.

Se nonostante tutto un idle si infila, non passa in silenzio: le risposte del
protocollo sono auto-descrittive (l'header conta all'indietro 0x60..0x01, i
dati riecheggiano l'indirizzo di destinazione) e il codice le verifica una per
una. Un desync viene NOMINATO e la corsa riparte da capo, che al GBA non costa
niente: il BIOS aspetta all'infinito.

USO
---
    python mb_multi.py ..\\hw\\siotest\\build\\siotest.gba
    python mb_multi.py ..\\hw\\mbstub\\build\\mbstub.gba --timing 3700

    python mb_multi.py --autotest        # nessun hardware: GBA simulato
"""

import argparse
import collections
import os
import sys
import time

# ---------------------------------------------------------------------------
# Costanti del protocollo (GBATEK + LinkCableMultiboot.hpp)
# ---------------------------------------------------------------------------

CMD_HANDSHAKE = 0x6200            # "6200"  -> FFFF / 0000 / 720x
ACK_HANDSHAKE = 0x7200            # "720x"
CMD_CONFIRM_CLIENTS = 0x6100      # "610y"
CMD_SEND_PALETTE = 0x6300         # "63pp"
CMD_CONFIRM_HANDSHAKE = 0x6400    # "64hh"
ACK_RESPONSE = 0x7300             # "73cc" / "73uu" / "73rr"
ACK_RESPONSE_MASK = 0xFF00
HANDSHAKE_DATA = 0x11             # hh = 0x11 + client_data[1..3]
CMD_ROM_END = 0x0065
ACK_ROM_END_WAIT = 0x0074
ACK_ROM_END = 0x0075
CMD_FINAL_CRC = 0x0066

HEADER_SIZE = 0xC0
HEADER_PARTS = HEADER_SIZE // 2   # 96 half-word

# palette_data: "81h + colore*10h + direzione*8 + velocita'*2".
# 0x93 e' il valore di LinkCableMultiboot (colore 1, direzione 0, velocita' 1):
# si tiene quello perche' e' il valore che gira su hardware vero.
PALETTE_DATA = 0x93

# Le costanti che CAMBIANO fra i due modi. Sbagliarle non da' un errore di
# protocollo: da' un CRC finale diverso a fine trasferimento, cioe' l'errore
# piu' costoso da diagnosticare. (GBATEK: "if multiplay_mode then c=FFF8h:
# x=A517h:k=6465646Fh".)
CRCC_START_MULTI = 0xFFF8
CRCC_XOR_MULTI = 0xA517
DATA_XOR_MULTI = 0x6465646F
SEED_MULTIPLIER = 0x6F646573

MIN_ROM_SIZE = 0x100 + 0xC0
MAX_ROM_SIZE = 256 * 1024

# Un solo slave, nello slot 1. Non e' una semplificazione pigra: l'adattatore
# e' il parent e sul cavo c'e' un GBA solo. y (client_bit) e x (slot) valgono
# entrambi 0x02.
CLIENT_BIT = 0x02

DETECTION_TRIES = 64              # scambi di detect prima di ricominciare
MAX_ROM_END_TRIES = 300


class MultibootError(Exception):
    """Errore che vale la pena mostrare a chi esegue: il messaggio nomina la
    fase e cosa e' arrivato invece di cosa era atteso."""


def crc_step(crc, data32, xor_val=CRCC_XOR_MULTI):
    """Un passo del checksum di GBATEK, bit per bit su 32 bit.

    GBATEK lo scrive come "c = c xor data; 32 volte: c = c shr 1; se carry
    allora c = c xor x". Questa forma (identica a LinkCableMultiboot e al
    multiboot.py di lorenzooone, entrambi funzionanti su hardware) fa entrare
    i bit del dato uno alla volta ed e' equivalente.
    """
    for _ in range(32):
        bit = (crc ^ data32) & 1
        data32 >>= 1
        crc >>= 1
        if bit:
            crc ^= xor_val
    return crc & 0xFFFFFFFF


def prepara_rom(data):
    """Allinea la ROM a 0x10 byte e controlla i limiti.

    Il vincolo del multiplo di 0x10 e' di GBATEK ("transfer length should be a
    multiple of 10h"): mbstub.gba misura 11948 byte, che multiplo di 0x10 non
    e', e senza questo padding la lunghezza dichiarata al GBA sarebbe sbagliata.
    """
    data = bytearray(data)
    if len(data) % 0x10:
        data += b"\x00" * (0x10 - (len(data) % 0x10))
    if len(data) < MIN_ROM_SIZE:
        raise MultibootError(
            f"ROM troppo piccola: {len(data)} byte, minimo {MIN_ROM_SIZE}")
    if len(data) > MAX_ROM_SIZE:
        raise MultibootError(
            f"ROM troppo grande: {len(data)} byte, massimo {MAX_ROM_SIZE}")
    return bytes(data)


class Multiboot:
    """La macchina a stati, sopra un trasporto che sa solo due cose:
    send_words(lista) e read_word(timeout)."""

    def __init__(self, link, palette=PALETTE_DATA, wait_ms=70,
                 timing_fast=None, timing_wait=None, verbose=True,
                 max_attempts=3, detect_s=5.0, sovrapponi=True, scorta=24):
        self.link = link
        self.palette = palette & 0xFF
        self.wait_ms = wait_ms
        self.timing_fast = timing_fast
        self.timing_wait = timing_wait
        self.verbose = verbose
        self.max_attempts = max_attempts
        self.detect_s = detect_s
        # Solo per l'autotest: spegnendola si riproduce il difetto del primo
        # giro sull'hardware. Non e' un'opzione da usare sul filo.
        self.sovrapponi = sovrapponi
        # Quante parole tenere sempre in volo. Non e' un margine a occhio: e'
        # quante volte il master puo' scambiare mentre l'host pensa. A
        # --timing 3700 uno scambio dura ~2,4 ms e un giro USB altrettanto,
        # quindi il buco vale ~1; ma a timing bassi lo scambio scende a mezzo
        # millisecondo e il buco cresce. L'autotest misura la soglia: con 6 si
        # rompeva gia' a 4 scambi di buco. La coda del firmware e' profonda 256
        # (rawRelaySection.cpp:6), quindi 24 non costa niente.
        self.scorta = scorta
        self.reset_counters()

    def reset_counters(self):
        self.sent = 0
        self.recv = 0
        self.skipped = 0      # risposte scartate riagganciandosi: puo' essere >0
        self.desync = 0
        self.restarts = 0
        self.trace = collections.deque(maxlen=60)
        self.rr = 0
        self.coda73 = []
        self.nonc = []
        self._diag = None

    # -- primitive ---------------------------------------------------------

    def _log(self, msg):
        if self.verbose:
            print(f"[mb  ] {msg}")

    def _push(self, words):
        self.link.send_words(words)
        self.sent += len(words)

    def _pull(self, timeout=3.0, fase=""):
        w = self.link.read_word(timeout=timeout)
        if w is None:
            raise MultibootError(
                f"nessuna risposta dal GBA entro {timeout:.0f} s (fase {fase}). "
                "Il master ha smesso di clockare, oppure il canale e' caduto.")
        self.recv += 1
        # Su questo progetto chi esegue e chi legge il codice non sono la
        # stessa persona e l'hardware non si puo' guardare: un fallimento che
        # non porta con se' la conversazione vera costa un giro intero di
        # "codice -> Lain esegue -> riporta". La traccia si stampa da sola.
        self.trace.append((fase, w))
        return w

    def diagnosi_crc(self, gba, nostro):
        """Un CRC finale sbagliato deve dire QUALE valore era sbagliato.

        Il checksum e' un LFSR, quindi e' lineare: dati i dati in chiaro, il
        valore finale dipende solo da crcB = hh|rr<<8|FFFF0000. Provare tutti i
        256 valori di rr costa niente e risponde alla domanda che altrimenti
        richiederebbe un'altra serata: era rr, o erano i dati (cioe' cc, cioe'
        il seme)? Sono le due cause con lo stesso sintomo, e distinguerle a
        occhio e' impossibile.
        """
        d = getattr(self, "_diag", None)
        if d is None or not self.verbose:
            return
        crc_dati, hh, cc = d
        print(f"[mb  ] diagnosi: crc dei dati in chiaro 0x{crc_dati:04X}, "
              f"cc letto 0x{cc:02X}, hh 0x{hh:02X}, rr letto 0x{self.rr:02X}")
        buoni = [rr for rr in range(256)
                 if (crc_step(crc_dati, hh | (rr << 8) | 0xFF000000 | 0xFF0000)
                     & 0xFFFF) == gba]
        if buoni:
            print(f"[mb  ] -> con rr = {[f'0x{r:02X}' for r in buoni]} il CRC "
                  "sarebbe tornato: i DATI erano giusti, e' rr a essere stato "
                  "letto male.")
        else:
            print("[mb  ] -> nessun rr avrebbe fatto tornare il CRC: i dati "
                  "decifrati dal GBA sono diversi dai nostri, quindi e' il "
                  "SEME (cioe' cc) a essere sbagliato, non rr.")
        if getattr(self, "coda73", None):
            print("[mb  ] ultime 0x73xx prima dei dati: "
                  + " ".join(f"0x{v:04X}" for v in self.coda73)
                  + f"   (diverse da cc: "
                  + " ".join(f"0x{v:04X}" for v in getattr(self, "nonc", []))
                  + ")")

    def dump_trace(self):
        if not self.trace:
            return
        print("[mb  ] ultime risposte del GBA (fase: valore):")
        riga = []
        for fase, w in self.trace:
            riga.append(f"{fase}:{w:04X}")
            if len(riga) == 6:
                print("[mb  ]   " + "  ".join(riga))
                riga = []
        if riga:
            print("[mb  ]   " + "  ".join(riga))

    def _keep_fed(self, word, target=None):
        """Rimette `word` in coda finche' ce ne sono `target` in volo.

        E' la riga che tiene in piedi tutto il resto. Le fasi ripetute del
        protocollo (detect, palette, fine dati) sono le uniche in cui non si
        puo' accodare un blocco calcolato in anticipo, quindi sono le uniche in
        cui la coda del firmware potrebbe svuotarsi fra una lettura e la
        successiva - e a coda vuota il firmware manda 0x7FFF. Rabboccando
        PRIMA di ogni lettura la coda non arriva mai a zero e la parola spuria
        non nasce affatto, invece di doverla riconoscere dopo.
        """
        if target is None:
            target = self.scorta
        in_volo = self.sent - self.recv
        # Si rabbocca a meta' scorta, non a ogni parola consumata: ogni push e'
        # un giro USB, e fra un push e il successivo il master continua a
        # clockare. Rabboccare di rado e tanto lascia meno occasioni di
        # svuotarsi che rabboccare spesso e poco.
        if in_volo > target // 2:
            return
        self._push([word] * (target - in_volo))

    def _pull_until(self, pred, fase, max_skip, timeout=3.0):
        """Legge scartando finche' pred(w) non e' vera. Ritorna la parola.

        Il "max_skip" non e' prudenza generica: e' il numero di parole che a
        quel punto del protocollo possono essere legittimamente ancora in volo.
        Superarlo significa che il flusso non e' quello che crediamo, ed e'
        meglio saperlo li' che tre fasi dopo.
        """
        for _ in range(max_skip + 1):
            w = self._pull(timeout=timeout, fase=fase)
            if pred(w):
                return w
            self.skipped += 1
        raise MultibootError(
            f"fase {fase}: {max_skip} risposte di fila non attese "
            f"(ultima 0x{w:04X}). Il flusso e' fuori sincrono.")

    # -- le fasi -----------------------------------------------------------

    def _detect(self):
        """1. 0x6200 ripetuto finche' il child risponde 0x720x.

        Il protocollo prevede lui stesso la ripetizione, quindi qui la coda si
        puo' tenere piena senza pensieri: e' l'unica fase in cui una parola in
        piu' non costa niente.
        """
        # Il budget e' a TEMPO, non a numero di scambi: il numero di scambi che
        # stanno in un secondo dipende da --timing, e un limite a conteggio
        # diventerebbe piu' severo proprio quando si va veloci - cioe' darebbe
        # un falso negativo su U-1 senza che niente sia rotto.
        scadenza = time.time() + self.detect_s
        visti = {}
        while time.time() < scadenza:
            self._keep_fed(CMD_HANDSHAKE)
            w = self._pull(timeout=3.0, fase="detect")
            if (w & 0xFFF0) == ACK_HANDSHAKE and (w & 0xF) == CLIENT_BIT:
                self._log(f"client 1 rilevato (0x{w:04X})")
                return
            # 0xFFFF = il child non e' ancora in modo multiplay;
            # 0x0000 = ci e' appena entrato. Entrambe sono attese.
            visti[w] = visti.get(w, 0) + 1
        # Cosa e' tornato dal filo separa le cause meglio di qualunque
        # supposizione, ed e' la stessa lettura di diagnosi_linea() in
        # usb_link.py: linea alta e viva = nessuno risponde; a massa = pin o
        # filo; sparso = timing o rumore.
        if visti:
            top = sorted(visti.items(), key=lambda kv: -kv[1])[:4]
            tot = sum(visti.values())
            self._log("risposte viste al detect: "
                      + ", ".join(f"0x{v:04X} x{n} ({n * 100 // tot}%)"
                                  for v, n in top))
            dom, ndom = top[0]
            if dom in (0xFFFF, 0x7FFF) and ndom * 10 >= tot * 9:
                self._log("  -> linea alta e viva: NESSUNO risponde "
                          "(non e' il pin: e' il GBA che non e' in attesa)")
            elif dom == 0x0000 and ndom * 10 >= tot * 9:
                self._log("  -> linea a massa: SD sul pin sbagliato o filo "
                          "assente")
        raise MultibootError(
            "il GBA non ha mai risposto 0x7202 al detect.\n"
            "       Le cause, in ordine di probabilita':\n"
            "       - il GBA non e' in attesa di multiboot (slot cartuccia\n"
            "         VUOTO, e acceso DOPO aver collegato il cavo);\n"
            "       - il cavo e' nel verso sbagliato (il verso e' marcato);\n"
            "       - SW1 non e' su 3,3 V.")

    def _header(self, rom, n_palette=4, coda=()):
        """2-4. 0x610y, i 96 half-word di header, poi 0x6200 e 0x620y.

        Tutto deterministico: si accoda in un blocco solo, cosi' fra il comando
        e l'header non c'e' nessuna finestra in cui la coda possa svuotarsi. Le
        risposte si riagganciano da sole: la prima parola di header vale 0x6002
        e da li' il contatore scende a 0x0102.

        `coda` e' la SOVRAPPOSIZIONE con la fase dopo, e non e' un dettaglio:
        e' il difetto che ha fatto fallire il primo giro sul filo (2026-08-02).
        Un blocco che finisce esattamente dove finiscono le sue risposte lascia
        la coda TX a zero per il tempo che serve a Python a decidere la parola
        seguente - un paio di millisecondi, cioe' circa uno scambio - e li' il
        firmware infila 0x7FFF. Il detect non ne soffriva perche' esce in
        anticipo lasciando residui in volo; questo blocco no, e infatti il GBA
        smetteva di rispondere di colpo subito dopo 0x6202.
        """
        blocco = [CMD_CONFIRM_CLIENTS | CLIENT_BIT]
        for i in range(HEADER_PARTS):
            blocco.append(rom[i * 2] | (rom[i * 2 + 1] << 8))
        blocco.append(CMD_HANDSHAKE)
        blocco.append(CMD_HANDSHAKE | CLIENT_BIT)
        blocco.extend([CMD_SEND_PALETTE | self.palette] * n_palette)
        blocco.extend(coda)
        in_volo = self.sent - self.recv
        self._push(blocco)

        # 0x6002 e' un'ancora distinguibile da tutto cio' che puo' essere
        # ancora in volo (0x7202, 0xFFFF, 0x0000), quindi qui ci si riaggancia
        # sul VALORE e il budget di scarto sta largo: se una parola spuria si
        # fosse infilata prima, e' meglio riprendere il filo che fallire.
        atteso = (HEADER_PARTS << 8) | CLIENT_BIT      # 0x6002
        self._pull_until(lambda w: w == atteso, "header",
                         max_skip=in_volo + 16)
        for remaining in range(HEADER_PARTS - 1, 0, -1):
            atteso = (remaining << 8) | CLIENT_BIT
            w = self._pull(fase="header")
            if w != atteso:
                raise MultibootError(
                    f"header: alla parola {HEADER_PARTS - remaining} il GBA ha "
                    f"risposto 0x{w:04X} invece di 0x{atteso:04X}")
            # Il periodo del master si allunga PRIMA della coda del blocco, non
            # dopo: il valore di timing viaggia nel PIO insieme alla parola e
            # vale come ritardo DOPO di essa, quindi va cambiato mentre le
            # parole che devono rallentare sono ancora in coda. Da qui in poi
            # ogni scambio lascia ~70 ms all'host - il tempo di leggere cc e di
            # cifrare la ROM senza che la coda si svuoti mai.
            if remaining == 10 and self.timing_wait is not None:
                self.link.set_timing(self.timing_wait,
                                     f"periodo lungo ({self.wait_ms} ms) per "
                                     "la coda dell'iniziazione")
        w = self._pull(fase="fine header")
        if w != CLIENT_BIT:
            raise MultibootError(
                f"fine header: 0x{w:04X} invece di 0x{CLIENT_BIT:04X}")
        w = self._pull(fase="fine header")
        if w != (ACK_HANDSHAKE | CLIENT_BIT):
            raise MultibootError(
                f"fine header: 0x{w:04X} invece di "
                f"0x{ACK_HANDSHAKE | CLIENT_BIT:04X}")

        # LA PALETTE STA NELLO STESSO BLOCCO, in numero FISSO, e cio' che conta
        # e' la risposta all'ULTIMA. Il GBA rigenera client_data a ogni 0x63pp
        # (misurato), quindi "quante ne mando" non e' un dettaglio di trasporto
        # ma un parametro del protocollo: mandarne un numero variabile - com'era
        # fino al 2026-08-02, dove il riempimento ne aggiungeva 24 - rende cc
        # indeterminabile. Qui non ci sono residui: si legge esattamente
        # n_palette risposte e vale l'ultima.
        cc = None
        for i in range(n_palette):
            w = self._pull(timeout=5.0, fase="palette")
            if (w & ACK_RESPONSE_MASK) == ACK_RESPONSE:
                cc = w & 0xFF
        if cc is None:
            raise MultibootError(
                f"nessuna delle {n_palette} risposte alla palette era 0x73cc: "
                "il GBA non era pronto. Alzare n_palette.")
        return cc

    def _palette(self):
        """5. 0x63pp ripetuto finche' il child risponde 0x73cc.

        cc e' il client_data: entra sia nel seme della cifratura sia nel
        calcolo di hh. Fase ripetibile, quindi la coda si tiene piena.
        """
        # NON si prende la prima 0x73xx che passa. Il 2026-08-02 due corse di
        # fila hanno sbagliato un valore ciascuna - una cc, l'altra rr - e la
        # firma e' quella di una lettura fragile, non di un errore di
        # algoritmo: cc sbagliato falsa il SEME della cifratura e si paga solo
        # alla fine, come "CRC finale diverso" dopo tutto il trasferimento.
        # Il seme e' l'unico valore del protocollo che il GBA ripete: la
        # palette risponde sempre lo stesso client_data. Quindi lo si pretende
        # UGUALE tre volte di fila, e ripetuto e' quasi certamente vero.
        uguali = 0
        ultimo = None
        for _ in range(DETECTION_TRIES):
            self._keep_fed(CMD_SEND_PALETTE | self.palette)
            w = self._pull(fase="palette")
            if (w & ACK_RESPONSE_MASK) != ACK_RESPONSE:
                continue
            if w == ultimo:
                uguali += 1
                if uguali >= 2:          # tre letture uguali in tutto
                    return w & 0xFF
            else:
                ultimo = w
                uguali = 0
        raise MultibootError(
            "il GBA non ha mai ripetuto lo stesso 0x73cc tre volte alla "
            f"palette (ultimo visto: 0x{ultimo:04X})" if ultimo is not None
            else "il GBA non ha mai risposto 0x73cc alla palette")

    def _handshake_e_lunghezza(self, cc, rom_size, coda=(), primo_dato=0x00C0):
        """6-8. 0x64hh, l'attesa di 1/16 s, e la parola di lunghezza.

        hh = 0x11 + client_data[1..3], con 0xFF per gli slave assenti.

        L'attesa e' l'unico punto in cui GBATEK vuole che il master STIA ZITTO,
        e noi non possiamo smettere di clockare. Si ottiene lo stesso effetto
        allungando il periodo del master: nel PIO il valore di timing viaggia
        insieme alla parola e vale come ritardo DOPO di essa, quindi 0x64hh
        esce con timing lungo e la lunghezza arriva 70 ms piu' tardi. Le due
        parole stanno nello stesso blocco: nessun buco in cui infilare un idle.
        """
        hh = (HANDSHAKE_DATA + cc + 0xFF + 0xFF) & 0xFF
        llll = (rom_size - 0x190) // 4

        # Qui la coda TX e' vuota, ma il master non puo' clockare: l'ultima
        # parola dell'iniziazione e' uscita col periodo lungo, quindi ci sono
        # ~70 ms prima dello scambio successivo. E' la finestra in cui si legge
        # cc, si cifra la ROM e si accoda tutto il resto - ed e' anche l'attesa
        # di 1/16 s che GBATEK chiede fra 0x64hh e la lunghezza, ottenuta senza
        # smettere di clockare.
        self._push([CMD_CONFIRM_HANDSHAKE | hh, llll] + list(coda))

        # Ora non ci sono piu' residui da indovinare: il blocco dell'header
        # conteneva un numero FISSO di 0x63pp e ne sono state lette altrettante
        # risposte, quindi le due che arrivano adesso sono esattamente uu e rr,
        # in quest'ordine. Niente conteggi variabili, niente riagganci.
        uu = self._pull(timeout=5.0, fase="0x64hh")
        rr_w = self._pull(timeout=5.0, fase="lunghezza")
        for eti, w in (("0x64hh", uu), ("lunghezza", rr_w)):
            if (w & ACK_RESPONSE_MASK) != ACK_RESPONSE:
                raise MultibootError(
                    f"{eti}: risposta 0x{w:04X}, attesa 0x73xx. Il flusso e'"
                    " sfasato: la prossima risposta dei dati non sarebbe "
                    f"0x{primo_dato:04X}.")
        if self.timing_fast is not None:
            self.link.set_timing(self.timing_fast, "ritorno al timing normale")
        rr = rr_w & 0xFF
        self.rr = rr
        self.coda73 = [uu, rr_w]
        self.nonc = [uu, rr_w]
        self._diag_hh, self._diag_cc = hh, cc
        self._log(f"cc 0x{cc:02X}  hh 0x{hh:02X}  uu 0x{uu:04X}  rr 0x{rr:02X}")
        crc_b = (hh | (rr << 8) | (0xFF << 16) | (0xFF << 24)) & 0xFFFFFFFF
        return hh, crc_b, 0

    def prepara_dati(self, rom, cc):
        """Cifra tutta la ROM e calcola il checksum, PRIMA di spedire.

        Si fa qui, e non dentro _rom, per un motivo di trasporto: il seme
        dipende solo da palette e client_data - non da rr, che arriva dopo -
        quindi le prime parole di dati si possono accodare insieme alla parola
        di lunghezza e chiudere anche quel buco.
        """
        parti = len(rom) // 4
        seed = (self.palette | (cc << 8) | (0xFF << 16) | (0xFF << 24))
        crc_c = CRCC_START_MULTI
        da_mandare = []
        attesi = []
        for i in range(HEADER_SIZE // 4, parti):
            seed = (seed * SEED_MULTIPLIER + 1) & 0xFFFFFFFF
            plain = int.from_bytes(rom[i * 4:i * 4 + 4], "little")
            enc = (plain ^ ((0xFE000000 - (i << 2)) & 0xFFFFFFFF)
                   ^ seed ^ DATA_XOR_MULTI) & 0xFFFFFFFF
            da_mandare.append(enc & 0xFFFF)
            da_mandare.append(enc >> 16)
            attesi.append((i << 2) & 0xFFFF)
            attesi.append(((i << 2) + 2) & 0xFFFF)
            crc_c = crc_step(crc_c, plain)
        return da_mandare, attesi, crc_c

    def _rom(self, da_mandare, attesi, crc_c, crc_b, gia_spedite=0,
             gia_lette=0, avanti=160):
        """9-10. I dati cifrati, poi il CRC finale.

        Ogni parola da 32 bit esce in due half-word (bassa poi alta) e il child
        risponde con l'indirizzo di destinazione: 0x00C0, 0x00C2, 0x00C4...
        E' la migliore verifica che ci sia - se il flusso scivola di una
        parola, si vede al primo confronto e non a fine trasferimento.

        "avanti" e' quanto si sta davanti: la coda del firmware e' profonda 256
        parole, oltre le scarta in silenzio (g_rawRelayTxQueueDrops, che da qui
        non si vede). Sotto, si rischia di svuotarla e farsi iniettare 0x7FFF.
        """
        totale = len(da_mandare)
        spedite = gia_spedite
        lette = gia_lette      # la prima l'ha gia' letta _handshake_e_lunghezza
        ultimo_avviso = 0
        coda_finale = False
        while lette < totale:
            while spedite < totale and (spedite - lette) < avanti:
                blocco = da_mandare[spedite:spedite + 32]
                self._push(blocco)
                spedite += len(blocco)
            if spedite >= totale and not coda_finale and self.sovrapponi:
                # Terzo e ultimo buco: dopo l'ultimo dato la coda si
                # svuoterebbe mentre si leggono le risposte che restano.
                # 0x0065 e' ripetibile per costruzione, quindi qui e' il
                # riempitivo giusto e non un espediente.
                coda_finale = True
                self._push([CMD_ROM_END] * self.scorta)
            w = self._pull(fase="dati")
            if w != attesi[lette]:
                raise MultibootError(
                    f"dati: alla half-word {lette} di {totale} il GBA ha "
                    f"risposto 0x{w:04X} invece di 0x{attesi[lette]:04X} "
                    "(scostamento nel flusso: una parola in piu' o in meno)")
            lette += 1
            if self.verbose and lette - ultimo_avviso >= totale // 10 + 1:
                ultimo_avviso = lette
                print(f"[mb  ] {lette * 100 // totale:3d}%", end="\r",
                      flush=True)
        if self.verbose:
            print("[mb  ] 100%    ")

        crc_c &= 0xFFFF
        self._diag = (crc_c, getattr(self, "_diag_hh", 0),
                      getattr(self, "_diag_cc", 0))
        crc_c = crc_step(crc_c, crc_b) & 0xFFFF

        # 0x0065 e' ripetibile per costruzione ("wait until all slaves reply
        # 0075 instead 0074"), quindi la coda si tiene piena a batch.
        pronto = False
        for _ in range(MAX_ROM_END_TRIES):
            self._keep_fed(CMD_ROM_END)
            w = self._pull(timeout=5.0, fase="fine dati")
            if w == ACK_ROM_END:
                pronto = True
                break
        if not pronto:
            raise MultibootError(
                "il GBA non ha mai risposto 0x0075: non si e' mai dichiarato "
                "pronto per il CRC finale")

        in_volo = self.sent - self.recv
        self._push([CMD_FINAL_CRC, crc_c])
        # Anche qui niente conteggi: i residui di 0x0065 e la risposta a
        # 0x0066 valgono tutti 0x0075, quindi la prima risposta DIVERSA da
        # 0x0075 e' per forza il CRC dello slave. (Se il nostro CRC valesse
        # proprio 0x0075 il ciclo si esaurirebbe senza trovarla - e in quel
        # caso ogni 0x0075 letta e' gia' la risposta giusta.)
        for _ in range(in_volo + 3 * self.scorta + 8):
            w = self._pull(timeout=5.0, fase="CRC")
            if w == ACK_ROM_END:
                self.skipped += 1
                continue
            if w != crc_c:
                self.diagnosi_crc(w, crc_c)
                raise MultibootError(
                    f"CRC finale diverso: il GBA dice 0x{w:04X}, noi "
                    f"0x{crc_c:04X}. I dati sono arrivati ma non sono quelli "
                    "che abbiamo mandato.")
            return crc_c
        if crc_c == ACK_ROM_END:
            return crc_c
        raise MultibootError(
            f"il GBA non ha mai mandato il suo CRC (solo 0x{ACK_ROM_END:04X})")

    # -- la corsa ----------------------------------------------------------

    def run(self, rom_bytes):
        rom = prepara_rom(rom_bytes)
        ultimo = None
        for tentativo in range(1, self.max_attempts + 1):
            self.reset_counters()
            self.restarts = tentativo - 1
            if tentativo > 1:
                self._log(f"tentativo {tentativo} di {self.max_attempts}")
                # GBATEK: se il detect fallisce si aspetta 1/16 s e si
                # RICOMINCIA la sessione, non si insiste. Qui vale per ogni
                # fallimento: se il flusso e' scivolato, riportare il PIO allo
                # stato iniziale e' l'unico modo di ripartire davvero puliti.
                riavvia = getattr(self.link, "restart", None)
                if riavvia is not None:
                    riavvia()
                time.sleep(0.5)
            self.link.drain_rx(0.3)
            t0 = time.time()
            try:
                self._detect()
                # Ogni blocco deterministico si sovrappone alla fase dopo, cosi'
                # la coda TX non tocca mai lo zero: e' la regola che tiene in
                # piedi tutto il file, e i tre punti in cui va applicata sono
                # esattamente i tre confini fra un blocco calcolato e il
                # successivo (header->palette, lunghezza->dati, dati->fine).
                # Iniziazione in un blocco unico e deterministico: comando,
                # header, i due handshake e un numero FISSO di palette. La
                # palette non e' piu' una fase a se' che si ripete finche' non
                # risponde - non puo' esserlo, perche' ogni 0x63pp cambia il
                # client_data del GBA.
                cc = self._header(rom, n_palette=4 if self.sovrapponi else 1)
                self._log(f"handshake ok (client_data 0x{cc:02X})")
                dati, attesi, crc_dati = self.prepara_dati(rom, cc)
                testa = dati[:4 * self.scorta] if self.sovrapponi else []
                hh, crc_b, gia_lette = self._handshake_e_lunghezza(
                    cc, len(rom), coda=testa, primo_dato=attesi[0])
                crc_c = self._rom(dati, attesi, crc_dati, crc_b,
                                  gia_spedite=len(testa),
                                  gia_lette=gia_lette)
                durata = time.time() - t0
                self._log(f"DONE!  CRC 0x{crc_c:04X}")
                return {
                    "byte": len(rom),
                    "durata": durata,
                    "parole_inviate": self.sent,
                    "risposte_lette": self.recv,
                    "scartate": self.skipped,
                    "desync": self.desync,
                    "riavvii": self.restarts,
                    "parole_s": self.recv / durata if durata else 0.0,
                    "crc": crc_c,
                }
            except MultibootError as e:
                self.desync += 1
                ultimo = e
                self._log(f"FALLITO: {e}")
                if self.verbose:
                    self.dump_trace()
        raise MultibootError(
            f"multiboot fallito dopo {self.max_attempts} tentativi. "
            f"Ultimo errore: {ultimo}")


def stampa_rapporto(st):
    print()
    print("  byte spediti      : %d" % st["byte"])
    print("  durata            : %.1f s" % st["durata"])
    print("  parole inviate    : %d" % st["parole_inviate"])
    print("  risposte lette    : %d  (%.0f parole/s)"
          % (st["risposte_lette"], st["parole_s"]))
    # Puo' essere > 0 senza che niente sia rotto: sono le ripetizioni ancora in
    # volo quando una fase finisce. Si stampa perche' se esplode e' il primo
    # sintomo di un idle iniettato.
    print("  scartate al riaggancio : %d" % st["scartate"])
    print("  desync            : %d" % st["desync"])
    print("  riavvii           : %d" % st["riavvii"])
    if st["desync"] == 0 and st["riavvii"] == 0:
        print("  -> pulito: nessuna parola spuria, nessun riavvio")


# ---------------------------------------------------------------------------
# Banco di prova senza hardware: un GBA slave simulato
# ---------------------------------------------------------------------------

class SlaveSimulato:
    """Il lato child del protocollo, abbastanza fedele da smentirci.

    Non serve a "provare che il codice gira": serve a provare che la
    CIFRATURA e il CHECKSUM sono giusti. Lo slave decifra davvero i dati con
    lo stesso seme, li confronta con la ROM originale, e calcola il CRC per
    conto suo con le costanti multiplay. Se una delle tre costanti che
    cambiano fra normal e multiplay fosse sbagliata, qui si vede - sull'
    hardware si vedrebbe solo come "CRC finale diverso" dopo 30 secondi.
    """

    def __init__(self, rom, cc=0x5A, uu=0x11, rr=0xC3, ritardo_detect=3):
        self.rom = rom
        self.cc = cc
        self.uu = uu
        self.rr = rr
        self.ritardo_detect = ritardo_detect
        self.stato = "detect"
        self.header = bytearray()
        self.header_rimasti = HEADER_PARTS
        self.post = 0
        self.palette = None
        self.hh = None
        self.llll = None
        self.seed = 0
        self.crc = CRCC_START_MULTI
        self.parte = HEADER_SIZE // 4
        self.meta_alta = False
        self.bassa = 0
        self.ricevuti = bytearray()
        self.end_attese = 2
        self.crc_finale = None
        self.errori = []

    def exchange(self, w):
        s = self.stato
        if s == "morto":
            # Cio' che ha fatto il GBA vero il 2026-08-02 quando gli e' arrivata
            # una parola spuria dopo 0x6202: ha smesso di trasmettere del tutto,
            # e il master ha letto 0xFFFF (nessuno slave) per sempre. Modellarlo
            # cosi' e' l'unico modo di far vedere il difetto al banco di prova.
            return 0xFFFF
        if s == "detect":
            if w == CMD_HANDSHAKE:
                if self.ritardo_detect > 0:
                    self.ritardo_detect -= 1
                    return 0xFFFF if self.ritardo_detect else 0x0000
                return ACK_HANDSHAKE | CLIENT_BIT
            if w == (CMD_CONFIRM_CLIENTS | CLIENT_BIT):
                self.stato = "header"
                return ACK_HANDSHAKE | CLIENT_BIT
            return 0xFFFF
        if s == "header":
            self.header += w.to_bytes(2, "little")
            r = (self.header_rimasti << 8) | CLIENT_BIT
            self.header_rimasti -= 1
            if self.header_rimasti == 0:
                self.stato = "post"
            return r
        if s == "post":
            self.post += 1
            if self.post == 1:
                return CLIENT_BIT                      # 0x000y
            self.stato = "palette"
            return ACK_HANDSHAKE | CLIENT_BIT          # 0x720y
        if s == "palette":
            if (w & 0xFF00) == CMD_SEND_PALETTE:
                self.palette = w & 0xFF
                # IL GBA VERO RIGENERA client_data A OGNI 0x63pp (misurato sul
                # filo il 2026-08-02: 22 risposte di fila tutte diverse). Vale
                # quella dell'ULTIMO 0x63pp mandato, ed e' il valore che entra
                # nel seme della cifratura. Il primo modello ne rispondeva uno
                # fisso, quindi mandare parole di riempimento sembrava gratis:
                # sul filo cambiava il seme sotto i piedi e si pagava alla fine
                # come "CRC finale diverso".
                self.cc = (self.cc * 73 + 41) & 0xFF
                return ACK_RESPONSE | self.cc
            if (w & 0xFF00) == CMD_CONFIRM_HANDSHAKE:
                self.hh = w & 0xFF
                atteso = (HANDSHAKE_DATA + self.cc + 0xFF + 0xFF) & 0xFF
                if self.hh != atteso:
                    self.errori.append(
                        f"hh sbagliato: 0x{self.hh:02X} invece di 0x{atteso:02X}")
                self.stato = "lunghezza"
                return ACK_RESPONSE | self.uu
            self.stato = "morto"
            return 0xFFFF
        if s == "lunghezza":
            self.llll = w
            atteso = (len(self.rom) - 0x190) // 4
            if w != atteso:
                self.errori.append(
                    f"lunghezza sbagliata: {w} invece di {atteso}")
            self.seed = (self.palette | (self.cc << 8)
                         | (0xFF << 16) | (0xFF << 24))
            self.stato = "dati"
            return ACK_RESPONSE | self.rr
        if s == "dati":
            if not self.meta_alta:
                self.seed = (self.seed * SEED_MULTIPLIER + 1) & 0xFFFFFFFF
                self.bassa = w
                self.meta_alta = True
                return (self.parte << 2) & 0xFFFF
            enc = (self.bassa | (w << 16)) & 0xFFFFFFFF
            plain = (enc ^ ((0xFE000000 - (self.parte << 2)) & 0xFFFFFFFF)
                     ^ self.seed ^ DATA_XOR_MULTI) & 0xFFFFFFFF
            self.ricevuti += plain.to_bytes(4, "little")
            self.crc = crc_step(self.crc, plain)
            r = ((self.parte << 2) + 2) & 0xFFFF
            self.parte += 1
            self.meta_alta = False
            if self.parte >= len(self.rom) // 4:
                self.stato = "fine"
                self.crc &= 0xFFFF
                self.crc = crc_step(self.crc, self._crc_b()) & 0xFFFF
            return r
        if s == "fine":
            if w == CMD_ROM_END:
                if self.end_attese > 0:
                    self.end_attese -= 1
                    return ACK_ROM_END_WAIT
                return ACK_ROM_END
            if w == CMD_FINAL_CRC:
                self.stato = "crc"
                return ACK_ROM_END
            return ACK_ROM_END
        if s == "crc":
            self.crc_finale = w
            return self.crc
        return 0xFFFF

    def _crc_b(self):
        return (self.hh | (self.rr << 8) | (0xFF << 16) | (0xFF << 24)) \
            & 0xFFFFFFFF

    def verifica(self):
        atteso = self.rom[HEADER_SIZE:]
        if bytes(self.ricevuti) != atteso:
            self.errori.append(
                f"dati diversi: {len(self.ricevuti)} byte ricevuti, "
                f"{len(atteso)} attesi, primo scostamento a "
                f"{next((i for i, (a, b) in enumerate(zip(self.ricevuti, atteso)) if a != b), '?')}")
        if bytes(self.header) != self.rom[:HEADER_SIZE]:
            self.errori.append("header diverso dall'originale")
        if self.crc_finale != self.crc:
            self.errori.append(
                f"CRC: master 0x{self.crc_finale:04X}, slave 0x{self.crc:04X}")
        return self.errori


class LinkSimulato:
    """Il trasporto finto, modellato sul firmware VERO.

    La prima stesura di questa classe processava ogni parola nell'istante in
    cui veniva accodata: comodo, e sbagliato. Cosi' la coda TX non era MAI
    vuota, quindi il difetto centrale del canale - `transmitCallback` che a
    coda vuota manda 0x7FFF e fa lo scambio lo stesso
    (rawRelaySection.cpp:147) - non poteva manifestarsi, e l'autotest dava
    "TUTTO PASSATO" mentre sul filo il multiboot moriva alla palette
    (2026-08-02, primo giro sull'hardware).

    Ora il modello e' quello giusto: **e' la lettura a consumare uno scambio**,
    e se in quel momento la coda e' vuota parte 0x7FFF, esattamente come fa il
    Pico. Un banco di prova che non puo' riprodurre il difetto che stai
    cercando non e' un banco di prova.
    """

    def __init__(self, slave, inietta_ogni=0, buco=1, timing_rif=3700):
        self.slave = slave
        self.inietta_ogni = inietta_ogni
        # Il tempo di pensiero dell'host e' fisso in millisecondi; QUANTI
        # scambi ci stiano dentro dipende dal periodo del master. E' l'effetto
        # su cui si regge tutta l'iniziazione: allungando il periodo con F-1,
        # lo stesso buco vale zero scambi invece di uno. Senza modellarlo, il
        # banco vedeva buchi che sul filo non esistono.
        self.timing_rif = timing_rif
        self.periodo = timing_rif
        # `buco`: quanti scambi il master fa DA SOLO nel tempo che Python
        # impiega a decidere la parola dopo. E' l'ingrediente che mancava al
        # primo modello: li' nulla consumava mentre l'host pensava, quindi la
        # coda non poteva svuotarsi e il difetto non poteva nascere. Sul filo
        # a --timing 3700 uno scambio dura ~2,4 ms e un giro USB ne dura
        # altrettanto: uno e' la stima onesta, non una comodita'.
        self.buco = buco
        self.tx = []
        self.risposte = []
        self.scambi = 0
        self.idle_mandati = 0
        self.avviato = False

    def _scambia(self, w):
        self.scambi += 1
        self.risposte.append(self.slave.exchange(w))

    def send_words(self, words):
        # Il master non aspetta l'host: nel tempo che passa fra due push ha
        # gia' fatto `buco` scambi, consumando cio' che c'era in coda e
        # riempiendo il resto di 0x7FFF. E' questo il conto che decide se la
        # scorta tenuta da _keep_fed e' abbastanza profonda.
        if self.avviato:
            for _ in range(int(self.buco * self.timing_rif / self.periodo)):
                if self.tx:
                    self._scambia(self.tx.pop(0))
                else:
                    self.idle_mandati += 1
                    self._scambia(0x7FFF)
        self.tx.extend(w & 0xFFFF for w in words)
        self.avviato = True

    def read_word(self, timeout=2.0):
        if not self.risposte:
            if self.inietta_ogni and (self.scambi + 1) % self.inietta_ogni == 0:
                self._scambia(0x7FFF)
            elif self.tx:
                self._scambia(self.tx.pop(0))
            else:
                self.idle_mandati += 1
                self._scambia(0x7FFF)
        return self.risposte.pop(0)

    def drain_rx(self, seconds=0.3):
        n = len(self.risposte)
        self.risposte.clear()
        return n

    def set_timing(self, iterations, label=None):
        self.periodo = max(1, int(iterations))


def autotest():
    """Prova la macchina a stati e la matematica senza toccare l'hardware."""
    import random
    random.seed(1234)

    # I tempi veri: il periodo del master a --timing 3700 e quello lungo che
    # l'iniziazione usa per lasciare all'host il tempo di leggere cc e cifrare
    # la ROM. Passarli SEMPRE, anche a secco: la leva del timing e' parte del
    # protocollo, non una rifinitura, e un banco che non la esercita mente.
    TIM = {"timing_fast": 3700, "timing_wait": int(70 * 1000 / 0.54)}

    rom = bytearray(random.getrandbits(8) for _ in range(0x800))
    rom[0:4] = b"\x2e\x00\x00\xea"
    rom = prepara_rom(bytes(rom))

    print("== autotest 1: corsa pulita ==")
    slave = SlaveSimulato(rom)
    link = LinkSimulato(slave)
    st = Multiboot(link, verbose=False, **TIM).run(rom)
    errori = slave.verifica()
    if errori:
        print("FALLITO:")
        for e in errori:
            print("  -", e)
        return 1
    print(f"  ok: {st['byte']} byte, {st['parole_inviate']} parole inviate, "
          f"{st['scartate']} scartate, CRC 0x{st['crc']:04X}")

    print("== autotest 1b: cc, uu e rr vanno letti per quello che sono ==")
    # Il 2026-08-02 due corse hanno sbagliato un valore ciascuna (una cc,
    # l'altra rr) e il sintomo era identico: "CRC finale diverso" a fine
    # trasferimento. Lo slave simulato usa tre valori DISTINTI apposta, cosi'
    # scambiarli non passa piu' inosservato.
    slave = SlaveSimulato(rom)          # cc=0x5A, uu=0x11, rr=0xC3
    link = LinkSimulato(slave)
    mb = Multiboot(link, verbose=False, max_attempts=1, **TIM)
    mb.run(rom)
    atteso = {"cc": slave.cc, "rr": slave.rr}
    letto = {"cc": mb._diag_cc, "rr": mb.rr}
    if letto != atteso:
        print(f"FALLITO: letti {letto}, attesi {atteso}")
        return 1
    if len(mb.nonc) != 2:
        print(f"FALLITO: fra palette e dati devono esserci 2 risposte diverse "
              f"da cc (uu e rr), ne sono state viste {len(mb.nonc)}")
        return 1
    print(f"  ok: cc=0x{letto['cc']:02X} rr=0x{letto['rr']:02X}, "
          f"e uu/rr sono esattamente 2")

    print("== autotest 2: costanti multiplay ==")
    if (CRCC_START_MULTI, CRCC_XOR_MULTI, DATA_XOR_MULTI) != \
            (0xFFF8, 0xA517, 0x6465646F):
        print("FALLITO: le costanti multiplay non sono quelle di GBATEK")
        return 1
    print("  ok: c=FFF8h x=A517h k=6465646Fh (GBATEK, ramo multiplay)")

    print("== autotest 3: una parola spuria DEVE essere vista, in OGNI fase ==")
    # Periodi scelti per far cadere l'iniezione in punti diversi del
    # protocollo: dentro il detect, dentro l'header, alla palette, nei dati.
    # Un solo periodo proverebbe solo che il controllo di QUELLA fase esiste.
    for periodo in (5, 23, 61, 97, 211):
        slave = SlaveSimulato(rom)
        link = LinkSimulato(slave, inietta_ogni=periodo)
        try:
            Multiboot(link, verbose=False, max_attempts=1, **TIM).run(rom)
        except MultibootError:
            pass
        else:
            print(f"FALLITO: con una parola ogni {periodo} scambi il "
                  "trasferimento e' andato a buon fine senza accorgersene. "
                  "E' esattamente il difetto che questo controllo esiste per "
                  "impedire.")
            return 1
    print("  ok: ogni periodo provato (5, 23, 61, 97, 211) viene nominato")

    print("== autotest 3b: il difetto del 2026-08-02 si riproduce e resta chiuso ==")
    # Il primo giro sull'hardware mori' con "il GBA non ha mai risposto 0x73cc
    # alla palette", e l'autotest di allora diceva TUTTO PASSATO. Questo
    # controllo esiste perche' quel divario non si ripeta: con la
    # sovrapposizione dei blocchi spenta il difetto DEVE tornare, con la
    # sovrapposizione accesa DEVE sparire. Se un giorno passano entrambi, non
    # e' una buona notizia: e' il banco di prova che ha smesso di misurare.
    slave = SlaveSimulato(rom)
    link = LinkSimulato(slave)
    try:
        Multiboot(link, verbose=False, max_attempts=1, sovrapponi=False, **TIM).run(rom)
    except MultibootError as e:
        prima = str(e).splitlines()[0]
        print(f"  ok: senza sovrapposizione fallisce -> {prima[-60:]}")
        print(f"      (il firmware simulato ha mandato {link.idle_mandati} "
              "parole 0x7FFF a coda vuota)")
    else:
        print("FALLITO: senza sovrapposizione doveva fallire. Il modello del "
              "trasporto non riproduce piu' il difetto reale.")
        return 1
    slave = SlaveSimulato(rom)
    link = LinkSimulato(slave)
    st = Multiboot(link, verbose=False, max_attempts=1, **TIM).run(rom)
    if slave.verifica():
        print("FALLITO: con la sovrapposizione il trasferimento e' corrotto")
        return 1
    print(f"  ok: con sovrapposizione passa, 0x7FFF a coda vuota: "
          f"{link.idle_mandati}")

    print("== autotest 3c: quanta latenza dell'host regge la scorta ==")
    # Il numero che conta: quanti scambi puo' fare il master mentre l'host
    # pensa, senza che la coda TX si svuoti. A --timing 3700 uno scambio dura
    # ~2,4 ms e un giro USB altrettanto, quindi sul filo il buco vale ~1: sotto
    # si chiede un margine di almeno 8x. Il limite superiore non e' un difetto
    # ma aritmetica (un buco piu' profondo della scorta la svuota per forza) -
    # cio' che si pretende li' e' che venga NOMINATO, non subito in silenzio.
    ultimo_ok = 0
    for buco in (1, 2, 4, 8, 16):
        slave = SlaveSimulato(rom)
        link = LinkSimulato(slave, buco=buco)
        try:
            Multiboot(link, verbose=False, max_attempts=1, **TIM).run(rom)
        except MultibootError:
            break
        if slave.verifica() or link.idle_mandati:
            break
        ultimo_ok = buco
    if ultimo_ok < 8:
        print(f"FALLITO: la scorta regge solo {ultimo_ok} scambi di buco, "
              "meno del margine 8x che il filo richiede")
        return 1
    print(f"  ok: nessun 0x7FFF a coda vuota fino a {ultimo_ok} scambi di buco "
          f"(sul filo ne serve ~1)")

    print("== autotest 4: padding a 0x10 ==")
    grezza = bytes(11948)
    if len(prepara_rom(grezza)) != 11952:
        print("FALLITO: padding sbagliato")
        return 1
    print("  ok: 11948 -> 11952 byte (mbstub.gba)")

    print("== autotest 5: i due file veri del progetto ==")
    qui = os.path.dirname(os.path.abspath(__file__))
    for nome, percorso in (
            ("siotest.gba", os.path.join(qui, "..", "hw", "siotest", "build",
                                         "siotest.gba")),
            ("mbstub.gba", os.path.join(qui, "..", "hw", "mbstub", "build",
                                        "mbstub.gba"))):
        if not os.path.isfile(percorso):
            print(f"  (saltato: {nome} non e' stato ancora costruito)")
            continue
        vera = prepara_rom(open(percorso, "rb").read())
        slave = SlaveSimulato(vera)
        link = LinkSimulato(slave)
        st = Multiboot(link, verbose=False, **TIM).run(vera)
        errori = slave.verifica()
        if errori:
            print(f"FALLITO su {nome}:")
            for e in errori:
                print("  -", e)
            return 1
        # I due numeri che servono davvero prima di sedersi davanti al GBA:
        # quanto dura, e quindi se conviene alzare o abbassare --timing.
        n = st["parole_inviate"]
        print(f"  ok: {nome}, {len(vera)} byte -> {n} parole "
              f"(~{n * 2.4 / 1000:.0f} s a --timing 3700, "
              f"~{n * 4.4 / 1000:.0f} s a --timing 7400)")

    print("\nTUTTO PASSATO (senza hardware: dice che il protocollo e' coerente,")
    print("non che il cavo funziona).")
    return 0


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Multiboot del GBA in modo MultiPlay 16 bit, via Celio "
                    "sul cavo GBA. Un cavo solo, un firmware solo.")
    ap.add_argument("rom", nargs="?", help="il .gba da caricare")
    ap.add_argument("--timing", type=int, default=3700,
                    help="periodo del master in iterazioni PIO (~540 ns): "
                         "3700 ~ 2 ms, 7400 ~ 4 ms. Default 3700.")
    ap.add_argument("--wait-ms", type=int, default=70,
                    help="attesa prima del trasferimento principale "
                         "(GBATEK: 1/16 s). 0 la disattiva.")
    ap.add_argument("--cable", choices=["auto", "gba", "gbc"], default="auto",
                    help="col cavo GBA 'auto' sceglie gia' il ramo giusto")
    ap.add_argument("--palette", type=lambda s: int(s, 0), default=PALETTE_DATA)
    ap.add_argument("--tentativi", type=int, default=3)
    ap.add_argument("--detect-s", type=float, default=5.0,
                    help="quanti secondi insistere col detect prima di "
                         "riavviare la sessione")
    ap.add_argument("--autotest", action="store_true",
                    help="prova protocollo e matematica senza hardware")
    ap.add_argument("--senza-riavvio", action="store_true",
                    help="NON riavviare il Pico a fine multiboot. Serve solo a "
                         "isolare il difetto dell'endpoint inceppato: senza "
                         "riavvio il canale GBA->PC si ferma dopo ~1 s "
                         "(NOTES 2026-08-02)")
    args = ap.parse_args()

    if args.autotest:
        return autotest()
    if not args.rom:
        ap.error("serve il file .gba (oppure --autotest)")
    if not os.path.isfile(args.rom):
        print(f"file non trovato: {args.rom}")
        return 2

    rom = open(args.rom, "rb").read()
    print(f"[mb  ] {args.rom}: {len(rom)} byte")

    from usb_link import UsbLink

    # Il timing lungo dell'attesa, in iterazioni PIO da ~540 ns. La guardia del
    # firmware e' un minimo (200), non un massimo: qui si va nella direzione
    # opposta.
    timing_wait = max(args.timing, int(args.wait_ms * 1000 / 0.54)) \
        if args.wait_ms > 0 else None

    link = UsbLink(timing=args.timing, cable=args.cable, raw=True)
    link.open()
    try:
        buttate = link.drain_rx(0.5)
        if buttate:
            print(f"[mb  ] {buttate} parole di rumore buttate prima di iniziare")
        mb = Multiboot(link, palette=args.palette, wait_ms=args.wait_ms,
                       timing_fast=args.timing, timing_wait=timing_wait,
                       max_attempts=args.tentativi, detect_s=args.detect_s)
        try:
            st = mb.run(rom)
        except MultibootError as e:
            print(f"\n[mb  ] FALLITO: {e}")
            return 1
        stampa_rapporto(st)

        # F-4: i ~6000 scambi del multiboot lasciano l'endpoint USB del Pico
        # in uno stato da cui la sessione di link non riparte (il canale
        # GBA->PC si ferma dopo ~1 s; NOTES 2026-08-02). Il rimedio provato e'
        # il power cycle del Pico — che finora era un gesto sul filo. Il Pico
        # e' alimentato da USB, quindi sys_reboot e' lo stesso identico reset:
        # da qui in poi lo fa il software, e la procedura resta "multiboot,
        # cartuccia, usb_link" senza mani sul cavo.
        if not args.senza_riavvio:
            # MEZZO SECONDO DI QUIETE PRIMA DELLA F-4 (2026-08-25). Il DONE
            # arriva quando NOI abbiamo letto il CRC dello slave; il BIOS del
            # GBA in quell'istante sta ancora chiudendo il multiboot e
            # saltando nel programma. Il riavvio del Pico fa sobbalzare le
            # linee del cavo (i GPIO tornano allo stato di reset), e un
            # sobbalzo su SC in quella finestra e' il sospettato del sintomo
            # "DONE! ma il GBA resta sul logo, niente schermo rosso" - visto
            # raramente in locale e 3 volte su 3 dal sito la sera del
            # 2026-08-25. In emulatore (senza Pico) lo stesso stub arriva
            # sempre al rosso: la gara, se c'e', e' solo qui. Mezzo secondo
            # e' un'eternita' per il salto del BIOS e non costa niente.
            time.sleep(0.5)
            print("\n[mb  ] riavvio il Pico (F-4), al posto dello "
                  "scollega/ricollega...")
            sparito, ricomparso = link.reboot_pico()
            if ricomparso:
                print("[mb  ] Pico sparito e ricomparso dal bus: canale pulito."
                      "\n[mb  ] Inserisci la cartuccia e lancia usb_link.py "
                      "senza toccare il cavo.")
            elif not sparito:
                print("[mb  ] il Pico NON e' mai sparito dal bus: il firmware "
                      "non ha la F-4 (serve >= 2.0.5).\n"
                      "[mb  ] Flasha hw/firmware/celio-f1f2b-f3-f4.uf2 "
                      "(BOOTSEL), oppure scollega e ricollega a mano.")
            else:
                print("[mb  ] il Pico e' sparito ma NON e' ricomparso entro "
                      "10 s: scollega e ricollega a mano.")
        return 0
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
