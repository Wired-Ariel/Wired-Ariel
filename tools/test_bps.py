#!/usr/bin/env python3
"""
test_bps.py - l'applicatore BPS provato contro patch costruite a mano.

Il test porta con se' un MINI-CODIFICATORE indipendente (scrive le quattro
azioni del formato secondo la specifica di byuu): cosi' il decodificatore
viene provato su tutte le azioni - SourceRead, TargetRead, SourceCopy anche
all'indietro, TargetCopy sovrapposto (RLE) - e sui tre CRC32 di guardia.

    python tools\\test_bps.py
"""

import os
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bps import applica


def _num(x):
    out = bytearray()
    while True:
        b = x & 0x7F
        x >>= 7
        if x == 0:
            out.append(b | 0x80)
            return bytes(out)
        out.append(b)
        x -= 1


def _con_segno(delta):
    return _num((abs(delta) << 1) | (1 if delta < 0 else 0))


def codifica(sorgente, azioni, dim_risultato):
    """Assembla una patch BPS dalle azioni date (per il test)."""
    corpo = bytearray(b'BPS1')
    corpo += _num(len(sorgente)) + _num(dim_risultato) + _num(0)
    risultato = bytearray()
    rel_s = rel_r = 0
    for az in azioni:
        if az[0] == 'source_read':
            n = az[1]
            corpo += _num(((n - 1) << 2) | 0)
            risultato += sorgente[len(risultato):len(risultato) + n]
        elif az[0] == 'target_read':
            dati = az[1]
            corpo += _num(((len(dati) - 1) << 2) | 1) + dati
            risultato += dati
        elif az[0] == 'source_copy':
            n, da = az[1], az[2]
            corpo += _num(((n - 1) << 2) | 2) + _con_segno(da - rel_s)
            risultato += sorgente[da:da + n]
            rel_s = da + n
        elif az[0] == 'target_copy':
            n, da = az[1], az[2]
            corpo += _num(((n - 1) << 2) | 3) + _con_segno(da - rel_r)
            for _ in range(n):
                risultato.append(risultato[da])
                da += 1
            rel_r = da
    assert len(risultato) == dim_risultato
    corpo += zlib.crc32(sorgente).to_bytes(4, 'little')
    corpo += zlib.crc32(bytes(risultato)).to_bytes(4, 'little')
    corpo += zlib.crc32(bytes(corpo)).to_bytes(4, 'little')
    return bytes(corpo), bytes(risultato)


class TestBps(unittest.TestCase):
    SORGENTE = bytes(range(64)) * 4          # 256 byte riconoscibili

    def test_tutte_le_azioni(self):
        patch, atteso = codifica(self.SORGENTE, [
            ('source_read', 16),             # identico all'inizio
            ('target_read', b'NUOVI BYTE!!'),
            ('source_copy', 8, 32),          # copia da piu' avanti
            ('source_copy', 8, 0),           # e poi da piu' INDIETRO
            ('target_copy', 24, 16),         # RLE sovrapposto sul risultato
            ('source_read', 4),
        ], 16 + 12 + 8 + 8 + 24 + 4)
        self.assertEqual(applica(self.SORGENTE, patch), atteso)

    def test_sorgente_sbagliato(self):
        patch, _ = codifica(self.SORGENTE, [('source_read', 16)], 16)
        altro = b'\x00' * len(self.SORGENTE)
        with self.assertRaisesRegex(ValueError, 'sorgente'):
            applica(altro, patch)

    def test_patch_corrotta(self):
        patch, _ = codifica(self.SORGENTE, [('source_read', 16)], 16)
        rotta = patch[:8] + bytes([patch[8] ^ 0xFF]) + patch[9:]
        with self.assertRaisesRegex(ValueError, 'corrotta|crc'):
            applica(self.SORGENTE, rotta)

    def test_magic_sbagliato(self):
        with self.assertRaisesRegex(ValueError, 'BPS'):
            applica(b'x', b'IPS1' + b'\x00' * 20)


if __name__ == '__main__':
    unittest.main(verbosity=2)
