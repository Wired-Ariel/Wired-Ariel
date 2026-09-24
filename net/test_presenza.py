#!/usr/bin/env python3
"""
test_presenza.py - CHI ARRIVA DOPO VEDE CHI C'ERA GIA' (2026-08-28).

IL DIFETTO CHE CHIUDE
---------------------
Il relay non inoltra gli HELLO e non conserva niente (relay.py, ramo T_HELLO):
in una stanza si ESISTE solo quando si parla. Finche' si cammina non si nota,
perche' il payload emette un SYNC assoluto al secondo; ma al Cable Club, nei
menu e in lotta il payload MOLLA la porta seriale e tace del tutto. Chi entrava
dopo non sapeva che l'altro c'era: ne' come avatar da disegnare, ne' come
partner per uno scambio. Il sintomo riportato dal campo era "se mi collego
prima di un amico non ci troviamo mai, ne' per scambi ne' in game".

Il rimedio (lo stesso che il Lua dell'emulatore aveva gia', ws.lastEvent): si
conserva una FOTOGRAFIA dell'ultima posizione assoluta, col tipo forzato a
SYNC, e la si rimanda quando il GBA tace da un po', all'ingresso in stanza e
come risposta al primo evento di un peer mai visto. Il relay non si tocca.

COSA PROVA, con relay e client VERI (al posto dei GBA, due socket che parlano
i 12 byte, come fa il ponte Lua di mGBA):

  1. A entra, dice UNA posizione e poi TACE (e' al bancone, o nei menu).
     B entra 3 s dopo: deve vedere A senza che A si muova.
  2. Il primo evento di B fa rispondere A subito, non al battito dopo.
  3. La fotografia e' un SYNC, non un PASSO: rimandare un passo farebbe
     camminare l'avatar una seconda volta a casa dell'amico.
  4. Il contatore `presenza` nel log del client sale (e' l'osservabile: senza,
     il rimedio potrebbe spegnersi in silenzio e nessuno se ne accorgerebbe).

    D:\\Progettini\\Python313\\python.exe net\\test_presenza.py
"""

import os
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

RELAY_PORT = 19040
LISTEN_A = 18141
LISTEN_B = 18142
ROOM = 47041

EV_STEP, EV_SYNC = 1, 2


def evento(kind, x, y, seq, mapg=0, mapn=2):
    return struct.pack("<BBBBBBhhBB", kind, 1, 0, seq & 0xFF, mapg, mapn,
                       x, y, 0, 0)


class GbaFinto:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(0.2)
        self.inbox = b""
        self.ricevuti = []
        self.seq = 0

    def cammina(self, x, y, kind=EV_STEP):
        self.seq = (self.seq + 1) % 256
        self.sock.sendall(evento(kind, x, y, self.seq))

    def drena(self, secondi=1.0):
        fine = time.monotonic() + secondi
        while time.monotonic() < fine:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            self.inbox += chunk
            while len(self.inbox) >= 12:
                self.ricevuti.append(self.inbox[:12])
                self.inbox = self.inbox[12:]

    def posizioni(self):
        out = []
        for ev in self.ricevuti:
            if (ev[0] & 0x0F) in (EV_STEP, EV_SYNC):
                x, y = struct.unpack_from("<hh", ev, 6)
                out.append((ev[0] & 0x0F, x, y))
        return out

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def main():
    procs = []
    gba = []
    ok = True
    log = ""
    try:
        procs.append(subprocess.Popen(
            [PY, "relay.py", "--port", str(RELAY_PORT)], cwd=HERE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace"))
        time.sleep(0.6)

        def avvia_client(listen, peer):
            procs.append(subprocess.Popen(
                [PY, "client.py", "--listen", str(listen),
                 "--relay", "127.0.0.1:%d" % RELAY_PORT,
                 "--peer-id", str(peer), "--room", str(ROOM), "--copie", "1"],
                cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace"))

        # --- A entra per primo, dice dove sta, poi TACE -------------------
        avvia_client(LISTEN_A, 1)
        time.sleep(0.8)
        a = GbaFinto(LISTEN_A)
        gba.append(a)
        time.sleep(0.4)
        a.cammina(40, 12, kind=EV_SYNC)
        # Da qui in poi A e' muto: e' al bancone del Cable Club, o in un menu.
        # Prima del 2026-08-28 questo silenzio lo rendeva invisibile.
        time.sleep(3.0)

        # --- B arriva DOPO ------------------------------------------------
        avvia_client(LISTEN_B, 2)
        time.sleep(0.8)
        b = GbaFinto(LISTEN_B)
        gba.append(b)
        time.sleep(0.4)
        # B si annuncia una volta sola: il suo primo evento deve bastare a
        # farsi rispondere da A.
        b.cammina(15, 60, kind=EV_SYNC)

        b.drena(3.5)
        a.drena(0.5)

        viste_da_b = b.posizioni()
        assert viste_da_b, (
            "B non ha ricevuto NIENTE da A, che era in stanza da prima e "
            "stava fermo: e' esattamente il difetto del 2026-08-28")
        assert (40, 12) in [(x, y) for _, x, y in viste_da_b], (
            "B non vede la posizione di A (40,12) - visto: %s" % viste_da_b)
        print("PASSO 1: chi entra dopo vede chi era gia' dentro e stava "
              "fermo, senza che quello si muova")

        tipi = {k for k, _x, _y in viste_da_b}
        assert EV_STEP not in tipi, (
            "la presenza e' arrivata come PASSO: farebbe camminare l'avatar "
            "una seconda volta. Deve essere un SYNC (visto: %s)" % viste_da_b)
        print("PASSO 2: la presenza e' un SYNC, non un PASSO")

        # A deve aver ricevuto B (il verso opposto non e' mai stato rotto, ma
        # se si rompesse qui il PASSO 1 potrebbe passare per caso).
        assert (15, 60) in [(x, y) for _, x, y in a.posizioni()], (
            "A non vede B: %s" % a.posizioni())
        print("PASSO 3: e il verso opposto continua a funzionare")
    except Exception as exc:
        ok = False
        print("FALLITO: %s" % exc)
    finally:
        for g in gba:
            g.close()
        testo = []
        for p in procs:
            p.terminate()
            try:
                testo.append(p.communicate(timeout=3)[0] or "")
            except subprocess.TimeoutExpired:
                p.kill()
                testo.append(p.communicate()[0] or "")
        log = "\n".join(testo)

    # IL CONTATORE, non solo l'effetto: un rimedio che si spegne in silenzio
    # e' peggio di nessun rimedio (regola del progetto).
    if ok:
        presenze = 0
        for riga in log.splitlines():
            if "| presenza " in riga:
                try:
                    presenze = max(presenze,
                                   int(riga.split("| presenza ")[1].split(" |")[0]))
                except (IndexError, ValueError):
                    pass
        if presenze < 1:
            ok = False
            print("FALLITO: il contatore `presenza` e' rimasto a zero: "
                  "l'effetto puo' essere arrivato per un'altra strada")
        else:
            print("PASSO 4: il contatore `presenza` nel log del client dice %d"
                  % presenze)

    if not ok:
        print("--- log ---")
        print(log)
    print("\nREGRESSIONE PRESENZA: " + ("PASSATA" if ok else "FALLITA"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
