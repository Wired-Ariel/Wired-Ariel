# -*- coding: utf-8 -*-
"""Gli slot avatar del client (2026-08-25, fino a 4 giocatori).

Il payload distingue i remoti dal nibble alto del type (EVENT_SLOT), e ad
assegnarlo e' il client del ricevente: primo evento -> primo slot libero,
T_BYE -> lo slot torna libero, quarto amico -> niente avatar (solo mappa
live). Qui si garantisce che:

  (a) gli slot si assegnano in ordine 0, 1, 2 e restano stabili;
  (b) il timbro finisce nel nibble alto di OGNI evento consegnato al GBA,
      copie comprese, e il tipo resta leggibile nel nibble basso;
  (c) il quarto amico non consegna niente al GBA (contatore, non silenzio);
  (d) il T_BYE consegna il VIA timbrato con lo slot giusto e lo libera:
      l'amico successivo lo riprende.

    D:\\Progettini\\Python313\\python.exe net\\test_slot.py
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import client as C  # noqa: E402
from protocol import EV_LEAVE, EV_STATUS, make_leave_event  # noqa: E402

EV_STEP, EV_SYNC, EV_TURN = 1, 2, 3


def evento(kind, dir_=1, seq=1, x=5, y=6):
    return struct.pack("<BBBBBBhhBB", kind, dir_, 0, seq, 0, 2, x, y, 0, 0)


class BridgeFinto(C.Bridge):
    """Solo i campi che heal_and_deliver / deliver toccano."""

    def __init__(self):
        self.righe = []
        self.stato_amici = {}
        self.no_heal = False
        self.peer_pos = {}
        self.heal_far = 0
        self.healed = 0
        self.leaves = 0
        self.consegnati = []
        self.pos_io = None
        self.pos_amici = {}
        self.pos_sporche = False
        self.peer_slots = {}
        self.slot_free = [0, 1, 2]
        self.slot_pieni_avvisati = set()
        self.slot_scartati = 0
        # campi toccati dal ramo T_BYE di handle_udp
        self.peer_room = {}
        self.last_seq = {}
        # il va-e-vieni (2026-08-30)
        self.peer_andato_at = {}
        self.flap = 0
        # il filo (deliver vero, non lo stub: qui si prova IL TIMBRO)
        self.club = None
        self.usb = None
        self.game = None
        self.wire_copies = 1
        self.wire_loss = 0.0
        self.wire_sent = 0
        self.wire_dropped = 0
        self.copies_skipped = 0
        self.copy_tokens = 8.0
        self.copy_stamp = 0.0

    def log(self, riga):
        self.righe.append(riga)

    def _deliver_one(self, event):
        self.consegnati.append(event)


class TestSlot(unittest.TestCase):
    def test_assegnazione_in_ordine_e_stabile(self):
        b = BridgeFinto()
        for peer in (7, 3, 9):
            b.heal_and_deliver(peer, evento(EV_SYNC))
        self.assertEqual(b.peer_slots, {7: 0, 3: 1, 9: 2})
        # lo stesso peer non cambia slot al secondo evento
        b.heal_and_deliver(3, evento(EV_SYNC, seq=2))
        self.assertEqual(b.peer_slots[3], 1)

    def test_timbro_nel_nibble_alto(self):
        b = BridgeFinto()
        b.heal_and_deliver(7, evento(EV_SYNC))          # slot 0
        b.heal_and_deliver(3, evento(EV_SYNC))          # slot 1
        b.heal_and_deliver(3, evento(EV_STEP, x=5, y=7, seq=2))
        tipi = [e[0] for e in b.consegnati]
        self.assertEqual(tipi[0], EV_SYNC)              # slot 0: nudo
        self.assertEqual(tipi[1], EV_SYNC | (1 << 4))   # slot 1: timbrato
        self.assertEqual(tipi[2], EV_STEP | (1 << 4))
        # il resto dell'evento non si tocca
        self.assertEqual(b.consegnati[2][1:], evento(EV_STEP, x=5, y=7, seq=2)[1:])

    def test_copie_tutte_timbrate(self):
        b = BridgeFinto()
        b.wire_copies = 2
        b.heal_and_deliver(3, evento(EV_SYNC))          # slot 0 (posizione base)
        b.consegnati.clear()
        b.heal_and_deliver(3, evento(EV_STEP, dir_=1, x=5, y=7, seq=2))
        self.assertEqual(len(b.consegnati), 2)          # originale + copia
        for e in b.consegnati:
            self.assertEqual(e[0], EV_STEP)             # slot 0: nibble 0
        b2 = BridgeFinto()
        b2.wire_copies = 2
        b2.heal_and_deliver(9, evento(EV_SYNC))         # slot 0
        b2.heal_and_deliver(4, evento(EV_SYNC))         # slot 1
        b2.consegnati.clear()
        b2.heal_and_deliver(4, evento(EV_STEP, dir_=1, x=5, y=7, seq=2))
        for e in b2.consegnati:
            self.assertEqual(e[0], EV_STEP | (1 << 4))

    def test_quarto_amico_senza_avatar(self):
        b = BridgeFinto()
        for peer in (1, 2, 3):
            b.heal_and_deliver(peer, evento(EV_SYNC))
        b.consegnati.clear()
        b.heal_and_deliver(4, evento(EV_SYNC))
        self.assertEqual(b.consegnati, [])              # niente al GBA
        self.assertEqual(b.slot_scartati, 1)            # ma contato
        # la mappa live lo vede comunque
        self.assertIn(4, b.pos_amici)
        # e l'avviso e' UNA riga, non una per evento
        b.heal_and_deliver(4, evento(EV_SYNC, seq=2))
        avvisi = [r for r in b.righe if "SENZA avatar" in r]
        self.assertEqual(len(avvisi), 1)

    def test_bye_libera_lo_slot_e_timbra_il_via(self):
        b = BridgeFinto()
        b.heal_and_deliver(7, evento(EV_SYNC))          # slot 0
        b.heal_and_deliver(3, evento(EV_SYNC))          # slot 1
        b.peer_room[3] = 0x0002
        b.consegnati.clear()
        # il T_BYE del peer 3 (handle_udp e' grosso: si simula il suo effetto
        # chiamando le stesse righe - qui si prova la POLITICA dello slot, e
        # la si prova dal punto d'ingresso vero piu' piccolo possibile)
        import protocol
        datagram = protocol.pack(protocol.T_BYE, 3, 7, 0)
        b.pending_ping = {}
        b.handle_udp(datagram, 0.0)
        self.assertEqual(len(b.consegnati), 1)
        via = b.consegnati[0]
        self.assertEqual(via[0], EV_LEAVE | (1 << 4))   # timbrato slot 1
        self.assertEqual(b.slot_free, [1, 2])           # lo slot e' tornato
        # il prossimo amico lo riprende
        b.heal_and_deliver(9, evento(EV_SYNC))
        self.assertEqual(b.peer_slots[9], 1)

    def test_bye_senza_slot_non_consegna(self):
        b = BridgeFinto()
        for peer in (1, 2, 3):
            b.heal_and_deliver(peer, evento(EV_SYNC))
        b.heal_and_deliver(4, evento(EV_SYNC))          # quarto: senza slot
        b.peer_room[4] = 0x0002
        b.consegnati.clear()
        import protocol
        b.pending_ping = {}
        b.handle_udp(protocol.pack(protocol.T_BYE, 4, 7, 0), 0.0)
        self.assertEqual(b.consegnati, [])              # niente VIA di slot 0!

    # --- il VA-E-VIENI (2026-08-30) --------------------------------------
    #
    # Il 30/08 un amico col Pico staccato ha fatto per dieci minuti: prende
    # uno slot, sparisce dieci secondi dopo, riprende lo slot trenta secondi
    # dopo. Il registro non lo diceva - alternava "amico N -> avatar slot 0" e
    # "peer N se n'e' andato", due righe che PRESE UNA PER UNA sono normali.
    # La cura vera sta dall'altra parte (bridge.js non ripubblica piu' la
    # presenza a adattatore morto, e manda un congedo); questa e' la spia che
    # rende il ciclo visibile a chi legge il log, ed e' quella che mancava.

    def _bye(self, b, peer, room=0x0002):
        import protocol
        b.peer_room[peer] = room
        b.pending_ping = {}
        b.handle_udp(protocol.pack(protocol.T_BYE, peer, 7, 0), 0.0)

    def test_va_e_vieni_riconosciuto_e_detto(self):
        b = BridgeFinto()
        b.heal_and_deliver(7, evento(EV_SYNC))
        self._bye(b, 7)
        b.righe.clear()
        b.heal_and_deliver(7, evento(EV_SYNC))          # torna subito
        self.assertEqual(b.flap, 1)
        self.assertTrue(any("VA E VIENE" in r for r in b.righe),
                        "il ritorno lampo non e' stato detto: %s" % b.righe)
        # e NON si dice la riga normale, che darebbe l'idea di un amico nuovo
        self.assertFalse(any("-> avatar slot" in r for r in b.righe))

    def test_ritorno_lento_e_un_amico_normale(self):
        """Chi torna dopo un bel po' non e' un ciclo: e' uno che si e'
        ricollegato, e va detto con la riga di sempre. Senza questo caso la
        spia nuova finirebbe per gridare a ogni riconnessione legittima."""
        b = BridgeFinto()
        b.heal_and_deliver(7, evento(EV_SYNC))
        self._bye(b, 7)
        b.peer_andato_at[7] -= (b.FLAP_S + 1.0)         # se n'e' andato un pezzo fa
        b.righe.clear()
        b.heal_and_deliver(7, evento(EV_SYNC))
        self.assertEqual(b.flap, 0)
        self.assertTrue(any("-> avatar slot" in r for r in b.righe))

    def test_va_e_vieni_non_intasa_il_registro(self):
        """A transizione: la prima volta e poi ogni dieci. Un ciclo che dura
        dieci minuti non deve produrre una riga ogni trenta secondi."""
        b = BridgeFinto()
        for _ in range(12):
            b.heal_and_deliver(7, evento(EV_SYNC))
            self._bye(b, 7)
        self.assertEqual(b.flap, 11)                    # il primo giro non conta
        righe = [r for r in b.righe if "VA E VIENE" in r]
        self.assertEqual(len(righe), 2)                 # la n. 1 e la n. 10


if __name__ == "__main__":
    unittest.main()
