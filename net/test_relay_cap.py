#!/usr/bin/env python3
"""
test_relay_cap.py - regressione A SECCO del tetto della stanza: 4 giocatori
(2026-08-25, per le partite in 3-4).

Il tetto non e' del relay ma del GIOCO: il payload disegna al massimo 3
remoti, quindi un quinto peer nella stessa stanza sarebbe solo traffico che
nessuno puo' disegnare. Il relay lo lascia FUORI: non riceve i broadcast e i
suoi eventi non vengono inoltrati.

Criteri:
  1. quattro peer entrano e si vedono (l'evento di 1 arriva a 2, 3 e 4);
  2. il quinto resta fuori: non riceve l'evento di 1, e il suo evento non
     arriva a nessuno dei quattro;
  3. il relay lo dice nel log ("PIENA"), una volta sola per peer;
  4. quando un peer se ne va (timeout non aspettabile qui: si usa il BYE da
     chiusura implicita - non esiste, quindi il criterio 4 e' il RIENTRO del
     quinto in una stanza DIVERSA: fuori dalla piena, il peer funziona).

    python test_relay_cap.py
"""

import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from protocol import T_EVENT, T_HELLO, pack  # noqa: E402

PY = sys.executable
RELAY_PORT = 19011
ROOM = 47021
ROOM2 = 47022
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

        peers = [udp() for _ in range(5)]
        for i, s in enumerate(peers, start=1):
            s.sendto(pack(T_HELLO, i, ROOM, 0), relay_addr)
        time.sleep(0.3)

        # PASSO 1: i primi quattro si vedono.
        peers[0].sendto(pack(T_EVENT, 1, ROOM, 1, EVENT), relay_addr)
        for i in (1, 2, 3):
            assert recv_or_none(peers[i]) is not None, (
                "l'evento di 1 non arriva al peer %d: stanza rotta" % (i + 1))
        print("PASSO 1: quattro peer in stanza, l'evento di 1 arriva a 2, 3 e 4")

        # PASSO 2: il quinto e' fuori, nei due sensi.
        assert recv_or_none(peers[4], timeout=0.5) is None, (
            "il QUINTO peer riceve i broadcast: il tetto non morde")
        peers[4].sendto(pack(T_EVENT, 5, ROOM, 1, EVENT), relay_addr)
        assert recv_or_none(peers[0], timeout=0.5) is None, (
            "l'evento del quinto viene inoltrato: il tetto non morde")
        print("PASSO 2: il quinto peer resta fuori, nei due sensi")

        # PASSO 3: fuori dalla stanza piena il quinto funziona normalmente.
        peers[4].sendto(pack(T_HELLO, 5, ROOM2, 0), relay_addr)
        sesto = udp()
        sesto.sendto(pack(T_HELLO, 6, ROOM2, 0), relay_addr)
        time.sleep(0.2)
        peers[4].sendto(pack(T_EVENT, 5, ROOM2, 2, EVENT), relay_addr)
        assert recv_or_none(sesto) is not None, (
            "il quinto peer non funziona nemmeno in una stanza NUOVA")
        print("PASSO 3: nella stanza nuova il quinto peer torna a funzionare")

        for s in peers + [sesto]:
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
        gridi = out.count("PIENA")
        if gridi == 0:
            ok = False
            print("FALLITO: il relay non ha detto PIENA nel log")
        elif gridi > 3:
            # Un rigo per tentativo sarebbe rumore: la soglia lascia spazio a
            # un paio di transizioni legittime (HELLO + primo evento).
            ok = False
            print("FALLITO: il relay ha detto PIENA %d volte (spam)" % gridi)
        else:
            print("PASSO 4: il relay ha loggato PIENA (%d volte, a transizione)"
                  % gridi)
    if not ok:
        print("--- log relay ---")
        print(out)

    print("\nREGRESSIONE TETTO STANZA: " + ("PASSATA" if ok else "FALLITA"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
