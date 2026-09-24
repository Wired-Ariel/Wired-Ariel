#!/usr/bin/env python3
"""
test_mappa_sprite.py - gli sprite del giocatore sulla mappa live (2026-08-30).

Fino al 30/08 sulla mappa si vedeva solo l'allenatore che CAMMINA: chi era in
bici o in surf appariva a piedi. Lo stato viaggiava nel protocollo da sempre
(byte 11 `avatarState`, byte 2 `speed` di NetEvent) e arrivava fino a
`giocatori[]`, ma il disegno non lo leggeva e i disegni non erano mai stati
ritagliati dalla decomp.

Questo test guarda le tre cose che possono rompersi in silenzio, e che a
schermo darebbero tutte lo stesso sintomo muto (un buco, o l'allenatore a
piedi al posto della bici):

  1. TUTTI i nomi che mappa.html sa costruire esistono su disco. E' la
     giuntura piu' fragile del giro: il nome del file lo compone la pagina a
     mano, `chi + modo + direzione + fase`, e gen_mappa.py lo compone per
     conto suo dall'altra parte. Se una delle due cambia convenzione, l'altra
     non se ne accorge - si vede solo guardando la mappa nel momento giusto,
     cioe' quasi mai.
  2. Le DIMENSIONI sono quelle attese: 16x32 a piedi e di corsa, 32x32 in
     bici e in surf. La pagina disegna i 32x32 larghi il doppio e centrati:
     se un file cambiasse forma senza che la pagina lo sappia, l'amico
     apparirebbe schiacciato o spostato di mezzo tile.
  3. Dentro un modo i disegni sono tutti DIVERSI. E' la stessa guardia che
     gen_mappa.py fa in produzione (2026-08-27, il difetto "cammina e gira la
     schiena"), rifatta qui sui file veri: se un giorno si leggessero i
     fotogrammi sbagliati dentro un PNG della decomp, due direzioni
     uscirebbero identiche.

Non prova l'aspetto a schermo: quello si guarda con gli occhi (procedura in
NOTES). Prova che i disegni ci siano, siano della forma giusta, e che la
pagina li chiami con lo stesso nome con cui sono stati scritti.

    D:\\Progettini\\Python313\\python.exe tools\\test_mappa_sprite.py
"""

import hashlib
import os
import re
import struct
import sys
import unittest

QUI = os.path.dirname(os.path.abspath(__file__))
RADICE = os.path.dirname(QUI)

SPRITE = os.path.join(RADICE, "net", "mappa", "sprite")
PAGINA = os.path.join(RADICE, "net", "mappa.html")

CHI = ("brendan", "may")
DIREZIONI = ("giu", "su", "sx", "dx")
FASI = ("", "_a", "_b")

# I modi, come li costruisce la pagina: prefisso -> (larghezza attesa,
# animato). Il surf non ha fotogrammi di andatura - nella decomp le andature
# rimandano allo stesso disegno (sPicTable_BrendanSurfing), perche' a
# dondolare e' la bolla del Pokemon, non l'allenatore.
MODI = {
    "":      (16, True),
    "corsa": (16, True),
    "mach":  (32, True),
    "acro":  (32, True),
    "surf":  (32, False),
}


def dim_png(path):
    """(larghezza, altezza) dall'IHDR, senza dipendenze esterne."""
    with open(path, "rb") as f:
        testa = f.read(24)
    if testa[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s non e' un PNG" % path)
    return struct.unpack(">II", testa[16:24])


def nome(chi, modo, direzione, fase):
    return "%s_%s%s%s" % (chi, (modo + "_") if modo else "", direzione, fase)


class TestSpriteGiocatore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(SPRITE):
            raise unittest.SkipTest("net/mappa/sprite non c'e': "
                                    "lancia prima tools/gen_mappa.py")
        with open(PAGINA, encoding="utf-8") as f:
            cls.html = f.read()

    def attesi(self):
        """Ogni nome che la pagina puo' chiedere, senza eccezioni."""
        for chi in CHI:
            for modo, (_, animato) in MODI.items():
                for direzione in DIREZIONI:
                    for fase in (FASI if animato else ("",)):
                        yield chi, modo, direzione, fase

    def test_esistono_tutti(self):
        mancanti = [nome(*a) for a in self.attesi()
                    if not os.path.exists(os.path.join(SPRITE, nome(*a) + ".png"))]
        self.assertEqual(mancanti, [],
                         "sprite che la pagina sa chiedere ma che non esistono: %s "
                         "- rilancia tools/gen_mappa.py" % mancanti[:8])

    def test_dimensioni(self):
        for chi, modo, direzione, fase in self.attesi():
            n = nome(chi, modo, direzione, fase)
            p = os.path.join(SPRITE, n + ".png")
            if not os.path.exists(p):
                continue          # lo dice gia' il test di sopra
            larg, alt = dim_png(p)
            self.assertEqual((larg, alt), (MODI[modo][0], 32),
                             "%s e' %dx%d, atteso %dx32" % (n, larg, alt, MODI[modo][0]))

    def test_niente_doppioni_dentro_un_modo(self):
        for chi in CHI:
            for modo, (_, animato) in MODI.items():
                visti = {}
                for direzione in DIREZIONI:
                    for fase in (FASI if animato else ("",)):
                        n = nome(chi, modo, direzione, fase)
                        p = os.path.join(SPRITE, n + ".png")
                        if not os.path.exists(p):
                            continue
                        with open(p, "rb") as f:
                            visti.setdefault(hashlib.sha1(f.read()).hexdigest(), []).append(n)
                doppi = [v for v in visti.values() if len(v) > 1]
                self.assertEqual(doppi, [],
                                 "%s modo '%s': disegni identici %s - si stanno "
                                 "leggendo i fotogrammi sbagliati dentro il PNG "
                                 "della decomp" % (chi, modo or "a piedi", doppi))

    def test_la_pagina_sceglie_il_modo(self):
        """Il ramo che legge avatarState e speed deve esistere: senza, i file
        generati resterebbero li' inutilizzati e il sintomo sarebbe di nuovo
        'si vede solo l'allenatore che cammina', senza nessun errore."""
        for atteso in ('g.avatar === 1', 'g.avatar === 2', 'g.avatar === 3',
                       'g.speed === 1', '"mach"', '"acro"', '"surf"', '"corsa"'):
            self.assertIn(atteso, self.html,
                          "mappa.html non contiene %s: il modo non viene scelto" % atteso)

    def test_la_pagina_allarga_i_32(self):
        """I disegni da 32 px vanno larghi il doppio e centrati sul tile: se
        questo ramo sparisse, bici e surf si vedrebbero schiacciati in un
        tile e spostati di mezzo tile rispetto ai piedi."""
        self.assertTrue(re.search(r"largo2\s*\?\s*sw\s*\*\s*2\s*:\s*sw", self.html),
                        "mappa.html non raddoppia la larghezza dei disegni 32x32")
        self.assertIn("(dw - sw) / 2", self.html,
                      "mappa.html non ricentra i disegni 32x32 sul tile")


if __name__ == "__main__":
    unittest.main(verbosity=2)
