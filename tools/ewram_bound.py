# W''-1: dimostrare dove finisce la memoria EWRAM del GIOCO, sulla ROM italiana.
#
# PERCHE' SERVE
# -------------
# Il payload vive nella coda di EWRAM. Fin qui il confine (0x0203CF64) veniva dal
# .map della build USA di pokeemerald, e per l'italiana era un'ASSUNZIONE. Il
# piano W'' pretende che ogni byte che prendiamo sia libero per un fatto del
# linker, non per un'osservazione: quindi l'assunzione va tolta di mezzo.
#
# PERCHE' NON BASTA CERCARE I NUMERI CHE "SEMBRANO" INDIRIZZI
# ----------------------------------------------------------
# Una scansione di tutte le parole allineate nell'intervallo EWRAM trova 37
# valori sopra 0x0203CF64 in ENTRAMBE le ROM. Sono falsi positivi: dati grafici
# (identici fra le due versioni, che differiscono solo nei testi) che per caso
# somigliano a un puntatore. Contarli come riferimenti direbbe che il gioco usa
# memoria che non usa, e ci farebbe rinunciare a spazio che ci serve.
#
# COSA FA INVECE
# --------------
# Cerca le istruzioni che MATERIALIZZANO una costante a 32 bit, che su ARMv4T
# sono le uniche due forme possibili:
#
#   Thumb   ldr rD, [pc, #imm8*4]     0100 1DDD IIII IIII
#   ARM     ldr rD, [pc, #imm12]      cccc 0101 U0011111 DDDD IIIIIIIIIIII
#
# e legge la parola a cui puntano. Un oggetto in EWRAM che il gioco tocca deve
# avere il proprio indirizzo base materializzato da qualche parte: non esiste
# altro modo di ottenere un puntatore assoluto in questo codice.
#
# LIMITE, DETTO SUBITO
# --------------------
# L'insieme dei BASE materializzati non e' un limite superiore degli INDIRIZZI
# TOCCATI: gHeap ha base 0x02000000 e il gioco ci scrive fino a 0x0201C000. Per
# questo il risultato di qui non vive da solo - vale insieme al .map, che da'
# la dimensione di ogni simbolo, e al fatto che il gioco non ha crescita
# dinamica (tutti gli InitHeap passano gHeap/HEAP_SIZE). Qui si dimostra una
# cosa sola, ed e' quella che mancava: che la ROM italiana non materializzi
# indirizzi che quella USA non ha.

import os
import sys

USA_ROM = r"D:\Progettini\GBA-USB\repo-studio\pokeemerald\pokeemerald.gba"

# La ROM italiana SI CERCA, non si scrive a mano. Fino al 2026-08-28 qui c'era
# un solo percorso fisso sul Desktop, e quando la cartuccia dumpata e' finita
# sul disco di backup questo strumento e' morto in silenzio - cioe' proprio la
# prova su cui poggia PAYLOAD_BASE non era piu' rilanciabile. Si accetta anche
# un percorso sulla riga di comando, e in ogni caso si controlla il gamecode:
# BPEI o si rifiuta, perche' misurare la ROM sbagliata darebbe un "PASSATO" che
# non vuol dire niente.
IT_CANDIDATI = [
    r"D:\Rom\[PKWWF]smeraldo.gba",
        os.path.join(os.path.expanduser("~"), "Desktop", "Pokemon - Versione Smeraldo (Italy).gba"),
]


def trova_it():
    for p in ([sys.argv[1]] if len(sys.argv) > 1 else []) + IT_CANDIDATI:
        if not os.path.exists(p):
            continue
        with open(p, "rb") as f:
            f.seek(0xAC)
            if f.read(4) == b"BPEI":
                return p
    print("ROM italiana (BPEI) non trovata. Cercata in:")
    for p in IT_CANDIDATI:
        print("   %s" % p)
    print("Passala sulla riga di comando: python tools/ewram_bound.py <rom.gba>")
    sys.exit(2)


IT_ROM = None   # risolto in main()

ROM_BASE = 0x08000000
EWRAM_LO, EWRAM_HI = 0x02000000, 0x02040000

# Fine della sezione `ewram` del gioco, dal .map della build matching di
# pokeemerald: `ewram 0x02000000 0x3cf64`. L'ultimo simbolo e' ewram_data di
# src/rayquaza_scene.o a 0x0203CF60, 4 byte.
EWRAM_SECTION_END = 0x0203CF64

