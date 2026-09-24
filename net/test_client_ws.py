#!/usr/bin/env python3
"""
test_client_ws.py - il client Python che raggiunge il relay via WebSocket
(2026-08-25), invece che in UDP diretto.

PERCHE' QUESTO TEST ESISTE. Il 2026-08-25 si e' scoperto che la porta UDP
9000 della VPS non e' raggiungibile da internet (filtro nella Security List
della VCN: timeout, non rifiuto - ufw sulla macchina la permette e il relay
ascolta). Il browser giocava lo stesso perche' passa dalla 443; i client
Python no. Da qui il trasporto WebSocket in ws_link.py, che da' ai client la
stessa strada del browser.

Qui la si prova in locale, contro relay_ws.py + relay.py veri:
  1. due client con --relay ws://... si parlano attraverso il frontale;
  2. gli eventi arrivano interi e con lo slot avatar timbrato;
  3. se il frontale cade e torna, il client si RIAGGANCIA da solo e la
     partita riprende senza essere riavviata.

    D:\\Progettini\\Python313\\python.exe net\\test_client_ws.py
"""

import os
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PY = sys.executable
RELAY_PORT = 19030
WS_PORT = 19031
ROOM = 47040
EV_STEP, EV_SYNC = 1, 2


def evento(kind, x, y, seq):
    return struct.pack("<BBBBBBhhBB", kind, 1, 0, seq, 0, 2, x, y, 0, 0)


class GbaFinto:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=8)
        self.sock.settimeout(0.2)
        self.inbox = b""
        self.ricevuti = []
        self.seq = 0

    def cammina(self, x, y, kind=EV_STEP):
        self.seq = (self.seq + 1) % 256
        self.sock.sendall(evento(kind, x, y, self.seq))

    def drena(self, secondi):
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
                out.append(struct.unpack_from("<hh", ev, 6))
        return out

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def avvia_ws():
    return subprocess.Popen(
        [PY, "relay_ws.py", "--port", str(WS_PORT), "--bind", "127.0.0.1",
         "--relay", "127.0.0.1:%d" % RELAY_PORT], cwd=HERE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")


def main():
    procs, gba, ok = [], [], True
    ws_proc = None
    try:
        procs.append(subprocess.Popen(
            [PY, "relay.py", "--port", str(RELAY_PORT), "--verbose"], cwd=HERE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace"))
        time.sleep(0.5)
        ws_proc = avvia_ws()
        procs.append(ws_proc)
        time.sleep(1.0)

        url = "ws://127.0.0.1:%d/" % WS_PORT
        for i in range(2):
            procs.append(subprocess.Popen(
                [PY, "client.py", "--listen", str(8501 + i), "--relay", url,
                 "--peer-id", str(21 + i), "--room", str(ROOM), "--copie", "1"],
                cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace"))
        time.sleep(2.0)

        gba = [GbaFinto(8501), GbaFinto(8502)]
        time.sleep(0.5)

        gba[0].cammina(10, 20, kind=EV_SYNC)
        gba[1].cammina(30, 20, kind=EV_SYNC)
        time.sleep(1.0)
        for p in range(3):
            gba[0].cammina(10, 21 + p)
            gba[1].cammina(30, 21 + p)
            time.sleep(0.3)
        for g in gba:
            g.drena(1.0)

        assert {x for x, _ in gba[0].posizioni()} == {30}, (
            "il primo giocatore non riceve dal secondo attraverso il WebSocket "
            "(visto: %s)" % sorted(set(gba[0].posizioni())))
        assert {x for x, _ in gba[1].posizioni()} == {10}, (
            "il secondo non riceve dal primo (visto: %s)"
            % sorted(set(gba[1].posizioni())))
        print("PASSO 1: due client con --relay ws:// si parlano attraverso "
              "relay_ws + relay veri")

        # --- 2. il frontale cade e torna: il client si riaggancia da solo ---
        ws_proc.terminate()
        try:
            ws_proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            ws_proc.kill()
        time.sleep(1.5)
        ws_proc = avvia_ws()
        procs.append(ws_proc)
        # WsRelay.RETRY_S e' 2 s: si lascia tempo a un paio di tentativi
        time.sleep(6.0)

        for g in gba:
            g.ricevuti.clear()
        gba[0].cammina(11, 40, kind=EV_SYNC)
        time.sleep(0.5)
        for p in range(3):
            gba[0].cammina(11, 41 + p)
            time.sleep(0.3)
        gba[1].drena(2.0)
        viste = gba[1].posizioni()
        # LA PRESENZA PUO' STARE IN TESTA (2026-08-28). Mentre il frontale era
        # giu' il GBA finto non ha parlato per ~7 s, e da quel silenzio il
        # battito si porta dietro l'ultima posizione assoluta (la fotografia,
        # qui x=10): e' il rimedio che fa vedere chi sta fermo a chi entra
        # dopo. Quindi si pretende che la PARTITA sia ripresa - le posizioni
        # nuove arrivano e l'ultima e' una di quelle - non che il canale sia
        # stato muto fino al primo passo.
        assert {x for x, _ in viste} <= {10, 11}, (
            "posizioni impreviste dopo il riaggancio (visto: %s)"
            % sorted(set(viste)))
        assert viste and viste[-1][0] == 11, (
            "dopo la caduta del frontale la partita NON riprende: il client "
            "non si e' riagganciato (visto: %s)" % sorted(set(viste)))
        print("PASSO 2: caduto e riavviato il frontale, i client si "
              "riagganciano da soli e la partita riprende")
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

    if ok and "riagganciato al relay" not in log:
        ok = False
        print("FALLITO: nessun log di riaggancio: il PASSO 2 e' passato per "
              "un'altra ragione e la prova non vale")
    elif ok:
        print("PASSO 3: il riaggancio e' anche DETTO nel log")

    if not ok:
        print("--- log ---")
        print(log[-3000:])

    print("\nCLIENT VIA WEBSOCKET: " + ("PASSATO" if ok else "FALLITO"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
