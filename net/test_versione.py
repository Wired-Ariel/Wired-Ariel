#!/usr/bin/env python3
"""
test_versione.py - il controllo di versione fra i due PC.

Perche' esiste: il 2026-08-20 la correzione della riapertura del link era
stata copiata su un PC solo. Il club e' morto identico a prima, e per
capirlo sono serviti i due log affiancati e l'occhio su un contatore che da
una parte c'era e dall'altra no (`riaperture del link`). Un'ora buttata per
una copia mancata.

Da allora ogni CLUB_STATUS porta in coda versione e impronta dei sorgenti,
e chi vede una differenza la grida. Questi test provano che la grida esca
quando serve e che NON esca quando i due sono allineati - un avviso che
compare sempre e' un avviso che si impara a ignorare.

    python test_versione.py
"""

import unittest

import client
import protocol
from protocol import (
    CLUB_STATUS, IMPRONTA_SORGENTI, VERSIONE_CLUB, club_status, club_unpack,
)


def bridge_finto():
    """Un Bridge senza __init__: qui serve solo il controllo di versione,
    non l'USB ne' i socket."""
    b = client.Bridge.__new__(client.Bridge)
    b.club_versione_detta = False
    b.righe = []
    b.log = b.righe.append
    return b


class TestImpronta(unittest.TestCase):

    def test_lo_stato_porta_versione_e_impronta(self):
        corpo = club_status(0x11223344, 5, 0xFF03)
        sub, epoca, payload = club_unpack(corpo)
        self.assertEqual(sub, CLUB_STATUS)
        self.assertEqual(epoca, 0x11223344)
        self.assertEqual(payload, (5, 0xFF03, VERSIONE_CLUB, IMPRONTA_SORGENTI))

    def test_uno_stato_vecchio_si_legge_lo_stesso(self):
        """La coda e' IN FONDO apposta: un client vecchio manda 9 byte e va
        letto senza errori - e si riconosce proprio dalla coda mancante."""
        corpo = club_status(0x11223344, 5, 0xFF03)[:9]
        sub, epoca, payload = club_unpack(corpo)
        self.assertEqual(payload, (5, 0xFF03, 0, 0))

    def test_l_impronta_non_e_zero(self):
        # Se il calcolo fallisse (file non trovati) verrebbe fuori un valore
        # costante, e il controllo direbbe "tutto bene" per sempre.
        self.assertNotEqual(IMPRONTA_SORGENTI, 0)
        self.assertEqual(IMPRONTA_SORGENTI, protocol._impronta_sorgenti())


class TestDenuncia(unittest.TestCase):

    def test_allineati_nessun_avviso(self):
        b = bridge_finto()
        b.controlla_versione(VERSIONE_CLUB, IMPRONTA_SORGENTI)
        self.assertEqual(b.righe, [])

    def test_file_vecchi_lo_dice(self):
        b = bridge_finto()
        b.controlla_versione(0, 0)
        testo = "\n".join(b.righe)
        self.assertIn("FILE VECCHI", testo)
        self.assertIn("ENTRAMBI i PC", testo)

    def test_versione_diversa_lo_dice(self):
        b = bridge_finto()
        b.controlla_versione(VERSIONE_CLUB - 1, IMPRONTA_SORGENTI)
        testo = "\n".join(b.righe)
        self.assertIn("VERSIONI DIVERSE", testo)

    def test_stessa_versione_ma_file_diversi_lo_dice(self):
        """Il caso vero del 2026-08-20: stesso numero di versione, pacchetto
        piu' vecchio da una parte."""
        b = bridge_finto()
        b.controlla_versione(VERSIONE_CLUB, IMPRONTA_SORGENTI ^ 0xFFFF)
        testo = "\n".join(b.righe)
        self.assertIn("FILE DIVERSI", testo)

    def test_lo_dice_una_volta_sola(self):
        """Gli stati arrivano a raffica: la denuncia deve restare leggibile,
        non seppellire il resto del registro."""
        b = bridge_finto()
        for _ in range(20):
            b.controlla_versione(0, 0)
        self.assertEqual(sum(1 for r in b.righe if "FILE VECCHI" in r), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
