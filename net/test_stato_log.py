# -*- coding: utf-8 -*-
"""Le righe [stato] del client (2026-08-21): una per TRANSIZIONE, in tutte e
due le direzioni, e l'evento passa comunque.

Il caso "scheda bianca" e' vissuto tre sessioni senza che nessun log dicesse
se il GBA emetteva lo stato giusto: questa e' la riga che lo dice, e qui si
garantisce che (a) scatti al cambio, (b) non si ripeta sulle copie e sui
battiti, (c) non mangi l'evento.

    D:\\Progettini\\Python313\\python.exe net\\test_stato_log.py
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client as C  # noqa: E402
from protocol import EV_STATUS  # noqa: E402


def evento(kind, dir_, seq=1):
    return struct.pack("<BBBBBBhhBB", kind, dir_, 0, seq, 0, 2, 5, 6, 0, 0)


class BridgeFinto(C.Bridge):
    """Solo i campi che forward_event / heal_and_deliver toccano."""

    def __init__(self):
        self.righe = []
        self.stato_io = None
        self.stato_amici = {}
        self.stato_cambi = 0
        self.club_riaggancio_dopo = 0.0
        self.club_stantii = 0
        # il silenzio post-club (2026-08-27): forward_event lo legge sempre
        self.gba_muto = False
        self.gba_muto_scartati = 0
        self.sent = 0
        self.out_seq = 0
        self.peer_id = 1
        self.room = 7
        self.map_key = None
        self.no_heal = False
        self.peer_pos = {}
        self.heal_far = 0
        self.consegnati = []
        # la mappa live (posizioni): campi toccati da _aggiorna_pos
        self.pos_io = None
        self.pos_amici = {}
        self.pos_sporche = False
        # gli slot avatar (fino a 4 giocatori): campi toccati da slot_for
        self.peer_slots = {}
        self.slot_free = [0, 1, 2]
        self.slot_pieni_avvisati = set()
        self.slot_scartati = 0

    def log(self, riga):
        self.righe.append(riga)

    def send_udp(self, datagram, now, simulate=True):
        pass

    def deliver(self, event, slot=0):
        self.consegnati.append(event)

    def stati(self):
        return [r for r in self.righe if r.startswith("[stato ]")]


class TestStatoLog(unittest.TestCase):
    def test_io_una_riga_per_transizione(self):
        b = BridgeFinto()
        b.forward_event(evento(EV_STATUS, 3, 10), 0.0)
        b.forward_event(evento(EV_STATUS, 3, 11), 0.0)   # copia: muta
        b.forward_event(evento(EV_STATUS, 3, 12), 0.0)   # battito: muta
        b.forward_event(evento(EV_STATUS, 4, 13), 0.0)   # zaino: riga
        self.assertEqual(b.stati(), ["[stato ] io -> menu  (dal GBA, #10)",
                                     "[stato ] io -> zaino  (dal GBA, #13)"])
        self.assertEqual(b.stato_cambi, 2)
        # Gli eventi passano TUTTI: la riga e' solo un'annotazione.
        self.assertEqual(b.sent, 4)

    def test_amico_una_riga_per_transizione_e_consegna(self):
        b = BridgeFinto()
        b.heal_and_deliver(2, evento(EV_STATUS, 4))
        b.heal_and_deliver(2, evento(EV_STATUS, 4))
        b.heal_and_deliver(2, evento(EV_STATUS, 0))
        self.assertEqual(b.stati(), [
            "[stato ] amico 2 -> zaino  (dalla rete, verso il GBA)",
            "[stato ] amico 2 -> overworld  (dalla rete, verso il GBA)"])
        self.assertEqual(len(b.consegnati), 3)

    def test_amici_distinti_non_si_confondono(self):
        b = BridgeFinto()
        b.heal_and_deliver(2, evento(EV_STATUS, 5))
        b.heal_and_deliver(3, evento(EV_STATUS, 5))
        self.assertEqual(len(b.stati()), 2)

    def test_stato_ignoto_non_rompe(self):
        b = BridgeFinto()
        b.forward_event(evento(EV_STATUS, 9, 1), 0.0)
        self.assertEqual(b.stati(), ["[stato ] io -> ?9  (dal GBA, #1)"])

    def test_posizioni_per_la_mappa_live(self):
        b = BridgeFinto()
        b.forward_event(evento(1, 2, 3), 0.0)            # PASSO su, mappa 0.2, (5,6)
        self.assertEqual((b.pos_io["gruppo"], b.pos_io["numero"], b.pos_io["x"],
                          b.pos_io["y"], b.pos_io["dir"], b.pos_io["stato"]), (0, 2, 5, 6, 2, 0))
        b.forward_event(evento(EV_STATUS, 4, 4), 0.0)    # zaino: lo stato, la posizione resta
        self.assertEqual((b.pos_io["stato"], b.pos_io["x"]), (4, 5))
        b.heal_and_deliver(2, evento(EV_STATUS, 1, 1))  # l'amico in lotta, mai visto prima
        self.assertEqual(b.pos_amici[2]["stato"], 1)
        b.heal_and_deliver(2, evento(4, 0, 2))          # VIA: sparisce
        self.assertNotIn(2, b.pos_amici)
        self.assertTrue(b.pos_sporche)

    def test_un_passo_non_e_uno_stato(self):
        b = BridgeFinto()
        b.forward_event(evento(1, 1, 1), 0.0)
        b.heal_and_deliver(2, evento(1, 1, 1))
        self.assertEqual(b.stati(), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
