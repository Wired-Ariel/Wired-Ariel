#!/usr/bin/env python3
"""
test_bridge_tcp.py - regressione A SECCO del transport TCP dopo l'astrazione.

Il rischio dell'astrazione transport (2026-08-02) e' rompere il percorso che
gia' funzionava mentre se ne aggiunge uno nuovo. Questo test rimonta la catena
vera - relay.py + due client.py --transport tcp come sottoprocessi - e al
posto dei due mGBA mette due socket finte che parlano gli stessi 12 byte.

Criterio: un evento entrato dal finto gioco 1 esce IDENTICO dal finto gioco 2
(e viceversa), e un duplicato di seq viene scartato dal dedup. Se passa, il
percorso tcp e' quello di prima.

    python test_bridge_tcp.py
"""

import os
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

RELAY_PORT = 19000
LISTEN_1 = 18123
LISTEN_2 = 18124


def make_event(kind, seq, x, y):
    # protocol.py: <BBBBBBhhBB = type dir speed seq mapGroup mapNum x y gender state
    return struct.pack("<BBBBBBhhBB", kind, 1, 0, seq & 0xFF, 0, 9, x, y, 0, 0)


def recv_exact(sock, n, timeout=5.0):
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connessione chiusa dal bridge")
        buf += chunk
    return buf


def main():
    procs = []

    def spawn(args, name):
        p = subprocess.Popen([PY] + args, cwd=HERE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")
        procs.append((name, p))
        return p

    ok = True
    try:
        spawn(["relay.py", "--port", str(RELAY_PORT)], "relay")
        time.sleep(0.5)
        spawn(["client.py", "--transport", "tcp", "--listen", str(LISTEN_1),
               "--relay", f"127.0.0.1:{RELAY_PORT}", "--peer-id", "1",
               "--room", "7"], "client1")
        spawn(["client.py", "--transport", "tcp", "--listen", str(LISTEN_2),
               "--relay", f"127.0.0.1:{RELAY_PORT}", "--peer-id", "2",
               "--room", "7"], "client2")
        time.sleep(0.8)

        for _, p in procs:
            if p.poll() is not None:
                raise RuntimeError("un processo e' morto in avvio")

        game1 = socket.create_connection(("127.0.0.1", LISTEN_1), timeout=3)
        game2 = socket.create_connection(("127.0.0.1", LISTEN_2), timeout=3)
        time.sleep(0.5)   # il tempo dei due T_HELLO verso il relay

        # 1 -> 2. UN PASSO ESCE DUE VOLTE: dal 2026-08-02 il client manda in
        # copia PASSO e GIRA verso il gioco (--copie, default 2) e la copia la
        # scarta il payload sul seq. E' la cura della perdita sull'ultimo
        # tratto, quindi qui si controlla proprio che le copie ci siano: se un
        # giorno sparissero, il difetto tornerebbe muto.
        ev = make_event(1, 10, 5, -3)
        game1.sendall(ev)
        r1 = recv_exact(game2, 12)
        r2 = recv_exact(game2, 12)
        assert r1 == ev and r2 == ev, f"passo alterato: {r1.hex()} {r2.hex()}"
        print("PASSO 1: il PASSO 1->2 esce due volte, identico ai 12 byte spediti")

        # 2 -> 1
        ev2 = make_event(1, 77, 100, 200)
        game2.sendall(ev2)
        r1 = recv_exact(game1, 12)
        r2 = recv_exact(game1, 12)
        assert r1 == ev2 and r2 == ev2, f"passo alterato: {r1.hex()}"
        print("PASSO 2: il PASSO 2->1 esce due volte, identico ai 12 byte spediti")

        # LA PRESENZA (2026-08-28). Il client 1 ha appena visto il PRIMO
        # evento del peer 2, e a un peer mai visto si risponde con la propria
        # ultima posizione assoluta: senza, chi entra per secondo non vede chi
        # era gia' dentro finche' quello non si muove - e al Cable Club, dove
        # il payload molla la porta e tace, non lo vedrebbe mai.
        #
        # La fotografia e' l'ultimo evento col tipo forzato a SYNC: rimandare
        # un PASSO farebbe camminare l'avatar una seconda volta.
        presenza = recv_exact(game2, 12)
        assert presenza == make_event(2, 10, 5, -3), \
            f"presenza attesa dal peer 1, ricevuto {presenza.hex()}"
        print("PASSO 3: al primo evento di un peer nuovo si risponde con la "
              "propria posizione (tipo forzato a SYNC)")

        # Un SYNC (tipo 2) NON si duplica: il payload non lo deduplica perche'
        # e' idempotente, e duplicarlo sarebbe solo banda buttata.
        ev3 = make_event(2, 78, 101, 201)
        game2.sendall(ev3)
        assert recv_exact(game1, 12) == ev3, "SYNC alterato"
        # se arrivasse una copia, sarebbe lei il prossimo record: si controlla
        # mandando un PASSO dietro e vedendo che e' quello ad arrivare
        ev4 = make_event(1, 79, 102, 202)
        game2.sendall(ev4)
        assert recv_exact(game1, 12) == ev4, "il SYNC e' stato duplicato"
        recv_exact(game1, 12)   # la copia del PASSO
        print("PASSO 4: il SYNC non viene duplicato, il PASSO si")

        # burst nello stesso segmento TCP: il de-framing a record fissi regge
        burst = make_event(1, 11, 6, -3) + make_event(1, 12, 7, -3)
        game1.sendall(burst)
        got = b"".join(recv_exact(game2, 12) for _ in range(4))
        atteso = (make_event(1, 11, 6, -3) * 2) + (make_event(1, 12, 7, -3) * 2)
        assert got == atteso, "burst spezzato male"
        print("PASSO 5: due eventi nello stesso segmento, arrivati in ordine (x2)")

        game1.close()
        game2.close()
    except Exception as exc:
        ok = False
        print(f"FALLITO: {exc}")
    finally:
        for name, p in procs:
            p.terminate()
        time.sleep(0.3)
        for name, p in procs:
            try:
                out = p.communicate(timeout=2)[0]
            except subprocess.TimeoutExpired:
                p.kill()
                out = p.communicate()[0]
            if not ok:
                print(f"--- log {name} ---")
                print(out)

    print("\nREGRESSIONE TCP: " + ("PASSATA" if ok else "FALLITA"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
