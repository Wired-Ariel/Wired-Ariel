#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""analizza_save.py - il salvataggio, aperto e guardato dentro.

PERCHE' ESISTE
--------------
Dopo la corsa del Surf (2026-08-21) la domanda di Lain e' diventata: "il mio
salvataggio e' rotto?". A quella domanda non si risponde a parole: si apre il
file e si guarda. Questo strumento legge un .sav di Smeraldo e dice tre cose,
tenute ben separate perche' hanno un valore diverso:

  CERTEZZE      cose che si possono affermare guardando un file solo:
                i checksum dei settori, le uova cattive, i Pokemon con il
                checksum sbagliato, i residui del payload, le quantita'
                impossibili, i bit del Pokedex fuori dai 386 veri,
                "posseduto ma mai visto".
  CONTESTO      numeri che servono a te per riconoscere se qualcosa non
                torna (soldi, medaglie, ore, conteggi): io non so quanto
                DOVREBBE essere.
  NON DIMOSTRABILE   un flag o una variabile cambiata di un bit e' invisibile
                in un file solo. Per quelli serve un CONFRONTO con un
                salvataggio precedente: si passa il secondo file e lo
                strumento dice esattamente cosa e' cambiato.

Uso:
    python tools/analizza_save.py partita.sav
    python tools/analizza_save.py adesso.sav ieri.sav      # confronto

Formato (da repo-studio/pokeemerald, include/save.h e include/global.h):
  32 settori da 4096 byte; ogni settore ha 3968 byte di dati e in coda
  id (u16), checksum (u16), firma 0x08012025 (u32), contatore (u32).
  Settori 0-13 = fessura A, 14-27 = fessura B, 28-31 = speciali.
  Dentro una fessura: 0 = SaveBlock2, 1-4 = SaveBlock1, 5-13 = il PC.