# Fine della regione di CODICE. Oltre questo indirizzo la ROM e' fatta di dati -
# grafica compressa e campioni audio - e li' dentro le sequenze di byte che
# somigliano a istruzioni sono inevitabili.
#
# Non e' una soglia messa per far tornare il risultato: senza il filtro
# sopravvive UN candidato, 0x0203DFA2, identico nelle due ROM. Cercato nel .map,
# l'istruzione che lo caricherebbe sta a 0x088305AA, dentro `.rodata` di
# data/sound_data.o - 2,6 MB di campioni audio. E' un suono, non un'istruzione.
#
# Il valore: nella build USA la prima .rodata grossa (src/data.o) comincia a
# 0x082FF1D8, e l'italiana ha lo stesso impianto (m4aSoundInit a 0x082E0FFC
# contro 0x082E0070). 0x08300000 sta sopra il codice di entrambe e sotto tutti i
# blob di dati.
#
# Lo script stampa SEMPRE quanti candidati il filtro ha scartato: se un giorno
# quel numero cambia, si guarda, non si alza la soglia.
CODE_END = 0x08300000


def read_rom(path):
    with open(path, "rb") as f:
        return f.read()


def word(d, off):
    return int.from_bytes(d[off:off + 4], "little")


def literal_targets(d):
    """Indirizzi EWRAM materializzati da una load PC-relative dentro il codice.

    Ritorna ({valore: [offset dell'istruzione, ...]}, scartati_fuori_dal_codice).
    """
    out = {}
    dropped = 0
    n = min(len(d), CODE_END - ROM_BASE)
    total = len(d)

    # --- Thumb: ldr rD, [pc, #imm8*4] -------------------------------------
    # L'indirizzo del letterale e' (PC & ~3) + imm8*4, con PC = istruzione + 4.
    for off in range(0, total - 1, 2):
        h = d[off] | (d[off + 1] << 8)
        if (h & 0xF800) != 0x4800:
            continue
        lit = ((off + 4) & ~3) + (h & 0xFF) * 4
        if lit + 4 > total:
            continue
        v = word(d, lit)
        if not (EWRAM_LO <= v < EWRAM_HI):
            continue
        if off < n:
            out.setdefault(v, []).append(off)
        elif v > EWRAM_SECTION_END:
            dropped += 1

    # --- ARM: ldr rD, [pc, #imm12] ----------------------------------------
    # cond 010 P=1 U B=0 W=0 L=1, Rn=15. Il bit U (23) resta fuori dalla
    # maschera perche' l'offset puo' essere sottratto.
    for off in range(0, total - 3, 4):
        w = word(d, off)
        if (w & 0x0F7F0000) != 0x051F0000:
            continue
        imm = w & 0xFFF
        lit = (off + 8) + imm if (w >> 23) & 1 else (off + 8) - imm
        if lit < 0 or lit + 4 > total:
            continue
        v = word(d, lit)
        if not (EWRAM_LO <= v < EWRAM_HI):
            continue
        if off < n:
            out.setdefault(v, []).append(off)
        elif v > EWRAM_SECTION_END:
            dropped += 1

    return out, dropped


def report(name, path):
    d = read_rom(path)
    hits, dropped = literal_targets(d)
    above = sorted(v for v in hits if v > EWRAM_SECTION_END)

    print("%s  (%s)" % (name, path))
    print("  indirizzi EWRAM materializzati da load PC-relative: %d" % len(hits))
    print("  scartati perche' fuori dalla regione di codice: %d" % dropped)
    print("  massimo: 0x%08X" % max(hits))
    print("  sopra 0x%08X (fine della sezione ewram): %d" % (EWRAM_SECTION_END, len(above)))
    for v in above:
        offs = hits[v][:3]
        print("     0x%08X  da %s%s" % (
            v,
            ", ".join("ROM+0x%X" % o for o in offs),
            " ..." if len(hits[v]) > 3 else ""))
    print()
    return hits


def main():
    usa = report("USA (BPEE)", USA_ROM)
    it  = report("IT  (BPEI)", trova_it())

    print("=== CONFRONTO ===")
    only_it = sorted(v for v in it if v not in usa)
    only_usa = sorted(v for v in usa if v not in it)
    print("  solo nell'italiana: %d" % len(only_it))
    print("  solo nell'americana: %d" % len(only_usa))

    # Cio' che conta davvero: l'italiana non deve materializzare NIENTE sopra il
    # confine che il .map della USA dichiara.
    it_above = sorted(v for v in it if v > EWRAM_SECTION_END)
    usa_above = sorted(v for v in usa if v > EWRAM_SECTION_END)

    print()
    if not it_above and not usa_above:
        print("PASSATO: nessuna delle due ROM materializza un indirizzo sopra")
        print("0x%08X. Il confine del .map vale anche per l'italiana," % EWRAM_SECTION_END)
        print("e la coda 0x%08X-0x%08X e' di nessuno." % (EWRAM_SECTION_END, EWRAM_HI))
        return 0

    print("NON PASSATO: ci sono indirizzi sopra il confine.")
    print("USA: %s" % ", ".join("0x%08X" % v for v in usa_above))
    print("IT : %s" % ", ".join("0x%08X" % v for v in it_above))
    print()
    print("Vanno risolti a mano PRIMA di abbassare PAYLOAD_BASE: ognuno va")
    print("guardato nel disassemblato per capire se e' codice vero o un dato")
    print("che somiglia a un'istruzione. Non si aggira alzando una soglia.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
