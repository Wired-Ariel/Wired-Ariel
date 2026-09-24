"""Prova a secco della ricucitura e del doppio invio di net/client.py.

Niente socket, niente hardware: si costruisce un Bridge finto con il solo
stato che i due metodi usano, gli si danno in pasto sequenze di eventi e si
guarda cosa esce verso il gioco.

Serve a tenere fermi due contratti che dall'altra parte del filo non si
vedono, e che si sono gia' rotti una volta a testa:

  1. SI RICUCE SOLO IN LINEA RETTA. Le movement action del remoto non
     controllano le collisioni: un rammendo a L taglia l'angolo attraverso i
     muri che il giocatore vero ha aggirato (visto in M-1).
  2. OGNI PASSO SINTETICO HA UN seq DIVERSO. Il payload scarta le copie del
     doppio invio confrontando il seq con quello dell'ultimo evento consumato:
     tre passi ricuciti con lo stesso numero verrebbero visti come un evento e
     due copie, e la ricucitura sparirebbe in silenzio.

Uso:  python tools\test_ricucitura.py
"""
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir, "net"))
from client import Bridge  # noqa: E402

FMT = "<BBBBBBhhBB"


def ev(kind, direction, x, y, mapg=0, mapn=2, speed=1, seq=7):
    return struct.pack(FMT, kind, direction, speed, seq, mapg, mapn, x, y, 0, 0)


class Fake(Bridge):
    def __init__(self, copie=1, perdita=0.0):
        # niente super(): solo lo stato che i metodi sotto prova toccano
        self.peer_pos = {}
        self.healed = 0
        self.heal_far = 0
        self.no_heal = False
        self.wire_copies = copie
        self.wire_loss = perdita
        self.wire_sent = 0
        self.wire_dropped = 0
        self.copies_skipped = 0
        # secchiello pieno e fermo: qui si prova la ridondanza, non il tetto
        self.copy_tokens = 1e9
        self.copy_stamp = 0.0
        # La mappa live e le transizioni di stato (client.py, 2026-08-21)
        # leggono questi campi dentro heal_and_deliver: senza, il Fake cadeva
        # con AttributeError prima del primo caso (trovato il 2026-08-23).
        self.pos_io = None
        self.pos_amici = {}
        self.pos_sporche = False
        self.stato_amici = {}
        self.out = []
        # Gli slot avatar (client.py, 2026-08-25, fino a 4 giocatori):
        # heal_and_deliver chiama slot_for() come PRIMA cosa, quindi senza
        # questi campi il Fake cade con AttributeError prima ancora del primo
        # caso - ed e' quello che faceva, da allora fino al 2026-08-28.
        # Stessa lezione della riga qui sopra: un Fake che elenca a mano lo
        # stato del vero Bridge va aggiornato quando il vero Bridge cresce, e
        # a dirlo e' solo il fatto di lanciare il test.
        self.peer_slots = {}
        self.slot_free = [0, 1, 2]
        self.slot_pieni_avvisati = set()
        self.slot_scartati = 0

    def log(self, msg):
        pass

    def _deliver_one(self, event):
        self.out.append(event)


def descr(e):
    k, d, _s, _q, _g, _n, x, y, _, _ = struct.unpack(FMT, e)
    return (k, d, x, y)


def dedup_payload(eventi):
    """Il dedup del payload, riscritto qui: e' l'altra meta' del contratto.

    Scarta un PASSO/GIRA il cui seq e' uguale a quello dell'ultimo PASSO/GIRA
    CONSUMATO (ConsumeRemoteEvents, blocco DOPPIO INVIO).
    """
    last = None
    out = []
    for e in eventi:
        if e[0] in (1, 3):
            if last is not None and e[3] == last:
                continue
            last = e[3]
        out.append(e)
    return out


tutti = True


