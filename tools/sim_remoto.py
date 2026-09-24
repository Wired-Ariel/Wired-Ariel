"""Simulazione del remoto: perdita, doppio invio, rammendo, posa da fermo.

E' la prova su cui e' stata presa la decisione del blocco N (2026-08-02): il
buffer di riproduzione, che sembrava la cura giusta leggendo il codice, qui si
vede che non serve senza perdita e che PEGGIORA con la perdita - mentre il
doppio invio toglie quasi tutte le scivolate. Sta nel repo apposta: era il
pezzo che mancava per non provare l'ennesima ipotesi sull'hardware di Lain.

Il parametro `buffered` e' rimasto per poter rifare quel confronto (il codice
del buffer nel payload non c'e' piu').


Riproduce il ciclo vero del payload (ConsumeRemoteEvents + posa da fermo), un
frame alla volta:

  mittente : emette un PASSO ogni P frame, all'ISTANTE in cui il passo comincia
  canale   : ritardo variabile + PERDITA (l'ultimo tratto PC->GBA e' il SIO,
             che perde una parola ogni tanto e con essa tutto il frame)
  ricevente: applica il passo in testa alla coda quando il movimento precedente
             e' finito; se il passo arriva a 2-3 tile (un passo perso) fa un
             passo di RAMMENDO in 4 frame e tiene l'evento in coda

Artefatti contati, dentro la camminata (non la coda finale):
  episodi = quante volte il remoto si ferma mentre l'amico cammina
  frame   = per quanti frame in totale
  slide   = passi di rammendo (4 frame per tile: si vedono come una scivolata)
"""
import random

P = 16
HURRY = 4
PLAYOUT_HOLD = 22
WALK = 40
WALKS = 80


def run(buffered, dup, delay, jitter, loss, seed=7):
    rnd = random.Random(seed)
    tot_ep = tot_fr = tot_slide = 0

    for w in range(WALKS):
        # --- canale -------------------------------------------------------
        arr = []
        for k in range(WALK):
            copies = 2 if dup else 1
            for c in range(copies):
                if rnd.random() < loss:
                    continue
                # la copia parte subito dopo: un frame sul filo = 9 parole
                d = delay + rnd.randint(0, jitter) + c * 3
                arr.append((k * P + d, k))
        arr.sort()

        queue = []          # indici di passo (la posizione e' l'indice stesso)
        ai = 0
        busy = -1
        target = -1         # ultimo tile dichiarato dagli eventi consumati
        drawn = -1          # dove il remoto e' disegnato
        hold = 0
        settled = True
        last_seq = -1
        ep = fr = slide = 0
        in_gap = False
        f = 0
        end = WALK * P + delay + jitter + PLAYOUT_HOLD + 2 * P

        while f < end:
            while ai < len(arr) and arr[ai][0] <= f:
                k = arr[ai][1]
                if k != last_seq:          # dedup sul seq, come il payload
                    queue.append(k)
                    last_seq = k
                ai += 1

            pending = len(queue)
            head = queue[0] if pending else None
            # continuita': la testa e' a un tile da dove siamo diretti?
            cont = head is not None and abs(head - target) <= 1
            holding = False
            if buffered and pending == 1 and hold < PLAYOUT_HOLD and cont:
                hold += 1
                holding = True
            elif pending != 1:
                hold = 0

            free = f >= busy
            if free and pending and not holding:
                gap = head - target
                if gap > 1:
                    # RAMMENDO: un passo verso il traguardo, evento in coda
                    target += 1
                    drawn = target
                    busy = f + HURRY
                    slide += 1
                    settled = False
                    free = False
                else:
                    queue.pop(0)
                    target = head
                    drawn = head
                    busy = f + P
                    settled = False
                    free = False

            walking = f < WALK * P          # l'amico sta ancora camminando
            if free:
                if walking:
                    fr += 1
                    if not in_gap:
                        ep += 1
                        in_gap = True
                if not settled and (pending == 0 or holding):
                    settled = True
            else:
                in_gap = False
            f += 1

        tot_ep += ep
        tot_fr += fr
        tot_slide += slide
    return tot_ep, tot_fr, tot_slide


