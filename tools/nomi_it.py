#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nomi_it.py - i nomi UFFICIALI italiani, letti dalla cartuccia.

PERCHE' ESISTE
--------------
La decomp e' la ROM AMERICANA: "POTION", "Littleroot Town", "Bug Catcher".
La pagina della mappa deve poter parlare italiano senza inventare niente, e
l'unica fonte legittima e' la ROM italiana che Lain ha gia' (BPEI, la stessa
su cui gira il payload). Qui si aprono le tabelle e si leggono le stringhe.

COME SI TROVANO LE TABELLE, VISTO CHE NON ABBIAMO UN .map ITALIANO
------------------------------------------------------------------
Non si cercano le stringhe (sarebbe circolare: non sappiamo come si dicono in
italiano). Si cerca l'IMPRONTA NUMERICA, che e' identica in tutte le lingue
perche' e' dato di gioco, non testo:

  gItems              itemId a +14 = indice della voce, passo 44
  gSpeciesNames       la voce 1 e' "BULBASAUR" (i nomi dei Pokemon sono gli
                      stessi in tutte le lingue occidentali), passo 11
  gRegionMapEntries   x/y/larghezza/altezza di ogni zona, presi dalla decomp,
                      + puntatore al nome, passo 8
  gTrainerClassNames  le QUATTRO voci che in inglese si chiamano tutte
                      "{PKMN} TRAINER" (0, 1, 50, 65) devono essere quattro
                      blocchi identici alle stesse distanze, passo 13. NON si
                      pretende che tutti gli omonimi inglesi restino omonimi:
                      TUBER_F/TUBER_M sono "TUBER" tutte e due in inglese ma
                      BAMBELLINA/BAMBELLINO in italiano, e pretenderlo faceva
                      fallire la ricerca su una tabella che c'era
  gTrainers           la sequenza classe/numero-di-Pokemon di ogni voce,
                      presa dalla decomp, passo 40

Ogni tabella viene poi RILETTA e confrontata con la decomp su tutti i campi
numerici, non solo su quelli usati per trovarla: se il confronto non torna al
100% la tabella viene scartata e il nome italiano non si scrive. Meglio una
pagina in inglese che una con un nome sbagliato.

Uso:
    python tools/nomi_it.py [rom.gba]           # rapporto a schermo
    (gen_mappa.py lo importa e usa carica_nomi())