def run_case(nome, eventi, attesi_out, attesi_healed, attesi_far=0):
    f = Fake()
    for e in eventi:
        f.heal_and_deliver(1, e)
    got = [descr(e) for e in f.out]
    ok = got == attesi_out and f.healed == attesi_healed and f.heal_far == attesi_far
    print(("ok  " if ok else "FAIL") + " " + nome)
    if not ok:
        print("   atteso:", attesi_out, "healed", attesi_healed, "far", attesi_far)
        print("   avuto :", got, "healed", f.healed, "far", f.heal_far)
    global tutti
    tutti = tutti and ok
    return f


# --- la ricucitura -----------------------------------------------------------

run_case("passi contigui, nessuna sintesi",
         [ev(1, 4, 11, 16), ev(1, 4, 12, 16)],
         [(1, 4, 11, 16), (1, 4, 12, 16)], 0)

run_case("passo perso in retta -> 1 sintetico",
         [ev(2, 4, 10, 16), ev(1, 4, 12, 16)],
         [(2, 4, 10, 16), (1, 4, 11, 16), (1, 4, 12, 16)], 1)

run_case("buco a L -> nessuna sintesi (regola della linea retta)",
         [ev(2, 4, 10, 16), ev(1, 1, 11, 17)],
         [(2, 4, 10, 16), (1, 1, 11, 17)], 0)

run_case("SYNC con buco a L -> nessuna sintesi",
         [ev(2, 4, 10, 16), ev(2, 4, 12, 17)],
         [(2, 4, 10, 16), (2, 4, 12, 17)], 0)

run_case("buco contro la direzione del passo -> nessuna sintesi",
         [ev(2, 3, 14, 16), ev(1, 4, 12, 16)],
         [(2, 3, 14, 16), (1, 4, 12, 16)], 0)

run_case("SYNC dopo 2 passi persi -> 2 sintetici",
         [ev(2, 4, 10, 16), ev(2, 4, 12, 16)],
         [(2, 4, 10, 16), (1, 4, 11, 16), (1, 4, 12, 16), (2, 4, 12, 16)], 2)

run_case("cambio mappa -> nessuna sintesi",
         [ev(2, 4, 10, 16, mapn=2), ev(1, 3, 66, 16, mapn=32)],
         [(2, 4, 10, 16), (1, 3, 66, 16)], 0)

run_case("buco largo -> heal_far, nessuna sintesi",
         [ev(2, 4, 10, 16), ev(2, 4, 20, 16)],
         [(2, 4, 10, 16), (2, 4, 20, 16)], 0, attesi_far=1)

run_case("GIRA dopo un passo perso -> 1 sintetico",
         [ev(2, 4, 10, 16), ev(3, 2, 11, 16)],
         [(2, 4, 10, 16), (1, 4, 11, 16), (3, 2, 11, 16)], 1)

run_case("SYNC da fermo, nessuna sintesi",
         [ev(2, 4, 10, 16), ev(2, 4, 10, 16), ev(2, 4, 10, 16)],
         [(2, 4, 10, 16), (2, 4, 10, 16), (2, 4, 10, 16)], 0)

run_case("STATO in mezzo non rompe la catena",
         [ev(2, 4, 10, 16), ev(5, 2, 0, 0), ev(1, 4, 11, 16)],
         [(2, 4, 10, 16), (5, 2, 0, 0), (1, 4, 11, 16)], 0)

run_case("PASSO con direzione fuori tabella -> consegna diretta",
         [ev(2, 4, 10, 16), ev(1, 9, 12, 16)],
         [(2, 4, 10, 16), (1, 9, 12, 16)], 0)

# --- i seq dei passi sintetici e il dedup ------------------------------------

f = run_case("tre passi ricuciti (seq controllato sotto)",
             [ev(2, 4, 10, 16, seq=40), ev(1, 4, 14, 16, seq=44)],
             [(2, 4, 10, 16), (1, 4, 11, 16), (1, 4, 12, 16),
              (1, 4, 13, 16), (1, 4, 14, 16)], 3)
