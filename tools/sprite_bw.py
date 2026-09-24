#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sprite_bw.py - gli sprite ANIMATI di Nero/Bianco 2, scaricati una volta.

PERCHE' NON STANNO NELLA DECOMP
-------------------------------
La decomp e' Smeraldo: i suoi sprite anteriori sono 64x64 a DUE fotogrammi
(anim_front.png, 64x128). Gli sprite animati veri - quelli che camminano sul
posto, sbattono le ali, respirano - sono di Nero/Bianco 2, cioe' Nintendo DS,
e nei repo locali non c'e' niente di simile. Vanno presi da fuori.

DA DOVE
-------
Due specchi che la comunita' usa da anni, provati in quest'ordine:
  play.pokemonshowdown.com/sprites/gen5ani/<nome>.gif
  img.pokemondb.net/sprites/black-white/anim/normal/<nome>.gif
Si scarica UNA VOLTA in net/mappa/bw/ e da li' in poi la pagina e' di nuovo
completamente offline: la regola "niente risorse esterne" del pannello resta
rispettata a pagina aperta, che e' il punto (nessuna richiesta verso internet
mentre giochi).

Sono grafiche Nintendo/Game Freak: vanno bene per uno strumento privato come
questo, esattamente come le grafiche gia' estratte dalla decomp e dalla
cartuccia. Non e' roba da mettere su un sito pubblico.

Uso:
    python tools/sprite_bw.py             # solo le specie che servono
    python tools/sprite_bw.py --tutte     # tutte quelle di Gen 3
    python tools/sprite_bw.py --rifai     # riscarica anche quelle gia' prese
"""

import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

QUI = os.path.dirname(os.path.abspath(__file__))
RADICE = os.path.dirname(QUI)
OUT = os.path.join(RADICE, "net", "mappa", "bw")
DATI = os.path.join(RADICE, "net", "mappa", "dati.json")
DECOMP = os.path.join(RADICE, "repo-studio", "pokeemerald")
if not os.path.isdir(DECOMP):
    DECOMP = os.path.join(os.path.dirname(RADICE), "repo-studio", "pokeemerald")

FONTI = [
    "https://play.pokemonshowdown.com/sprites/gen5ani/%s.gif",
    "https://img.pokemondb.net/sprites/black-white/anim/normal/%s.gif",
]

# I nomi che i due specchi scrivono diversamente dalla costante della decomp.
# Tutto il resto e' "SPECIES_X" -> "x" senza trattini bassi.
ECCEZIONI = {
    "SPECIES_NIDORAN_F": "nidoranf",
    "SPECIES_NIDORAN_M": "nidoranm",
    "SPECIES_MR_MIME": "mrmime",
    "SPECIES_HO_OH": "ho-oh",
    "SPECIES_DEOXYS": "deoxys",
    "SPECIES_FARFETCHD": "farfetchd",
}


def nome_web(const):
    if const in ECCEZIONI:
        return ECCEZIONI[const]
    return const.replace("SPECIES_", "").lower().replace("_", "")


def specie_da_usare(tutte):
    """Le specie che la pagina puo' dover mostrare: quelle gia' renderizzate in
    `front` (selvatiche + squadre degli allenatori), oppure tutte quelle di
    Gen 3 se lo si chiede."""
    if tutte:
        testo = io.open(os.path.join(DECOMP, "include/constants/species.h"),
                        encoding="utf-8").read()
        voci = {}
        for m in re.finditer(r"^#define\s+(SPECIES_\w+)\s+(\d+)", testo, re.M):
            n = int(m.group(2))
            if 1 <= n <= 386 and n not in voci:
                voci[n] = m.group(1)
        return [voci[k] for k in sorted(voci)]
    if not os.path.exists(DATI):
        print("dati.json non c'e': lancia prima tools/gen_mappa.py")
        return []
    d = json.load(io.open(DATI, encoding="utf-8"))
    return sorted(d.get("front", {}).keys())


def scarica(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (passotile, strumento privato)",
        "Accept": "image/gif,image/*",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        dati = r.read()
    if not dati.startswith(b"GIF8"):
        raise ValueError("non e' una GIF (%d byte)" % len(dati))
    return dati


def main():
    tutte = "--tutte" in sys.argv
    rifai = "--rifai" in sys.argv
    lista = specie_da_usare(tutte)
    if not lista:
        return 1
    os.makedirs(OUT, exist_ok=True)
    print("specie da prendere: %d%s" % (len(lista), " (tutte le Gen 3)" if tutte else ""))
    presi, saltati, falliti, byte = 0, 0, [], 0
    for i, const in enumerate(lista):
        dest = os.path.join(OUT, const.replace("SPECIES_", "") + ".gif")
        if os.path.exists(dest) and not rifai:
            saltati += 1
            byte += os.path.getsize(dest)
            continue
        nome = nome_web(const)
        ok = False
        for fonte in FONTI:
            try:
                dati = scarica(fonte % nome)
            except Exception as e:   # noqa: BLE001
                ultimo = "%s: %s" % (fonte.split("/")[2], e)
                continue
            open(dest, "wb").write(dati)
            byte += len(dati)
            presi += 1
            ok = True
            break
        if not ok:
            falliti.append((const, nome, ultimo))
        # gentilezza verso lo specchio: non e' roba nostra
        time.sleep(0.12)
        if (i + 1) % 40 == 0:
            print("  %d/%d (%d presi, %d gia' c'erano)" % (i + 1, len(lista), presi, saltati), flush=True)

    manifesto = {}
    for f in sorted(os.listdir(OUT)):
        if f.endswith(".gif"):
            manifesto["SPECIES_" + f[:-4]] = "bw/" + f
    io.open(os.path.join(OUT, "indice.json"), "w", encoding="utf-8").write(
        json.dumps(manifesto, ensure_ascii=False))

    print("presi %d, gia' c'erano %d, falliti %d, in tutto %.1f MB"
          % (presi, saltati, len(falliti), byte / 1048576.0))
    for const, nome, err in falliti[:12]:
        print("  FALLITO %s (cercato come '%s') - %s" % (const, nome, err))
    if falliti:
        print("  per questi la pagina usa lo sprite a due fotogrammi della decomp")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