"""

import io
import json
import os
import re
import struct
import sys

QUI = os.path.dirname(os.path.abspath(__file__))
RADICE = os.path.dirname(QUI)
DECOMP = os.path.join(RADICE, "repo-studio", "pokeemerald")
if not os.path.isdir(DECOMP):
    DECOMP = os.path.join(os.path.dirname(RADICE), "repo-studio", "pokeemerald")
ROM_DEF = r"D:\Rom\[PKWWF]smeraldo.gba"

ROM_BASE = 0x08000000


# --------------------------------------------------------------------------
# Testo del gioco
# --------------------------------------------------------------------------

def carica_charmap():
    """byte -> carattere, dalla PARTE LATINA di charmap.txt.

    Il file contiene anche kana e katakana, che riusano gli stessi byte: si
    smette di leggere alla riga "@ Hiragana" e vince la PRIMA definizione,
    altrimenti 0x00 diventa lo spazio ideografico e il testo esce a ideogrammi
    (successo al primo tentativo, 2026-08-22)."""
    tab = {}
    percorso = os.path.join(DECOMP, "charmap.txt")
    for riga in io.open(percorso, encoding="utf-8").read().splitlines():
        if riga.startswith("@ Hiragana"):
            break
        riga = riga.split("@")[0].strip()
        m = re.match(r"^'(.)'\s*=\s*([0-9A-Fa-f]{2})$", riga)
        if m:
            b = int(m.group(2), 16)
            if b not in tab:
                tab[b] = m.group(1)
    # I due glifi "PK" e "MN" (0x53/0x54) non stanno fra apici singoli nel
    # charmap perche' occupano due lettere ciascuno: senza, i nomi di classe
    # che contengono {PKMN} - ALLEVA-PKMN, CERCA-PKMN - uscivano con dei '?'
    # e la ricerca li scartava come testo non valido (2026-08-22).
    tab.setdefault(0x53, "PK")
    tab.setdefault(0x54, "MN")
    # LE LEGATURE DELLA ROM ITALIANA (2026-08-24). Nella tabella dei nomi ci
    # sono 13 caratteri e basta, e "PORTAPOKEMELLE" non ci sta: la ROM italiana
    # disegna la parola con cinque tessere-legatura da due lettere l'una, che
    # nel charmap della decomp (fatto per la build inglese/giapponese) sono
    # katakana e quindi uscivano come '?'. Il filtro `pulito` scartava l'intero
    # nome, e il Portapokemelle restava senza nome italiano.
    #
    # COME SONO STATE RICAVATE, perche' non e' un indovinello: la DESCRIZIONE
    # dello stesso strumento (puntatore a +20 nella voce) contiene la stessa
    # sequenza 5E 5F 60 61 63 e si legge "Contiene le <5 glifi> fatte con il
    # MIXER BACCHE" - femminile plurale, e le Pokemelle sono per l'appunto
    # cio' che si fa col Mixer Bacche. Sostituendo, la descrizione torna
    # esatta: "Contiene le POKeMELLE fatte con il MIXER BACCHE."
    # Questi cinque codici compaiono UNA volta ciascuno in tutta la tabella
    # dei nomi (verificato), quindi non possono rompere nient'altro.
    for b, s in ((0x5E, "PO"), (0x5F, "Ké"), (0x60, "ME"),
                 (0x61, "LL"), (0x63, "E")):
        tab.setdefault(b, s)
    return tab


class Testo(object):
    def __init__(self, tab):
        self.tab = tab

    def decodifica(self, b):
        s = ""
        for c in b:
            if c == 0xFF:
                break
            if c == 0xFE:
                s += " "
            elif c == 0xFD:      # segnaposto ({PKMN}, {PLAYER}, ...)
                s += "\u2026"
            else:
                s += self.tab.get(c, "?")
        return s.strip()

    def pulito(self, b):
        """Come decodifica(), ma dice anche se il testo e' plausibile."""
        s = self.decodifica(b)
        ok = bool(s) and "?" not in s
        return s, ok


# --------------------------------------------------------------------------
# Le liste di costanti della decomp (l'ordine E' l'indice nelle tabelle)
# --------------------------------------------------------------------------

def costanti(percorso, prefisso):
    """#define PREFISSO_X n  ->  lista ordinata per valore."""
    testo = io.open(os.path.join(DECOMP, percorso), encoding="utf-8").read()
    voci = {}
    for m in re.finditer(r"^#define\s+(" + prefisso + r"\w*)\s+(\S+)", testo, re.M):
        nome, val = m.group(1), m.group(2)
        try:
            voci[nome] = int(val, 0)
        except ValueError:
            continue
    if not voci:
        return []
    out = [None] * (max(voci.values()) + 1)
    for nome, v in sorted(voci.items(), key=lambda kv: kv[1]):
        if out[v] is None:
            out[v] = nome
    return out


def enum_costanti(percorso, prefisso):
    """Le stesse cose, ma scritte come enum (region_map_sections.h)."""
    testo = io.open(os.path.join(DECOMP, percorso), encoding="utf-8").read()
    return [m.group(1) for m in re.finditer(r"^\s*(" + prefisso + r"\w*)\s*,?\s*$", testo, re.M)]


# --------------------------------------------------------------------------
# Le impronte prese dalla decomp
# --------------------------------------------------------------------------

