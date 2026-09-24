#!/usr/bin/env python3
"""
port_syms.py - ritrova sulla ROM italiana i simboli che gen_syms.py prende dal
.map della build USA, e genera payload/game_syms_it.h.

PERCHE' ESISTE
--------------
Il payload chiama routine del gioco per indirizzo. Quegli indirizzi vengono dal
.map di pokeemerald, che e' la build USA (BPEE). Sulla cartuccia italiana (BPEI)
stanno altrove: misurato con tools/rom_probe.py, la regione di codice delle due
ROM ha ZERO pagine da 4 KB in comune. Un .map italiano non esiste e non
esistera' mai, quindi gli indirizzi vanno ritrovati leggendo la ROM.

COME
----
Tre metodi, in cascata, a confidenza decrescente. Nessuno dei tre indovina: o
un simbolo si risolve con un riscontro unico, o viene dichiarato irrisolto.

1. FIRMA MASCHERATA. Dal .map si conoscono inizio e fine di ogni funzione. Si
   prendono i suoi byte dalla ROM USA e si mettono a wildcard tutti i
   riferimenti che fra due localizzazioni cambiano per forza:
     - le word dei literal pool che puntano a ROM/EWRAM/IWRAM;
     - le coppie BL, il cui offset e' relativo alla distanza fra chiamante e
       chiamato.
   Restano gli opcode e i salti interni, che sono la logica pura: stesso
   compilatore, stessa sorgente, quindi identici. Un match unico nella ROM
   italiana e' l'indirizzo. Zero match o piu' di uno: fallimento dichiarato.

2. ANCORAGGIO SUL CALL GRAPH. E' il metodo forte, e non e' un ripiego. Se A e'
   gia' stata localizzata, ogni BL dentro A punta a una funzione: si legge il
   target nella copia USA e nella copia italiana ALLA STESSA POSIZIONE. Se il
   target USA e' un simbolo che ci interessa, il target italiano e' il suo
   indirizzo, senza cercare niente. Vale anche al contrario, come verifica.

3. LITERAL POOL PER I DATI. gMain, gSaveBlock1Ptr, gObjectEvents e gli altri
   non hanno un corpo da cercare: esistono solo come word dentro i pool delle
   funzioni. Localizzata la funzione, si legge la word alla stessa posizione.
   Si pretende che lo stesso dato esca identico da ALMENO DUE funzioni diverse:
   una sola conferma non basta.

Il procedimento e' iterativo: ogni simbolo risolto puo' sbloccarne altri, e si
gira finche' non si scopre piu' niente.

Uso:
    python tools/port_syms.py <pokeemerald.map> <pokeemerald.gba> <rom-italiana.gba>

Uscite:
    payload/game_syms_it.h   - generato, non si modifica a mano
    build/port_report.md     - una riga per simbolo: come e' stato trovato

REGOLA DEL PROGETTO: un indirizzo che non viene da qui, o dal .map, non si usa.
"""

import argparse
import bisect
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_syms import FUNCTIONS, DATA, ARM_FUNCTIONS, parse_map  # noqa: E402

ROM_BASE = 0x08000000
ROM_END = 0x0A000000

# Lunghezza della firma. Sotto il minimo una funzione e' troppo corta perche' un
# match sia significativo e si rimanda al call graph; oltre il massimo si taglia,
# perche' non serve altro per essere univoci e la ricerca costa.
SIG_MIN = 24
SIG_MAX = 768

# Sotto questa frazione di byte non mascherati la firma e' quasi tutta wildcard e
# non prova niente, anche se il match fosse unico.
MIN_CONCRETE_RATIO = 0.40


def is_pointer(value):
    """Word che quasi certamente e' un indirizzo, quindi cambia fra localizzazioni."""
    return (ROM_BASE <= value < ROM_END          # ROM
            or 0x02000000 <= value < 0x02040000  # EWRAM
            or 0x03000000 <= value < 0x03008000)  # IWRAM


def decode_pointer(value):
    """Riconosce un indirizzo dentro una word di literal pool, nelle tre forme in
    cui il compilatore lo scrive, e lo riporta alla forma canonica.

    Le tre forme:
      - diretta, per un dato o una chiamata;
      - con il bit Thumb, quando l'indirizzo di una funzione viene preso come
        PUNTATORE invece che chiamato - e' il caso di SetMainCallback2(CB2_X),
        che infatti non produce nessuna BL verso CB2_X;
      - negata, perche' GCC compila `cb2 == ADDR` come `cb2 + (-ADDR) == 0` e nel
        pool finisce -ADDR (0xF7E55464 = -0x081AAB9C). Ci si e' gia' inciampati
        una volta: NOTES.md:36-39.

    Ritorna (indirizzo_canonico, descrizione_della_forma) oppure (None, None).
    """
    if is_pointer(value):
        canon = (value & ~1) if ROM_BASE <= value < ROM_END else value
        return canon, ("puntatore a funzione" if value & 1 else "diretta")
    neg = (-value) & 0xFFFFFFFF
    if is_pointer(neg):
        canon = (neg & ~1) if ROM_BASE <= neg < ROM_END else neg
        return canon, ("negata, puntatore a funzione" if neg & 1 else "negata")
    return None, None