"""

import io
import os
import struct
import sys

SETTORE = 4096
DATI = 3968
FIRMA = 0x08012025
N_SETTORI = 32

DIM_SB2 = 0xF2C        # 3884
DIM_SB1 = 0x3D88       # 15752
DIM_PC = 0x83D0        # 33744

# --- offset dentro SaveBlock1 (include/global.h, commenti /*0xNNN*/) --------
SB1_PARTY_COUNT = 0x234
SB1_PARTY = 0x238
SB1_MONEY = 0x490
SB1_COINS = 0x494
SB1_PC_ITEMS = 0x498
SB1_TASCHE = [("strumenti", 0x560, 30), ("chiave", 0x5D8, 30), ("ball", 0x650, 16),
              ("MT/MN", 0x690, 64), ("bacche", 0x790, 46)]
SB1_SEEN1 = 0x988
SB1_OBJ_EVENTS = 0xA30
SB1_FLAGS = 0x1270
SB1_VARS = 0x139C
SB1_STATS = 0x159C
OBJ_EVENT_SIZE = 0x24

# --- offset dentro SaveBlock2 ----------------------------------------------
SB2_NOME = 0x00
SB2_GENERE = 0x08
SB2_TRAINER_ID = 0x0A
SB2_ORE = 0x0E
SB2_POKEDEX = 0x18
SB2_DEX_OWNED = SB2_POKEDEX + 0x10
SB2_DEX_SEEN = SB2_POKEDEX + 0x44
SB2_CHIAVE = 0xAC

DEX_BYTES = 52          # NUM_DEX_FLAG_BYTES: 412 specie -> 52 byte = 416 bit
DEX_VERI = 386          # NATIONAL_DEX_DEOXYS: oltre questo i bit non si usano
N_SPECIE = 412

MON = 100               # struct Pokemon
BOXMON = 80             # struct BoxPokemon
BOX_N = 14
BOX_CAP = 30
# LE SCATOLE PARTONO A 0x0004, NON A 0x0001 (2026-08-22).
# include/pokemon_storage_system.h annota `/*0x0001*/ boxes[...]` dopo un u8,
# ma struct BoxPokemon comincia con due u32 e vuole allineamento a 4: il
# compilatore infila 3 byte di riempimento. La prova sta due righe piu' sotto
# nello stesso file: boxNames e' annotato a 0x8344, e 4 + 14*30*80 = 0x8344
# (mentre 1 + 14*30*80 farebbe 0x8341).
# Con l'offset sbagliato ogni Pokemon del PC risultava col checksum rotto e
# molti "Uovo Cattivo": 31 uova e 72 checksum falsi sul salvataggio VERO di
# Lain. Se ne e' accorto il nome letto storto ("NaeMAGIKAR" invece di
# "MAGIKARP"): tre byte di scarto, esattamente il riempimento. Il collaudo
# sul salvataggio finto NON poteva trovarlo, perche' lo scriveva con lo
# stesso offset sbagliato con cui poi lo rileggeva.
BOX_BASE = 4

# il localId del nostro giocatore remoto: 0xE0 da oggi, 0xF0 fino a ieri
NOSTRI_LOCALID = (0xE0, 0xF0)

CHARMAP_BASE = {}


def carica_charmap(decomp):
    import re
    p = os.path.join(decomp, "charmap.txt")
    if not os.path.exists(p):
        return {}
    tab = {}
    for riga in io.open(p, encoding="utf-8").read().splitlines():
        if riga.startswith("@ Hiragana"):
            break
        riga = riga.split("@")[0].strip()
        m = re.match(r"^'(.)'\s*=\s*([0-9A-Fa-f]{2})$", riga)
        if m and int(m.group(2), 16) not in tab:
            tab[int(m.group(2), 16)] = m.group(1)
    return tab


def nomi_specie(decomp):
    """id -> nome, da src/data/text/species_names.h. Serve solo a rendere
    leggibile l'elenco della squadra: se il file non c'e', si stampano i
    numeri e non cambia niente della diagnosi."""
    import re
    p = os.path.join(decomp, "src/data/text/species_names.h")
    if not os.path.exists(p):
        return {}
    testo_ = io.open(p, encoding="utf-8").read()
    cost = {}
    sp = os.path.join(decomp, "include/constants/species.h")
    if os.path.exists(sp):
        for m in re.finditer(r"^#define\s+(SPECIES_\w+)\s+(\d+)", io.open(sp, encoding="utf-8").read(), re.M):
            cost.setdefault(m.group(1), int(m.group(2)))
    out = {}
    for m in re.finditer(r"\[(SPECIES_\w+)\]\s*=\s*_\(\"([^\"]*)\"\)", testo_):
        n = cost.get(m.group(1))
        if n is not None:
            out[n] = m.group(2)
    return out


NOMI_SPECIE = {}


def testo(b):
    s = ""
    for c in b:
        if c == 0xFF:
            break
        s += CHARMAP_BASE.get(c, "?")
    return s.strip()


# ---------------------------------------------------------------- settori

class Settore(object):
    def __init__(self, dati, n):
        self.n = n
        self.dati = dati[:DATI]
        coda = dati[DATI:]
        self.id, self.chk, self.firma, self.contatore = struct.unpack_from("<HHII", coda, len(coda) - 12)

    @property
    def valida(self):
        return self.firma == FIRMA


def checksum(dati, dim):
    somma = 0
    for i in range(dim // 4):
        somma = (somma + struct.unpack_from("<I", dati, i * 4)[0]) & 0xFFFFFFFF
    return ((somma >> 16) + somma) & 0xFFFF


def dimensione_pezzo(sid):
    """Quanti byte di quel settore fanno parte davvero del blocco."""
    if sid == 0:
        return DIM_SB2
    if 1 <= sid <= 4:
        resto = DIM_SB1 - (sid - 1) * DATI
        return min(resto, DATI)
    if 5 <= sid <= 13:
        resto = DIM_PC - (sid - 5) * DATI
        return min(resto, DATI)
    return DATI


class Partita(object):
    """Una fessura di salvataggio, rimontata."""

    def __init__(self, settori, nome):
        self.nome = nome
        self.settori = settori
        self.problemi = []
        visti = {}
        for s in settori:
            if not s.valida:
                continue
            dim = dimensione_pezzo(s.id)
            atteso = checksum(s.dati, dim)
            if atteso != s.chk:
                self.problemi.append("settore %d (pezzo %d): checksum 0x%04X, atteso 0x%04X"
                                     % (s.n, s.id, s.chk, atteso))
                continue
            visti[s.id] = s
        self.pezzi = visti
        self.contatore = max([s.contatore for s in settori if s.valida] or [0])
        self.completa = all(i in visti for i in range(14))
        self.sb2 = self._monta([0], DIM_SB2)
        self.sb1 = self._monta(range(1, 5), DIM_SB1)
        self.pc = self._monta(range(5, 14), DIM_PC)

    def _monta(self, ids, dim):
        out = bytearray(dim)
        pieno = True
        for k, sid in enumerate(ids):
            s = self.pezzi.get(sid)
            if s is None:
                pieno = False
                continue
            inizio = k * DATI
            n = min(dimensione_pezzo(sid), dim - inizio)
            out[inizio:inizio + n] = s.dati[:n]
        return bytes(out) if pieno or any(out) else None


# ---------------------------------------------------------------- Pokemon

ORDINE = [
    "GAEM", "GAME", "GEAM", "GEMA", "GMAE", "GMEA",
    "AGEM", "AGME", "AEGM", "AEMG", "AMGE", "AMEG",
    "EGAM", "EGMA", "EAGM", "EAMG", "EMGA", "EMAG",
    "MGAE", "MGEA", "MAGE", "MAEG", "MEGA", "MEAG",
]


def leggi_mon(b, off):
    """Ritorna un dizionario, o None se lo slot e' vuoto."""
    personality, otid = struct.unpack_from("<II", b, off)
    nickname = b[off + 8:off + 18]
    lingua = b[off + 18]
    flag = b[off + 19]
    otname = b[off + 20:off + 27]
    chk = struct.unpack_from("<H", b, off + 28)[0]
    sicuro = bytearray(b[off + 32:off + 80])
    if personality == 0 and otid == 0 and not any(sicuro):
        return None
    chiave = (personality ^ otid) & 0xFFFFFFFF
    for i in range(0, 48, 4):
        v = struct.unpack_from("<I", sicuro, i)[0] ^ chiave
        struct.pack_into("<I", sicuro, i, v)
    somma = 0
    for i in range(0, 48, 2):
        somma = (somma + struct.unpack_from("<H", sicuro, i)[0]) & 0xFFFF
    ordine = ORDINE[personality % 24]
    pezzi = {ordine[i]: sicuro[i * 12:(i + 1) * 12] for i in range(4)}
    specie = struct.unpack_from("<H", pezzi["G"], 0)[0]
    return {
        "personality": personality, "otId": otid,
        "nickname": testo(nickname), "otName": testo(otname), "lingua": lingua,
        "isBadEgg": bool(flag & 1), "hasSpecies": bool(flag & 2), "isEgg": bool(flag & 4),
        "checksum_ok": somma == chk, "checksum": chk, "calcolato": somma,
        "specie": specie, "flagByte": flag,
    }


