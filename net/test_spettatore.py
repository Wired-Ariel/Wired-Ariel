#!/usr/bin/env python3
"""
test_spettatore.py - regressione A SECCO dello SPETTATORE (T_WATCH, 2026-08-26).

Chi gioca in emulatore vuole aprire il sito per vedere la mappa live senza
rubare un posto alla partita: entra con T_WATCH invece di T_HELLO e il relay
lo tratta da spettatore - riceve i broadcast, NON conta nel tetto dei 4, i
suoi eventuali eventi non vengono inoltrati, la sua uscita non produce un
T_BYE per gli altri.

Ogni caso ha un criterio esplicito; il log del relay e' parte del criterio
(regola del progetto: una correzione che non si vede nel log non e'
verificabile).

    D:\\Progettini\\Python313\\python.exe net\\test_spettatore.py
"""

import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from protocol import T_BYE, T_EVENT, T_HELLO, T_PING, T_WATCH, pack  # noqa: E402

PY = sys.executable
RELAY_PORT = 19012
ROOM = 47031
EVENT = bytes(range(12))


def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(2.0)
    return s


def recv_or_none(sock, timeout=1.0):
    sock.settimeout(timeout)
    try:
        return sock.recvfrom(2048)[0]
    except socket.timeout:
        return None


def scarica(sock):
    """Svuota la coda: prima di un criterio 'non deve arrivare niente' non
    devono restare in giro pacchetti di un passo precedente."""
    while recv_or_none(sock, timeout=0.15) is not None:
        pass