def run_drift(dup, delay, jitter, loss, run_max=3, seed=7):
    """Il RIALLINEAMENTO IN CAMMINO (2026-08-28), e il rischio che porta.

    Dal 2026-08-28 il payload riallinea il remoto anche mentre l'amico
    cammina, se lo scostamento resta li' per `run_max` SYNC di fila (il SYNC
    arriva ~1/s). Serve al caso "l'amico non si ferma mai e resta indietro per
    sempre", che prima nessuno correggeva.

    Ma il pericolo e' l'opposto: durante una camminata NORMALE il remoto e'
    quasi sempre indietro di un tile - e' la latenza, non un guasto - e
    teleportarlo li' sarebbe lo scatto da cheat che il progetto evita. Questa
    simulazione conta quante volte la regola scatterebbe in una camminata
    sana: `spurii` DEVE restare basso, o run_max e' troppo corto.

    Il modello del payload, punto per punto: il SYNC porta la posizione VERA
    del mittente all'istante in cui e' partito; il payload confronta con dove
    il remoto e' DISEGNATO; incrementa la corsa se lo scostamento c'e',
    l'azzera se non c'e'; e corregge solo quando nessun movimento e' in corso
    (RemoteReadyForMovement), che qui e' `free`.
    """
    rnd = random.Random(seed)
    tot_spurii = tot_veri = 0

    for w in range(WALKS):
        arr = []
        for k in range(WALK):
            for c in range(2 if dup else 1):
                if rnd.random() < loss:
                    continue
                arr.append((k * P + delay + rnd.randint(0, jitter) + c * 3, k, False))
        # i SYNC: uno ogni 60 frame, con la posizione vera di quel momento
        for t in range(0, WALK * P, 60):
            if rnd.random() < loss:
                continue
            vero = min(t // P, WALK - 1)
            arr.append((t + delay + rnd.randint(0, jitter), vero, True))
        arr.sort()

        queue = []
        ai = 0
        busy = -1
        target = -1
        last_seq = -1
        run = 0
        spurii = veri = 0
        f = 0
        end = WALK * P + delay + jitter + 2 * P

        while f < end:
            while ai < len(arr) and arr[ai][0] <= f:
                _, k, is_sync = arr[ai]
                ai += 1
                if is_sync:
                    drift = abs(k - target)
                    run = run + 1 if drift else 0
                    if drift and f >= busy and run >= run_max:
                        # e' il teleport: si conta e la corsa riparte
                        if drift <= 1 and len(queue):
                            spurii += 1   # aveva ancora eventi da applicare
                        else:
                            veri += 1
                        target = k
                        run = 0
                elif k != last_seq:
                    queue.append(k)
                    last_seq = k

            if f >= busy and queue:
                head = queue[0]
                gap = head - target
                if gap > 1:
                    target += 1
                    busy = f + HURRY
                else:
                    queue.pop(0)
                    target = head
                    busy = f + P
            f += 1

        tot_spurii += spurii
        tot_veri += veri
    return tot_spurii, tot_veri


def show(nome, **kw):
    print(nome)
    for etichetta, b, d in (("  oggi          ", False, False),
                            ("  doppio invio  ", False, True)):
        ep, fr, sl = run(b, d, **kw)
        print("%s episodi %4d (%5.2f/camminata)  frame fermi %5d  scivolate %4d"
              % (etichetta, ep, ep / WALKS, fr, sl))
    print()


N = WALKS * WALK
print("%d camminate da %d passi = %d passi\n" % (WALKS, WALK, N))
show("mGBA: ritardo 1, tremolio 1, perdita 0%",
     delay=1, jitter=1, loss=0.0)
show("GBA fisico: ritardo 3, tremolio 3, perdita 4%",
     delay=3, jitter=3, loss=0.04)

print("riallineamento in cammino: quante volte scatta, e quante a sproposito")
print("(spurii = con la coda ancora piena, cioe' il remoto stava gia' "
      "recuperando da solo)\n")
for etichetta, kw in (("mGBA        ", dict(delay=1, jitter=1, loss=0.0)),
                      ("GBA fisico  ", dict(delay=3, jitter=3, loss=0.04)),
                      ("linea pessima", dict(delay=8, jitter=6, loss=0.12))):
    for run_max in (2, 3, 5):
        sp, ve = run_drift(dup=True, run_max=run_max, **kw)
        print("  %s  run_max %d -> %4d scatti utili, %4d spurii "
              "(%.3f per camminata)" % (etichetta, run_max, ve, sp, sp / WALKS))
    print()
show("GBA fisico: ritardo 3, tremolio 3, perdita 8%",
     delay=3, jitter=3, loss=0.08)
show("GBA fisico: perdita 0% (solo tremolio) - il buffer da solo",
     delay=3, jitter=3, loss=0.0)