# ---------------------------------------------------------------- controlli

def analizza(p, verboso=True):
    r = {"certezze": [], "sospetti": [], "contesto": []}
    sb1, sb2, pc = p.sb1, p.sb2, p.pc
    if sb1 is None or sb2 is None:
        r["certezze"].append(("ROTTO", "la fessura non e' completa: manca almeno un pezzo"))
        return r
    chiave = struct.unpack_from("<I", sb2, SB2_CHIAVE)[0]

    # --- contesto: chi e' -----------------------------------------------
    nome = testo(sb2[SB2_NOME:SB2_NOME + 8])
    tid = struct.unpack_from("<H", sb2, SB2_TRAINER_ID)[0]
    sid = struct.unpack_from("<H", sb2, SB2_TRAINER_ID + 2)[0]
    ore = struct.unpack_from("<H", sb2, SB2_ORE)[0]
    minuti = sb2[SB2_ORE + 2]
    soldi = struct.unpack_from("<I", sb1, SB1_MONEY)[0] ^ chiave
    gettoni = struct.unpack_from("<H", sb1, SB1_COINS)[0] ^ (chiave & 0xFFFF)
    r["contesto"].append(("allenatore", "%s  ID %05d/%05d  %s  %dh%02d di gioco"
                          % (nome, tid, sid, "F" if sb2[SB2_GENERE] else "M", ore, minuti)))
    r["contesto"].append(("soldi", "%d  |  gettoni %d" % (soldi, gettoni)))

    # --- CERTEZZA 1: residui del payload --------------------------------
    residui = []
    for i in range(16):
        o = SB1_OBJ_EVENTS + i * OBJ_EVENT_SIZE
        localid = sb1[o + 8]
        attivo = sb1[o] & 1
        if localid in NOSTRI_LOCALID:
            residui.append("slot %d: localId 0x%02X%s" % (i, localid, " ATTIVO" if attivo else ""))
    if residui:
        r["certezze"].append(("RESIDUO", "object event del payload nel salvataggio: " + "; ".join(residui)))
    else:
        r["certezze"].append(("pulito", "nessun object event del payload (localId 0xE0/0xF0): 0 su 16 slot"))

    # --- CERTEZZA 2: squadra e PC ---------------------------------------
    cattive, rotti, controllati = [], [], 0
    n_party = sb1[SB1_PARTY_COUNT]
    squadra = []
    for i in range(6):
        m = leggi_mon(sb1, SB1_PARTY + i * MON)
        if m is None:
            continue
        controllati += 1
        livello = sb1[SB1_PARTY + i * MON + 84]
        squadra.append("%s Lv%d%s" % (NOMI_SPECIE.get(m["specie"], "#%d" % m["specie"]), livello,
                                      "" if m["nickname"].upper() == NOMI_SPECIE.get(m["specie"], "").upper()
                                      else " (%s)" % m["nickname"]))
        dove = "squadra #%d" % (i + 1)
        if m["isBadEgg"]:
            cattive.append(dove)
        if not m["checksum_ok"]:
            rotti.append("%s (%s)" % (dove, m["nickname"] or "?"))
    if pc is not None:
        for b in range(BOX_N):
            for k in range(BOX_CAP):
                off = BOX_BASE + (b * BOX_CAP + k) * BOXMON
                if off + BOXMON > len(pc):
                    break
                m = leggi_mon(pc, off)
                if m is None:
                    continue
                controllati += 1
                dove = "Box %d pos %d" % (b + 1, k + 1)
                if m["isBadEgg"]:
                    cattive.append(dove)
                if not m["checksum_ok"]:
                    rotti.append("%s (%s)" % (dove, m["nickname"] or "?"))
    if squadra:
        r["contesto"].append(("squadra", " | ".join(squadra)))
    if cattive:
        r["certezze"].append(("UOVA CATTIVE", "%d: %s" % (len(cattive), ", ".join(cattive[:10]))))
    else:
        r["certezze"].append(("pulito", "nessun Uovo Cattivo su %d Pokemon (squadra %d + PC)"
                              % (controllati, n_party)))
    if rotti:
        r["certezze"].append(("CHECKSUM SBAGLIATO", "%d Pokemon: %s" % (len(rotti), ", ".join(rotti[:10]))))
    else:
        r["certezze"].append(("pulito", "tutti i %d Pokemon hanno il checksum giusto" % controllati))

    # --- CERTEZZA 3: la borsa -------------------------------------------
    # NOTA sulle caselle VUOTE (imparata al primo collaudo, 2026-08-22): quando
    # itemId e' 0 la casella e' vuota e il gioco non guarda MAI la quantita',
    # quindi "nessuno strumento ma quantita' X" non e' un guasto - e' rumore.
    # Pero' un'informazione ce la da' lo stesso: il gioco azzera tutte le
    # caselle vuote allo stesso modo, quindi devono decifrarsi TUTTE allo
    # stesso valore. Se una sola e' diversa dalle altre, li' ha scritto
    # qualcuno.
    guai = []
    vuote = {}
    for etichetta, base, quanti in SB1_TASCHE:
        for i in range(quanti):
            o = base + i * 4
            item = struct.unpack_from("<H", sb1, o)[0]
            qta = struct.unpack_from("<H", sb1, o + 2)[0] ^ (chiave & 0xFFFF)
            if item == 0:
                vuote.setdefault(qta, []).append("%s #%d" % (etichetta, i))
            elif qta == 0 or qta > 999:
                guai.append("%s #%d: strumento %d quantita' %d" % (etichetta, i, item, qta))
            elif item > 400:
                guai.append("%s #%d: id strumento fuori scala (%d)" % (etichetta, i, item))
    for i in range(50):
        o = SB1_PC_ITEMS + i * 4
        item = struct.unpack_from("<H", sb1, o)[0]
        qta = struct.unpack_from("<H", sb1, o + 2)[0]
        if item == 0:
            continue
        if qta == 0 or qta > 999:
            guai.append("PC #%d: strumento %d quantita' %d" % (i, item, qta))
        elif item > 400:
            guai.append("PC #%d: id strumento fuori scala (%d)" % (i, item))
    if len(vuote) > 1:
        ordinate = sorted(vuote.items(), key=lambda kv: -len(kv[1]))
        rare = ordinate[1:]
        guai.append("caselle vuote della borsa non tutte uguali: il valore comune e' %d (%d caselle), "
                    "ma %s" % (ordinate[0][0], len(ordinate[0][1]),
                               "; ".join("%s vale %d" % (", ".join(v[:3]), k) for k, v in rare[:4])))
    if guai:
        r["certezze"].append(("BORSA", "%d anomalie: %s" % (len(guai), "; ".join(guai[:8]))))
    else:
        r["certezze"].append(("pulito", "borsa e PC strumenti: nessuna quantita' impossibile"))

    # --- CERTEZZA 4: il Pokedex -----------------------------------------
    owned = sb2[SB2_DEX_OWNED:SB2_DEX_OWNED + DEX_BYTES]
    seen = sb2[SB2_DEX_SEEN:SB2_DEX_SEEN + DEX_BYTES]
    seen1 = sb1[SB1_SEEN1:SB1_SEEN1 + DEX_BYTES]

    def bit(b, i):
        return (b[i >> 3] >> (i & 7)) & 1

    n_own = sum(bit(owned, i) for i in range(DEX_VERI))
    n_seen = sum(bit(seen, i) for i in range(DEX_VERI))
    fantasmi = [i + 1 for i in range(DEX_VERI) if bit(owned, i) and not bit(seen, i)]
    oltre = [i + 1 for i in range(DEX_VERI, DEX_BYTES * 8)
             if bit(owned, i) or bit(seen, i) or bit(seen1, i)]
    disallineati = [i + 1 for i in range(DEX_VERI) if bit(seen, i) != bit(seen1, i)]
    r["contesto"].append(("Pokedex", "%d visti, %d catturati" % (n_seen, n_own)))
    if fantasmi:
        r["certezze"].append(("POKEDEX", "%d specie CATTURATE ma mai VISTE (impossibile giocando): %s"
                              % (len(fantasmi), fantasmi[:10])))
    if oltre:
        r["certezze"].append(("POKEDEX", "bit accesi oltre le 386 specie vere (numeri %s): "
                              "sono caselle che il gioco non usa, se sono accese qualcuno ha scritto li'"
                              % oltre[:10]))
    if disallineati:
        r["sospetti"].append(("Pokedex", "%d specie con 'visto' diverso fra le due copie che il gioco "
                              "tiene (SaveBlock1/SaveBlock2): %s" % (len(disallineati), disallineati[:10])))
    if not fantasmi and not oltre:
        r["certezze"].append(("pulito", "Pokedex coerente: nessun 'catturato ma mai visto', nessun bit fuori scala"))

    # --- contesto: medaglie, flag, statistiche --------------------------
    n_flag = sum(bin(b).count("1") for b in sb1[SB1_FLAGS:SB1_FLAGS + 300])
    r["contesto"].append(("flag accesi", "%d su 2400" % n_flag))
    passi = struct.unpack_from("<I", sb1, SB1_STATS + 4 * 5)[0] ^ chiave   # GAME_STAT_STEPS
    salvataggi = struct.unpack_from("<I", sb1, SB1_STATS + 4 * 0)[0] ^ chiave
    r["contesto"].append(("statistiche", "salvataggi %d, passi %d (se sono numeri assurdi, dimmelo)"
                          % (salvataggi if salvataggi < 10 ** 7 else -1,
                             passi if passi < 10 ** 8 else -1)))
    return r