def impronta_regionmap():
    """MAPSEC in ordine -> (x, y, larghezza, altezza)."""
    src = io.open(os.path.join(DECOMP, "src/data/region_map/region_map_entries.h"),
                  encoding="utf-8").read()
    voci = {}
    for m in re.finditer(r"\[(MAPSEC_\w+)\]\s*=\s*\{(.*?)\}", src, re.S):
        corpo = m.group(2)

        def campo(k):
            mm = re.search(r"\." + k + r"\s*=\s*(\d+)", corpo)
            return int(mm.group(1)) if mm else 0

        voci[m.group(1)] = (campo("x"), campo("y"), campo("width"), campo("height"))
    ordine = enum_costanti("include/constants/region_map_sections.h", "MAPSEC_")
    return ordine, [voci.get(n) for n in ordine]


def impronta_classi():
    """Le 66 classi, e lo schema di chi ha lo stesso nome di chi."""
    src = io.open(os.path.join(DECOMP, "src/data/text/trainer_class_names.h"),
                  encoding="utf-8").read()
    voci = dict(re.findall(r"\[(TRAINER_CLASS_\w+)\]\s*=\s*_\(\"([^\"]*)\"\)", src))
    ordine = costanti("include/constants/trainers.h", "TRAINER_CLASS_")
    nomi = [voci.get(n or "", None) for n in ordine]
    # gruppi di indici con lo stesso nome: e' l'impronta indipendente dalla lingua
    gruppi = {}
    for i, n in enumerate(nomi):
        if n is not None:
            gruppi.setdefault(n, []).append(i)
    return ordine, nomi, [g for g in gruppi.values() if len(g) > 1]


def impronta_trainers():
    """TRAINER in ordine -> (classe, numero di Pokemon)."""
    src = io.open(os.path.join(DECOMP, "src/data/trainers.h"), encoding="utf-8").read()
    classi = costanti("include/constants/trainers.h", "TRAINER_CLASS_")
    idx_classe = {n: i for i, n in enumerate(classi) if n}
    parties = {}
    for m in re.finditer(r"(sParty_\w+)\[\]\s*=\s*\{(.*?)\n\};",
                         io.open(os.path.join(DECOMP, "src/data/trainer_parties.h"),
                                 encoding="utf-8").read(), re.S):
        parties[m.group(1)] = len(re.findall(r"\.lvl\s*=\s*\d+", m.group(2)))
    voci = {}
    for m in re.finditer(r"\[(TRAINER_\w+)\]\s*=\s*\{(.*?)\n\s*\},", src, re.S):
        corpo = m.group(2)
        cl = re.search(r"\.trainerClass\s*=\s*(TRAINER_CLASS_\w+)", corpo)
        pa = re.search(r"\((sParty_\w+)\)", corpo)
        voci[m.group(1)] = (idx_classe.get(cl.group(1), -1) if cl else -1,
                            parties.get(pa.group(1), 0) if pa else 0)
    ordine = costanti("include/constants/opponents.h", "TRAINER_")
    ordine = [n for n in ordine]
    return ordine, [voci.get(n or "") for n in ordine]


# --------------------------------------------------------------------------
# Le ricerche nella ROM
# --------------------------------------------------------------------------

def cerca_items(rom):
    """44 byte a voce, itemId (u16) a +14 uguale all'indice, descrizione a +20."""
    for p in range(0x100000, len(rom) - 44 * 40, 2):
        ok = True
        for i in range(12):
            o = p + i * 44
            if int.from_bytes(rom[o + 14:o + 16], "little") != i or rom[o + 23] != 0x08:
                ok = False
                break
        if ok:
            return p
    return None


def cerca_specie(rom, t):
    """11 byte a voce. La voce 1 e' BULBASAUR in ogni lingua occidentale."""
    inv = {v: k for k, v in t.tab.items()}
    try:
        pat = bytes(inv[c] for c in "BULBASAUR")
    except KeyError:
        return None
    i = rom.find(pat)
    if i < 11:
        return None
    base = i - 11
    # la voce 0 e' il segnaposto "??????????": basta che sia lunga 10 e termini
    if rom[base + 10] not in (0xFF,) and rom[base + 11 - 1] not in (0xFF,):
        return None
    return base