seq_visti = [e[3] for e in f.out]
ok = seq_visti == [40, 41, 42, 43, 44]
print(("ok  " if ok else "FAIL")
      + " i sintetici prendono i seq persi (S-gap..S-1)")
if not ok:
    print("   atteso: [40, 41, 42, 43, 44]")
    print("   avuto :", seq_visti)
tutti = tutti and ok

# La prova che conta: ricucitura + doppio invio + dedup del payload devono
# lasciare passare ESATTAMENTE gli eventi ricuciti, una volta ciascuno.
f = Fake(copie=2)
f.heal_and_deliver(1, ev(2, 4, 10, 16, seq=40))
f.heal_and_deliver(1, ev(1, 4, 14, 16, seq=44))
dopo = [descr(e) for e in dedup_payload(f.out)]
atteso = [(2, 4, 10, 16), (1, 4, 11, 16), (1, 4, 12, 16),
          (1, 4, 13, 16), (1, 4, 14, 16)]
# il SYNC non si duplica, i quattro passi si': 1 + 4*2 = 9 frame sul filo
ok = dopo == atteso and len(f.out) == 9 and f.wire_sent == 9
print(("ok  " if ok else "FAIL")
      + " ricucitura + copie x2 + dedup = ogni passo applicato una volta")
if not ok:
    print("   atteso:", atteso, "9 frame sul filo")
    print("   avuto :", dopo, len(f.out), "frame,", f.wire_sent, "scritti")
tutti = tutti and ok

# Con una copia persa su due, il passo arriva lo stesso: e' tutto il punto.
f = Fake(copie=2)
f.heal_and_deliver(1, ev(1, 4, 11, 16, seq=50))
persi_una = [f.out[0]]            # sopravvive solo la prima copia
f2 = Fake(copie=2)
f2.heal_and_deliver(1, ev(1, 4, 11, 16, seq=50))
persi_altra = [f2.out[1]]         # sopravvive solo la seconda
ok = (dedup_payload(persi_una) == [f.out[0]]
      and dedup_payload(persi_altra) == [f2.out[1]]
      and descr(f.out[0]) == descr(f2.out[1]))
print(("ok  " if ok else "FAIL")
      + " persa una copia qualunque delle due, il passo arriva comunque")
tutti = tutti and ok

# Il VIA (tipo 4) non si duplica MAI: il payload non lo deduplica, e due VIA
# vorrebbero dire despawnare due volte.
f = Fake(copie=2)
f.deliver(ev(4, 0, 0, 0, seq=0))
ok = len(f.out) == 1
print(("ok  " if ok else "FAIL") + " il VIA non viene mai duplicato")
tutti = tutti and ok

# Lo STATO idem: ha gia' le sue ripetizioni nel payload.
f = Fake(copie=2)
f.deliver(ev(5, 1, 0, 0, seq=9))
ok = len(f.out) == 1
print(("ok  " if ok else "FAIL") + " lo STATO non viene mai duplicato")
tutti = tutti and ok

# Il tetto delle copie: a secchiello vuoto il passo esce UNA volta sola e la
# copia saltata si conta. Senza questa prova il tetto sarebbe un ramo che non
# ha mai girato finche' qualcuno non sale sulla mach bike.
import time as _t  # noqa: E402
f = Fake(copie=2)
f.copy_tokens = 0.0
f.copy_stamp = _t.monotonic()
f.deliver(ev(1, 4, 11, 16, seq=60))
ok = len(f.out) == 1 and f.copies_skipped == 1
print(("ok  " if ok else "FAIL")
      + " a secchiello vuoto la copia si salta e si conta")
if not ok:
    print("   avuto:", len(f.out), "frame,", f.copies_skipped, "saltate")
tutti = tutti and ok

print()
print("TUTTO PASSATO" if tutti else "CI SONO FALLIMENTI")
sys.exit(0 if tutti else 1)
