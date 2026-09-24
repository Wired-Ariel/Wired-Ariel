#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_mappa.py - genera i dati della MAPPA LIVE del pannello dalla decomp
(repo-studio/pokeemerald), senza nessuna immagine altrui (2026-08-21).

Produce in net/mappa/:
    dati.json        mappe (gruppo.numero -> nome, dimensioni, immagine, offset
                     nel mondo, strumenti, incontri selvatici), mondi (le
                     componenti connesse cucite con le connessioni), indice
                     specie -> dove si trova, nomi
    img/<layout>.png ogni layout renderizzato (PNG indicizzato 8 bit)
    icone/<specie>.png   icone dei Pokemon (32x32, dalla decomp)
    oe/<GFX>_<dir>.png   sprite degli object event (NPC, allenatori, palle, alberi, massi)
    sprite/<chi>_<dir>.png  Brendan/May, 4 direzioni (16x32)

Tutto in libreria standard: PNG decodificato e codificato a mano (zlib).
Le coordinate degli EVENTI del payload sono di GRIGLIA (mappa + MAP_OFFSET 7):
chi disegna sottrae 7. Qui le coordinate degli strumenti sono di MAPPA (0-based,
come nei map.json).

    D:\\Progettini\\Python313\\python.exe tools\\gen_mappa.py
    D:\\Progettini\\Python313\\python.exe tools\\gen_mappa.py --solo-esterni   (prova veloce)