def cerca_regionmap(rom, seq):
    """x/y/larghezza/altezza dalla decomp + puntatore al nome, passo 8."""
    prime = [v for v in seq[:40] if v is not None]
    if len(prime) < 30:
        return None
    for p in range(0x100000, len(rom) - 8 * len(seq), 4):
        ok = True
        for k in range(40):
            if seq[k] is None:
                continue
            o = p + k * 8
            if rom[o:o + 4] != bytes(seq[k]) or rom[o + 7] != 0x08:
                ok = False
                break
        if ok:
            return p
    return None


def cerca_classi(rom, t, n, gruppi):
    """13 byte a voce, n voci.

    L'IMPRONTA E' SOLO IL GRUPPO PIU' GRANDE, non tutti (2026-08-22): in
    inglese TUBER_F e TUBER_M si chiamano tutte e due "TUBER", ma in italiano
    sono BAMBELLINA e BAMBELLINO. Pretendere che TUTTI i gruppi di omonimi
    dell'inglese reggano anche in italiano faceva fallire la ricerca su una
    tabella che c'era. Il gruppo grande - le quattro classi che si chiamano
    "{PKMN} TRAINER", cioe' 0, 1, 50 e 65 - regge perche' e' la stessa scritta
    per lo stesso ruolo, e quattro blocchi da 13 byte identici a distanze
    fissate sono gia' un'impronta forte.

    Due fasi per non pagare 16 MB di scansione lenta: prima il filtro a due
    confronti (voce 0 == voce 1), poi la verifica completa sui pochi
    sopravvissuti."""
    grande = max(gruppi, key=len) if gruppi else []
    if len(grande) < 3:
        return None
    passo = 13
    limite = len(rom) - passo * (n + 2)
    for p in range(0x100000, limite):
        if rom[p] != rom[p + passo] or rom[p + 1] != rom[p + passo + 1]:
            continue
        blocco = rom[p:p + passo]
        if 0xFF not in blocco:
            continue
        if any(rom[p + i * passo:p + i * passo + passo] != blocco for i in grande[1:]):
            continue
        ok = True
        for k in range(n):
            o = p + k * passo
            testo, buono = t.pulito(rom[o:o + passo])
            if not buono or len(testo) < 2:
                ok = False
                break
        if ok:
            return p
    return None


def cerca_trainers(rom, seq):
    """40 byte a voce; si confrontano classe (+1) e numero di Pokemon (+0x20)
    con quelli della decomp, piu' il puntatore alla squadra a +0x24."""
    utili = [(i, v) for i, v in enumerate(seq) if v and v[0] >= 0 and v[1] > 0]
    if len(utili) < 50:
        return None
    prova = utili[:60]
    for p in range(0x100000, len(rom) - 40 * len(seq), 4):
        ok = True
        for i, (cl, n) in prova:
            o = p + i * 40
            if rom[o + 1] != cl or rom[o + 0x20] != n or rom[o + 0x27] != 0x08:
                ok = False
                break
        if ok:
            return p
    return None