def function_extent(sorted_addrs, addr, hard_cap=SIG_MAX):
    """Fine della funzione = simbolo successivo in ordine di indirizzo."""
    i = bisect.bisect_right(sorted_addrs, addr)
    if i < len(sorted_addrs):
        return min(sorted_addrs[i] - addr, hard_cap)
    return hard_cap


class Signature:
    """Byte di una funzione piu' la maschera di cio' che va ignorato."""

    def __init__(self, data, mask, bl_sites, pool_sites):
        self.data = data            # bytes
        self.mask = mask            # bytearray, 1 = confronta, 0 = ignora
        self.bl_sites = bl_sites    # [(offset, target_addr_usa)]
        self.pool_sites = pool_sites  # [(offset, valore_usa)]

    @property
    def concrete(self):
        return sum(self.mask)

    @property
    def ratio(self):
        return self.concrete / len(self.mask) if self.mask else 0.0


def build_signature(rom, base_addr, addr, length):
    """Estrae i byte della funzione e maschera cio' che cambia fra localizzazioni.

    Il codice Gen 3 e' Thumb. Interessano due forme:
      - LDR Rd,[PC,#imm]  -> opcode 0x48..0x4F: carica una word da un literal
        pool. Se quella word e' un puntatore, va a wildcard;
      - BL               -> coppia di halfword 0xF000-0xF7FF + 0xF800-0xFFFF,
        offset relativo: sempre a wildcard.
    I salti interni (B, Bcond) NON si mascherano: sono relativi ma restano
    dentro la funzione, quindi se il codice e' identico lo sono anche loro. Sono
    la parte piu' informativa della firma.
    """
    off = addr - base_addr
    data = rom[off:off + length]
    if len(data) < length:
        return None
    mask = bytearray(b"\x01" * length)
    bl_sites = []
    pool_sites = []
    pool_offsets = set()

    i = 0
    while i + 1 < length:
        hw = int.from_bytes(data[i:i + 2], "little")

        # LDR Rd,[PC,#imm8]
        if 0x4800 <= hw <= 0x4FFF:
            imm = (hw & 0xFF) * 4
            pc = (addr + i + 4) & ~3
            target = pc + imm
            t_off = target - addr
            if 0 <= t_off + 4 <= length:
                value = int.from_bytes(data[t_off:t_off + 4], "little")
                pool_offsets.add(t_off)
                canon, _form = decode_pointer(value)
                if canon is not None:
                    pool_sites.append((t_off, canon))
                    for k in range(4):
                        mask[t_off + k] = 0
            i += 2
            continue

        # BL: coppia di halfword.
        if 0xF000 <= hw <= 0xF7FF and i + 3 < length:
            hw2 = int.from_bytes(data[i + 2:i + 4], "little")
            if 0xF800 <= hw2 <= 0xFFFF:
                off_hi = hw & 0x7FF
                off_lo = hw2 & 0x7FF
                delta = (off_hi << 12) | (off_lo << 1)
                if delta & (1 << 22):
                    delta -= (1 << 23)
                target = addr + i + 4 + delta
                bl_sites.append((i, target))
                for k in range(4):
                    mask[i + k] = 0
                i += 4
                continue
        i += 2

    return Signature(bytes(data), mask, bl_sites, pool_sites)


def search(rom, sig, limit=8):
    """Cerca la firma nella ROM. Ritorna la lista degli offset che combaciano."""
    parts = []
    run = []
    for i, m in enumerate(sig.mask):
        if m:
            run.append(sig.data[i])
        else:
            if run:
                parts.append(re.escape(bytes(run)))
                run = []
            parts.append(b".")
    if run:
        parts.append(re.escape(bytes(run)))
    pattern = re.compile(b"".join(parts), re.DOTALL)

    hits = []
    for m in pattern.finditer(rom):
        hits.append(m.start())
        if len(hits) > limit:
            break
    return hits


def read_bl_target(rom, base_addr, addr, offset):
    """Rilegge il target di un BL a una data posizione dentro una funzione."""
    off = addr - base_addr + offset
    hw = int.from_bytes(rom[off:off + 2], "little")
    hw2 = int.from_bytes(rom[off + 2:off + 4], "little")
    if not (0xF000 <= hw <= 0xF7FF and 0xF800 <= hw2 <= 0xFFFF):
        return None
    delta = ((hw & 0x7FF) << 12) | ((hw2 & 0x7FF) << 1)
    if delta & (1 << 22):
        delta -= (1 << 23)
    return addr + offset + 4 + delta


