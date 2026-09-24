#!/usr/bin/env python3
"""
bps.py - applica una patch BPS a un file. Solo libreria standard.

Perche' esiste: le distribuzioni ricreate dalla community (Goppier,
Gen3DistributionRoms) sono patch .bps sopra la ROM di distribuzione
originale. Lo strumento canonico e' Flips, ma e' un download in piu' e un
eseguibile in piu': il formato BPS e' semplice e documentato (byuu), quindi
lo applichiamo da soli, con la verifica dei tre CRC32 che il formato porta
con se' (sorgente, risultato, patch stessa).

    python bps.py <sorgente> <patch.bps> <destinazione>
"""

import sys
import zlib


def _uvarint(dati, pos):
    """La codifica a lunghezza variabile del formato BPS."""
    x, shift = 0, 1
    while True:
        b = dati[pos]
        pos += 1
        x += (b & 0x7F) * shift
        if b & 0x80:
            return x, pos
        shift <<= 7
        x += shift


def applica(sorgente, patch):
    """Applica la patch e restituisce i byte del risultato.

    Alza ValueError se la patch non e' BPS o se uno dei tre CRC32 non
    torna: in quel caso o la patch e' rotta o il file sorgente non e'
    QUELLO per cui la patch e' stata fatta (il caso tipico: dump diverso).
    """
    if patch[:4] != b'BPS1':
        raise ValueError('non e\' una patch BPS (magic %r)' % patch[:4])
    crc_sorgente, crc_risultato, crc_patch = (
        int.from_bytes(patch[-12:-8], 'little'),
        int.from_bytes(patch[-8:-4], 'little'),
        int.from_bytes(patch[-4:], 'little'))
    if zlib.crc32(patch[:-4]) != crc_patch:
        raise ValueError('la patch stessa e\' corrotta (crc32 patch)')
    if zlib.crc32(sorgente) != crc_sorgente:
        raise ValueError(
            'il file sorgente non e\' quello atteso dalla patch: '
            'crc32 %08X, atteso %08X'
            % (zlib.crc32(sorgente), crc_sorgente))

    pos = 4
    dim_sorgente, pos = _uvarint(patch, pos)
    dim_risultato, pos = _uvarint(patch, pos)
    dim_meta, pos = _uvarint(patch, pos)
    pos += dim_meta
    fine_azioni = len(patch) - 12

    out = bytearray(dim_risultato)
    scritto = 0                  # outputOffset
    rel_sorgente = 0             # sourceRelativeOffset
    rel_risultato = 0            # targetRelativeOffset
    while pos < fine_azioni:
        dato, pos = _uvarint(patch, pos)
        azione, lunghezza = dato & 3, (dato >> 2) + 1
        if azione == 0:          # SourceRead: dal sorgente, stessa posizione
            out[scritto:scritto + lunghezza] = \
                sorgente[scritto:scritto + lunghezza]
            scritto += lunghezza
        elif azione == 1:        # TargetRead: byte nuovi, dalla patch
            out[scritto:scritto + lunghezza] = patch[pos:pos + lunghezza]
            pos += lunghezza
            scritto += lunghezza
        elif azione == 2:        # SourceCopy: dal sorgente, posizione mobile
            salto, pos = _uvarint(patch, pos)
            rel_sorgente += (-1 if salto & 1 else 1) * (salto >> 1)
            out[scritto:scritto + lunghezza] = \
                sorgente[rel_sorgente:rel_sorgente + lunghezza]
            rel_sorgente += lunghezza
            scritto += lunghezza
        else:                    # TargetCopy: dal risultato, PUO' sovrapporsi
            salto, pos = _uvarint(patch, pos)
            rel_risultato += (-1 if salto & 1 else 1) * (salto >> 1)
            for _ in range(lunghezza):   # byte a byte: e' la semantica RLE
                out[scritto] = out[rel_risultato]
                scritto += 1
                rel_risultato += 1

    if scritto != dim_risultato:
        raise ValueError('patch incompleta: scritti %d byte su %d'
                         % (scritto, dim_risultato))
    if zlib.crc32(bytes(out)) != crc_risultato:
        raise ValueError('risultato col crc32 sbagliato: patch o sorgente rotti')
    if dim_sorgente != len(sorgente):
        # dopo i crc e' ridondante, ma un BPS coerente la dichiara giusta
        raise ValueError('dimensione sorgente dichiarata %d, reale %d'
                         % (dim_sorgente, len(sorgente)))
    return bytes(out)


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    sorgente = open(sys.argv[1], 'rb').read()
    patch = open(sys.argv[2], 'rb').read()
    out = applica(sorgente, patch)
    open(sys.argv[3], 'wb').write(out)
    print('ok: %s (%d byte, crc32 %08X)'
          % (sys.argv[3], len(out), zlib.crc32(out)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