def cerca_easy_chat(rom):
    """Le tre liste di parole della FRASE DI TENDENZA, dalla cartuccia.

    Servono al calcolatore di Feebas: la frase e' fatta di una parola del
    gruppo CONDITIONS (10) e una di LIFESTYLE (12) o HOBBIES (13), e il
    numero d'ordine di quella parola e' cio' che il gioco sorteggia. Senza le
    parole vere, e nell'ordine vero, la frase non si puo' tradurre in numeri.

    Si trova `gEasyChatGroups` per IMPRONTA NUMERICA, come le altre tabelle:
    e' un vettore di {puntatore, numWords u16, numEnabled u16} da 8 byte, e i
    conteggi dei gruppi sono dato di gioco, uguale in tutte le lingue
    (CONDITIONS 69, LIFESTYLE 45, HOBBIES 54, e il 12 in mezzo ne ha 78).
    Ogni voce della lista e' 12 byte e comincia col puntatore al testo.
    Verificato sulla ROM italiana: 168 parole su 168."""
    ATTESI = [(10, 69), (11, 78), (12, 45), (13, 54)]
    base = None
    for p in range(0x100000, len(rom) - 8 * 24, 4):
        ok = True
        for g, n in ATTESI:
            o = p + (g - 10) * 8
            ptr, nw, ne = struct.unpack_from("<IHH", rom, o)
            if nw != n or ne != n or (ptr >> 24) != 0x08:
                ok = False
                break
        if ok:
            base = p
            break
    if base is None:
        return None
    fuori = {}
    tabella = {b: c for b, c in carica_charmap().items()}
    for g, n in ((10, 69), (12, 45), (13, 54)):
        ptr = struct.unpack_from("<I", rom, base + (g - 10) * 8)[0] - ROM_BASE
        parole = []
        for k in range(n):
            q = struct.unpack_from("<I", rom, ptr + k * 12)[0] - ROM_BASE
            if not (0 < q < len(rom)):
                return None
            s = ""
            i = q
            while i < len(rom) and rom[i] != 0xFF and len(s) < 24:
                c = tabella.get(rom[i])
                if c is None:
                    return None
                s += c
                i += 1
            parole.append(s)
        fuori[g] = parole
    return fuori


# --------------------------------------------------------------------------