def confronta(a, b):
    """Cosa e' cambiato fra due salvataggi. E' l'unico modo di vedere un flag
    o una variabile spostata di un bit."""
    fuori = []
    regioni = [
        ("SaveBlock1 posizione/mappa", "sb1", 0x00, 0x34),
        ("SaveBlock1 vista mappa", "sb1", 0x34, 0x234),
        ("squadra", "sb1", 0x238, 0x490),
        ("soldi/gettoni", "sb1", 0x490, 0x498),
        ("PC strumenti", "sb1", 0x498, 0x560),
        ("borsa", "sb1", 0x560, 0x848),
        ("pokeblock", "sb1", 0x848, 0x988),
        ("Pokedex (copia SB1)", "sb1", 0x988, 0x9BC),
        ("object events", "sb1", 0xA30, 0xC70),
        ("template object events", "sb1", 0xC70, 0x1270),
        ("FLAG", "sb1", 0x1270, 0x139C),
        ("VARIABILI", "sb1", 0x139C, 0x159C),
        ("statistiche", "sb1", 0x159C, 0x169C),
        ("alberi di bacche", "sb1", 0x169C, 0x1A9C),
        ("basi segrete", "sb1", 0x1A9C, 0x271C),
        ("nome/ore/opzioni", "sb2", 0x00, 0x18),
        ("Pokedex (SB2)", "sb2", 0x18, 0x90),
        ("chiave di cifratura", "sb2", 0xAC, 0xB0),
        ("Fronte Lotta e record", "sb2", 0x21C, DIM_SB2),
    ]
    for nome, blocco, i0, i1 in regioni:
        x = getattr(a, blocco)
        y = getattr(b, blocco)
        if x is None or y is None:
            continue
        i1 = min(i1, len(x), len(y))
        diversi = [i for i in range(i0, i1) if x[i] != y[i]]
        if diversi:
            fuori.append((nome, len(diversi), diversi[:6],
                          [(hex(i), x[i], y[i]) for i in diversi[:4]]))
    return fuori