"""
import json
import os
import re
import struct
import sys
import time
import zlib
from pathlib import Path

QUI = Path(__file__).resolve().parent
ROOT = QUI.parent                      # overworld-link
DECOMP = ROOT.parent / "repo-studio" / "pokeemerald"
OUT = ROOT / "net" / "mappa"

MAP_OFFSET = 7
NUM_TILES_IN_PRIMARY = 512
NUM_METATILES_IN_PRIMARY = 512
NUM_PALS_IN_PRIMARY = 6
NUM_PALS_TOTAL = 13

# --------------------------------------------------------------------------
# PNG: decodifica (indicizzato 1/2/4/8 bit) e codifica (indicizzato 8 bit)
# --------------------------------------------------------------------------

def png_decode(path):
    """Ritorna (w, h, bitdepth, palette[(r,g,b)], trns[list], righe) dove righe
    e' una lista di bytes con UN byte per pixel (indice di palette)."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s: non e' un PNG" % path)
    pos = 8
    w = h = bd = ct = None
    palette = []
    trns = []
    idat = []
    while pos < len(data):
        ln, = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, bd, ct, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if interlace:
                raise ValueError("%s: PNG interlacciato, non supportato" % path)
        elif typ == b"PLTE":
            palette = [(body[i], body[i + 1], body[i + 2]) for i in range(0, len(body), 3)]
        elif typ == b"tRNS":
            trns = list(body)
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if ct != 3:
        raise ValueError("%s: tipo colore %s, serve indicizzato (3)" % (path, ct))
    raw = zlib.decompress(b"".join(idat))
    stride = (w * bd + 7) // 8
    bpp = 1  # byte per pixel ai fini del filtro (bitdepth <= 8)
    righe = []
    prev = bytearray(stride)
    p = 0
    for _ in range(h):
        f = raw[p]
        p += 1
        cur = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(stride):
                cur[i] = (cur[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (cur[i] + pr) & 0xFF
        elif f != 0:
            raise ValueError("%s: filtro PNG %d sconosciuto" % (path, f))
        prev = cur
        # spacchetta in un byte per pixel
        if bd == 8:
            righe.append(bytes(cur[:w]))
        else:
            out = bytearray(w)
            per_byte = 8 // bd
            mask = (1 << bd) - 1
            for x in range(w):
                b = cur[x // per_byte]
                shift = 8 - bd * (x % per_byte + 1)
                out[x] = (b >> shift) & mask
            righe.append(bytes(out))
    return w, h, bd, palette, trns, righe


def _chunk(typ, body):
    c = typ + body
    return struct.pack(">I", len(body)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)


def png_encode_indexed(w, h, righe, palette, trns=None, level=6):
    """righe: lista di bytes (un byte per pixel). palette: lista di (r,g,b)."""
    raw = b"".join(b"\x00" + r for r in righe)
    plte = b"".join(bytes(c) for c in palette)
    out = [b"\x89PNG\r\n\x1a\n",
           _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0)),
           _chunk(b"PLTE", plte)]
    if trns:
        out.append(_chunk(b"tRNS", bytes(trns)))
    out.append(_chunk(b"IDAT", zlib.compress(raw, level)))
    out.append(_chunk(b"IEND", b""))
    return b"".join(out)


def leggi_pal(path):
    righe = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    assert righe[0].strip() == "JASC-PAL", path
    n = int(righe[2])
    cols = []
    for r in righe[3:3 + n]:
        a, b, c = [int(v) for v in r.split()[:3]]
        cols.append((a, b, c))
    while len(cols) < 16:
        cols.append((0, 0, 0))
    return cols[:16]


# --------------------------------------------------------------------------
# Tileset
# --------------------------------------------------------------------------

class Tileset:
    def __init__(self, label, dirpath, secondary):
        self.label = label
        self.dir = Path(dirpath)
        self.secondary = secondary
        w, h, bd, _, _, righe = png_decode(self.dir / "tiles.png")
        assert w == 128, "%s: tiles.png largo %d, atteso 128" % (label, w)
        self.tile_rows = righe           # un byte per pixel, 16 tile per riga
        self.ntiles = (h // 8) * 16
        # I tileset delle basi segrete (secret_base/red_cave, ...) hanno solo i
        # tiles.png nella propria cartella: metatili, attributi e palette stanno
        # nella cartella MADRE (data/tilesets/secondary/secret_base/). Si cerca
        # nella cartella, poi nella madre.
        def trova(nome):
            for d in (self.dir, self.dir.parent):
                if (d / nome).exists():
                    return d / nome
            raise FileNotFoundError("%s: manca %s (anche nella cartella madre)" % (label, nome))

        mt = trova("metatiles.bin").read_bytes()
        self.metatiles = struct.unpack("<%dH" % (len(mt) // 2), mt)
        attr = trova("metatile_attributes.bin").read_bytes()
        self.attrs = struct.unpack("<%dH" % (len(attr) // 2), attr)
        paldir = trova("palettes")
        self.pals = []
        for i in range(16):
            p = paldir / ("%02d.pal" % i)
            self.pals.append(leggi_pal(p) if p.exists() else [(0, 0, 0)] * 16)

    def tile_pixel(self, t, x, y):
        """indice colore (0-15) del pixel (x,y) del tile t, gia' nel sistema
        del tileset (t locale)."""
        row = self.tile_rows[(t >> 4) * 8 + y]
        return row[((t & 15) << 3) + x]


class TilesetPair:
    """Primario + secondario: render dei metatile in blocchi 16x16 di indici
    GLOBALI (palette*16 + colore), con cache."""

    def __init__(self, prim, sec):
        self.prim = prim
        self.sec = sec
        self.cache = {}
        # palette globale a 256: 0..207 = 13 palette x 16; il resto nero
        self.palette = []
        for p in range(NUM_PALS_TOTAL):
            src = prim.pals[p] if p < NUM_PALS_IN_PRIMARY else sec.pals[p]
            self.palette.extend(src)
        while len(self.palette) < 256:
            self.palette.append((0, 0, 0))

    def _tile(self, t):
        if t < NUM_TILES_IN_PRIMARY:
            return self.prim, t
        return self.sec, t - NUM_TILES_IN_PRIMARY

    def behavior(self, mid):
        """Il metatile behavior (MB_*) di un metatile: i primi 8 bit degli
        attributi. E' quello che il gioco guarda per decidere se un tile e'
        acqua profonda (immersione), l'ingresso di una base segreta, o acqua
        pescabile - vedi src/fieldmap.c:409-426, che fa esattamente questo.
        Senza questa riga la mappa non poteva sapere DOVE ci si immerge: nei
        dati degli eventi non c'e' nessun marcatore, e' tutto nel terreno."""
        if mid < NUM_METATILES_IN_PRIMARY:
            ts, i = self.prim, mid
        else:
            ts, i = self.sec, mid - NUM_METATILES_IN_PRIMARY
        if i >= len(ts.attrs):
            return 0
        return ts.attrs[i] & 0xFF

    def metatile(self, mid):
        """16 righe di 16 byte (indici globali)."""
        blk = self.cache.get(mid)
        if blk is not None:
            return blk
        if mid < NUM_METATILES_IN_PRIMARY:
            ts, base = self.prim, mid * 8
        else:
            ts, base = self.sec, (mid - NUM_METATILES_IN_PRIMARY) * 8
        if base + 8 > len(ts.metatiles):
            blk = [bytes(16)] * 16
            self.cache[mid] = blk
            return blk
        rows = [bytearray(16) for _ in range(16)]
        for layer in (0, 1):             # 0 = sotto (opaco), 1 = sopra
            for q in range(4):
                e = ts.metatiles[base + layer * 4 + q]
                t = e & 0x3FF
                hflip = (e >> 10) & 1
                vflip = (e >> 11) & 1
                pal = (e >> 12) & 0xF
                src, tl = self._tile(t)
                if tl >= src.ntiles:
                    continue
                ox = (q & 1) * 8
                oy = (q >> 1) * 8
                for y in range(8):
                    sy = 7 - y if vflip else y
                    row = src.tile_rows[(tl >> 4) * 8 + sy]
                    rbase = (tl & 15) << 3
                    dst = rows[oy + y]
                    for x in range(8):
                        sx = 7 - x if hflip else x
                        c = row[rbase + sx]
                        if c == 0:
                            if layer == 0:
                                dst[ox + x] = 0       # sfondo: colore 0 della palette 0
                            continue
                        dst[ox + x] = (pal << 4) | c
        blk = [bytes(r) for r in rows]
        self.cache[mid] = blk
        return blk


def carica_tileset_index():
    """gTileset_X -> (cartella, isSecondary) dai due .h della decomp."""
    headers = (DECOMP / "src/data/tilesets/headers.h").read_text(encoding="utf-8", errors="replace")
    # I secondari stanno in src/data/tilesets/graphics.h, i PRIMARI (General,
    # Building) in src/graphics.c: si leggono tutti e due.
    graphics = ""
    for f in ("src/data/tilesets/graphics.h", "src/graphics.c"):
        graphics += (DECOMP / f).read_text(encoding="utf-8", errors="replace")
    tiles_to_dir = {}
    for m in re.finditer(r"gTilesetTiles_(\w+)\[\]\s*=\s*INCGFX_U32\(\"([^\"]+)/tiles\.png\"", graphics):
        tiles_to_dir["gTilesetTiles_" + m.group(1)] = DECOMP / m.group(2)
    out = {}
    for m in re.finditer(r"const struct Tileset (gTileset_\w+) =\s*\{(.*?)\};", headers, re.S):
        label, body = m.group(1), m.group(2)
        sec = re.search(r"\.isSecondary\s*=\s*(TRUE|FALSE)", body).group(1) == "TRUE"
        tiles = re.search(r"\.tiles\s*=\s*(\w+)", body).group(1)
        if tiles in tiles_to_dir:
            out[label] = (tiles_to_dir[tiles], sec)
    return out


# --------------------------------------------------------------------------
# Nomi
# --------------------------------------------------------------------------

def camel_split(s):
    """'LittlerootTown_BrendansHouse_1F' -> 'Littleroot Town - Brendans House 1F';
    'ItemPPUp' -> 'PP Up'; 'TM10' -> 'TM10'."""
    parti = []
    for pezzo in s.split("_"):
        toks = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+|[A-Z]", pezzo)
        # riunisci "TM" + "10", "1" + "F"
        merged = []
        for t in toks:
            if merged and ((t.isdigit() and merged[-1].isalpha() and merged[-1].isupper())
                           or (t.isalpha() and len(t) == 1 and merged[-1].isdigit())):
                merged[-1] += t
            else:
                merged.append(t)
        parti.append(" ".join(merged))
    return " - ".join(p for p in parti if p)


SPECIE_SPECIALI = {
    "NIDORAN_F": "Nidoran\u2640", "NIDORAN_M": "Nidoran\u2642", "MR_MIME": "Mr. Mime",
    "FARFETCHD": "Farfetch'd", "HO_OH": "Ho-Oh", "PORYGON2": "Porygon2",
}


def nome_specie(const):
    k = const.replace("SPECIES_", "")
    if k in SPECIE_SPECIALI:
        return SPECIE_SPECIALI[k]
    return " ".join(p.capitalize() for p in k.split("_"))


def nome_item(const):
    k = const.replace("ITEM_", "")
    m = re.match(r"^(TM|HM)(\d+)$", k)
    if m:
        return m.group(1) + m.group(2)
    return " ".join(p.upper() if p in ("PP", "HP", "EXP") else p.capitalize() for p in k.split("_"))


def nome_item_da_script(script):
    m = re.search(r"_EventScript_Item(\w+)$", script or "")
    if not m:
        return None
    return camel_split(m.group(1))


def nome_mappa(map_name, region_section):
    return camel_split(map_name)


def nomi_mapsec_en():
    """MAPSEC_X -> nome inglese, come lo scrive il gioco ("LITTLEROOT TOWN").
    Serve per sapere DOVE finisce il pezzo traducibile del nome di una mappa:
    "Littleroot Town - Brendans House - 1F" diventa italiano solo nella prima
    parte, perche' "Brendans House - 1F" nel gioco italiano non esiste come
    stringa - e inventarla sarebbe esattamente cio' che non si fa."""
    src = (DECOMP / "src/data/region_map/region_map_entries.h").read_text(
        encoding="utf-8", errors="replace")
    testi = dict(re.findall(r"(sMapName_\w+)\[\]\s*=\s*_\(\"([^\"]*)\"\)", src))
    out = {}
    for m in re.finditer(r"\[(MAPSEC_\w+)\]\s*=\s*\{(.*?)\}", src, re.S):
        sim = re.search(r"\.name\s*=\s*(sMapName_\w+)", m.group(2))
        if sim and sim.group(1) in testi:
            out[m.group(1)] = testi[sim.group(1)]
    return out


def titolo(s):
    """"POZIONE" -> "Pozione", ma "MT06" resta "MT06".

    LE LEGATURE ROMPEVANO IL CONTO (2026-08-25). "PORTAPOKéMELLE" la ROM la
    disegna con la tessera "Ké", che porta dentro una minuscola: `isupper()`
    diceva di no e la parola restava URLATA in mezzo a "Chic Ball" e "Tessera
    Gare" (visto da Lain nella schedina). Adesso conta come maiuscola anche
    una parola senza minuscole ASCII che contiene lettere accentate."""
    def urlata(w):
        if w.isupper():
            return True
        # nessuna minuscola ASCII, almeno una maiuscola, e qualche lettera
        # fuori ASCII (la legatura): e' un nome scritto tutto maiuscolo
        return (not any("a" <= c <= "z" for c in w)
                and any("A" <= c <= "Z" for c in w)
                and any(ord(c) > 127 for c in w))
    fuori = []
    for w in (s or "").split(" "):
        fuori.append(w if re.match(r"^[A-Z]+\d+$", w) else (w.capitalize() if urlata(w) else w))
    return " ".join(fuori)


# --------------------------------------------------------------------------
# Incontri selvatici
# --------------------------------------------------------------------------

TASSI = {
    "land_mons": [20, 20, 10, 10, 10, 10, 5, 5, 4, 4, 1, 1],
    "water_mons": [60, 30, 5, 4, 1],
    "rock_smash_mons": [60, 30, 5, 4, 1],
}
PESCA = {"old_rod": ([0, 1], [70, 30]), "good_rod": ([2, 3, 4], [60, 20, 20]),
         "super_rod": ([5, 6, 7, 8, 9], [40, 40, 15, 4, 1])}
METODO_NOME = {"land_mons": "terra", "water_mons": "acqua", "rock_smash_mons": "roccia",
               "old_rod": "pesca (amo vecchio)", "good_rod": "pesca (amo buono)",
               "super_rod": "pesca (super amo)"}


def aggrega(mons, rates, tenuti=None):
    per = {}
    for slot, r in zip(mons, rates):
        sp = slot["species"]
        e = per.setdefault(sp, {"specie": nome_specie(sp), "cost": sp, "pct": 0,
                                "lv_min": 999, "lv_max": 0})
        e["pct"] += r
        e["lv_min"] = min(e["lv_min"], slot["min_level"])
        e["lv_max"] = max(e["lv_max"], slot["max_level"])
    out = []
    for e in sorted(per.values(), key=lambda v: -v["pct"]):
        voce = {"specie": e["specie"], "cost": e["cost"], "pct": e["pct"],
                "lv": ("%d" % e["lv_min"]) if e["lv_min"] == e["lv_max"]
                      else "%d-%d" % (e["lv_min"], e["lv_max"])}
        # Lo strumento che il selvatico puo' TENERE addosso: e' una proprieta'
        # della specie, non della mappa, ma qui e' dove serve leggerla.
        if tenuti is not None:
            t = tenuti_di(e["cost"], tenuti)
            if t:
                voce["tenuti"] = t
        out.append(voce)
    return out


def carica_incontri(tenuti=None):
    d = json.loads((DECOMP / "src/data/wild_encounters.json").read_text(encoding="utf-8"))
    gruppo = [g for g in d["wild_encounter_groups"] if g.get("for_maps")][0]
    per_mappa = {}
    for e in gruppo["encounters"]:
        voce = {}
        for tipo in ("land_mons", "water_mons", "rock_smash_mons"):
            if tipo in e:
                voce[METODO_NOME[tipo]] = {"tasso": e[tipo]["encounter_rate"],
                                          "specie": aggrega(e[tipo]["mons"], TASSI[tipo], tenuti)}
        if "fishing_mons" in e:
            mons = e["fishing_mons"]["mons"]
            for canna, (slots, rates) in PESCA.items():
                voce[METODO_NOME[canna]] = {"tasso": e["fishing_mons"]["encounter_rate"],
                                            "specie": aggrega([mons[i] for i in slots], rates, tenuti)}
        # piu' voci per la stessa mappa (es. Altering Cave) si sommano: si tiene la prima
        per_mappa.setdefault(e["map"], voce)
    return per_mappa


# --------------------------------------------------------------------------
# Object event: gli sprite dei personaggi e degli oggetti visibili
# --------------------------------------------------------------------------

ANIM_DIREZIONALI = ("sAnimTable_Standard", "sAnimTable_BrendanMayNormal", "sAnimTable_AcroBike",
                    "sAnimTable_Surfing", "sAnimTable_FieldMove", "sAnimTable_Fishing")


def carica_oe_catalogo():
    """OBJ_EVENT_GFX_X -> {w, h, frames, png, direzionale}. Quattro file della
    decomp, incatenati: pointers (costante -> info), info (misure, anims,
    images), pic tables (images -> pic symbol + numero di frame), graphics
    (pic symbol -> png)."""
    d = DECOMP / "src/data/object_events"
    pointers = (d / "object_event_graphics_info_pointers.h").read_text(encoding="utf-8", errors="replace")
    info = (d / "object_event_graphics_info.h").read_text(encoding="utf-8", errors="replace")
    pics = (d / "object_event_pic_tables.h").read_text(encoding="utf-8", errors="replace")
    # le tabelle degli alberi di bacche stanno in un file a parte
    bt = d / "berry_tree_graphics_tables.h"
    if bt.exists():
        pics += "\n" + bt.read_text(encoding="utf-8", errors="replace")
    gfx = (d / "object_event_graphics.h").read_text(encoding="utf-8", errors="replace")
    const_to_info = dict(re.findall(r"\[(OBJ_EVENT_GFX_\w+)\]\s*=\s*&(\w+)", pointers))
    infos = {}
    for m in re.finditer(r"const struct ObjectEventGraphicsInfo (\w+) = \{(.*?)\};", info, re.S):
        body = m.group(2)
        w = re.search(r"\.width\s*=\s*(\d+)", body)
        h = re.search(r"\.height\s*=\s*(\d+)", body)
        im = re.search(r"\.images\s*=\s*(\w+)", body)
        an = re.search(r"\.anims\s*=\s*(\w+)", body)
        if w and h and im:
            infos[m.group(1)] = (int(w.group(1)), int(h.group(1)), im.group(1), an.group(1) if an else "")
    tables = {}
    misure_tab = {}
    for m in re.finditer(r"sPicTable_(\w+)\[\]\s*=\s*\{(.*?)\};", pics, re.S):
        # ogni voce: (simbolo della grafica, indice del frame dentro quella grafica)
        voci, misure = [], []
        for mm in re.finditer(r"overworld_frame\((\w+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)|obj_frame_tiles\((\w+)\)", m.group(2)):
            if mm.group(1):
                voci.append((mm.group(1), int(mm.group(4))))
                misure.append((int(mm.group(2)) * 8, int(mm.group(3)) * 8))
            else:
                voci.append((mm.group(5), 0))
                misure.append(None)
        if voci:
            tables["sPicTable_" + m.group(1)] = voci
            # LE MISURE STANNO NELLA TABELLA, NON NELL'INFO. Per quasi tutti i
            # personaggi le due cose coincidono, ma non per gli alberi di
            # bacche: gObjectEventGraphicsInfo_BerryTree dichiara 16x16 mentre
            # i fotogrammi dell'albero coi frutti sono overworld_frame(..., 2, 4)
            # cioe' 16x32. Con l'info si ritagliava mezzo albero.
            misure_tab["sPicTable_" + m.group(1)] = misure
    paths = dict(re.findall(r"(gObjectEventPic_\w+)\[\]\s*=\s*INC\w+_U32\(\"([^\"]+\.png)\"", gfx))
    out = {}
    # UN ALBERO PER BACCA. gObjectEventGraphicsInfo_BerryTree punta a
    # sPicTable_PechaBerryTree (object_event_graphics_info.h:1175): usandolo per
    # tutti, OGNI albero del gioco si disegnava come un albero di Pecha. Il
    # gioco invece scambia la tabella a caldo con gBerryTreePicTablePointers
    # (event_object_movement.c:1908). Qui si rifa' la stessa cosa, una voce
    # sintetica per bacca, con l'ultimo fotogramma = stadio con i frutti.
    bt_pointers = dict(re.findall(
        r"\[(ITEM_\w+) - FIRST_BERRY_INDEX\]\s*=\s*(sPicTable_\w+)", pics))
    info_bt = infos.get("gObjectEventGraphicsInfo_BerryTree")
    for item, tabella in bt_pointers.items():
        voci = tables.get(tabella)
        if not voci or not info_bt:
            continue
        png = paths.get(voci[-1][0])
        if not png:
            continue
        mis = (misure_tab.get(tabella) or [None])[-1] or (info_bt[0], info_bt[1])
        out["BERRYTREE_" + item.replace("ITEM_", "")] = {
            "w": mis[0], "h": mis[1],
            "frames": [(DECOMP / png, voci[-1][1])], "direzionale": False}

    for const, infoname in const_to_info.items():
        if infoname not in infos:
            continue
        w, h, tab, anims = infos[infoname]
        if tab not in tables:
            continue
        voci = tables[tab]
        # un albero di bacche si disegna nello stadio FINALE (con i frutti), che
        # e' l'ultima voce della sua tabella; tutto il resto parte dalla prima.
        scelte = voci[:3] if (anims in ANIM_DIREZIONALI and len(voci) >= 3) else ([voci[-1]] if "BERRY_TREE" in const else [voci[0]])
        frames = []
        for pic, fi in scelte:
            png = paths.get(pic)
            if not png:
                frames = []
                break
            frames.append((DECOMP / png, fi))
        if not frames:
            continue
        out[const] = {"w": w, "h": h, "frames": frames,
                      "direzionale": anims in ANIM_DIREZIONALI and len(voci) >= 3}
    return out


def rendi_oe(cat, usati, outdir):
    """Scrive oe/<GFX>_<dir>.png per ogni gfx usato; ritorna {GFX: {w,h,img:{dir:file}}}."""
    res = {}
    cache = {}
    for const in sorted(usati):
        c = cat.get(const)
        if c is None:
            continue
        fw, fh = c["w"], c["h"]

        def frame(png, i, flip=False):
            if png not in cache:
                cache[png] = png_decode(png)
            W, H, bd, pal, trns, righe = cache[png]
            cols = max(1, W // fw)
            fx, fy = (i % cols) * fw, (i // cols) * fh
            if fy + fh > H or fx + fw > W:
                fx, fy = 0, 0
            out = []
            for y in range(fh):
                seg = righe[fy + y][fx:fx + fw]
                out.append(bytes(reversed(seg)) if flip else seg)
            return out, pal, trns

        nome = const.replace("OBJ_EVENT_GFX_", "")
        imgs = {}
        if c["direzionale"]:
            dirs = (("giu", c["frames"][0], False), ("su", c["frames"][1], False),
                    ("sx", c["frames"][2], False), ("dx", c["frames"][2], True))
        else:
            dirs = (("giu", c["frames"][0], False),)
        try:
            for nd, (png, fi), flip in dirs:
                if not png.exists():
                    raise FileNotFoundError(png)
                righe, pal, trns = frame(png, fi, flip)
                fn = "oe/%s_%s.png" % (nome, nd)
                (outdir / fn).write_bytes(png_encode_indexed(fw, fh, righe, pal[:256], trns or [0]))
                imgs[nd] = fn
        except Exception as e:   # noqa: BLE001
            print("  sprite %s: %s" % (const, e))
            continue
        res[const] = {"w": fw, "h": fh, "img": imgs}
    return res


# --------------------------------------------------------------------------
# Allenatori e script
# --------------------------------------------------------------------------

def nome_mossa(const):
    """MOVE_KARATE_CHOP -> "Karate Chop" (inglese: l'italiano arriva a valle
    dalla ROM, come per items e specie)."""
    return const.replace("MOVE_", "").replace("_", " ").title()


def carica_soldi_classi():
    """TRAINER_CLASS_X -> moltiplicatore del premio, da gTrainerMoneyTable
    (src/battle_main.c). Il premio vero e': 4 x livello dell'ULTIMO Pokemon
    della squadra x questo valore (x2 in lotta doppia) - la formula sta in
    GetTrainerMoneyToGive (src/battle_script_commands.c:5570-5630)."""
    t = (DECOMP / "src/battle_main.c").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"gTrainerMoneyTable\[\]\s*=\s*\{(.*?)\n\};", t, re.S)
    out = {}
    if m:
        for cl, v in re.findall(r"\{\s*(TRAINER_CLASS_\w+)\s*,\s*(\d+)\s*\}", m.group(1)):
            out[cl] = int(v)
    out["_default"] = 5   # {0xFF, 5}: le classi non elencate
    return out


def carica_allenatori():
    """TRAINER_X -> {nome, classe, squadra:[{specie, lv, mosse?, tiene?}],
    soldi, doppia} da trainers.h, trainer_parties.h, trainer_class_names.h
    e gTrainerMoneyTable."""
    t = (DECOMP / "src/data/trainers.h").read_text(encoding="utf-8", errors="replace")
    pr = (DECOMP / "src/data/trainer_parties.h").read_text(encoding="utf-8", errors="replace")
    cn = (DECOMP / "src/data/text/trainer_class_names.h").read_text(encoding="utf-8", errors="replace")
    classi = {k: v for k, v in re.findall(r"\[(TRAINER_CLASS_\w+)\]\s*=\s*_\(\"([^\"]*)\"\)", cn)}
    soldi_classi = carica_soldi_classi()
    parties = {}
    for m in re.finditer(r"(sParty_\w+)\[\]\s*=\s*\{(.*?)\n\};", pr, re.S):
        # Ogni voce inizia con .lvl e .species; .heldItem e .moves (se ci
        # sono) seguono PRIMA del .lvl successivo: si affetta cosi', senza
        # contare le graffe annidate di .moves.
        mons = []
        for mm in re.finditer(r"\.lvl\s*=\s*(\d+)\s*,\s*\.species\s*=\s*(SPECIES_\w+)(.*?)(?=\.lvl\s*=|\Z)",
                              m.group(2), re.S):
            mon = {"specie": nome_specie(mm.group(2)), "cost": mm.group(2), "lv": int(mm.group(1))}
            coda = mm.group(3)
            tiene = re.search(r"\.heldItem\s*=\s*(ITEM_\w+)", coda)
            if tiene and tiene.group(1) != "ITEM_NONE":
                mon["tiene"] = tiene.group(1)
            mosse = re.search(r"\.moves\s*=\s*\{([^}]*)\}", coda)
            if mosse:
                elenco = re.findall(r"MOVE_\w+", mosse.group(1))
                if any(mv != "MOVE_NONE" for mv in elenco):
                    mon["mosse"] = [mv for mv in elenco if mv != "MOVE_NONE"]
            mons.append(mon)
        parties[m.group(1)] = mons
    out = {}
    for m in re.finditer(r"\[(TRAINER_\w+)\]\s*=\s*\{(.*?)\n\s*\},", t, re.S):
        body = m.group(2)
        nome = re.search(r"\.trainerName\s*=\s*_\(\"([^\"]*)\"\)", body)
        cl = re.search(r"\.trainerClass\s*=\s*(TRAINER_CLASS_\w+)", body)
        pa = re.search(r"\((sParty_\w+)\)", body)
        squadra = parties.get(pa.group(1), []) if pa else []
        doppia = ".doubleBattle = TRUE" in body
        # Il premio in denaro: 4 x livello dell'ULTIMO della squadra x
        # moltiplicatore della classe (x2 se lotta doppia).
        soldi = 0
        if squadra and cl:
            base = soldi_classi.get(cl.group(1), soldi_classi["_default"])
            soldi = 4 * squadra[-1]["lv"] * base * (2 if doppia else 1)
        out[m.group(1)] = {"nome": (nome.group(1) if nome else "?").title(),
                           "classe": classi.get(cl.group(1), cl.group(1) if cl else "?").title(),
                           "classe_c": cl.group(1) if cl else "",
                           "squadra": squadra,
                           "soldi": soldi,
                           "doppia": doppia}
    return out


def carica_rematch():
    """TRAINER base -> [TRAINER delle rivincite], da gRematchTable.

    Le RIVINCITE (src/battle_setup.c, gRematchTable) sono 78 voci da cinque
    trainer id: il primo e' l'allenatore come lo si incontra la prima volta,
    gli altri quattro sono squadre diverse e piu' forti, che il gioco tira
    fuori quando quello ti richiama col PokeNav. Sono normalissime voci di
    gTrainers, quindi le squadre carica_allenatori() le ha GIA' tutte: qui
    serve solo il collegamento fra la base e le sue rivincite.

    L'AGGANCIO E' IL PRIMO ID, NON LA MAPPA. La mappa della tabella per i
    capipalestra e' la CITTA' (MAP_RUSTBORO_CITY), non la palestra dove sta
    l'allenatore: usarla per l'associazione li perderebbe tutti.

    Due casi da non confondere con un errore di lettura:
      * CINDY salta il _2 (e' _1, _3, _4, _5, _6);
      * WALLY_VR ripete il _5 nell'ultimo slot, e i cinque della Superquattro
        (Sidney, Phoebe, Glacia, Drake, Wallace) ripetono lo STESSO trainer
        cinque volte - li' non c'e' nessuna squadra nuova da mostrare. Il
        filtro dei duplicati lo fa chi costruisce la voce, piu' sotto.
    """
    t = (DECOMP / "src/battle_setup.c").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"gRematchTable\[[^\]]*\]\s*=\s*\{(.*?)\n\};", t, re.S)
    if not m:
        return {}
    out = {}
    for voce in re.finditer(r"REMATCH\(([^)]*)\)", m.group(1)):
        pezzi = [p.strip() for p in voce.group(1).split(",")]
        trainer = [p for p in pezzi if p.startswith("TRAINER_")]
        if len(trainer) < 2:
            continue
        out[trainer[0]] = trainer[1:]
    return out


def blocchi_script(testo):
    """label -> corpo, per un .inc: le etichette stanno a colonna 0."""
    out = {}
    cur = None
    for riga in testo.splitlines():
        m = re.match(r"^([A-Za-z_][\w]*)::?\s*(?:@.*)?$", riga)
        if m:
            cur = m.group(1)
            out[cur] = []
        elif cur is not None:
            out[cur].append(riga)
    return {k: "\n".join(v) for k, v in out.items()}


def indice_script_globale():
    """label -> corpo, da TUTTI gli .inc del gioco.

    PERCHE' GLOBALE E NON PER MAPPA (2026-08-24). Prima si leggeva solo
    data/maps/<mappa>/scripts.inc e si cercava `giveitem` nel corpo IMMEDIATO
    dell'object event. Cosi' sparivano due categorie intere:

      - gli oggetti dietro un salto: `MauvilleCity_EventScript_Wattson` fa
        `goto_if_set` verso `..._CompletedNewMauville`, ed e' LI' che c'e'
        `giveitem ITEM_TM_THUNDERBOLT` (la MT24). Stessa cosa per la Chiave
        Seminterrato, sempre di Wattson;
      - gli oggetti dati da copioni CONDIVISI: il Portapokemelle sta in
        data/scripts/contest_hall.inc, che non e' lo scripts.inc di nessuna
        mappa.

    Con l'indice globale + la visita del grafo delle chiamate (doni_da_script)
    tutti e due i casi si ritrovano da soli."""
    out = {}
    for p in sorted((DECOMP / "data/maps").glob("*/scripts.inc")) + \
             sorted((DECOMP / "data/scripts").glob("*.inc")):
        try:
            out.update(blocchi_script(p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


# giveitem / giveitem_std / additem: tre modi di dare la stessa cosa.
# `finditem` NO: quello e' la palla a terra, che ha gia' la sua strada.
RE_DONO = re.compile(r"^\s*(?:giveitem|giveitem_std|additem)\s+(ITEM_\w+)", re.M)
# Un riferimento a un altro script: goto, call, goto_if_*, call_if_*, switch...
# Invece di elencare i comandi si cercano gli identificatori che ESISTONO
# come etichetta - piu' robusto, e le macro nuove non lo rompono.
RE_ETICHETTA = re.compile(r"\b([A-Za-z_]\w*_EventScript_\w+|EventScript_\w+)\b")

RE_TRAINERBATTLE = re.compile(r"trainerbattle\w*\s+(TRAINER_\w+)")

_CACHE_DONI = {}
_CACHE_TRAINER = {}


def trainer_da_script(label, indice, tetto=40):
    """Il TRAINER_ di uno script il cui `trainerbattle` sta DIETRO UN SALTO.

    Il caso che l'ha resa necessaria e' NORMAN: l'object event della palestra
    di Petalburg porta a uno `switch VAR_PETALBURG_GYM_STATE` e la lotta vera
    sta in un ramo (`..._EventScript_NormanBattle`), quindi cercando il
    comando nel solo corpo immediato il capo della quinta palestra non
    compariva affatto sulla mappa - e la cosa si notava proprio guardando le
    rivincite, che lui ha.

    Si usa SOLO come ripiego, quando nel corpo immediato non c'e' niente: chi
    ha il comando in casa non passa mai di qui, e uno script condiviso che
    contenga un trainerbattle non puo' quindi contaminare gli NPC normali. Il
    tetto e' basso apposta - si guarda il vicinato dello script, non mezzo
    gioco - e per lo stesso motivo si prende solo se il ramo raggiunto e'
    UNIVOCO: due allenatori diversi a valle vogliono dire che si e' finiti in
    un copione condiviso, e li' non si indovina.
    """
    if label in _CACHE_TRAINER:
        return _CACHE_TRAINER[label]
    _CACHE_TRAINER[label] = None      # anti-ricorsione
    visti, coda, trovati = set(), [label], set()
    while coda and len(visti) < tetto:
        l = coda.pop()
        if l in visti:
            continue
        visti.add(l)
        corpo = indice.get(l)
        if corpo is None:
            continue
        trovati.update(RE_TRAINERBATTLE.findall(corpo))
        if len(trovati) > 1:
            return None               # ambiguo: meglio nessuno che sbagliato
        for rif in RE_ETICHETTA.findall(corpo):
            if rif not in visti and rif in indice:
                coda.append(rif)
    uno = trovati.pop() if len(trovati) == 1 else None
    _CACHE_TRAINER[label] = uno
    return uno


def doni_da_script(label, indice, tetto=400):
    """Tutti gli ITEM_* raggiungibili da questo script, seguendo goto/call.

    Il tetto sui nodi visitati e' una cintura: alcuni copioni (i Mart, la
    Lega) si richiamano fra loro e senza limite la visita esplorerebbe mezzo
    gioco per ogni NPC."""
    if label in _CACHE_DONI:
        return _CACHE_DONI[label]
    _CACHE_DONI[label] = []          # anti-ricorsione: chi rientra vede vuoto
    visti, coda, trovati = set(), [label], []
    while coda and len(visti) < tetto:
        l = coda.pop()
        if l in visti:
            continue
        visti.add(l)
        corpo = indice.get(l)
        if corpo is None:
            continue
        for it in RE_DONO.findall(corpo):
            if it not in trovati:
                trovati.append(it)
        for rif in RE_ETICHETTA.findall(corpo):
            if rif not in visti and rif in indice:
                coda.append(rif)
    _CACHE_DONI[label] = trovati
    return trovati


def carica_strumenti_tenuti():
    """SPECIES_X -> (comune, raro) da species_info.h.

    Le probabilita' NON stanno qui ma in SetWildMonHeldItem (src/pokemon.c:
    6664-6716) e sono fisse: rnd = Random() % 100, chanceNoItem = 45,
    chanceNotRare = 95. Quindi 45% niente, 50% il comune, 5% il raro - e se
    comune e raro sono LO STESSO strumento e' garantito al 100% (ramo
    esplicito nel codice). Con Occhio Composto in testa alla squadra le
    soglie diventano 20 e 80: 20% niente, 60% comune, 20% raro."""
    t = (DECOMP / "src/data/pokemon/species_info.h").read_text(encoding="utf-8", errors="replace")
    out = {}
    for m in re.finditer(r"\[(SPECIES_\w+)\]\s*=\s*\{", t):
        i = m.end()
        prof, j = 1, i
        while j < len(t) and prof:
            if t[j] == "{":
                prof += 1
            elif t[j] == "}":
                prof -= 1
            j += 1
        corpo = t[i:j]
        c = re.search(r"\.itemCommon\s*=\s*(ITEM_\w+)", corpo)
        r = re.search(r"\.itemRare\s*=\s*(ITEM_\w+)", corpo)
        c = c.group(1) if c else "ITEM_NONE"
        r = r.group(1) if r else "ITEM_NONE"
        if c != "ITEM_NONE" or r != "ITEM_NONE":
            out[m.group(1)] = (c, r)
    return out


def tenuti_di(specie, tabella):
    """[{c, nome, pct}] per una specie, o [] se non tiene niente."""
    v = tabella.get(specie)
    if not v:
        return []
    c, r = v
    if c == r:                      # stesso strumento nei due slot: garantito
        return [{"c": c, "nome": nome_item(c), "pct": 100}]
    out = []
    if c != "ITEM_NONE":
        out.append({"c": c, "nome": nome_item(c), "pct": 50})
    if r != "ITEM_NONE":
        out.append({"c": r, "nome": nome_item(r), "pct": 5})
    return out


# --------------------------------------------------------------------------
# Quello che sta nel TERRENO, non negli eventi
# --------------------------------------------------------------------------
#
# Immersione, basi segrete e Feebas non hanno nessun marcatore nei dati degli
# eventi: il gioco li decide leggendo il metatile behavior del tile davanti al
# giocatore. Per disegnarli sulla mappa bisogna fare la stessa cosa, cioe'
# decodificare il blockdata del layout e chiedere il behavior a ogni tile.

MB_DIVE = (0x11, 0x12, 0x14)          # INTERIOR_DEEP / DEEP / SOOTOPOLIS_DEEP
MB_NO_RIEMERSIONE = (0x19, 0x2A)      # NO_SURFACING, SEAWEED_NO_SURFACING
MB_BASE_MIN, MB_BASE_MAX = 0x90, 0x9D
# I behavior su cui si puo' navigare (TILE_FLAG_SURFABLE, metatile_behavior.c),
# tolta la cascata: e' l'insieme che il gioco conta per numerare i punti di
# pesca di Feebas.
MB_PESCABILI = (0x10, 0x11, 0x12, 0x14, 0x15, 0x19, 0x22, 0x2A,
                0x50, 0x51, 0x52, 0x53, 0x6C, 0x6D, 0x6F)

BASE_TIPI = {0x90: "roccia rossa", 0x91: "roccia rossa",
             0x92: "roccia marrone", 0x93: "roccia marrone",
             0x94: "roccia gialla", 0x95: "roccia gialla",
             0x96: "albero", 0x97: "albero", 0x9C: "albero", 0x9D: "albero",
             0x98: "cespuglio", 0x99: "cespuglio",
             0x9A: "roccia blu", 0x9B: "roccia blu"}


def behavior_griglia(tp, blocks, w, h):
    """[(x, y, behavior)] per ogni tile del layout."""
    ids = struct.unpack("<%dH" % (len(blocks) // 2), blocks)
    out = []
    for by in range(h):
        for bx in range(w):
            i = by * w + bx
            if i < len(ids):
                out.append((bx, by, tp.behavior(ids[i] & 0x3FF)))
    return out


def carica_dive_connessioni():
    """mappa di superficie -> mappa sottomarina, dalle connessioni `dive`.

    Non sono connessioni spaziali (il gioco le salta nello scroll della
    camera, fieldmap.c:775): sono un warp che CONSERVA x,y. Quindi la mappa
    sott'acqua si apre nello stesso punto in cui ti sei immerso."""
    giu, su = {}, {}
    for mj in sorted((DECOMP / "data/maps").glob("*/map.json")):
        try:
            d = json.loads(mj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for c in d.get("connections") or []:
            if c.get("direction") == "dive":
                giu[d["id"]] = c["map"]
            elif c.get("direction") == "emerge":
                su[d["id"]] = c["map"]
    return giu, su


def carica_dive_fissi():
    """setdivewarp: le mappe SENZA connessione usano un warp a coordinate
    fisse, messo da uno script (scrcmd.c:851). Sono i fondali delle grotte,
    la Camera Sigillata e la Nave Abbandonata."""
    out = {}
    for sc in sorted((DECOMP / "data/maps").glob("*/scripts.inc")):
        try:
            t = sc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        trovati = [(m.group(1), int(m.group(2)), int(m.group(3)))
                   for m in re.finditer(r"setdivewarp\s+(MAP_\w+)\s*,\s*(\d+)\s*,\s*(\d+)", t)]
        if trovati:
            # LISTA, non primo match: Underwater_SealedChamber ne ha DUE
            # (Percorso 134 e la Camera Sigillata, scelti dallo script in
            # base alla posizione) e col primo soltanto la Camera Sigillata
            # spariva dalla navigazione. Doppioni via, ordine conservato.
            visti, puliti = set(), []
            for v in trovati:
                if v[0] not in visti:
                    visti.add(v[0])
                    puliti.append(v)
            out[sc.parent.name] = puliti
    return out


def carica_basi_segrete():
    """SECRET_BASE_X -> mappa interna, da secret_bases.h + secret_base.c.

    L'interno lo decide `id // 10` (il commento in secret_bases.h:11 lo dice
    esplicitamente): dieci basi diverse condividono lo stesso interno."""
    h = (DECOMP / "include/constants/secret_bases.h").read_text(encoding="utf-8", errors="replace")
    # SOLO gli id degli INGRESSI (SECRET_BASE_RED_CAVE1_2): le sei costanti
    # di famiglia (SECRET_BASE_RED_CAVE 1) e i gruppi (= SECRET_BASE_GROUP(n))
    # hanno la stessa forma ma non sono ingressi.
    val = {}
    for nome, v in re.findall(r"^#define\s+(SECRET_BASE_\w+\d_\d+)\s+(\d+)\s*$", h, re.M):
        val[nome] = int(v)
    c = (DECOMP / "src/secret_base.c").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"sSecretBaseEntrancePositions\[[^]]*\]\s*=\s*\{(.*?)\n\};", c, re.S)
    gruppi = []
    if m:
        for mm in re.finditer(r"MAP_NUM\(MAP_(\w+)\)", m.group(1)):
            gruppi.append("MAP_" + mm.group(1))
    return val, gruppi


def feebas_spot(tp, blocks, w, h):
    """I tile dove Feebas PUO' comparire su Percorso 119.

    Il gioco numera in ordine (riga per riga, x crescente) tutti i tile
    navigabili-non-cascata e ne sorteggia sei con un generatore suo, semato
    dalla frase di tendenza di Ceneride (wild_encounter.c:113-179): quali
    siano i sei dipende dal SALVATAGGIO e cambia quando cambia la frase,
    quindi da fuori non si possono sapere. Quello che si puo' dire - ed e'
    l'informazione utile - e' l'insieme dei candidati. I primi tre sono
    scartati dal gioco stesso perche' irraggiungibili."""
    ids = struct.unpack("<%dH" % (len(blocks) // 2), blocks)
    spot, n = [], 0
    for by in range(h):
        for bx in range(w):
            i = by * w + bx
            if i >= len(ids):
                continue
            b = tp.behavior(ids[i] & 0x3FF)
            if b in MB_PESCABILI:
                n += 1
                if n > 3:                 # 1,2,3 sono scartati da CheckFeebas
                    spot.append((bx, by, n))
    return spot


def carica_item_ball_scripts():
    """label -> ITEM_X dai finditem di data/scripts/item_ball_scripts.inc."""
    blk = blocchi_script((DECOMP / "data/scripts/item_ball_scripts.inc").read_text(encoding="utf-8", errors="replace"))
    out = {}
    for k, v in blk.items():
        m = re.search(r"finditem\s+(ITEM_\w+)", v)
        if m:
            out[k] = m.group(1)
    return out


def carica_nomi_italiani():
    """I nomi ufficiali italiani, se la ROM di Lain e' al suo posto. Se non
    c'e', la pagina resta in inglese e basta: non si inventa niente."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import nomi_it
        return nomi_it.carica_nomi(verboso=True)
    except Exception as e:   # noqa: BLE001
        print("nomi italiani non disponibili: %s" % e)
        return {}


def carica_icone_strumenti():
    """ITEM_X -> (png dell'icona, pal della palette). Due file incatenati:
    item_icon_table.h (costante -> simboli) e data/graphics/items.h (simbolo ->
    percorso del png). La palette e' un file a parte perche' e' quella che il
    gioco carica: quella dentro il png e' solo un residuo dello strumento di
    conversione, e su parecchie icone NON e' la stessa."""
    tab = (DECOMP / "src/data/item_icon_table.h").read_text(encoding="utf-8", errors="replace")
    gfx = (DECOMP / "src/data/graphics/items.h").read_text(encoding="utf-8", errors="replace")
    percorsi = dict(re.findall(r"(gItemIcon\w+)\[\]\s*=\s*INCGFX_U32\(\"([^\"]+)\"", gfx))
    out = {}
    for const, sim_png, sim_pal in re.findall(
            r"\[(ITEM_\w+)\]\s*=\s*\{\s*(\w+)\s*,\s*(\w+)\s*\}", tab):
        png, pal = percorsi.get(sim_png), percorsi.get(sim_pal)
        if png and pal:
            out[const] = (DECOMP / png, DECOMP / pal)
    # GLI ALIAS. Le MT e le MN hanno due nomi per lo stesso numero
    # (ITEM_TM06 e ITEM_TM_TOXIC): i map script usano il secondo, la tabella
    # delle icone il primo, e senza questo passaggio 31 strumenti su 157
    # restavano senza icona.
    for nome, val in valori_item().items():
        if nome in out:
            continue
        for altro, v2 in valori_item().items():
            if v2 == val and altro in out:
                out[nome] = out[altro]
                break
    return out


def valori_item():
    """ITEM_X -> numero, risolvendo gli alias (#define ITEM_TM_TOXIC ITEM_TM06)."""
    global _VALORI_ITEM
    if _VALORI_ITEM is not None:
        return _VALORI_ITEM
    testo = (DECOMP / "include/constants/items.h").read_text(encoding="utf-8", errors="replace")
    grezzi = dict(re.findall(r"^#define\s+(ITEM_\w+)\s+(\S+)", testo, re.M))
    val = {}
    for nome in grezzi:
        v, giri = grezzi[nome], 0
        while v in grezzi and giri < 4:
            v, giri = grezzi[v], giri + 1
        try:
            val[nome] = int(v, 0)
        except ValueError:
            continue
    # ITEM_TM_TOXIC & co. non sono #define: li fabbrica una X-macro
    # (constants/tms_hms.h, FOREACH_TM/FOREACH_HM), quindi vanno contati
    # nell'ordine in cui compaiono, a partire da ITEM_TM01 / ITEM_HM01.
    tms = (DECOMP / "include/constants/tms_hms.h").read_text(encoding="utf-8", errors="replace")
    for macro, primo in (("FOREACH_TM", "ITEM_TM01"), ("FOREACH_HM", "ITEM_HM01")):
        m = re.search(r"#define\s+" + macro + r"\(F\)([^#]*)", tms)
        if not m or primo not in val:
            continue
        for i, nome in enumerate(re.findall(r"F\((\w+)\)", m.group(1))):
            val.setdefault("ITEM_" + macro.split("_")[1] + "_" + nome, val[primo] + i)
    _VALORI_ITEM = val
    return val


_VALORI_ITEM = None


def rendi_strumenti(icone, usati, outdir):
    """Scrive item/<ITEM>.png per ogni strumento che compare sulle mappe."""
    (outdir / "item").mkdir(parents=True, exist_ok=True)
    res = {}
    for const in sorted(usati):
        v = icone.get(const)
        if v is None:
            continue
        png, pal = v
        try:
            W, H, bd, _p, trns, righe = png_decode(png)
            palette = leggi_pal(pal)
        except Exception as e:   # noqa: BLE001
            print("  icona %s: %s" % (const, e))
            continue
        fn = "item/%s.png" % const.replace("ITEM_", "")
        (outdir / fn).write_bytes(png_encode_indexed(W, H, righe, palette[:16], [0]))
        res[const] = fn
    return res


def carica_front():
    """SPECIES_X -> (anim_front.png, normal.pal). L'anim_front e' 64x128: DUE
    fotogrammi impilati, ed e' l'animazione vera del gioco - quella con cui il
    Pokemon "respira" in lotta. Non sono gli sprite di Bianco/Nero 2 (che in
    nessuno dei repo locali esistono), sono quelli della cartuccia di Lain."""
    ft = (DECOMP / "src/data/pokemon_graphics/front_pic_table.h").read_text(encoding="utf-8", errors="replace")
    pt = (DECOMP / "src/data/pokemon_graphics/palette_table.h").read_text(encoding="utf-8", errors="replace")
    g1 = (DECOMP / "src/anim_mon_front_pics.c").read_text(encoding="utf-8", errors="replace")
    g2 = (DECOMP / "src/data/graphics/pokemon.h").read_text(encoding="utf-8", errors="replace")
    percorsi = dict(re.findall(r"(gMon\w+)\[\]\s*=\s*INCGFX_U32\(\"([^\"]+)\"", g1 + "\n" + g2))
    # Le due tabelle usano una macro, non l'indicizzazione:
    #   SPECIES_SPRITE(BULBASAUR, gMonFrontPic_Bulbasaur)
    #   SPECIES_PAL(BULBASAUR, gMonPalette_Bulbasaur)
    pic = {"SPECIES_" + a: b for a, b in
           re.findall(r"SPECIES_SPRITE\(\s*(\w+)\s*,\s*(gMonFrontPic_\w+)\s*\)", ft)}
    pal = {"SPECIES_" + a: b for a, b in
           re.findall(r"SPECIES_PAL\(\s*(\w+)\s*,\s*(gMonPalette_\w+)\s*\)", pt)}
    out = {}
    for const, sim in pic.items():
        a, b = percorsi.get(sim), percorsi.get(pal.get(const, ""))
        if a and b:
            out[const] = (DECOMP / a, DECOMP / b)
    return out


def rendi_front(front, usati, outdir):
    """Scrive front/<SPECIE>.png (64x128, i due fotogrammi impilati)."""
    (outdir / "front").mkdir(parents=True, exist_ok=True)
    res = {}
    for const in sorted(usati):
        v = front.get(const)
        if v is None:
            continue
        png, pal = v
        try:
            W, H, bd, _p, trns, righe = png_decode(png)
            palette = leggi_pal(pal)
        except Exception as e:   # noqa: BLE001
            print("  front %s: %s" % (const, e))
            continue
        fn = "front/%s.png" % const.replace("SPECIES_", "")
        (outdir / fn).write_bytes(png_encode_indexed(W, H, righe, palette[:16], [0]))
        res[const] = {"img": fn, "w": W, "h": H // 2, "frames": 2 if H >= 2 * W else 1}
    return res


def carica_bacche_piantate():
    """BERRY_TREE_X -> ITEM_Y_BERRY, da data/scripts/new_game.inc.

    QUESTO E' IL PEZZO CHE MANCAVA e che faceva sembrare gli alberi "sballati":
    l'informazione su quale bacca cresce dove non sta nel map.json, sta nello
    script che prepara la partita nuova. Un albero che qui non compare, nel
    gioco e' INVISIBILE (gSaveBlock1Ptr->berryTrees parte vuoto e
    SetBerryTreeGraphics nasconde lo sprite): disegnarlo era gia' un errore in
    se', prima ancora del disegno sbagliato."""
    testo = (DECOMP / "data/scripts/new_game.inc").read_text(encoding="utf-8", errors="replace")
    return {m.group(1): m.group(2) for m in re.finditer(
        r"setberrytree\s+(BERRY_TREE_\w+)\s*,\s*ITEM_TO_BERRY\((ITEM_\w+)\)", testo)}


def unisci_porte(porte):
    """Una casa ha due tile di soglia che portano allo stesso posto, e le scale
    ne hanno spesso due affiancate: senza questo si disegnerebbero due icone
    sovrapposte sullo stesso uscio. Si tiene la prima di ogni gruppo di tile
    ADIACENTI con la stessa destinazione (4-vicinato), che e' esattamente il
    criterio con cui un umano le conta guardando la mappa."""
    # LE BASI SEGRETE NON SI FONDONO MAI: due ingressi vicini che portano allo
    # stesso interno sono due basi DIVERSE (l'interno e' condiviso da dieci id
    # diversi, secret_bases.h:11), e fonderli ne farebbe sparire una dalla
    # mappa. Idem le porte di immersione, che sono gia' una per mappa.
    # I PASSAGGI INTERNI non si fondono mai: hanno tutti la stessa
    # destinazione (la mappa stessa) ma un ARRIVO diverso ciascuno, e fonderli
    # farebbe sparire 35 porte su 36 nella Palestra di Petalipoli.
    def _a_parte(w):
        return w.get("tipo") in ("base", "immersione", "riemersione") or w.get("stessa")
    a_parte = [w for w in porte if _a_parte(w)]
    porte = [w for w in porte if not _a_parte(w)]
    # `a` puo' essere None (l'uscita "dinamica" delle basi segrete): la
    # chiave di confronto usa "" al suo posto, o sorted() esplode.
    porte = sorted(porte, key=lambda w: (w["a"] or "", w["y"], w["x"]))
    tenute = list(a_parte)
    presi = set()
    for w in porte:
        vicino = False
        for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
            if (w["a"] or "", w["x"] + dx, w["y"] + dy) in presi:
                vicino = True
                break
        if vicino:
            continue
        presi.add((w["a"] or "", w["x"], w["y"]))
        tenute.append(w)
    return tenute


def dedup_strumenti(strumenti):
    """Lo STESSO oggetto emesso piu' volte a pochi tile di distanza e' quasi
    sempre UN oggetto solo: i trigger a contatto occupano una riga di tile
    (la Mega Ball di Ferruggipoli sta su 5 celle, una per corsia del
    corridoio), e i copioni condivisi la ripetono per ogni chiamante vicino.
    Si fonde per (costante, nascosto, dono) quando i punti distano al massimo
    3 tile in catena; il rappresentante e' la PRIMA voce emessa, che per
    costruzione e' quella dell'object event (l'NPC), non del trigger.
    Punti lontani restano separati: le 4 Conchiglie della Grotta Bassofondo
    sono davvero quattro raccolte diverse."""
    gruppi = {}
    ordine = []
    for s in strumenti:
        k = (s.get("c") or s.get("nome"), bool(s.get("nascosto")), bool(s.get("dono")))
        if k not in gruppi:
            gruppi[k] = []
            ordine.append(k)
        gruppi[k].append(s)
    fuori = []
    for k in ordine:
        voci = gruppi[k]
        resti = list(voci)
        while resti:
            grappolo = [resti.pop(0)]
            cresce = True
            while cresce:
                cresce = False
                for s in list(resti):
                    if any(abs(s["x"] - g["x"]) <= 3 and abs(s["y"] - g["y"]) <= 3
                           for g in grappolo):
                        grappolo.append(s)
                        resti.remove(s)
                        cresce = True
            capo = grappolo[0]
            if len(grappolo) > 1:
                capo = dict(capo)
                capo["punti"] = len(grappolo)   # "dato in N punti vicini"
            fuori.append(capo)
    return fuori


def umanizza_gfx(const):
    return camel_split("".join(p.capitalize() for p in const.replace("OBJ_EVENT_GFX_", "").split("_")))


# --------------------------------------------------------------------------
# La CODA del nome mappa in italiano. La mapsec (la testa) viene dalla ROM;
# la coda ("Brendans House - 2F") e' un'etichetta della decomp, non un testo
# di gioco: tradurla e' localizzazione dell'interfaccia. Frasi prima delle
# parole singole (l'ordine conta), piani NF -> NP e BNF -> SNP. Le parole
# sconosciute restano in inglese: meglio una coda mezza inglese che una
# tradotta a caso.
CODA_FRASI = [
    # SOLO NOMI LETTI NELLA CARTUCCIA ITALIANA (2026-08-25). Ogni voce qui
    # sotto e' stata CERCATA dentro il testo della ROM (in maiuscolo, come il
    # gioco scrive) e il numero e' quante volte compare. Quello che il gioco
    # NON nomina non si traduce: resta l'etichetta inglese della decomp -
    # meglio mezza inglese che un nome inventato.
    #
    # Cosi' sono cadute sette invenzioni della prima stesura: "Megaemporio"
    # (il gioco dice CENTRO COMMERCIALE), "Fabbrica Lotta" (AZIENDA LOTTA),
    # "Arena Lotta" (DOJO LOTTA), "Tenda Lotta" (TENDONE LOTTA), "Museo
    # Oceanico" (solo MUSEO), "Cantiere navale" (solo CANTIERE) e soprattutto
    # "Prof. Betulla", che in italiano si chiama PROF. BIRCH.
    ("Pokemon Center", "Centro Pokémon"),          # CENTRO POKéMON x13
    ("Department Store", "Centro Commerciale"),    # x7
    ("Cable Car Station", "Funivia"),              # FUNIVIA x8
    ("Cable Car", "Funivia"),
    ("Battle Tower", "Torre Lotta"),               # x64
    ("Battle Dome", "Cupola Lotta"),               # x22
    ("Battle Palace", "Palazzo Lotta"),            # x12
    ("Battle Arena", "Dojo Lotta"),                # x21
    ("Battle Factory", "Azienda Lotta"),           # x15
    ("Battle Pike", "Serpe Lotta"),                # x9
    ("Battle Pyramid", "Piramide Lotta"),          # x13
    ("Battle Tent", "Tendone Lotta"),              # x26
    ("Battle Colosseum", "Colosseo"),              # x2
    ("Space Center", "Centro Spaziale"),           # x11
    ("Fan Club", "Fan Club"),                      # x15
    ("Sterns Shipyard", "Cantiere"),               # CANTIERE x11 (del CAP. REMO)
    ("Oceanic Museum", "Museo"),                   # MUSEO x22
    ("Professor Birchs Lab", "Laboratorio del Prof. Birch"),   # PROF. BIRCH x54
    ("SS Tidal", "M/N Marea"),                     # M/N MAREA: il traghetto
]
CODA_PAROLE = {
    "House": "Casa", "Room": "Stanza", "Rooms": "Stanze", "Entrance": "Ingresso",
    "Exterior": "Esterno", "Outside": "Esterno", "Inside": "Interno",
    "Summit": "Vetta", "Top": "Cima", "Cave": "Grotta", "Lobby": "Atrio",
    "Corridor": "Corridoio", "Corridors": "Corridoi", "Hall": "Sala",
    "Hallway": "Corridoio", "Lounge": "Salotto", "Elevator": "Ascensore",
    "Harbor": "Porto", "Museum": "Museo", "Gym": "Palestra", "Mart": "Market",
    "Shop": "Negozio", "Store": "Negozio", "Tower": "Torre", "Bridge": "Ponte",
    "Basement": "Seminterrato", "Square": "Settore", "Deck": "Ponte",
    "Inner": "Interno", "Puzzle": "Enigma", "End": "Fondo", "Unused": "Inutilizzata",
    "Down": "Giù", "Up": "Su", "Underwater": "Sott'acqua", "Shipyard": "Cantiere",
    "Apartment": "Appartamento", "Flat": "Appartamento", "Beauty": "Bellezza",
    "Cool": "Classe", "Cute": "Grazia", "Smart": "Acume", "Tough": "Grinta",
    "Prototype": "Prototipo", "North": "Nord", "South": "Sud", "East": "Est",
    "West": "Ovest", "Northwest": "Nord-ovest", "Northeast": "Nord-est",
    "Southwest": "Sud-ovest", "Southeast": "Sud-est", "Tree": "Albero",
    "Shrub": "Cespuglio",
}


def traduci_coda(resto, nomi_persone=None):
    """Traduce la coda del nome mappa. `nomi_persone` mappa i nomi inglesi dei
    personaggi ai nomi italiani LETTI DALLA ROM (Brendan -> Bruno, ...): se un
    nome non c'e' nella ROM resta com'e', non si indovina."""
    for en, it_ in CODA_FRASI:
        resto = re.sub(r"\b%s\b" % re.escape(en), it_, resto)
    # "Brendans House" -> "Casa di Brendan" (col nome italiano se la ROM lo sa)
    def possessivo(m):
        nome = m.group(1)
        if nomi_persone and nome in nomi_persone:
            nome = nomi_persone[nome]
        return "Casa di " + nome
    resto = re.sub(r"\b([A-Z]\w+)s House\b", possessivo, resto)
    resto = re.sub(r"\bRoute (\d+)\b", r"Percorso \1", resto)
    resto = re.sub(r"\bB(\d+)F\b", r"S\1P", resto)
    resto = re.sub(r"\b(\d+)F\b", r"\1P", resto)
    resto = re.sub(r"\b([A-Za-z]+)\b",
                   lambda m: CODA_PAROLE.get(m.group(1), m.group(1)), resto)
    return resto


DIR_DA_MOVIMENTO = {"MOVEMENT_TYPE_FACE_UP": "su", "MOVEMENT_TYPE_FACE_LEFT": "sx",
                    "MOVEMENT_TYPE_FACE_RIGHT": "dx", "MOVEMENT_TYPE_FACE_DOWN": "giu",
                    "MOVEMENT_TYPE_FACE_DOWN_AND_UP": "giu", "MOVEMENT_TYPE_FACE_LEFT_AND_RIGHT": "sx",
                    "MOVEMENT_TYPE_FACE_UP_AND_LEFT": "su", "MOVEMENT_TYPE_FACE_UP_AND_RIGHT": "su",
                    "MOVEMENT_TYPE_FACE_DOWN_AND_LEFT": "giu", "MOVEMENT_TYPE_FACE_DOWN_AND_RIGHT": "giu"}


# --------------------------------------------------------------------------
# Mappe
# --------------------------------------------------------------------------

def main():
    solo_esterni = "--solo-esterni" in sys.argv
    t0 = time.time()
    if not DECOMP.exists():
        print("decomp non trovata:", DECOMP)
        return 1
    (OUT / "img").mkdir(parents=True, exist_ok=True)
    (OUT / "icone").mkdir(parents=True, exist_ok=True)
    (OUT / "sprite").mkdir(parents=True, exist_ok=True)

    tileset_idx = carica_tileset_index()
    tilesets = {}

    def tileset(label):
        if label not in tilesets:
            d, sec = tileset_idx[label]
            tilesets[label] = Tileset(label, d, sec)
        return tilesets[label]

    pairs = {}

    def pair(p, s):
        k = (p, s)
        if k not in pairs:
            pairs[k] = TilesetPair(tileset(p), tileset(s))
        return pairs[k]

    layouts = {l["id"]: l for l in json.loads((DECOMP / "data/layouts/layouts.json").read_text(encoding="utf-8"))["layouts"]}
    groups = json.loads((DECOMP / "data/maps/map_groups.json").read_text(encoding="utf-8"))
    tenuti_specie = carica_strumenti_tenuti()
    incontri = carica_incontri(tenuti_specie)
    # FEEBAS NON STA in wild_encounters.json: il gioco lo inietta a runtime
    # quando peschi su uno dei 6 tile sorteggiati (wild_encounter.c:113-179,
    # CheckFeebas prima della scelta della canna). Senza questa voce il
    # Percorso 119 non lo elencava fra i catturabili. Il 50% e la qualunque
    # canna sono quelli del codice; `solo6` dice alla pagina di spiegare che
    # vale solo sui 6 tile del sorteggio.
    r119 = incontri.get("MAP_ROUTE119")
    if r119:
        for met in ("pesca (amo vecchio)", "pesca (amo buono)", "pesca (super amo)"):
            if met in r119:
                r119[met]["specie"].append({"specie": "Feebas", "cost": "SPECIES_FEEBAS",
                                            "pct": 50, "lv": "20-25", "solo6": True})
    oe_cat = carica_oe_catalogo()
    allenatori = carica_allenatori()
    rematch = carica_rematch()
    # `basi` e' un INSIEME e non un contatore: lo stesso allenatore puo' stare
    # su piu' object event (le coppie tipo Amy e Liv sono due sprite), e un
    # numero gonfiato direbbe "78 su 78" facendo credere che siano agganciati
    # tutti quando ne mancano.
    rematch_conti = {"basi": set(), "squadre": 0, "nascoste": 0}
    item_ball = carica_item_ball_scripts()
    # L'indice di TUTTI gli script del gioco: serve a seguire goto/call e a
    # trovare i copioni condivisi (vedi indice_script_globale).
    script_tutti = indice_script_globale()
    dive_giu, dive_su = carica_dive_connessioni()
    dive_fissi = carica_dive_fissi()
    basi_id, basi_gruppi = carica_basi_segrete()
    layout_beh = {}          # layout -> {(x,y): behavior} solo dei tile che ci servono
    basi_grezze = {}         # mappa -> i bg_event delle basi segrete
    feebas_di = {}           # mappa -> [(x, y, n)] i candidati di Feebas
    print("script indicizzati: %d etichette" % len(script_tutti))
    icone_item = carica_icone_strumenti()
    front_cat = carica_front()
    bacche = carica_bacche_piantate()
    gfx_usati = set()
    item_usati = set()
    front_usati = set()
    print("cataloghi: %d sprite di object event, %d allenatori, %d palle con finditem"
          % (len(oe_cat), len(allenatori), len(item_ball)))
    print("           %d icone di strumenti, %d sprite anteriori, %d alberi piantati"
          % (len(icone_item), len(front_cat), len(bacche)))

    mappe = {}          # "g.n" -> voce
    by_id = {}          # MAP_X -> chiave
    warp_grezzi = {}    # chiave -> warp_events cosi' come stanno nel map.json:
                        # servono DOPO, quando by_id e' completo, sia per
                        # risolvere la destinazione sia per sapere su che tile
                        # si atterra (dest_warp_id e' un indice in QUESTA lista)
    layout_img = {}     # layout id -> (file, w, h)
    n_render = 0

    for g, gname in enumerate(groups["group_order"]):
        for n, mname in enumerate(groups[gname]):
            mpath = DECOMP / "data/maps" / mname / "map.json"
            if not mpath.exists():
                continue
            m = json.loads(mpath.read_text(encoding="utf-8"))
            # LE MAPPE CHE EREDITANO GLI EVENTI (shared_events_map): le 5
            # Contest Hall vere e le 6 inutilizzate hanno object/warp/coord/bg
            # event nella mappa madre (ContestHall) e da sole risultavano
            # VUOTE. Si copia dal genitore solo cio' che manca.
            condivisa = m.get("shared_events_map")
            if condivisa:
                cpath = DECOMP / "data/maps" / condivisa / "map.json"
                if cpath.exists():
                    cm = json.loads(cpath.read_text(encoding="utf-8"))
                    for campo in ("object_events", "warp_events",
                                  "coord_events", "bg_events"):
                        if not m.get(campo):
                            m[campo] = cm.get(campo, [])
            # I LAYOUT ALTERNATIVI (2026-08-25). Alcune mappe hanno piu' di un
            # disegno e il gioco sceglie con `setmaplayoutindex` in
            # ON_TRANSITION. Il `layout` scritto nel map.json e' solo quello
            # iniziale, e in due casi NON e' quello che si vede giocando:
            #
            #   Percorso 131 -> LAYOUT_ROUTE131_SKY_PILLAR, messo SENZA
            #     CONDIZIONI a ogni ingresso (Route131/scripts.inc:5-12):
            #     la TORRE DEI CIELI e' quindi SEMPRE visibile in partita, e
            #     disegnare l'altro layout era semplicemente sbagliato
            #     (segnalato da Lain: "sei sicuro che non si veda?" - no,
            #     si vede, ed e' li' nella sua posizione vera);
            #   Percorso 130 -> LAYOUT_ROUTE130_MIRAGE_ISLAND, condizionato
            #     (l'isola appare solo nei giorni giusti): si disegna lo
            #     stesso perche' un'isola mai vista non serve, e la scheda
            #     spiega la condizione.
            #
            # NON si tocca il resto: Percorso 111 senza Torre Miraggio
            # (dopo il crollo), piani della Torre "puliti" (prima
            # dell'evento), Ceneride durante la lotta dei leggendari e il
            # laboratorio col tavolo sono stati TEMPORANEI o successivi:
            # il disegno normale e' quello del map.json.
            LAYOUT_VERO = {"MAP_ROUTE131": "LAYOUT_ROUTE131_SKY_PILLAR",
                           "MAP_ROUTE130": "LAYOUT_ROUTE130_MIRAGE_ISLAND"}
            if m["id"] in LAYOUT_VERO:
                m["layout"] = LAYOUT_VERO[m["id"]]
            lay = layouts.get(m["layout"])
            if lay is None:
                continue
            esterno = m["map_type"] in ("MAP_TYPE_TOWN", "MAP_TYPE_CITY", "MAP_TYPE_ROUTE",
                                        "MAP_TYPE_OCEAN_ROUTE", "MAP_TYPE_UNDERWATER")
            if solo_esterni and not esterno:
                continue
            key = "%d.%d" % (g, n)
            # --- render del layout (una volta per layout) ---
            if m["layout"] not in layout_img:
                w, h = lay["width"], lay["height"]
                blocks = (DECOMP / lay["blockdata_filepath"]).read_bytes()
                ids = struct.unpack("<%dH" % (len(blocks) // 2), blocks)
                tp = pair(lay["primary_tileset"], lay["secondary_tileset"])
                righe = []
                for by in range(h):
                    blk_row = [tp.metatile(ids[by * w + bx] & 0x3FF) for bx in range(w)]
                    for py in range(16):
                        righe.append(b"".join(b[py] for b in blk_row))
                fname = "img/%s.png" % lay["name"].replace("_Layout", "")
                (OUT / fname).write_bytes(png_encode_indexed(w * 16, h * 16, righe, tp.palette))
                layout_img[m["layout"]] = (fname, w, h)
                # QUELLO CHE STA NEL TERRENO. Immersione e basi segrete non hanno
                # nessun marcatore fra gli eventi: il gioco guarda il metatile
                # behavior del tile davanti al giocatore, e qui si fa lo stesso.
                # Si tengono SOLO i behavior che interessano: tenerli tutti
                # vorrebbe dire un dizionario da centinaia di migliaia di voci
                # per niente.
                beh = {}
                for bx, by, b in behavior_griglia(tp, blocks, w, h):
                    if b in MB_DIVE or MB_BASE_MIN <= b <= MB_BASE_MAX:
                        beh[(bx, by)] = b
                layout_beh[m["layout"]] = beh
                if lay["name"].startswith("Route119"):
                    feebas_di["_layout_" + m["layout"]] = feebas_spot(tp, blocks, w, h)
                n_render += 1
                if n_render % 25 == 0:
                    print("  %d layout renderizzati (%.0f s)" % (n_render, time.time() - t0), flush=True)
            fname, w, h = layout_img[m["layout"]]
            # --- strumenti e personaggi ---
            strumenti = []
            personaggi = []
            spath = DECOMP / "data/maps" / mname / "scripts.inc"
            blocchi = blocchi_script(spath.read_text(encoding="utf-8", errors="replace")) if spath.exists() else {}
            for o in m.get("object_events", []):
                gid = o.get("graphics_id", "")
                script = o.get("script") or ""
                # il ripiego sull'indice globale serve alle mappe con
                # shared_scripts_map (Battle Pyramid ecc.): il loro
                # scripts.inc rimanda a quello di un'altra mappa.
                corpo = blocchi.get(script) or script_tutti.get(script, "")
                # Seguendo goto/call su TUTTI gli script, non solo il corpo
                # immediato: e' cosi' che si ritrovano la MT24 di Wattson
                # (dietro un goto_if_set) e il Portapokemelle (in un copione
                # condiviso). Vedi doni_da_script.
                doni_c = doni_da_script(script, script_tutti) if script else []
                doni = [nome_item(i) for i in doni_c]
                if gid == "OBJ_EVENT_GFX_ITEM_BALL":
                    cost = item_ball.get(script)
                    if cost:
                        nome = nome_item(cost)
                    elif "Electrode" in script or "Voltorb" in script:
                        nome = "Pokemon travestito (%s)" % re.sub(r"\d+$", "", script.split("_")[-1])
                    elif "BattlePyramid" in script:
                        nome = "Strumento (casuale della Piramide)"
                    else:
                        nome = nome_item_da_script(script) or "Strumento"
                    voce_s = {"x": o["x"], "y": o["y"], "nome": nome, "nascosto": False}
                    if cost:
                        voce_s["c"] = cost
                        item_usati.add(cost)
                    strumenti.append(voce_s)
                    gfx_usati.add(gid)
                    continue
                if gid.startswith("OBJ_EVENT_GFX_VAR_"):
                    # Grafica decisa a runtime da una variabile. Gli slot
                    # SENZA script sono i mobili del giocatore nelle basi
                    # segrete: quelli nel gioco non esistono finche' non li
                    # piazzi, e restano invisibili. Chi invece ha uno script
                    # e' una persona vera (l'ospite del Record Mix nelle
                    # basi, gli inquilini del Battle Pike): prima venivano
                    # scartati tutti e le mappe risultavano VUOTE.
                    if not script or script == "0x0":
                        continue
                    gid = "OBJ_EVENT_GFX_LITTLE_BOY"   # segnaposto: l'aspetto vero si decide in partita
                tr = re.search(r"trainerbattle\w*\s+(TRAINER_\w+)", corpo)
                tc = tr.group(1) if tr else None
                if tc is None and script:
                    # Il trainerbattle dietro un salto (Norman): vedi
                    # trainer_da_script. Solo come ripiego, e solo se a valle
                    # c'e' un allenatore soltanto.
                    tc = trainer_da_script(script, script_tutti)
                pers = {"x": o["x"], "y": o["y"], "gfx": gid,
                        "dir": DIR_DA_MOVIMENTO.get(o.get("movement_type", ""), "giu")}
                if tc and tc in allenatori:
                    a = allenatori[tc]
                    pers["tipo"] = "allenatore"
                    pers["nome"] = a["classe"] + " " + a["nome"]
                    pers["squadra"] = a["squadra"]
                    # le costanti servono per i nomi italiani (nomi_it.py) e
                    # per gli sprite della squadra
                    pers["tc"] = tc
                    pers["cc"] = a.get("classe_c", "")
                    if a.get("soldi"):
                        pers["soldi"] = a["soldi"]
                    if a.get("doppia"):
                        pers["doppia"] = True
                    for mon in a["squadra"]:
                        front_usati.add(mon["cost"])
                        if mon.get("tiene"):
                            item_usati.add(mon["tiene"])
                    # LE RIVINCITE. Quando l'allenatore ti richiama col
                    # PokeNav non rimette in campo la stessa squadra: ne ha
                    # altre quattro, piu' forti. Stanno gia' in gTrainers -
                    # qui si aggiungono alla voce, saltando le ripetizioni
                    # (Wally VR ha il _5 due volte; la Superquattro ripete lo
                    # stesso trainer cinque volte, e li' non c'e' niente di
                    # nuovo da mostrare).
                    rem = []
                    vista = [json.dumps(a["squadra"], sort_keys=True)]
                    for tcr in rematch.get(tc, []):
                        ar = allenatori.get(tcr)
                        if not ar or not ar["squadra"]:
                            continue
                        impronta = json.dumps(ar["squadra"], sort_keys=True)
                        if impronta in vista:
                            rematch_conti["nascoste"] += 1
                            continue
                        vista.append(impronta)
                        voce = {"tc": tcr, "squadra": ar["squadra"]}
                        if ar.get("soldi"):
                            voce["soldi"] = ar["soldi"]
                        rem.append(voce)
                        for mon in ar["squadra"]:
                            front_usati.add(mon["cost"])
                            if mon.get("tiene"):
                                item_usati.add(mon["tiene"])
                    if rem:
                        pers["rematch"] = rem
                        rematch_conti["basi"].add(tc)
                        rematch_conti["squadre"] += len(rem)
                elif gid == "OBJ_EVENT_GFX_BERRY_TREE":
                    tree_id = o.get("trainer_sight_or_berry_tree_id") or ""
                    bacca = bacche.get(tree_id)
                    if not bacca:
                        continue      # nel gioco e' invisibile: non si disegna
                    pers["tipo"] = "albero"
                    pers["nome"] = "Albero di " + nome_item(bacca)
                    pers["gfx"] = "BERRYTREE_" + bacca.replace("ITEM_", "")
                    pers["c"] = bacca
                    item_usati.add(bacca)
                    gid = pers["gfx"]
                elif gid in ("OBJ_EVENT_GFX_PUSHABLE_BOULDER", "OBJ_EVENT_GFX_BREAKABLE_ROCK", "OBJ_EVENT_GFX_CUTTABLE_TREE"):
                    pers["tipo"] = "oggetto"
                    pers["nome"] = umanizza_gfx(gid)
                else:
                    pers["tipo"] = "npc"
                    pers["nome"] = umanizza_gfx(gid)
                if doni:
                    pers["doni"] = doni
                    pers["doni_c"] = doni_c   # le costanti: servono ai nomi italiani
                    for dn, dc in zip(doni, doni_c):
                        strumenti.append({"x": o["x"], "y": o["y"], "nome": dn,
                                          "nascosto": False, "dono": True, "c": dc})
                        item_usati.add(dc)
                fl = o.get("flag") or "0"
                if fl not in ("0", "FLAG_TEMP_1") and fl.startswith("FLAG_"):
                    pers["flag"] = fl
                personaggi.append(pers)
                gfx_usati.add(gid)
            basi_grezze[key] = [b for b in m.get("bg_events", [])
                                if b.get("type") == "secret_base"]
            for b in m.get("bg_events", []):
                if b.get("type") == "hidden_item":
                    strumenti.append({"x": b["x"], "y": b["y"], "nome": nome_item(b["item"]),
                                      "nascosto": True, "c": b["item"]})
                    item_usati.add(b["item"])
                elif b.get("script"):
                    # I CARTELLI E GLI USCI danno oggetti piu' spesso di quanto
                    # sembri (533 bg_event di tipo `sign` in tutto il gioco).
                    for dc in doni_da_script(b["script"], script_tutti):
                        strumenti.append({"x": b["x"], "y": b["y"], "nome": nome_item(dc),
                                          "nascosto": False, "dono": True, "c": dc})
                        item_usati.add(dc)
            # GLI EVENTI A CONTATTO: si attivano camminandoci sopra, non
            # parlando a nessuno - e alcuni consegnano oggetti (289 in tutto).
            for c in m.get("coord_events", []):
                if not c.get("script"):
                    continue
                for dc in doni_da_script(c["script"], script_tutti):
                    strumenti.append({"x": c["x"], "y": c["y"], "nome": nome_item(dc),
                                      "nascosto": False, "dono": True, "c": dc})
                    item_usati.add(dc)
            voce = {
                "id": m["id"], "nome": nome_mappa(m["name"], m.get("region_map_section")),
                "mapsec": m.get("region_map_section") or "",
                "gruppo": g, "numero": n, "w": w, "h": h, "img": fname,
                "tipo": m["map_type"].replace("MAP_TYPE_", "").lower(),
                "esterno": esterno,
                "connessioni": [c for c in (m.get("connections") or [])
                                if c["direction"] in ("up", "down", "left", "right")],
                "strumenti": dedup_strumenti(strumenti),
                "personaggi": personaggi,
                "selvatici": incontri.get(m["id"], {}),
                # I TILE DI FEEBAS (solo Percorso 119). Quali siano i SEI veri
                # dipende dal salvataggio - il gioco li sorteggia con un
                # generatore semato dalla frase di tendenza di Ceneride
                # (wild_encounter.c:113-179) - quindi da fuori si puo' dire
                # solo l'insieme dei CANDIDATI, che e' comunque l'informazione
                # utile: fuori da questi tile Feebas non esce mai.
                "feebas": [[x, y] for x, y, _n in feebas_di.get("_layout_" + m["layout"], [])],
                "miraggio": m["id"] == "MAP_ROUTE130",
                # ausiliari, tolti prima di scrivere il JSON: servono solo
                # alle porte di immersione e alle basi segrete
                "_layout": m["layout"], "_dir": mname,
            }
            mappe[key] = voce
            by_id[m["id"]] = key
            warp_grezzi[key] = m.get("warp_events", []) or []

    print("mappe: %d, layout renderizzati: %d (%.0f s)" % (len(mappe), n_render, time.time() - t0))

    # --- LE PORTE ------------------------------------------------------------
    # Un warp del gioco e' gia' tutto quello che serve per navigare: il tile su
    # cui sta la porta, la mappa di destinazione, e - via dest_warp_id, che e'
    # un indice nella lista dei warp DELLA DESTINAZIONE - il tile su cui si
    # atterra. Quindi la pagina puo' aprire l'interno E centrarlo dove si
    # arriva davvero, invece di lasciare l'utente a cercare la porta a mano.
    #
    # Tre tipi, ed e' la distinzione che serve a chi guarda:
    #   entra    da un esterno a un interno (casa, grotta, palestra)
    #   esce     da un interno a un esterno  -> l'uscita
    #   interno  fra due interni (piani, stanze, cunicoli di una grotta)
    #   altrove  fra due esterni che il disegno non cuce (Petalburg Woods,
    #            Jagged Pass, i fondali): non e' ne' entrare ne' uscire, e
    #            chiamarlo "interno" sarebbe una bugia a chi legge
    # MAP_NONE / MAP_DYNAMIC e i warp verso se stessi non sono navigazione e
    # si scartano: by_id non li conosce.
    n_porte = 0
    for key, voce in mappe.items():
        porte = []
        for w in warp_grezzi.get(key, []):
            dm = w.get("dest_map") or ""
            if dm == "MAP_DYNAMIC" and not voce["esterno"]:
                # L'uscita delle basi segrete (e delle stanze del Battle
                # Pike): il gioco torna "da dove sei entrato", che da qui
                # non si puo' sapere. La porta si disegna lo stesso -
                # senza destinazione la pagina non la fa navigare, ma
                # almeno l'uscita non sparisce dalla mappa.
                porte.append({"x": w["x"], "y": w["y"], "a": None,
                              "nome": "", "tipo": "esce",
                              "ax": None, "ay": None, "dinamica": True})
                continue
            kk = by_id.get(dm)
            if kk is None:
                continue
            # I PASSAGGI INTERNI (2026-08-25, segnalati da Lain con ragione).
            # 91 warp del gioco portano alla MAPPA STESSA su un altro tile, e
            # li scartavo tutti: sono i teletrasporti veri del gioco, non
            # rumore. Con essi sparivano la CAMERA DEI REGI (Rovine Sabbiose,
            # Grotta Insulare, Tomba Antica: si entra nella grotta ma la
            # stanza del Regi era irraggiungibile), le porte della Palestra di
            # Petalipoli (36), le piastre del Rifugio Idro (27), quelle della
            # Palestra di Ripoli (12) e l'Enigma 7 della Casa degli Scherzi.
            # Restano porte a tutti gli effetti, solo che la destinazione e'
            # un punto di QUESTA mappa: `stessa` lo dice alla pagina.
            interno_stesso = (kk == key)
            dw = mappe[kk]
            try:
                idx = int(str(w.get("dest_warp_id", "0")), 0)
            except ValueError:
                idx = -1
            arrivo = warp_grezzi.get(kk) or []
            ax = ay = None
            if 0 <= idx < len(arrivo):
                ax, ay = arrivo[idx]["x"], arrivo[idx]["y"]
            if voce["esterno"] and not dw["esterno"]:
                tipo = "entra"
            elif not voce["esterno"] and dw["esterno"]:
                tipo = "esce"
            elif voce["esterno"]:
                tipo = "altrove"
            else:
                tipo = "interno"
            voce_p = {"x": w["x"], "y": w["y"], "a": kk, "nome": dw["nome"],
                      "tipo": tipo, "ax": ax, "ay": ay}
            if interno_stesso:
                # senza il tile d'arrivo un passaggio interno non serve a
                # niente (non si saprebbe dove porta): si scarta solo quello
                if ax is None or (ax == w["x"] and ay == w["y"]):
                    continue
                voce_p["tipo"] = "interno"
                voce_p["stessa"] = True
            porte.append(voce_p)
        # --- IMMERSIONE (2026-08-24) ------------------------------------
        # Due modi, e vanno tenuti distinti perche' si comportano diversamente:
        #   - connessione `dive`: conserva x,y, quindi si riemerge dove ti sei
        #     immerso. La porta si mette sul primo tile di acqua profonda;
        #   - `setdivewarp`: coordinate FISSE decise da uno script (i fondali
        #     delle grotte, la Camera Sigillata, la Nave Abbandonata).
        # In tutti e due i casi ci si immerge su QUALUNQUE tile di acqua
        # profonda (behavior 0x11/0x12/0x14), non su un punto segnato:
        # metatile_behavior.c:853. Quindi la porta e' un rappresentante, e
        # `tile` dice quanti tile sono davvero immergibili.
        mid = voce.get("_layout")
        prof = sorted(k for k, b in (layout_beh.get(mid) or {}).items() if b in MB_DIVE)
        sott = voce["id"].startswith("MAP_UNDERWATER") or "UNDERWATER" in voce["id"]
        dest = dive_giu.get(voce["id"]) or dive_su.get(voce["id"])
        fissi = dive_fissi.get(voce.get("_dir") or "") or []
        fisso = fissi[0] if fissi else None
        if dest is None and fisso:
            dest = fisso[0]
        kk = by_id.get(dest or "")
        if kk is not None and kk != key:
            dw = mappe[kk]
            if prof:
                px, py = prof[len(prof) // 2]
            elif fisso:
                px, py = fisso[1], fisso[2]
            else:
                px, py = voce["w"] // 2, voce["h"] // 2
            porte.append({"x": px, "y": py, "a": kk, "nome": dw["nome"],
                          "tipo": "riemersione" if sott else "immersione",
                          "ax": (fisso[1] if fisso else px), "ay": (fisso[2] if fisso else py),
                          "tile": len(prof)})
            # TUTTI i tile immergibili, non solo il rappresentante: la
            # pagina li colora e li rende cliccabili, perche' nel gioco ci
            # si immerge (o si riemerge) da qualunque tile di acqua
            # profonda, non da un punto solo.
            if prof:
                voce["sub"] = [[px2, py2] for px2, py2 in prof]
        # Le destinazioni OLTRE la prima (Underwater_SealedChamber ne ha due:
        # lo script sceglie in base al tile). Il tile di partenza qui si
        # ricava dal setdivewarp INVERSO della destinazione: un'immersione e'
        # verticale, quindi il punto in cui LEI manda da noi e' lo stesso da
        # cui NOI andiamo da lei.
        for fmap, ffx, ffy in fissi[1:]:
            if fmap == dest:
                continue
            kk3 = by_id.get(fmap)
            if kk3 is None or kk3 == key:
                continue
            dw3 = mappe[kk3]
            px3, py3 = None, None
            for imap, ix, iy in dive_fissi.get(dw3.get("_dir") or "") or []:
                if imap == voce["id"]:
                    px3, py3 = ix, iy
                    break
            if px3 is None:
                px3, py3 = ffx, ffy   # meglio del centro mappa: e' il punto gemello
            porte.append({"x": px3, "y": py3, "a": kk3, "nome": dw3["nome"],
                          "tipo": "riemersione" if sott else "immersione",
                          "ax": ffx, "ay": ffy, "tile": 0})

        # --- BASI SEGRETE ----------------------------------------------------
        # L'ingresso e' un bg_event; l'INTERNO lo decide `id // 10`
        # (secret_bases.h:11). Il tipo che si vede fuori (albero, cespuglio,
        # roccia) va letto dal TERRENO e non dal nome dell'id: in 9 casi su 75
        # i due non coincidono, quindi il nome mentirebbe sul disegno.
        for b in basi_grezze.get(key, []):
            idn = basi_id.get(b["secret_base_id"])
            if idn is None:
                continue
            gidx = idn // 10
            if gidx >= len(basi_gruppi):
                continue
            kk2 = by_id.get(basi_gruppi[gidx])
            if kk2 is None:
                continue
            bh = (layout_beh.get(mid) or {}).get((b["x"], b["y"]))
            porte.append({"x": b["x"], "y": b["y"], "a": kk2,
                          "nome": mappe[kk2]["nome"], "tipo": "base",
                          "ax": None, "ay": None,
                          "base": BASE_TIPI.get(bh, "ingresso"),
                          "bid": b["secret_base_id"]})

        voce["porte"] = unisci_porte(porte)
        n_porte += len(voce["porte"])
    print("porte: %d navigabili (%d mappe con almeno una)"
          % (n_porte, sum(1 for v in mappe.values() if v["porte"])))

    # --- i mondi: componenti connesse, offset in tile ---
    mondi = {}
    visitate = set()
    for key in mappe:
        if key in visitate:
            continue
        comp = {key: (0, 0)}
        coda = [key]
        visitate.add(key)
        while coda:
            k = coda.pop(0)
            mk = mappe[k]
            x0, y0 = comp[k]
            for c in mk["connessioni"]:
                kk = by_id.get(c["map"])
                if kk is None or kk in comp:
                    continue
                mb = mappe[kk]
                off = c["offset"]
                if c["direction"] == "up":
                    pos = (x0 + off, y0 - mb["h"])
                elif c["direction"] == "down":
                    pos = (x0 + off, y0 + mk["h"])
                elif c["direction"] == "left":
                    pos = (x0 - mb["w"], y0 + off)
                else:
                    pos = (x0 + mk["w"], y0 + off)
                comp[kk] = pos
                visitate.add(kk)
                coda.append(kk)
        if len(comp) == 1:
            continue
        minx = min(p[0] for p in comp.values())
        miny = min(p[1] for p in comp.values())
        maxx = max(p[0] + mappe[k]["w"] for k, p in comp.items())
        maxy = max(p[1] + mappe[k]["h"] for k, p in comp.items())
        if "MAP_LITTLEROOT_TOWN" in [mappe[k]["id"] for k in comp]:
            wid = "hoenn"
            wname = "Hoenn"
        else:
            primo = min(comp, key=lambda k: (mappe[k]["gruppo"], mappe[k]["numero"]))
            wid = mappe[primo]["id"].replace("MAP_", "").lower()
            wname = mappe[primo]["nome"] + " (and surroundings)"
        for k, p in comp.items():
            mappe[k]["mondo"] = wid
            mappe[k]["ox"] = p[0] - minx
            mappe[k]["oy"] = p[1] - miny
        mondi[wid] = {"nome": wname, "w": maxx - minx, "h": maxy - miny,
                      "mappe": sorted(comp.keys(), key=lambda k: (mappe[k]["gruppo"], mappe[k]["numero"]))}
    # COERENZA: ogni connessione dentro un mondo deve essere rispettata dalle
    # posizioni calcolate (il BFS ne usa una per mappa, qui si controllano
    # TUTTE). Una discrepanza = due route che il gioco dichiara adiacenti
    # ma che il disegno non cuce: va gridata, non disegnata storta.
    storte = 0
    for key, mk in mappe.items():
        if "mondo" not in mk:
            continue
        for c in mk["connessioni"]:
            kk = by_id.get(c["map"])
            if kk is None or mappe[kk].get("mondo") != mk["mondo"]:
                continue
            mb = mappe[kk]
            off = c["offset"]
            att = {"up": (mk["ox"] + off, mk["oy"] - mb["h"]),
                   "down": (mk["ox"] + off, mk["oy"] + mk["h"]),
                   "left": (mk["ox"] - mb["w"], mk["oy"] + off),
                   "right": (mk["ox"] + mk["w"], mk["oy"] + off)}[c["direction"]]
            if att != (mb["ox"], mb["oy"]):
                storte += 1
                print("  connessione asimmetrica nei dati del gioco: %s -%s-> %s: "
                      "dice %s, messa a %s (scarto %d tile)"
                      % (mk["nome"], c["direction"], mb["nome"], att, (mb["ox"], mb["oy"]),
                         abs(att[0] - mb["ox"]) + abs(att[1] - mb["oy"])))
    # Nei dati di Smeraldo 8 connessioni sono asimmetriche di 2 tile (Mauville
    # <-> Route 117, Route 108 <-> 109, Route 111 <-> 112/113): il gioco usa la
    # connessione della mappa in cui ti trovi, quindi il mondo "vero" non e'
    # cucibile in modo esatto. Si tiene la prima incontrata dal BFS: lo scarto
    # e' di 2 tile su una mappa di 800.
    print("connessioni verificate: %s" % ("tutte coerenti" if storte == 0
                                           else "%d asimmetriche (note, scarto di 2 tile)" % storte))
    # --- LE PORTE RECIPROCHE (2026-08-25, "una volta per tutte") ------------
    #
    # Censimento: 72 mappe non avevano NESSUNA porta in ingresso. Tre cause,
    # tre cure, e in fondo una GUARDIA che conta le irraggiungibili a ogni
    # build, cosi' il problema non puo' tornare in silenzio.
    #
    # Cura 1: se A ha una porta verso B col tile d'arrivo noto, B deve avere
    # la porta inversa. Sistema da solo gli Enigmi della Casa degli Scherzi,
    # il market inutilizzato di Alghepoli, eccetera.
    def _tipo_inverso(da, a):
        if da["esterno"] and not a["esterno"]:
            return "esce"
        if not da["esterno"] and a["esterno"]:
            return "entra"
        return "altrove" if da["esterno"] else "interno"
    n_rec = 0
    for key, voce in list(mappe.items()):
        for w in list(voce["porte"]):
            kk = w.get("a")
            if not kk or kk not in mappe or w.get("tipo") in ("base", "immersione", "riemersione"):
                continue
            dest = mappe[kk]
            if any(p.get("a") == key for p in dest["porte"]):
                continue
            px = w["ax"] if w.get("ax") is not None else dest["w"] // 2
            py = w["ay"] if w.get("ay") is not None else dest["h"] // 2
            dest["porte"].append({"x": px, "y": py, "a": key, "nome": voce["nome"],
                                  "tipo": _tipo_inverso(mappe[kk], voce),
                                  "ax": w["x"], "ay": w["y"]})
            n_rec += 1

    # Cura 2: le mappe in cui si entra SOLO per copione (il gioco ti
    # teletrasporta: sfide del Parco Lotta, Sale Gare, salette del Cable
    # Club, il camion dell'inizio, l'alta marea della Grotta Ondosa...).
    # Nel loro map.json non c'e' nessun warp da reciprocare: si aggiunge una
    # coppia di porte "evento" fra la mappa e il suo OSPITE naturale - il
    # posto da cui, giocando, ci si arriva davvero. La porta e' marcata
    # `evento: True` e la pagina spiega che non e' un uscio fisico.
    OSPITI = [
        (r"^MAP_(\w+)_BATTLE_TENT_(CORRIDOR|BATTLE_ROOM)$", lambda m: "MAP_%s_BATTLE_TENT_LOBBY" % m.group(1)),
        (r"^MAP_BATTLE_FRONTIER_BATTLE_(TOWER|DOME|ARENA|FACTORY|PIKE|PALACE|PYRAMID)_(?!LOBBY)\w+$",
         lambda m: "MAP_BATTLE_FRONTIER_BATTLE_%s_LOBBY" % m.group(1)),
        (r"^MAP_BATTLE_PYRAMID_SQUARE\d+$", lambda m: "MAP_BATTLE_FRONTIER_BATTLE_PYRAMID_LOBBY"),
        (r"^MAP_(CONTEST_HALL\w*|UNUSED_CONTEST_HALL\d)$", lambda m: "MAP_LILYCOVE_CITY_CONTEST_LOBBY"),
        (r"^MAP_(BATTLE_COLOSSEUM_2P|BATTLE_COLOSSEUM_4P|RECORD_CORNER|TRADE_CENTER|UNION_ROOM)$",
         lambda m: "MAP_OLDALE_TOWN_POKEMON_CENTER_2F"),
        (r"^MAP_INSIDE_OF_TRUCK$", lambda m: "MAP_LITTLEROOT_TOWN"),
        (r"^MAP_SS_TIDAL_CORRIDOR$", lambda m: "MAP_SLATEPORT_CITY_HARBOR"),
        (r"^MAP_AQUA_HIDEOUT_UNUSED_RUBY_MAP\d$", lambda m: "MAP_AQUA_HIDEOUT_1F"),
        (r"^MAP_SHOAL_CAVE_HIGH_TIDE_(\w+)$", lambda m: "MAP_SHOAL_CAVE_LOW_TIDE_%s" % m.group(1)),
        (r"^MAP_ROUTE104_PROTOTYPE$", lambda m: "MAP_ROUTE104"),
        (r"^MAP_ROUTE104_PROTOTYPE_PRETTY_PETAL_FLOWER_SHOP$", lambda m: "MAP_ROUTE104_PROTOTYPE"),
    ]
    n_evento = 0
    for _giro in range(3):     # a catena: il negozio del prototipo pende dal prototipo
        # il criterio e' RAGGIUNGIBILE DAI MONDI, non "ha un ingresso": le
        # tre stanze della M/N Marea si puntano a vicenda (ciclo chiuso) e
        # con il vecchio criterio sembravano a posto pur essendo un'isola.
        con_ingresso = set(k for k in mappe if mappe[k].get("mondo"))
        coda_r = list(con_ingresso)
        while coda_r:
            cur = coda_r.pop()
            for w in mappe[cur]["porte"]:
                kk2 = w.get("a")
                if kk2 and kk2 in mappe and kk2 not in con_ingresso:
                    con_ingresso.add(kk2)
                    coda_r.append(kk2)
        for key, voce in list(mappe.items()):
            if key in con_ingresso or voce.get("mondo"):
                continue
            ospite = None
            for rx, fn in OSPITI:
                m2 = re.match(rx, voce["id"])
                if m2:
                    ospite = by_id.get(fn(m2))
                    break
            if ospite is None or ospite == key:
                continue
            osp = mappe[ospite]
            # tutte le porte-evento di un ospite stanno sullo STESSO tile
            # (in alto a sinistra): il clic apre il menu di scelta
            osp["porte"].append({"x": 2, "y": 2, "a": key, "nome": voce["nome"],
                                 "tipo": "altrove", "evento": True,
                                 "ax": voce["w"] // 2, "ay": voce["h"] // 2})
            if not any(p.get("a") == ospite for p in voce["porte"]):
                voce["porte"].append({"x": 2, "y": 2, "a": ospite, "nome": osp["nome"],
                                      "tipo": "altrove", "evento": True,
                                      "ax": 2, "ay": 2})
            n_evento += 1
    print("porte aggiunte: %d reciproche, %d di evento" % (n_rec, n_evento))

    # --- I SATELLITI (riscritti il 2026-08-25 su riscontro di Lain) ----------
    #
    # SOLO i posti che il GIOCO mette sulla sua mappa pur non cucendoli alle
    # route: la Torre dei Cieli e le isole (Parco Lotta, Isola Remota, e le
    # tre isole evento). La posizione viene dalla CASELLA che il gioco gli
    # assegna in gRegionMapEntries - un dato della cartuccia, non una scelta
    # nostra - convertita in tile con la scala dell'ancora piu' vicina, e
    # spostata del minimo se il posto e' occupato. Ceneride, il Monte Pira e
    # gli altri NON sono piu' satelliti: si visitano dalle loro porte, come
    # prima (la geografia del gioco per loro non esiste, e inventarla era
    # sbagliato).
    rme = {}
    try:
        _ord, _seq = __import__("nomi_it").impronta_regionmap()
        rme = {n: v for n, v in zip(_ord, _seq) if v}
    except Exception as e:   # noqa: BLE001
        print("region map non disponibile (%s): niente satelliti" % e)

    def _sovrappone(ax, ay, aw, ah, bx, by, bw, bh):
        return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah

    def _occupati():
        return [(mk["ox"], mk["oy"], mk["w"], mk["h"])
                for mk in mappe.values() if mk.get("mondo") == "hoenn"]

    def _cerca_posto(px, py, w0, h0, MARGINE=2):
        occ = _occupati()
        for raggio in range(0, 60):
            for dx, dy in ((0, 0), (0, -1), (0, 1), (-1, 0), (1, 0),
                           (-1, -1), (1, -1), (-1, 1), (1, 1)):
                qx, qy = px + dx * raggio * 4, py + dy * raggio * 4
                if not any(_sovrappone(qx - MARGINE, qy - MARGINE,
                                       w0 + 2 * MARGINE, h0 + 2 * MARGINE, *r)
                           for r in occ):
                    return qx, qy
        return None

    def _tile_da_cella(cx, cy):
        """cella della mappa di gioco -> tile del mondo, con la scala
        dell'ancora (mappa di hoenn con mapsec) piu' vicina per cella."""
        meglio = None
        for mk in mappe.values():
            if mk.get("mondo") != "hoenn" or mk.get("satellite"):
                continue
            sec = rme.get(mk.get("mapsec"))
            if not sec or sec[2] < 1 or sec[3] < 1:
                continue
            d = abs(sec[0] - cx) + abs(sec[1] - cy)
            if meglio is None or d < meglio[0]:
                meglio = (d, mk, sec)
        if meglio is None:
            return None
        _, mk, sec = meglio
        tpx = max(16, min(40, mk["w"] // sec[2]))
        tpy = max(16, min(40, mk["h"] // sec[3]))
        return (mk["ox"] + (cx - sec[0]) * tpx, mk["oy"] + (cy - sec[1]) * tpy)

    def _porta_verso(id_da, id_a):
        """il tile (assoluto in hoenn) della porta di id_da verso id_a"""
        kda = by_id.get(id_da)
        if kda is None or mappe[kda].get("mondo") != "hoenn":
            return None
        for w in mappe[kda]["porte"]:
            if w.get("a") == by_id.get(id_a):
                return (mappe[kda]["ox"] + w["x"], mappe[kda]["oy"] + w["y"])
        return None

    def _aggancia(id_mappa, pos, ancora, ancora_id=None):
        k = by_id.get(id_mappa)
        if k is None or pos is None:
            return False
        mk = mappe[k]
        posto = _cerca_posto(pos[0] - mk["w"] // 2, pos[1] - mk["h"] // 2, mk["w"], mk["h"])
        if posto is None:
            return False
        mk["mondo"] = "hoenn"
        mk["ox"], mk["oy"] = posto
        mk["satellite"] = list(ancora) if ancora else [posto[0] + mk["w"] // 2, posto[1] + mk["h"] // 2]
        if ancora_id:
            mk["satellite_da"] = ancora_id      # per scrivere "da: <mappa>"
        mondi["hoenn"]["mappe"].append(k)
        return True

    n_sat = 0
    # il porto di Alghepoli: da li' partono i traghetti per tutte le isole
    porto = _porta_verso("MAP_LILYCOVE_CITY", "MAP_LILYCOVE_CITY_HARBOR")

    # 1. LA TORRE DEI CIELI non e' piu' un satellite: adesso il Percorso 131
    #    si disegna col layout che il gioco usa davvero, e la Torre e' LI',
    #    nel mare a nord-est, alla sua posizione vera. Le sue stanze
    #    (Ingresso, Esterno, i piani, la Cima) si visitano dalla porta,
    #    come ogni altro interno: non serve appiccicarle alla mappa.

    # 2. IL PARCO LOTTA: il suo mondo intero (Ovest+Est) entra in hoenn alla
    #    cella (22,12), a sud-est, con la linea dal porto del traghetto.
    sec = rme.get("MAPSEC_BATTLE_FRONTIER")
    wid_bf = None
    for wid, w in mondi.items():
        if any(mappe[k]["id"] == "MAP_BATTLE_FRONTIER_OUTSIDE_WEST" for k in w["mappe"]):
            wid_bf = wid
    if sec and wid_bf:
        pos = _tile_da_cella(sec[0], sec[1])
        if pos:
            bf = [k for k in mondi[wid_bf]["mappe"]]
            bw = mondi[wid_bf]["w"]
            bh = mondi[wid_bf]["h"]
            posto = _cerca_posto(pos[0] - bw // 2, pos[1], bw, bh)
            if posto:
                for k in bf:
                    mk = mappe[k]
                    mk["mondo"] = "hoenn"
                    mk["ox"] += posto[0]
                    mk["oy"] += posto[1]
                    mondi["hoenn"]["mappe"].append(k)
                kw = by_id["MAP_BATTLE_FRONTIER_OUTSIDE_WEST"]
                mappe[kw]["satellite"] = list(porto) if porto else [posto[0], posto[1]]
                if porto:
                    mappe[kw]["satellite_da"] = by_id.get("MAP_LILYCOVE_CITY")
                # UN SOLO RIQUADRO per tutto il Parco Lotta: Ovest ed Est sono
                # affiancate e formano una zona sola. Disegnare due bordi e due
                # etichette la faceva sembrare spezzata in due.
                mappe[kw]["satellite_box"] = [posto[0], posto[1], bw, bh]
                mappe[kw]["satellite_nome"] = "MAPSEC_BATTLE_FRONTIER"
                for k in bf:
                    if k != kw:
                        mappe[k]["satellite_parte"] = kw
                del mondi[wid_bf]
                n_sat += len(bf)

    # 3. L'ISOLA REMOTA (cella 12,14) e le tre isole EVENTO, che il gioco
    #    tiene FUORI dalla sua mappa (cella 0,0): l'Isola Suprema, il Monte
    #    Cordone e l'Isola Materna vanno in fila lungo il bordo sud, tutte
    #    con la linea dal porto di Alghepoli, da cui si salpa davvero.
    sec = rme.get("MAPSEC_SOUTHERN_ISLAND")
    if sec:
        if _aggancia("MAP_SOUTHERN_ISLAND_EXTERIOR", _tile_da_cella(sec[0], sec[1]), porto, by_id.get("MAP_LILYCOVE_CITY")):
            n_sat += 1
    fondo = max((mk["oy"] + mk["h"] for mk in mappe.values() if mk.get("mondo") == "hoenn"),
                default=0)
    passo_x = 250      # largo: sotto stanno le ETICHETTE, che a mappa intera
                       # sono piu' larghe delle isole e si accavallavano
    for j, ide in enumerate(("MAP_FARAWAY_ISLAND_ENTRANCE",
                             "MAP_NAVEL_ROCK_EXTERIOR",
                             "MAP_BIRTH_ISLAND_EXTERIOR")):
        if _aggancia(ide, (140 + j * passo_x, fondo + 30), porto, by_id.get("MAP_LILYCOVE_CITY")):
            n_sat += 1

    # --- DOVE SI SBARCA (2026-08-25) ----------------------------------------
    # Su un'isola non ci si arriva da una porta disegnata: ci si arriva col
    # traghetto, e il gioco ti deposita su un tile preciso scritto in un
    # `warp` di copione (LilycoveCity_Harbor e SlateportCity_Harbor). Quel
    # tile e' l'unica risposta vera a "da dove si parte", e senza segnarlo
    # bisognava cliccare per scoprirlo.
    import re as _re
    _rx_warp = _re.compile(r"\bwarp\s+(MAP_\w+)\s*,\s*(\d+)\s*,\s*(\d+)")
    _sbarchi = {}
    for _f in sorted((DECOMP / "data/maps").glob("*/scripts.inc")) + \
              sorted((DECOMP / "data/scripts").glob("*.inc")):
        try:
            _t = _f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for _m in _rx_warp.finditer(_t):
            _sbarchi.setdefault(_m.group(1), (int(_m.group(2)), int(_m.group(3))))
    n_sbarchi = 0
    for k, mk in mappe.items():
        if not mk.get("satellite"):
            continue
        xy = _sbarchi.get(mk["id"])
        if xy is None:
            # niente warp di copione: si usa la porta verso il PORTO dell'isola
            # (Monte Cordone, Isola Materna), che e' dove si mette piede
            for w in mk["porte"]:
                if w.get("a") and "HARBOR" in mappe[w["a"]]["id"]:
                    xy = (w["x"], w["y"])
                    break
        if xy and 0 <= xy[0] < mk["w"] and 0 <= xy[1] < mk["h"]:
            mk["sbarco"] = [mk["ox"] + xy[0], mk["oy"] + xy[1]]
            n_sbarchi += 1
    print("sbarchi segnati: %d su %d satelliti"
          % (n_sbarchi, sum(1 for v in mappe.values() if v.get("satellite"))))

    # rinormalizza il mondo hoenn (i satelliti possono uscire dai bordi)
    chiavi_h = [k for k in mondi["hoenn"]["mappe"] if mappe[k].get("mondo") == "hoenn"]
    minx = min(mappe[k]["ox"] for k in chiavi_h)
    miny = min(mappe[k]["oy"] for k in chiavi_h)
    for k in chiavi_h:
        mappe[k]["ox"] -= minx
        mappe[k]["oy"] -= miny
        if mappe[k].get("satellite"):
            mappe[k]["satellite"] = [mappe[k]["satellite"][0] - minx,
                                     mappe[k]["satellite"][1] - miny]
        if mappe[k].get("satellite_box"):
            bx0, by0, bw0, bh0 = mappe[k]["satellite_box"]
            mappe[k]["satellite_box"] = [bx0 - minx, by0 - miny, bw0, bh0]
        if mappe[k].get("sbarco"):
            mappe[k]["sbarco"] = [mappe[k]["sbarco"][0] - minx,
                                  mappe[k]["sbarco"][1] - miny]
    mondi["hoenn"]["w"] = max(mappe[k]["ox"] + mappe[k]["w"] for k in chiavi_h)
    mondi["hoenn"]["h"] = max(mappe[k]["oy"] + mappe[k]["h"] for k in chiavi_h)
    mondi["hoenn"]["mappe"] = sorted(set(chiavi_h),
                                     key=lambda k: (mappe[k]["gruppo"], mappe[k]["numero"]))
    print("satelliti: %d mappe agganciate a hoenn (%s)"
          % (n_sat, ", ".join(sorted(mappe[k].get("nome_it") or mappe[k]["nome"]
                                     for k in mappe if mappe[k].get("satellite")))))

    # --- LA GUARDIA: nessuna mappa deve restare irraggiungibile --------------
    # BFS dalle mappe dei mondi lungo le porte: chi non si visita cliccando
    # viene GRIDATO. Se il numero sale, una modifica ha rotto qualcosa e la
    # build lo dice subito.
    visitabili = set(k for k in mappe if mappe[k].get("mondo"))
    coda2 = list(visitabili)
    while coda2:
        cur = coda2.pop()
        for w in mappe[cur]["porte"]:
            kk = w.get("a")
            if kk and kk in mappe and kk not in visitabili:
                visitabili.add(kk)
                coda2.append(kk)
    fuori_rete = [k for k in mappe if k not in visitabili]
    if fuori_rete:
        print("!!! MAPPE IRRAGGIUNGIBILI CLICCANDO: %d" % len(fuori_rete))
        for k in sorted(fuori_rete):
            print("      %s %s" % (k, mappe[k]["id"]))
    else:
        print("raggiungibilita': TUTTE le %d mappe si visitano cliccando" % len(mappe))

    print("mondi: %d -> %s" % (len(mondi), ", ".join("%s (%d mappe, %dx%d tile)" % (w["nome"], len(w["mappe"]), w["w"], w["h"]) for w in mondi.values())))

    # --- indice specie -> dove ---
    specie = {}
    for key, mk in mappe.items():
        for metodo, v in mk["selvatici"].items():
            for s in v["specie"]:
                front_usati.add(s["cost"])
                specie.setdefault(s["specie"], {"cost": s["cost"], "dove": []})["dove"].append(
                    {"mappa": key, "metodo": metodo, "pct": s["pct"], "lv": s["lv"]})
    for s in specie.values():
        s["dove"].sort(key=lambda d: -d["pct"])

    # --- sprite degli object event usati (NPC, allenatori, palle, alberi) ---
    (OUT / "oe").mkdir(parents=True, exist_ok=True)
    oe = rendi_oe(oe_cat, gfx_usati, OUT)
    n_pers = sum(len(v["personaggi"]) for v in mappe.values())
    n_all = sum(1 for v in mappe.values() for p_ in v["personaggi"] if p_["tipo"] == "allenatore")
    print("personaggi: %d (allenatori %d), sprite renderizzati: %d su %d gfx usati"
          % (n_pers, n_all, len(oe), len(gfx_usati)))
    mancanti = sorted(g for g in gfx_usati if g not in oe)
    if mancanti:
        print("  gfx senza sprite:", ", ".join(mancanti))

    # --- icone dei Pokemon (dalla decomp, 32x32: il primo dei due frame) ---
    # Per le specie selvatiche (indice `specie`) E per quelle delle squadre
    # degli allenatori: l'indice `icone` (costante -> file) copre entrambe.
    icone = {}
    da_iconare = {s["cost"] for s in specie.values()}
    for v in mappe.values():
        for p_ in v["personaggi"]:
            for mon in p_.get("squadra", []):
                da_iconare.add(mon["cost"])
    n_icone = 0
    for cost in sorted(da_iconare):
        s = {"cost": cost}
        cart = cost.replace("SPECIES_", "").lower()
        p = DECOMP / "graphics/pokemon" / cart / "icon.png"
        if not p.exists():
            alt = DECOMP / "graphics/pokemon" / cart / "a" / "icon.png"   # unown
            p = alt if alt.exists() else None
        if p is None:
            continue
        try:
            w, h, bd, pal, trns, righe = png_decode(p)
        except Exception as e:      # noqa: BLE001
            print("  icona %s: %s" % (cart, e))
            continue
        fr = righe[:min(32, h)]
        (OUT / "icone" / (cart + ".png")).write_bytes(png_encode_indexed(w, len(fr), fr, pal[:256], trns or [0]))
        icone[cost] = "icone/%s.png" % cart
        n_icone += 1
    for s in specie.values():
        if s["cost"] in icone:
            s["icona"] = icone[s["cost"]]
    print("icone: %d (selvatiche + squadre degli allenatori)" % n_icone)

    # --- sprite del giocatore: Brendan e May, 4 direzioni, CINQUE modi ---
    #
    # walking.png e' 144x32 = NOVE fotogrammi da 16x32, e l'ordine NON e'
    # "tre terzine" (2026-08-27): i fermi stanno in testa - 0 giu', 1 su,
    # 2 sinistra - e i passi seguono A PAIA, una direzione alla volta:
    # 3-4 giu', 5-6 su, 7-8 sinistra. Destra e' la sinistra SPECCHIATA (il
    # gioco fa lo stesso, non esiste un disegno per destra).
    #
    # PERCHE' CONTA. Fino al 27/08 si leggeva base+3 e base+6, cioe' i
    # fotogrammi 3/6 per "giu'", 4/7 per "su" e 5/8 per "sinistra": solo
    # il primo passo di ogni direzione era quello giusto, il secondo veniva
    # da un'ALTRA direzione. Da qui il difetto visto sul sito - chi
    # camminava girava la schiena a meta' passo e poi tornava a posto -
    # perche' brendan_sx_a era in realta' il passo VISTO DA DIETRO.
    # La prova che erano sbagliati stava anche nei file: may_giu_b e
    # may_dx_a uscivano IDENTICI (fotogramma 6 e fotogramma 5 specchiato),
    # e due direzioni diverse non possono avere lo stesso disegno.
    #
    # GLI ALTRI QUATTRO MODI (2026-08-30). Sulla mappa si vedeva solo
    # l'allenatore che cammina: chi era in bici o in surf appariva a piedi,
    # anche se lo stato viaggia nel protocollo da sempre (byte 2 `speed` e
    # byte 11 `avatarState` di NetEvent). Gli indici NON sono stati indovinati
    # guardando le immagini: vengono dalle tabelle della decomp, che sono la
    # sola fonte di verita'.
    #   corsa   running.png  144x32, 9 frame da 16x32. sAnim_RunSouth (
    #           object_event_anims.h:346) usa gli indici 12,9,13 della pic
    #           table sPicTable_BrendanNormal, i cui elementi 9..17 sono i
    #           frame 0..8 di running.png -> 3,0,4: LO STESSO SCHEMA della
    #           camminata.
    #   mach    mach_bike.png 288x32, 9 frame da 32x32 (sAnimTable_Standard,
    #           stessi sAnim_FaceSouth/GoSouth -> stesso schema).
    #   acro    acro_bike.png 864x32, 27 frame da 32x32: i primi 9 sono lo
    #           schema standard, gli altri 18 sono impennate e saltelli che
    #           sulla mappa non servono.
    #   surf    surfing.png   192x32, 6 frame da 32x32, e qui lo schema
    #           CAMBIA: sPicTable_BrendanSurfing (object_event_pic_tables.h:64)
    #           rimappa gli indici 0..8 sui soli frame 0 (giu'), 2 (su) e 4
    #           (sinistra) - le andature non hanno fotogrammi propri, perche'
    #           a dondolare e' la bolla del Pokemon, non l'allenatore. Quindi
    #           niente _a/_b per il surf: un disegno per direzione.
    PASSO_STD = (("giu", 0, 3, False), ("su", 1, 5, False),
                 ("sx", 2, 7, False), ("dx", 2, 7, True))
    SURF_STD = (("giu", 0, None, False), ("su", 2, None, False),
                ("sx", 4, None, False), ("dx", 4, None, True))
    MODI = (
        # prefisso, file, larghezza del fotogramma, direzioni
        ("",      "walking.png",   16, PASSO_STD),
        ("corsa", "running.png",   16, PASSO_STD),
        ("mach",  "mach_bike.png", 32, PASSO_STD),
        ("acro",  "acro_bike.png", 32, PASSO_STD),
        ("surf",  "surfing.png",   32, SURF_STD),
    )
    n_sprite = 0
    modi_fatti = []
    for chi in ("brendan", "may"):
        for modo, file_png, larg, direzioni in MODI:
            p = DECOMP / "graphics/object_events/pics/people" / chi / file_png
            if not p.exists():
                continue
            w, h, bd, pal, trns, righe = png_decode(p)
            visti = {}
            for nome_dir, fermo, passo, flip in direzioni:
                fasi = ((None, 0),) if passo is None else ((None, 0), ("_a", 0), ("_b", 1))
                for suff, salto in fasi:
                    frame = fermo if suff is None else passo + salto
                    nome = "%s_%s%s%s" % (chi, (modo + "_") if modo else "",
                                          nome_dir, suff or "")
                    fr = []
                    for r in righe[:32]:
                        seg = r[frame * larg:frame * larg + larg]
                        fr.append(bytes(reversed(seg)) if flip else seg)
                    (OUT / "sprite" / (nome + ".png")).write_bytes(
                        png_encode_indexed(larg, 32, fr, pal[:256], trns or [0]))
                    visti.setdefault(bytes(b"".join(fr)), []).append(nome)
                    n_sprite += 1
            # LA GUARDIA (2026-08-27, estesa a tutti i modi il 2026-08-30).
            # Dentro un modo i disegni devono essere tutti diversi: se due
            # file escono identici vuol dire che si stanno leggendo i
            # fotogrammi sbagliati - che e' esattamente il modo in cui il
            # difetto "cammina e gira la schiena" era passato inosservato per
            # giorni. Meglio fermare lo script che pubblicare sprite storti.
            doppi = [n for n in visti.values() if len(n) > 1]
            if doppi:
                raise SystemExit("sprite %s di %s: fotogrammi duplicati %s - "
                                 "l'ordine dentro %s non e' quello atteso"
                                 % (modo or "a piedi", chi, doppi, file_png))
            if modo not in modi_fatti:
                modi_fatti.append(modo)
    print("sprite: %d (%d modi: %s)"
          % (n_sprite, len(modi_fatti),
             ", ".join(m or "a piedi" for m in modi_fatti)))

    # --- icone vere degli strumenti e sprite anteriori delle squadre ---
    item_img = rendi_strumenti(icone_item, item_usati, OUT)
    print("strumenti: %d icone vere su %d costanti usate" % (len(item_img), len(item_usati)))
    front_img = rendi_front(front_cat, front_usati, OUT)
    print("sprite anteriori: %d (64x128, due fotogrammi: l'animazione del gioco)"
          % len(front_img))

    # --- gli sprite ANIMATI di Nero/Bianco 2, se sono gia' stati scaricati ---
    # Non li produce questo script: li prende tools/sprite_bw.py, una volta, da
    # uno specchio pubblico. Qui si legge solo l'indice, cosi' la pagina sa
    # quali specie hanno la GIF e quali devono ripiegare sui due fotogrammi
    # della decomp.
    bw = {}
    indice_bw = OUT / "bw" / "indice.json"
    if indice_bw.exists():
        try:
            bw = json.loads(indice_bw.read_text(encoding="utf-8"))
        except Exception as e:   # noqa: BLE001
            print("indice degli sprite animati illeggibile: %s" % e)
    print("sprite animati (Nero/Bianco 2): %d specie%s"
          % (len(bw), "" if bw else " - lancia tools/sprite_bw.py per averli"))

    # --- i nomi italiani, dalla cartuccia ---
    print("nomi italiani (dalla ROM BPEI):")
    it = carica_nomi_italiani()
    if it.get("items"):
        # stessa storia degli alias delle MT: la ROM parla per numero, i map
        # script per nome, e i due nomi dello stesso numero devono ricevere
        # entrambi la traduzione
        val = valori_item()
        per_num = {}
        for nome, n in val.items():
            if nome in it["items"]:
                per_num.setdefault(n, it["items"][nome])
        for nome, n in val.items():
            if nome not in it["items"] and n in per_num:
                it["items"][nome] = per_num[n]

    # --- il nome italiano di ogni mappa -------------------------------------
    if it.get("mapsec"):
        en = nomi_mapsec_en()
        n_it = 0
        for k, v in it["mapsec"].items():
            it["mapsec"][k] = titolo(v)
        for k in ("items", "specie", "classi", "trainers", "mosse"):
            if it.get(k):
                it[k] = {kk: titolo(vv) for kk, vv in it[k].items()}
        # le parole della frase di tendenza restano MAIUSCOLE come le scrive
        # il gioco: nel menu della frase si leggono cosi'.
        # I nomi italiani dei personaggi ricorrenti, LETTI DALLA ROM (sono
        # allenatori, quindi stanno in gTrainers): servono per "Casa di X".
        nomi_persone = {}
        for eng, prefissi in (("Brendan", ("TRAINER_BRENDAN_",)),
                              ("May", ("TRAINER_MAY_",)),
                              ("Steven", ("TRAINER_STEVEN",)),
                              ("Wally", ("TRAINER_WALLY_",))):
            for tk, tv in (it.get("trainers") or {}).items():
                if tk.startswith(prefissi) and tv:
                    nomi_persone[eng] = tv
                    break
        for chiave, mk in mappe.items():
            sec = mk.get("mapsec") or ""
            ita = it["mapsec"].get(sec)
            nome = mk["nome"]
            if ita:
                # il confronto ignora spazi, trattini E punti: "Mt. Chimney"
                # nella mapsec contro "Mt Chimney" nel nome della cartella
                # (prima il punto faceva sparire l'italiano da 10 mappe)
                testa = (en.get(sec) or "").replace(" ", "").replace(".", "").lower()
                senza = nome.replace(" ", "").replace("-", "").replace(".", "").lower()
                if testa and senza.startswith(testa):
                    # quante lettere del nome visualizzato coprono la testa?
                    conta, presi = 0, 0
                    for ch in nome:
                        presi += 1
                        if ch not in (" ", "-", "."):
                            conta += 1
                            if conta == len(testa):
                                break
                    resto = nome[presi:].lstrip(" -")
                    if resto:
                        resto = traduci_coda(resto, nomi_persone)
                    mk["nome_it"] = ita + (" - " + resto if resto else "")
                    n_it += 1
                    continue
            # Niente mapsec utile (le salette del Cable Club, le Sale Gare,
            # la nave, la Piramide...): si traduce l'intero nome come coda.
            tradotto = traduci_coda(nome, nomi_persone)
            if tradotto != nome:
                mk["nome_it"] = tradotto
                n_it += 1
        # I PERSONAGGI CON UN NOME PROPRIO (Adriano, Rocco, Ivan, Walter...).
        # Il loro nome sulla mappa viene dalla costante grafica, che e'
        # inglese: OBJ_EVENT_GFX_WALLACE -> "Wallace". Ma quelle persone sono
        # anche allenatori, e il loro nome ITALIANO sta nella cartuccia
        # (gTrainers). Si aggancia la grafica alla voce di gTrainers con lo
        # stesso nome - solo quando esiste, senza indovinare - e si porta la
        # costante nel JSON: il nome lo sceglie la pagina, come per tutto il
        # resto.
        trad = it.get("trainers") or {}
        n_pers = 0
        for mk in mappe.values():
            for pe in mk["personaggi"]:
                if pe.get("tipo") not in ("npc", "oggetto"):
                    continue
                base = pe["gfx"].replace("OBJ_EVENT_GFX_", "")
                cand = None
                for c in ("TRAINER_" + base, "TRAINER_" + base + "_1"):
                    if c in trad:
                        cand = c
                        break
                if cand is None:
                    prefisso = "TRAINER_" + base + "_"
                    simili = sorted(c for c in trad if c.startswith(prefisso))
                    if simili:
                        # tutti gli omonimi devono dare lo STESSO nome, o non
                        # si sa quale sia la persona giusta e si lascia stare
                        nomi = {trad[c] for c in simili}
                        if len(nomi) == 1:
                            cand = simili[0]
                if cand:
                    pe["pc"] = cand
                    n_pers += 1
        print("           %d personaggi con nome proprio agganciati alla cartuccia" % n_pers)

        # anche i mondi parlano italiano: il nome viene dalla mappa eponima
        for wid, w in mondi.items():
            if wid == "hoenn":
                w["nome_it"] = "Hoenn"
            else:
                prima = w["mappe"][0]
                ni = mappe[prima].get("nome_it")
                if ni:
                    w["nome_it"] = ni + " (e dintorni)"
        print("           %d mappe su %d con nome italiano" % (n_it, len(mappe)))

    # LE RIVINCITE: il conto va detto, e a zero va gridato. Il collegamento e'
    # una regex su gRematchTable e un lookup per nome di trainer: se la decomp
    # cambia forma, l'unico sintomo sarebbe una tendina che non compare piu' -
    # cioe' niente, in una pagina piena di roba.
    if rematch_conti["basi"]:
        # Delle 78 voci di gRematchTable, 5 sono la Superquattro (lo stesso
        # trainer ripetuto: nessuna squadra nuova) e non producono tendina: il
        # numero atteso e' quindi 73, non 78. Se scende parecchio sotto, un
        # allenatore ha smesso di essere riconosciuto sulla mappa.
        mancanti = sorted(set(rematch) - rematch_conti["basi"])
        print("rivincite: %d allenatori su %d voci della tabella, %d squadre "
              "in piu', %d nascoste perche' identiche alla precedente"
              % (len(rematch_conti["basi"]), len(rematch),
                 rematch_conti["squadre"], rematch_conti["nascoste"]))
        if mancanti:
            print("           senza tendina: %s" % ", ".join(
                x.replace("TRAINER_", "") for x in mancanti))
    else:
        print("!!! NESSUNA RIVINCITA AGGANCIATA: gRematchTable letta male "
              "(%d voci) o i nomi dei trainer non combaciano. La tendina "
              "delle rivincite sara' vuota ovunque." % len(rematch))

    dati = {
        "generato": time.strftime("%Y-%m-%d %H:%M"),
        "map_offset": MAP_OFFSET,
        "mappe": mappe,
        "mondi": mondi,
        "specie": specie,
        "oe": oe,
        "icone": icone,
        "item": item_img,
        "front": front_img,
        "bw": bw,
        "it": it,
    }
    # Via gli ausiliari: dentro dati.json non ci vanno.
    for v in mappe.values():
        v.pop("_layout", None)
        v.pop("_dir", None)
        if not v.get("miraggio"):
            v.pop("miraggio", None)
    (OUT / "dati.json").write_text(json.dumps(dati, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tot = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print("scritto %s: %d mappe, %d specie, %.1f MB in tutto (%.0f s)"
          % (OUT, len(mappe), len(specie), tot / 1e6, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