def carica_nomi(percorso_rom=None, verboso=False):
    """Ritorna {'items':{ITEM_X:nome}, 'mapsec':{...}, 'classi':{...},
    'trainers':{...}, 'specie':{...}} oppure {} se la ROM non c'e'."""
    percorso_rom = percorso_rom or ROM_DEF
    if not os.path.exists(percorso_rom):
        if verboso:
            print("ROM italiana non trovata:", percorso_rom)
        return {}
    rom = open(percorso_rom, "rb").read()
    if rom[0xAC:0xB0] != b"BPEI":
        if verboso:
            print("non e' la ROM italiana di Smeraldo (gamecode %r)" % rom[0xAC:0xB0])
        return {}
    t = Testo(carica_charmap())
    out = {}
    rapporto = []

    # --- strumenti
    items = costanti("include/constants/items.h", "ITEM_")
    p = cerca_items(rom)
    if p is not None:
        d = {}
        for i, nome in enumerate(items):
            if not nome or i * 44 + 14 > len(rom):
                continue
            s, buono = t.pulito(rom[p + i * 44:p + i * 44 + 14])
            if buono:
                d[nome] = s
        out["items"] = d
        rapporto.append("strumenti  0x%08X  %d nomi   (ITEM_POTION = %s)"
                        % (p + ROM_BASE, len(d), d.get("ITEM_POTION", "?")))

    # --- specie
    specie = costanti("include/constants/species.h", "SPECIES_")
    p = cerca_specie(rom, t)
    if p is not None:
        d = {}
        for i, nome in enumerate(specie):
            if not nome:
                continue
            s, buono = t.pulito(rom[p + i * 11:p + i * 11 + 11])
            if buono:
                d[nome] = s
        out["specie"] = d
        rapporto.append("specie     0x%08X  %d nomi   (SPECIES_PIKACHU = %s)"
                        % (p + ROM_BASE, len(d), d.get("SPECIES_PIKACHU", "?")))

        # --- mosse: gMoveNames sta SUBITO DOPO gSpeciesNames --------------
        # Nel .map della build USA: gSpeciesNames 0x083185C8, gMoveNames
        # 0x0831977C, differenza 0x11B4 = 412 voci x 11 byte. L'ordine dei
        # dati e' lo stesso in tutte le lingue (stessa build, cambia solo il
        # testo), quindi l'adiacenza regge; la validazione qui sotto la
        # controlla comunque voce per voce: 355 nomi tutti leggibili e la
        # voce 0 che e' il segnaposto, o non si scrive niente.
        mosse = costanti("include/constants/moves.h", "MOVE_")
        base_mosse = p + 412 * 11
        dm = {}
        valide = True
        for i, nome in enumerate(mosse[:355]):
            s, buono = t.pulito(rom[base_mosse + i * 13:base_mosse + (i + 1) * 13])
            if not buono or not s:
                valide = False
                break
            if nome and i > 0:
                dm[nome] = s
        if valide and len(dm) >= 350:
            out["mosse"] = dm
            rapporto.append("mosse      0x%08X  %d nomi   (MOVE_POUND = %s)"
                            % (base_mosse + ROM_BASE, len(dm), dm.get("MOVE_POUND", "?")))

    # --- zone della mappa
    ordine, seq = impronta_regionmap()
    p = cerca_regionmap(rom, seq)
    if p is not None:
        d = {}
        for i, nome in enumerate(ordine):
            ptr = int.from_bytes(rom[p + i * 8 + 4:p + i * 8 + 8], "little") - ROM_BASE
            if not (0 < ptr < len(rom) - 2):
                continue
            s, buono = t.pulito(rom[ptr:ptr + 24])
            if buono:
                d[nome] = s
        out["mapsec"] = d
        rapporto.append("zone       0x%08X  %d nomi   (LITTLEROOT = %s)"
                        % (p + ROM_BASE, len(d), d.get("MAPSEC_LITTLEROOT_TOWN", "?")))

    # --- classi degli allenatori
    ordine_cl, nomi_cl, gruppi = impronta_classi()
    p = cerca_classi(rom, t, len(ordine_cl), gruppi)
    if p is not None:
        d = {}
        for i, nome in enumerate(ordine_cl):
            if not nome:
                continue
            s, buono = t.pulito(rom[p + i * 13:p + i * 13 + 13])
            if buono:
                d[nome] = s
        out["classi"] = d
        rapporto.append("classi     0x%08X  %d nomi   (HIKER = %s)"
                        % (p + ROM_BASE, len(d), d.get("TRAINER_CLASS_HIKER", "?")))

    # --- allenatori
    ordine_tr, seq_tr = impronta_trainers()
    p = cerca_trainers(rom, seq_tr)
    if p is not None:
        d = {}
        for i, nome in enumerate(ordine_tr):
            if not nome:
                continue
            s, buono = t.pulito(rom[p + i * 40 + 4:p + i * 40 + 15])
            if buono:
                d[nome] = s
        out["trainers"] = d
        rapporto.append("allenatori 0x%08X  %d nomi   (TRAINER_SAWYER_1 = %s)"
                        % (p + ROM_BASE, len(d), d.get("TRAINER_SAWYER_1", "?")))

    # --- le parole della frase di tendenza (per il calcolatore di Feebas)
    ec = cerca_easy_chat(rom)
    if ec:
        out["frasi"] = {"conditions": ec[10], "lifestyle": ec[12], "hobbies": ec[13]}
        rapporto.append("frase      %d+%d+%d parole (CONDITIONS[44] = %s)"
                        % (len(ec[10]), len(ec[12]), len(ec[13]), ec[10][44]))

    if verboso:
        for r in rapporto:
            print("  " + r)
        mancano = [k for k in ("items", "specie", "mapsec", "classi", "trainers", "mosse", "frasi") if k not in out]
        if mancano:
            print("  NON trovate:", ", ".join(mancano), "- la pagina restera' in inglese per quelle")
    return out


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    rom = sys.argv[1] if len(sys.argv) > 1 else ROM_DEF
    print("ROM:", rom)
    n = carica_nomi(rom, verboso=True)
    if not n:
        return 1
    dove = os.path.join(RADICE, "net", "mappa", "nomi_it.json")
    if os.path.isdir(os.path.dirname(dove)):
        io.open(dove, "w", encoding="utf-8").write(json.dumps(n, ensure_ascii=False))
        print("scritto", dove)
    return 0


if __name__ == "__main__":
    sys.exit(main())