def main():
    relay_addr = ("127.0.0.1", RELAY_PORT)
    proc = subprocess.Popen(
        [PY, "relay.py", "--port", str(RELAY_PORT)], cwd=HERE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    ok = True
    try:
        time.sleep(0.5)
        if proc.poll() is not None:
            raise RuntimeError("il relay e' morto in avvio")

        # --- 1. lo spettatore riceve e NON occupa un posto -----------------
        giocatori = [udp() for _ in range(4)]
        for i, s in enumerate(giocatori, start=1):
            s.sendto(pack(T_HELLO, i, ROOM, 0), relay_addr)
        spett = udp()
        spett.sendto(pack(T_WATCH, 90, ROOM, 0), relay_addr)
        time.sleep(0.3)

        giocatori[0].sendto(pack(T_EVENT, 1, ROOM, 1, EVENT), relay_addr)
        for i in (1, 2, 3):
            assert recv_or_none(giocatori[i]) is not None, (
                "l'evento non arriva al giocatore %d" % (i + 1))
        assert recv_or_none(spett) is not None, (
            "lo SPETTATORE non riceve gli eventi: e' la ragione per cui esiste")
        print("PASSO 1: 4 giocatori + 1 spettatore, e lo spettatore riceve")

        # ...e il quinto GIOCATORE vero resta comunque fuori.
        quinto = udp()
        quinto.sendto(pack(T_HELLO, 5, ROOM, 0), relay_addr)
        time.sleep(0.2)
        scarica(quinto)
        giocatori[0].sendto(pack(T_EVENT, 1, ROOM, 2, EVENT), relay_addr)
        assert recv_or_none(quinto, timeout=0.5) is None, (
            "il QUINTO giocatore e' entrato: lo spettatore non doveva liberare "
            "un posto, ma il tetto dei 4 giocatori resta")
        print("PASSO 2: il quinto GIOCATORE resta fuori (il tetto vale ancora)")

        # --- 3. eventi dello spettatore: non inoltrati ---------------------
        for s in giocatori:
            scarica(s)
        spett.sendto(pack(T_EVENT, 90, ROOM, 1, EVENT), relay_addr)
        assert recv_or_none(giocatori[0], timeout=0.5) is None, (
            "l'evento dello SPETTATORE e' stato inoltrato")
        print("PASSO 3: gli eventi dello spettatore non vengono inoltrati")

        # --- 4. niente T_BYE ai giocatori quando lo spettatore se ne va ----
        for s in giocatori:
            scarica(s)
        spett.sendto(pack(T_BYE, 90, ROOM, 0), relay_addr)
        assert recv_or_none(giocatori[0], timeout=0.5) is None, (
            "i giocatori hanno ricevuto un T_BYE per lo spettatore: "
            "despawnerebbero un avatar che non e' mai esistito")
        print("PASSO 4: l'uscita dello spettatore non manda BYE ai giocatori")

        for s in giocatori + [spett, quinto]:
            s.close()

        # --- 5. ordine inverso: lo spettatore entra PRIMA ------------------
        room2 = ROOM + 1
        s0 = udp()
        s0.sendto(pack(T_WATCH, 91, room2, 0), relay_addr)
        time.sleep(0.2)
        g2 = [udp() for _ in range(4)]
        for i, s in enumerate(g2, start=1):
            s.sendto(pack(T_HELLO, i, room2, 0), relay_addr)
        time.sleep(0.3)
        scarica(g2[3])
        g2[0].sendto(pack(T_EVENT, 1, room2, 1, EVENT), relay_addr)
        assert recv_or_none(g2[3]) is not None, (
            "il QUARTO giocatore e' rimasto fuori: lo spettatore entrato per "
            "primo si e' preso un posto (il tetto non e' dinamico)")
        print("PASSO 5: spettatore entrato per primo, il 4o giocatore entra lo stesso")

        # --- 6. rientro-NAT non incrociato --------------------------------
        # Lo spettatore usa lo STESSO peer-id del giocatore 1 (il caso vero:
        # chi gioca in emulatore apre il sito e ci mette il suo numero).
        room3 = ROOM + 2
        gioc = udp()
        gioc.sendto(pack(T_HELLO, 7, room3, 0), relay_addr)
        altro = udp()
        altro.sendto(pack(T_HELLO, 8, room3, 0), relay_addr)
        time.sleep(0.2)
        spia = udp()
        spia.sendto(pack(T_WATCH, 7, room3, 0), relay_addr)   # stesso id di gioc
        time.sleep(0.3)
        scarica(gioc)
        altro.sendto(pack(T_EVENT, 8, room3, 1, EVENT), relay_addr)
        assert recv_or_none(gioc) is not None, (
            "il GIOCATORE 7 e' stato espulso dal suo stesso spettatore")
        assert recv_or_none(spia) is not None, (
            "lo spettatore col peer-id del giocatore non riceve")
        print("PASSO 6: spettatore e giocatore con lo stesso peer-id convivono")

        # E il PING del giocatore non deve espellere lo spettatore.
        scarica(spia)
        gioc.sendto(pack(T_PING, 7, room3, 9), relay_addr)
        time.sleep(0.2)
        altro.sendto(pack(T_EVENT, 8, room3, 2, EVENT), relay_addr)
        assert recv_or_none(spia) is not None, (
            "il PING del giocatore ha espulso lo spettatore")
        print("PASSO 7: il PING del giocatore non espelle lo spettatore")

        for s in (gioc, altro, spia, s0) + tuple(g2):
            s.close()
    except Exception as exc:
        ok = False
        print("FALLITO: %s" % exc)
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=2)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]

    if ok:
        # Il log fa parte del criterio.
        if "GUARDA la stanza" not in out:
            ok = False
            print("FALLITO: il relay non distingue lo spettatore nel log")
        elif "SPETTATORE ma manda eventi" not in out:
            ok = False
            print("FALLITO: l'evento rifiutato allo spettatore non e' stato loggato")
        elif out.count("RIENTRA") != 0:
            ok = False
            print("FALLITO: c'e' stato un rientro-NAT incrociato (log 'RIENTRA')")
        else:
            print("PASSO 8: il log dice GUARDA, dice l'evento rifiutato, "
                  "e non contiene rientri incrociati")
    if not ok:
        print("--- log relay ---")
        print(out)

    print("\nREGRESSIONE SPETTATORE: " + ("PASSATA" if ok else "FALLITA"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
