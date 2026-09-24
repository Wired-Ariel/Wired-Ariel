#!/usr/bin/env python3
"""
test_relay_rebind.py - regressione A SECCO del rientro in stanza dopo un
rebinding NAT (2026-08-02, per la partita via internet).

Il caso reale che simula: il NAT di casa dell'amico ricicla la porta sorgente.
Per il relay - che indicizza i peer per (IP, porta) - e' un peer nuovo, e prima
di questa correzione restava FUORI dalla stanza finche' il gioco non emetteva
un evento, mentre il vecchio indirizzo fantasma continuava a ricevere i
broadcast fino al timeout.

Qui il NAT lo facciamo noi: stesso peer_id, socket nuova (= porta sorgente
nuova), e da quella si manda SOLO un PING. Criteri:

  1. il PING basta a rimettere il peer in stanza (prima serviva un T_EVENT);
  2. l'evento successivo dell'altro giocatore arriva all'indirizzo NUOVO;
  3. all'indirizzo VECCHIO non arriva piu' niente (il fantasma e' stato tolto);
  4. il relay lo ha GRIDATO nel log ("RIENTRA").

    python test_relay_rebind.py
"""

import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from protocol import T_EVENT, T_HELLO, T_PING, pack  # noqa: E402

PY = sys.executable
RELAY_PORT = 19010
ROOM = 51234        # una stanza "da internet": alta e non ovvia
EVENT = bytes(range(12))


def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))   # porta effimera: ogni socket = un "indirizzo NAT"
    s.settimeout(2.0)
    return s


def recv_or_none(sock, timeout=1.0):
    sock.settimeout(timeout)
    try:
        return sock.recvfrom(2048)[0]
    except socket.timeout:
        return None


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

        a = udp()   # il giocatore che resta fermo (Lain)
        b = udp()   # l'amico, prima del rebinding
        a.sendto(pack(T_HELLO, 1, ROOM, 0), relay_addr)
        b.sendto(pack(T_HELLO, 2, ROOM, 0), relay_addr)
        time.sleep(0.2)

        # Sanita': la stanza funziona nei due sensi.
        a.sendto(pack(T_EVENT, 1, ROOM, 1, EVENT), relay_addr)
        assert recv_or_none(b) is not None, "l'evento 1->2 non arriva: stanza rotta"
        print("PASSO 1: stanza viva, l'evento arriva al vecchio indirizzo")

        # IL REBINDING: socket nuova, stesso peer_id, e da qui SOLO un PING.
        b2 = udp()
        b2.sendto(pack(T_PING, 2, ROOM, 7), relay_addr)
        assert recv_or_none(b2) is not None, "nessun PONG al nuovo indirizzo"
        print("PASSO 2: il PING dal nuovo indirizzo riceve il PONG")

        time.sleep(0.2)
        a.sendto(pack(T_EVENT, 1, ROOM, 2, EVENT), relay_addr)
        got_new = recv_or_none(b2)
        assert got_new is not None, (
            "l'evento NON arriva al nuovo indirizzo: il PING non ha rimesso "
            "in stanza (la correzione non c'e' o non morde)")
        print("PASSO 3: dopo il solo PING, l'evento arriva al NUOVO indirizzo")

        got_old = recv_or_none(b, timeout=0.5)
        assert got_old is None, (
            "l'indirizzo VECCHIO riceve ancora: il fantasma non e' stato tolto")
        print("PASSO 4: l'indirizzo vecchio non riceve piu' niente")

        for s in (a, b, b2):
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

    if ok and "RIENTRA" not in out:
        ok = False
        print("FALLITO: il relay non ha gridato il rientro nel log")
    elif ok:
        print("PASSO 5: il relay ha loggato il rientro (RIENTRA)")
    if not ok:
        print("--- log relay ---")
        print(out)

    print("\nREGRESSIONE REBIND: " + ("PASSATA" if ok else "FALLITA"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