def bit_accesi_in_piu(a, b):
    """I bit passati da 0 a 1 in FLAG e VARIABILI: e' la firma del difetto del
    Surf, che poteva solo ACCENDERE bit, mai spegnerli."""
    out = []
    for nome, i0, i1 in (("FLAG", SB1_FLAGS, SB1_FLAGS + 300),
                         ("VARIABILI", SB1_VARS, SB1_VARS + 512)):
        su, giu = 0, 0
        dove = []
        for i in range(i0, min(i1, len(a.sb1), len(b.sb1))):
            x, y = a.sb1[i], b.sb1[i]
            if x == y:
                continue
            nuovi = x & ~y
            persi = y & ~x
            su += bin(nuovi).count("1")
            giu += bin(persi).count("1")
            if nuovi and len(dove) < 8:
                dove.append("byte 0x%X: 0x%02X -> 0x%02X" % (i - i0, y, x))
        out.append((nome, su, giu, dove))
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    qui = os.path.dirname(os.path.abspath(__file__))
    radice = os.path.dirname(qui)
    decomp = os.path.join(radice, "repo-studio", "pokeemerald")
    if not os.path.isdir(decomp):
        decomp = os.path.join(os.path.dirname(radice), "repo-studio", "pokeemerald")
    global CHARMAP_BASE, NOMI_SPECIE
    CHARMAP_BASE = carica_charmap(decomp)
    NOMI_SPECIE = nomi_specie(decomp)

    partite = []
    for percorso in sys.argv[1:3]:
        grezzo = open(percorso, "rb").read()
        print("=" * 72)
        print("FILE: %s  (%d byte)" % (percorso, len(grezzo)))
        if len(grezzo) < SETTORE * 28:
            print("  troppo corto per essere un salvataggio di Smeraldo")
            return 1
        settori = [Settore(grezzo[i * SETTORE:(i + 1) * SETTORE], i) for i in range(N_SETTORI)]
        valide = sum(1 for s in settori if s.valida)
        print("  settori con firma valida: %d su %d" % (valide, N_SETTORI))
        a = Partita([s for s in settori[:14]], "A")
        b = Partita([s for s in settori[14:28]], "B")
        for f in (a, b):
            stato = "completa" if f.completa else "INCOMPLETA"
            print("  fessura %s: %s, contatore %d, %d pezzi buoni%s"
                  % (f.nome, stato, f.contatore, len(f.pezzi),
                     ", PROBLEMI: " + "; ".join(f.problemi) if f.problemi else ""))
        scelta = a if (a.completa and a.contatore >= b.contatore) or not b.completa else b
        print("  -> si analizza la fessura %s (quella che il gioco caricherebbe)" % scelta.nome)
        print()
        r = analizza(scelta)
        for etichetta, righe in (("CERTEZZE", r["certezze"]), ("SOSPETTI", r["sospetti"]),
                                 ("CONTESTO", r["contesto"])):
            if not righe:
                continue
            print("  --- %s ---" % etichetta)
            for tag, testo_ in righe:
                segno = "  OK  " if tag == "pulito" else "  !!  "
                print("  %s %-18s %s" % (segno, tag, testo_))
            print()
        partite.append(scelta)
        altra = b if scelta is a else a
        if altra.completa and len(sys.argv) < 3:
            print("  --- CONFRONTO CON LA FESSURA %s (il salvataggio PRECEDENTE, gratis) ---" % altra.nome)
            fuori = confronta(scelta, altra)
            if not fuori:
                print("      le due fessure sono identiche")
            for nome_, quanti, primi, esempi in fuori:
                print("      %-28s %5d byte diversi" % (nome_, quanti))
            for nome_, su, giu, dove in bit_accesi_in_piu(scelta, altra):
                print("      %-12s bit accesi in piu': %-5d spenti: %-5d" % (nome_, su, giu))
            print()

    if len(partite) == 2:
        print("=" * 72)
        print("CONFRONTO (il primo file rispetto al secondo)")
        print()
        fuori = confronta(partite[0], partite[1])
        if not fuori:
            print("  nessuna differenza nelle regioni note. Sono lo stesso salvataggio.")
        for nome, quanti, primi, esempi in fuori:
            print("  %-28s %5d byte diversi   es. %s" % (nome, quanti, esempi))
        print()
        print("  BIT ACCESI IN PIU' nel primo rispetto al secondo")
        print("  (il difetto del Surf poteva solo ACCENDERE bit, mai spegnerli:")
        print("   se qui compaiono bit accesi in FLAG o VARIABILI che il gioco non")
        print("   spiega, quella e' la sua impronta)")
        for nome, su, giu, dove in bit_accesi_in_piu(partite[0], partite[1]):
            print("    %-12s accesi in piu': %-5d spenti: %-5d %s"
                  % (nome, su, giu, ("  " + "; ".join(dove)) if dove else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
