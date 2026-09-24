#!/usr/bin/env python3
"""
test_deframer.py - unit test A SECCO del framer/de-framer di usb_link.py.

Sono i casi che sull'hardware costerebbero un giro con Lain e qui costano
un decimo di secondo: frame spezzati fra piu' read USB (succede SEMPRE, un
EVENT da 18 byte attraversa piu' flush da ~10 ms), zeri legittimi nei dati
(il punto della F-2), checksum rotti con riaggancio, rumore fra i frame.

    python test_deframer.py
"""

import struct
import unittest

from usb_link import (SioDeframer, sio_frame, SIO_SYNC, SIO_T_EVENT,
                      SIO_T_PING, EVENT_WORDS)


def words_of(frame_bytes):
    return [w for (w,) in struct.iter_unpack("<H", frame_bytes)]


class TestFramer(unittest.TestCase):

    def test_frame_ping_noto(self):
        # Lo stesso frame che il banco di prova ha verificato sull'hardware:
        # HEAD 0x0302, dati 0x1234 0xABCD -> la somma totale deve fare 0.
        raw = sio_frame(SIO_T_PING, [0x1234, 0xABCD])
        w = words_of(raw)
        self.assertEqual(w[0], SIO_SYNC)
        self.assertEqual(w[1], 0x0302)
        self.assertEqual((sum(w[1:])) & 0xFFFF, 0)

    def test_nessun_padding(self):
        # F-2: si mandano SOLO i byte veri. 5 parole = 10 byte, non 64.
        self.assertEqual(len(sio_frame(SIO_T_PING, [1, 2])), 10)


class TestDeframer(unittest.TestCase):

    def setUp(self):
        self.d = SioDeframer()

    def test_frame_pulito(self):
        out = self.d.feed(sio_frame(SIO_T_PING, [0x1111, 0x2222]))
        self.assertEqual(out, [(SIO_T_PING, [0x1111, 0x2222])])
        self.assertEqual(self.d.frame_err, 0)

    def test_frame_spezzato_su_tre_read(self):
        raw = sio_frame(SIO_T_EVENT, [1, 2, 3, 4, 5, 6])
        out = []
        out += self.d.feed(raw[:5])       # taglio a meta' parola, apposta
        out += self.d.feed(raw[5:11])
        out += self.d.feed(raw[11:])
        self.assertEqual(out, [(SIO_T_EVENT, [1, 2, 3, 4, 5, 6])])
        self.assertEqual(self.d.frame_err, 0)

    def test_zero_legittimo_nei_dati(self):
        # Il caso della F-2: x=0, y=0, dir=0 in un NetEvent sono dati veri.
        evento = [0x0001, 0x0000, 0x0000, 0x0000, 0x0000, 0x0100]
        out = self.d.feed(sio_frame(SIO_T_EVENT, evento))
        self.assertEqual(out, [(SIO_T_EVENT, evento)])
        self.assertEqual(self.d.frame_err, 0)

    def test_idle_e_rumore_fra_i_frame(self):
        rumore = struct.pack("<4H", 0x7FFF, 0xFFFF, 0x7FFF, 0x1234)
        out = self.d.feed(rumore + sio_frame(SIO_T_PING, [9]) +
                          rumore + sio_frame(SIO_T_PING, [10]))
        self.assertEqual(out, [(SIO_T_PING, [9]), (SIO_T_PING, [10])])
        self.assertEqual(self.d.frame_err, 0)
        self.assertEqual(self.d.idle_words, 6)

    def test_checksum_rotto_e_riaggancio(self):
        raw = bytearray(sio_frame(SIO_T_PING, [0x1111, 0x2222]))
        raw[-2] ^= 0xFF   # checksum sbagliato
        out = self.d.feed(bytes(raw))
        self.assertEqual(out, [])
        self.assertEqual(self.d.frame_err, 1)
        self.assertEqual(self.d.resync, 1)
        # il frame successivo passa: il canale si e' riagganciato
        out = self.d.feed(sio_frame(SIO_T_PING, [0x3333]))
        self.assertEqual(out, [(SIO_T_PING, [0x3333])])

    def test_testa_implausibile(self):
        # SYNC seguito da una testa con tipo 0: il riaggancio prudente di
        # sio.c la rifiuta senza consegnare niente.
        buf = struct.pack("<2H", SIO_SYNC, 0x0006)
        self.assertEqual(self.d.feed(buf), [])
        self.assertEqual(self.d.frame_err, 1)
        out = self.d.feed(sio_frame(SIO_T_PING, [5]))
        self.assertEqual(out, [(SIO_T_PING, [5])])

    def test_frame_senza_dati(self):
        head = (SIO_T_PING << 8) | 0
        cksum = (-head) & 0xFFFF
        buf = struct.pack("<3H", SIO_SYNC, head, cksum)
        self.assertEqual(self.d.feed(buf), [(SIO_T_PING, [])])

    def test_giro_completo_evento(self):
        # Come fara' il bridge: 12 byte -> frame -> byte "sul filo" -> 12 byte.
        evento12 = struct.pack("<BBBBBBhhBB", 1, 2, 0, 7, 0, 9, -3, 0, 1, 0)
        parole = [w for (w,) in struct.iter_unpack("<H", evento12)]
        raw = sio_frame(SIO_T_EVENT, parole)
        # sul filo arriva com'e' comodo al firmware: un byte per volta
        out = []
        for i in range(len(raw)):
            out += self.d.feed(raw[i:i + 1])
        self.assertEqual(len(out), 1)
        ftype, words = out[0]
        self.assertEqual(ftype, SIO_T_EVENT)
        self.assertEqual(len(words), EVENT_WORDS)
        ricostruito = b"".join(w.to_bytes(2, "little") for w in words)
        self.assertEqual(ricostruito, evento12)
        self.assertEqual(self.d.frame_err, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