class Porter:
    def __init__(self, symbols, rom_usa, rom_ita):
        self.symbols = symbols                      # nome -> addr USA
        self.rom_usa = rom_usa
        self.rom_ita = rom_ita
        self.sorted_addrs = sorted(set(a for a in symbols.values()
                                       if ROM_BASE <= a < ROM_END))
        # addr USA -> nome, per TUTTI i simboli del .map: e' la rete su cui si
        # propaga il call graph. Limitarla ai 42 che ci servono sprecherebbe
        # 32000 ancoraggi gia' disponibili.
        self.by_addr = {}
        for name, addr in symbols.items():
            self.by_addr.setdefault(addr, name)
        self.resolved = {}                          # nome -> addr ITA
        self.method = {}                            # nome -> descrizione
        self.evidence = {}                          # nome -> lista di conferme
        self.conflicted = set()                     # simboli con catene divergenti
        self.sig_cache = {}
        self._locating = set()
        self.bl_index = None                        # target USA -> [addr della BL]
        self.word_index = None                      # valore USA -> [offset della word]

    def wanted(self):
        return list(FUNCTIONS) + list(DATA)

    def containing_symbol(self, addr):
        """Il simbolo del .map che contiene questo indirizzo."""
        i = bisect.bisect_right(self.sorted_addrs, addr) - 1
        if i < 0:
            return None, 0
        start = self.sorted_addrs[i]
        return self.by_addr.get(start), start

    # --- indici inversi sulla ROM USA ---------------------------------------
    def build_indexes(self):
        """Scandisce la regione di codice della ROM USA una volta sola per sapere,
        dato un simbolo, CHI lo chiama e CHI ne carica l'indirizzo.

        Serve per i simboli che la firma non sa prendere: le funzioni troppo
        corte (PlaySE, ArePlayerFieldControlsLocked) e i dati in RAM, che un
        corpo da cercare non ce l'hanno proprio.
        """
        end = self.sorted_addrs[-1] - ROM_BASE + 0x1000
        end = min(end, len(self.rom_usa))
        rom = self.rom_usa

        bl_index = {}
        i = 0
        while i + 3 < end:
            hw = rom[i] | (rom[i + 1] << 8)
            if 0xF000 <= hw <= 0xF7FF:
                hw2 = rom[i + 2] | (rom[i + 3] << 8)
                if 0xF800 <= hw2 <= 0xFFFF:
                    delta = ((hw & 0x7FF) << 12) | ((hw2 & 0x7FF) << 1)
                    if delta & (1 << 22):
                        delta -= (1 << 23)
                    target = ROM_BASE + i + 4 + delta
                    bl_index.setdefault(target, []).append(ROM_BASE + i)
                    i += 4
                    continue
            i += 2

        word_index = {}
        for off in range(0, end - 3, 4):
            value = int.from_bytes(rom[off:off + 4], "little")
            canon, _form = decode_pointer(value)
            if canon is not None:
                word_index.setdefault(canon, []).append(off)

        self.bl_index = bl_index
        self.word_index = word_index
        print(f"  indice BL   : {len(bl_index)} bersagli distinti")
        print(f"  indice word : {len(word_index)} valori distinti")

    def locate(self, name, depth=2):
        """Localizza un simbolo QUALUNQUE del .map: prima per firma, e se non basta
        risalendo a chi lo nomina.

        La ricorsione serve davvero: un simbolo puo' essere nominato solo da una
        funzione che a sua volta e' irriconoscibile per firma. Senza risalire di
        un altro passo si resta fermi.
        """
        if name in self.resolved:
            return self.resolved[name]
        if name in self.conflicted or name in self._locating:
            return None
        addr, why = self.by_signature(name)
        if addr is not None:
            self.record(name, addr, why, "firma diretta")
            return addr
        if depth <= 0:
            return None
        self._locating.add(name)
        try:
            return self.resolve_by_referrer(name, max_tries=8, depth=depth - 1)
        finally:
            self._locating.discard(name)

    def record(self, name, addr, method, evidence):
        if name in self.conflicted:
            return
        if name in self.resolved:
            if self.resolved[name] != addr:
                # Due catene indipendenti danno risposte diverse: il simbolo non
                # e' affidabile. Per i 42 che ci servono e' un errore fatale, per
                # gli altri basta toglierlo dalla rete.
                if name in self.wanted():
                    raise SystemExit(
                        f"ERRORE: {name} risolto a 0x{self.resolved[name]:08X} "
                        f"ma {evidence} dice 0x{addr:08X}. Una delle due catene e' "
                        f"sbagliata, e finche' non si sa quale non si va avanti.")
                self.conflicted.add(name)
                self.resolved.pop(name, None)
                return
            self.evidence.setdefault(name, []).append(evidence)
            return
        self.resolved[name] = addr
        self.method[name] = method
        self.evidence.setdefault(name, []).append(evidence)

    def resolve_by_referrer(self, name, max_tries=25, depth=2):
        """Risolve un simbolo partendo da chi lo nomina.

        Per una funzione: si cercano le BL che la chiamano, si localizza per firma
        la funzione che contiene una di quelle BL, e si rilegge il target alla
        stessa posizione nella ROM italiana.
        Per un dato in RAM: stessa cosa con le word dei literal pool.
        """
        usa_addr = self.symbols[name]
        tried = 0

        # 1) chi la chiama con una BL (solo per simboli in ROM)
        for site in self.bl_index.get(usa_addr, []):
            if tried >= max_tries:
                break
            caller, caller_start = self.containing_symbol(site)
            if caller is None or caller == name:
                continue
            tried += 1
            ita_caller = self.locate(caller, depth)
            if ita_caller is None:
                continue
            offset = site - caller_start
            target = read_bl_target(self.rom_ita, ROM_BASE, ita_caller, offset)
            if target is None or not (ROM_BASE <= target < ROM_END):
                continue
            self.record(name, target, f"call graph inverso: BL da {caller}",
                        f"BL da {caller}")
            return self.resolved.get(name)

        # 2) chi ne carica l'indirizzo da un literal pool
        confirmations = {}
        for off in self.word_index.get(usa_addr, []):
            if tried >= max_tries:
                break
            holder, holder_start = self.containing_symbol(ROM_BASE + off)
            if holder is None or holder == name:
                continue
            tried += 1
            ita_holder = self.locate(holder, depth)
            if ita_holder is None:
                continue
            delta = (ROM_BASE + off) - holder_start
            ita_off = ita_holder - ROM_BASE + delta
            raw = self.rom_ita[ita_off:ita_off + 4]
            if len(raw) < 4:
                continue
            canon, _form = decode_pointer(int.from_bytes(raw, "little"))
            if canon is None:
                continue
            confirmations.setdefault(canon, []).append(holder)

        # Un dato vale solo se due funzioni diverse danno lo stesso indirizzo.
        agreed = [(v, s) for v, s in confirmations.items() if len(set(s)) >= 2]
        if len(agreed) == 1:
            value, sources = agreed[0]
            self.record(name, value,
                        f"literal pool, confermato da {len(set(sources))} funzioni",
                        "; ".join(f"pool di {s}" for s in sorted(set(sources))[:4]))
            return self.resolved.get(name)
        if len(agreed) > 1:
            raise SystemExit(
                f"ERRORE: {name} ha piu' valori confermati da due funzioni: "
                + ", ".join(f"0x{v:08X}" for v, _ in agreed))

        # Nessun valore ha due conferme. Succede per i callback dei menu, che
        # compaiono UNA volta sola: SetMainCallback2(CB2_X) mette il puntatore nel
        # pool di una sola funzione e non produce nessuna BL. Qui la seconda
        # conferma la da' la firma, verificata sul posto.
        verified = [(v, s) for v, s in confirmations.items() if self.verify_at(name, v)]
        if len(verified) == 1:
            value, sources = verified[0]
            src = sorted(set(sources))[0]
            self.record(name, value,
                        f"literal pool di {src}, firma verificata sul posto",
                        f"pool di {src}; firma confermata a destinazione")
            return self.resolved.get(name)
        if len(verified) > 1:
            raise SystemExit(
                f"ERRORE: {name} ha piu' candidati che superano la verifica di firma: "
                + ", ".join(f"0x{v:08X}" for v, _ in verified))
        return self.resolve_by_neighbour(name)

    def resolve_by_neighbour(self, name):
        """Ultimo metodo: il vicinato, e SOLO come generatore di candidati.

        Due funzioni compilate una accanto all'altra nello stesso file oggetto
        restano vicine anche nell'altra localizzazione, e spesso alla stessa
        distanza. Questo pero' NON e' una regola: qui serve solo a proporre un
        indirizzo, e l'indirizzo viene accettato unicamente se verify_at lo
        conferma - byte non mascherati E destinazioni dei riferimenti. Il
        vicinato non decide niente da solo.
        """
        usa_addr = self.symbols[name]
        if not (ROM_BASE <= usa_addr < ROM_END):
            return None
        anchors = sorted(
            ((abs(self.symbols[n] - usa_addr), n) for n in self.resolved
             if n in self.symbols and ROM_BASE <= self.symbols[n] < ROM_END),
            key=lambda t: t[0])[:12]

        hits = {}
        for _dist, anchor in anchors:
            candidate = self.resolved[anchor] + (usa_addr - self.symbols[anchor])
            if candidate % 2:
                continue
            if self.verify_at(name, candidate):
                hits.setdefault(candidate, []).append(anchor)
        if len(hits) == 1:
            candidate, sources = next(iter(hits.items()))
            self.record(name, candidate,
                        f"vicinato di {sources[0]}, firma e riferimenti verificati sul posto",
                        f"distanza da {sources[0]}; riferimenti confermati")
            return candidate
        if len(hits) > 1:
            raise SystemExit(
                f"ERRORE: {name} ha piu' candidati di vicinato che superano la verifica: "
                + ", ".join(f"0x{v:08X}" for v in hits))
        return None

    def signature_for(self, name, min_len=SIG_MIN):
        """La firma per la RICERCA pretende SIG_MIN byte: sotto quella soglia un
        riscontro in 16 MB non significherebbe niente. Per la VERIFICA PUNTUALE
        (§verify_at) la soglia non serve: li' l'indirizzo e' gia' un candidato e
        la firma deve solo confermarlo o smentirlo, quindi min_len scende."""
        key = (name, min_len)
        if key in self.sig_cache:
            return self.sig_cache[key]
        addr = self.symbols[name]
        if not (ROM_BASE <= addr < ROM_END):
            self.sig_cache[key] = None
            return None
        length = function_extent(self.sorted_addrs, addr)
        sig = build_signature(self.rom_usa, ROM_BASE, addr, length) if length >= min_len else None
        self.sig_cache[key] = sig
        return sig

    def verify_at(self, name, ita_addr):
        """Il candidato regge? Confronta la firma mascherata ESATTAMENTE a quell'
        indirizzo, invece di cercarla ovunque.

        E' una prova molto piu' forte di quanto sembri: qui non si cerca un ago
        in 16 MB, si chiede a un indirizzo gia' proposto da un'altra catena di
        somigliare alla funzione giusta. Anche una firma corta o molto mascherata,
        che come chiave di ricerca non varrebbe niente, come conferma vale.
        """
        sig = self.signature_for(name, min_len=4)
        if sig is None or not (ROM_BASE <= ita_addr < ROM_END):
            return False
        off = ita_addr - ROM_BASE
        chunk = self.rom_ita[off:off + len(sig.mask)]
        if len(chunk) < len(sig.mask):
            return False
        for i, m in enumerate(sig.mask):
            if m and chunk[i] != sig.data[i]:
                return False

        # I byte non mascherati tornano. Ma per una funzione corta e piena di
        # riferimenti quelli sono pochi, quindi si controlla anche DOVE PUNTA:
        # ogni BL e ogni puntatore che nella copia USA finisce su un simbolo gia'
        # localizzato deve finire sullo stesso simbolo anche qui. Con una rete di
        # centinaia di ancoraggi e' una prova molto piu' stringente dei byte.
        checks = 0
        for offset, usa_target in sig.bl_sites:
            target_name = self.by_addr.get(usa_target)
            if target_name is None or target_name not in self.resolved:
                continue
            got = read_bl_target(self.rom_ita, ROM_BASE, ita_addr, offset)
            if got != self.resolved[target_name]:
                return False
            checks += 1
        for offset, usa_value in sig.pool_sites:
            target_name = self.by_addr.get(usa_value)
            if target_name is None or target_name not in self.resolved:
                continue
            raw = self.rom_ita[off + offset:off + offset + 4]
            if len(raw) < 4:
                return False
            got, _form = decode_pointer(int.from_bytes(raw, "little"))
            if got != self.resolved[target_name]:
                return False
            checks += 1

        # Se la firma da sola e' quasi tutta wildcard, si pretende che almeno un
        # riferimento sia stato verificato: altrimenti non si sta provando niente.
        if sig.ratio < MIN_CONCRETE_RATIO and checks == 0:
            return False
        return True

    # --- metodo 1 -----------------------------------------------------------
    def by_signature(self, name):
        sig = self.signature_for(name)
        if sig is None:
            return None, "firma non costruibile (funzione troppo corta o non in ROM)"
        if sig.ratio < MIN_CONCRETE_RATIO:
            return None, (f"firma troppo mascherata "
                          f"({sig.concrete}/{len(sig.mask)} byte concreti)")
        hits = search(self.rom_ita, sig)
        if len(hits) == 1:
            return ROM_BASE + hits[0], (f"firma {sig.concrete}/{len(sig.mask)} byte concreti, "
                                        f"riscontro unico")
        if not hits:
            return None, f"firma su {len(sig.mask)} byte: nessun riscontro"
        return None, f"firma ambigua: {len(hits)} riscontri"

    # --- metodo 2 -----------------------------------------------------------
    def propagate_calls(self):
        """Da ogni funzione gia' localizzata, legge i BL e risolve i chiamati.

        Si propaga su tutti i simboli del .map, non solo sui 42 che servono: ogni
        funzione in piu' che si localizza e' un ancoraggio in piu' per il giro
        dopo, e i 42 sono spesso raggiungibili solo passando per funzioni che di
        per se' non ci interessano.
        """
        found = 0
        for name, ita_addr in list(self.resolved.items()):
            usa_addr = self.symbols.get(name)
            if usa_addr is None or not (ROM_BASE <= usa_addr < ROM_END):
                continue
            sig = self.signature_for(name)
            if sig is None:
                continue
            for offset, usa_target in sig.bl_sites:
                target_name = self.by_addr.get(usa_target)
                if target_name is None or target_name in self.conflicted:
                    continue
                known = target_name in self.resolved
                ita_target = read_bl_target(self.rom_ita, ROM_BASE, ita_addr, offset)
                if ita_target is None or not (ROM_BASE <= ita_target < ROM_END):
                    continue
                self.record(target_name, ita_target,
                            f"call graph: BL da {name}", f"BL da {name}")
                if not known and target_name in self.resolved:
                    found += 1
        return found

    # --- metodo 3 -----------------------------------------------------------
    def propagate_pools(self):
        """Legge i dati (EWRAM/IWRAM) dai literal pool delle funzioni localizzate.

        Un dato vale solo se due funzioni diverse danno lo stesso indirizzo: una
        conferma sola non prova niente, ed e' proprio dove un errore passerebbe
        inosservato.
        """
        candidates = {}   # nome -> {addr: [funzioni che lo confermano]}
        for name, ita_addr in list(self.resolved.items()):
            usa_addr = self.symbols.get(name)
            if usa_addr is None or not (ROM_BASE <= usa_addr < ROM_END):
                continue
            sig = self.signature_for(name)
            if sig is None:
                continue
            ita_off = ita_addr - ROM_BASE
            for offset, usa_value in sig.pool_sites:
                target_name = self.by_addr.get(usa_value)
                if target_name is None or target_name in self.conflicted:
                    continue
                raw = self.rom_ita[ita_off + offset:ita_off + offset + 4]
                if len(raw) < 4:
                    continue
                ita_value, _form = decode_pointer(int.from_bytes(raw, "little"))
                if ita_value is None:
                    continue
                candidates.setdefault(target_name, {}).setdefault(ita_value, []).append(name)

        found = 0
        for target_name, options in candidates.items():
            agreed = [(v, s) for v, s in options.items() if len(set(s)) >= 2]
            if len(agreed) != 1:
                if len(agreed) > 1 and target_name in self.wanted():
                    raise SystemExit(
                        f"ERRORE: {target_name} ha piu' valori confermati due volte: "
                        + ", ".join(f"0x{v:08X}" for v, _ in agreed))
                continue
            value, sources = agreed[0]
            known = target_name in self.resolved
            self.record(target_name, value,
                        f"literal pool, confermato da {len(set(sources))} funzioni",
                        "; ".join(f"pool di {s}" for s in sorted(set(sources))[:4]))
            if not known and target_name in self.resolved:
                found += 1
        return found

    def check_coherence(self, wanted):
        """Controllo indipendente dal metodo con cui i simboli sono stati trovati.

        Fra due localizzazioni il codice trasla a blocchi: tutte le funzioni di
        una stessa zona si spostano dello STESSO delta, e il delta cambia solo
        dove si accumula una differenza di dimensione nei dati intercalati. Quindi:

          - i dati in RAM devono avere delta ZERO, perche' la RAM la assegna il
            linker in base alle dimensioni delle struct, che i testi non toccano;
          - una funzione il cui delta non coincide con quello di NESSUNO dei suoi
            vicini nella rete e' sospetta, qualunque metodo l'abbia trovata.

        Con centinaia di simboli nella rete i blocchi sono fitti, e un simbolo
        risolto male salta fuori come un valore isolato in mezzo a un blocco
        omogeneo. E' l'unica verifica che non dipende da come ci siamo arrivati.
        """
        print()
        print("--- coerenza: i delta devono formare blocchi, non rumore")

        ram = [(n, self.symbols[n], self.resolved[n]) for n in wanted
               if n in self.resolved and not (ROM_BASE <= self.symbols[n] < ROM_END)]
        ram_bad = [(n, u, i) for n, u, i in ram if u != i]
        print(f"  dati in RAM con delta zero : {len(ram) - len(ram_bad)}/{len(ram)}")
        for n, u, i in ram_bad:
            print(f"    ATTENZIONE {n}: 0x{u:08X} -> 0x{i:08X}, delta {i - u:+d}. "
                  f"Un dato che si sposta va spiegato prima di usarlo.")

        # La rete intera, non solo i 42: e' cio' che rende il controllo fitto.
        net = sorted(((self.symbols[n], self.resolved[n] - self.symbols[n], n)
                      for n in self.resolved
                      if n in self.symbols and ROM_BASE <= self.symbols[n] < ROM_END),
                     key=lambda t: t[0])
        index = {t[2]: k for k, t in enumerate(net)}

        isolated = []
        for name in wanted:
            k = index.get(name)
            if k is None:
                continue
            delta = net[k][1]
            neighbours = []
            if k > 0:
                neighbours.append(net[k - 1][1])
            if k + 1 < len(net):
                neighbours.append(net[k + 1][1])
            if neighbours and delta not in neighbours:
                isolated.append((name, delta, neighbours))

        deltas = sorted(set(d for _a, d, _n in net))
        print(f"  blocchi di traslazione     : {len(deltas)} delta distinti su "
              f"{len(net)} simboli in ROM")
        if isolated:
            print(f"  DA GUARDARE: {len(isolated)} simboli con delta isolato "
                  f"rispetto ai vicini")
            for name, delta, neigh in isolated:
                print(f"    {name}: delta {delta:+d}, vicini "
                      + ", ".join(f"{d:+d}" for d in neigh))
            print("    Non e' per forza un errore (a un confine di blocco e' normale),")
            print("    ma e' esattamente dove si nasconderebbe un simbolo sbagliato.")
        else:
            print("  nessun simbolo con delta isolato")
        return {"ram_total": len(ram), "ram_bad": ram_bad,
                "isolated": isolated, "blocks": len(deltas), "net_rom": len(net)}

    def run(self):
        wanted = self.wanted()
        notes = {}

        print("--- indici inversi sulla ROM USA")
        self.build_indexes()

        print()
        print("--- metodo 1: firma mascherata")
        for name in wanted:
            addr = self.symbols.get(name)
            if addr is None or not (ROM_BASE <= addr < ROM_END):
                notes[name] = "non e' in ROM: si risolve solo dai literal pool"
                continue
            found, why = self.by_signature(name)
            notes[name] = why
            if found is not None:
                self.record(name, found, why, "firma diretta")
                print(f"  OK   0x{found:08X}  {name}  ({why})")
            else:
                print(f"  --                 {name}  ({why})")

        print()
        print("--- metodi 2 e 3: call graph e literal pool, fino a convergenza")
        for round_no in range(1, 16):
            gained = self.propagate_calls() + self.propagate_pools()
            done = sum(1 for n in wanted if n in self.resolved)
            print(f"  giro {round_no}: {gained} simboli nuovi in rete, "
                  f"{done}/{len(wanted)} di quelli che servono")
            if gained == 0:
                break

        missing = [n for n in wanted if n not in self.resolved]
        if missing:
            print()
            print("--- ricerca inversa per i simboli rimasti")
            for name in missing:
                got = self.resolve_by_referrer(name)
                if got is not None:
                    print(f"  OK   0x{got:08X}  {name}  ({self.method[name]})")
                else:
                    print(f"  --                 {name}  (nessun referente utilizzabile)")

            # Ogni simbolo nuovo puo' sbloccarne altri: si rigira.
            for round_no in range(1, 8):
                gained = self.propagate_calls() + self.propagate_pools()
                if gained == 0:
                    break
            still = [n for n in wanted if n not in self.resolved]
            for name in still:
                self.resolve_by_referrer(name)

        print()
        print(f"--- rete di ancoraggio: {len(self.resolved)} simboli localizzati in totale")
        if self.conflicted:
            print(f"    scartati per catene divergenti: {len(self.conflicted)}")
        return notes


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("map_usa", help="repo-studio/pokeemerald/pokeemerald.map")
    ap.add_argument("rom_usa", help="repo-studio/pokeemerald/pokeemerald.gba")
    ap.add_argument("rom_ita", help="dump della cartuccia italiana")
    args = ap.parse_args()

    map_path = Path(args.map_usa)
    rom_usa = Path(args.rom_usa).read_bytes()
    rom_ita = Path(args.rom_ita).read_bytes()
    sha_ita = hashlib.sha1(rom_ita).hexdigest()
    sha_usa = hashlib.sha1(rom_usa).hexdigest()

    print(f"ROM USA : {args.rom_usa}")
    print(f"          sha1 {sha_usa}")
    print(f"ROM ITA : {args.rom_ita}")
    print(f"          sha1 {sha_ita}")
    gamecode = rom_ita[0xAC:0xB0].decode("ascii", "replace")
    print(f"          gamecode {gamecode!r}")
    print()

    symbols = parse_map(map_path)
    print(f"simboli letti dal .map: {len(symbols)}")
    wanted = list(FUNCTIONS) + list(DATA)
    missing = [n for n in wanted if n not in symbols]
    if missing:
        print("ERRORE: simboli assenti dal .map: " + ", ".join(missing), file=sys.stderr)
        return 1
    print(f"simboli da portare    : {len(wanted)}")
    print()

    porter = Porter(symbols, rom_usa, rom_ita)
    notes = porter.run()

    unresolved = [n for n in wanted if n not in porter.resolved]
    done = [n for n in wanted if n in porter.resolved]
    print()
    print(f"=== risolti {len(done)}/{len(wanted)} "
          f"(rete di ancoraggio: {len(porter.resolved)} simboli)")

    coherence = porter.check_coherence(wanted)
    if unresolved:
        print("=== IRRISOLTI (il payload NON si compila cosi'):")
        for n in unresolved:
            print(f"  - {n}: {notes.get(n, 'nessun tentativo diretto')}")

    root = Path(__file__).resolve().parent.parent
    report = root / "build" / "port_report.md"
    report.parent.mkdir(exist_ok=True)
    lines = [
        "# port_report.md - come e' stato trovato ogni simbolo italiano",
        "",
        "GENERATO DA tools/port_syms.py. Va letto riga per riga prima di fidarsi.",
        "",
        f"- ROM USA: `{args.rom_usa}` sha1 `{sha_usa}`",
        f"- ROM ITA: `{args.rom_ita}` sha1 `{sha_ita}`, gamecode `{gamecode}`",
        f"- risolti: **{len(done)}/{len(wanted)}**",
        f"- rete di ancoraggio: {len(porter.resolved)} simboli, "
        f"{coherence['blocks']} blocchi di traslazione su {coherence['net_rom']} in ROM",
        f"- dati in RAM con delta zero: "
        f"{coherence['ram_total'] - len(coherence['ram_bad'])}/{coherence['ram_total']}",
        f"- simboli con delta isolato rispetto ai vicini: {len(coherence['isolated'])}",
        "",
        "| Simbolo | USA | ITA | Delta | Metodo | Conferme |",
        "|---|---|---|---|---|---|",
    ]
    for name in wanted:
        usa = f"0x{symbols[name]:08X}"
        if name in porter.resolved:
            ita_addr = porter.resolved[name]
            ita = f"0x{ita_addr:08X}"
            delta = f"{ita_addr - symbols[name]:+d}"
            method = porter.method.get(name, "")
            seen = []
            for e in porter.evidence.get(name, []):
                if e not in seen:
                    seen.append(e)
            ev = "; ".join(seen[:6])
            if len(seen) > 6:
                ev += f" (+{len(seen) - 6} altre)"
        else:
            ita = "**IRRISOLTO**"
            delta = ""
            method = notes.get(name, "")
            ev = ""
        lines.append(f"| `{name}` | {usa} | {ita} | {delta} | {method} | {ev} |")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nscritto {report}")

    if unresolved:
        print("\nNessun game_syms_it.h generato: si genera solo a simboli completi.")
        return 1

    out = root / "payload" / "game_syms_it.h"
    header = [
        "/* GENERATO DA tools/port_syms.py - non modificare a mano.",
        f" * ROM italiana: gamecode {gamecode}, sha1 {sha_ita}",
        f" * Riferimento USA: sha1 {sha_usa}",
        " *",
        " * Ogni indirizzo qui dentro e' stato RITROVATO nella ROM italiana, non",
        " * dedotto: vedi build/port_report.md per il metodo simbolo per simbolo.",
        " */",
        "",
        "#ifndef GAME_SYMS_H",
        "#define GAME_SYMS_H",
        "",
        "typedef unsigned char  u8;",
        "typedef unsigned short u16;",
        "typedef unsigned int   u32;",
        "typedef signed char    s8;",
        "typedef signed short   s16;",
        "typedef signed int     s32;",
        "typedef unsigned char  bool8;",
        "",
        "/* --- funzioni del gioco (bit Thumb gia' impostato) --- */",
        "",
    ]
    for name, signature in FUNCTIONS.items():
        addr = porter.resolved[name]
        thumb = 0 if name in ARM_FUNCTIONS else 1
        mode = "ARM" if thumb == 0 else "Thumb"
        header.append(f"/* {name} @ 0x{addr:08X} ({mode}) */")
        header.append(f"#define {name} (({signature})(0x{addr | thumb:08X}))")
        header.append("")
    header.append("/* --- dati del gioco --- */")
    header.append("")
    for name in DATA:
        header.append(f"#define ADDR_{name} (0x{porter.resolved[name]:08X}u)")
    header.append("")
    header.append("#endif /* GAME_SYMS_H */")
    out.write_text("\n".join(header) + "\n")
    print(f"scritto {out}")

    # --- profilo per l'iniettore ---------------------------------------------
    # inject_body.lua si rifiuta di iniettare se non riconosce la ROM: controlla
    # il gamecode e il prologo di CB2_Overworld. Quei valori sono USA e cablati,
    # quindi sulla cartuccia italiana l'iniezione verrebbe rifiutata. Qui si
    # emettono quelli italiani - ritrovati, non dedotti: CB1_Overworld esce dalla
    # stessa rete di ancoraggio, e il prologo si legge dal dump.
    extra = {}
    for name in ("CB1_Overworld",):
        if name in porter.resolved:
            extra[name] = porter.resolved[name]
        else:
            got = porter.locate(name, depth=2) or porter.resolve_by_referrer(name)
            if got is None:
                print(f"ATTENZIONE: {name} non ritrovato: il profilo per l'iniettore "
                      f"resta incompleto e l'iniezione sulla ROM italiana verra' rifiutata.")
            else:
                extra[name] = got

    cb2 = porter.resolved["CB2_Overworld"]
    prologue = int.from_bytes(rom_ita[cb2 - ROM_BASE:cb2 - ROM_BASE + 4], "little")
    profile = root / "payload" / "rom_profile_it.txt"
    plines = [
        "# GENERATO DA tools/port_syms.py - profilo della ROM per mgba/inject_body.lua.",
        f"# {gamecode}, sha1 {sha_ita}",
        f"GAMECODE={gamecode}",
        f"CB2_OVERWORLD=0x{cb2:08X}",
        f"CB2_WORD=0x{prologue:08X}",
    ]
    if "CB1_Overworld" in extra:
        plines.append(f"CB1_OVERWORLD=0x{extra['CB1_Overworld'] | 1:08X}")
    profile.write_text("\n".join(plines) + "\n")
    print(f"scritto {profile}")
    for line in plines[2:]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
