#!/usr/bin/env python3
"""
test_tre_giocatori.py - la partita a TRE (e a QUATTRO) end-to-end sul PC,
con relay e client VERI (2026-08-25).

E' il banco che sta un gradino sotto la procedura Q: al posto dei tre mGBA
ci sono tre "GBA finti" che parlano il protocollo dal lato gioco (12 byte su
TCP, esattamente come fa il ponte Lua di mGBA), e in mezzo girano relay.py e
tre client.py senza nessuna modifica. Prova quello che i test unitari non
possono provare: che TRE processi in una stanza si smistino gli eventi sui
tre slot avatar, in tutte le direzioni.

Criteri (ognuno fallisce da solo, con il suo messaggio):
  1. ogni giocatore RICEVE dagli altri due, e nessuno riceve se stesso;
  2. gli slot sono assegnati per amico, distinti e STABILI: il payload
     disegnerebbe due avatar diversi, non uno che rimbalza;
  3. il tipo nel nibble basso resta leggibile (PASSO resta PASSO);
  4. il quarto giocatore entra e si vede (slot 2 dagli altri);
  5. il quinto NON entra: il relay lo lascia fuori (tetto della stanza);
  6. chi se ne va produce un VIA timbrato con IL SUO slot, e lo slot torna
     libero per il prossimo.

    D:\\Progettini\\Python313\\python.exe net\\test_tre_giocatori.py
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
RELAY_PORT = 19020
ROOM = 47030
EV_STEP, EV_SYNC, EV_LEAVE = 1, 2, 4


def evento(kind, x, y, seq, dir_=1, mapg=0, mapn=2):
    """I 12 byte come li emette il payload (struct NetEvent)."""
    return struct.pack("<BBBBBBhhBB", kind, dir_, 0, seq, mapg, mapn, x, y, 0, 0)


def kind_of(ev):
    return ev[0] & 0x0F


def slot_of(ev):
    return (ev[0] >> 4) & 0x03


class GbaFinto:
    """Il lato gioco: si collega al ponte TCP del suo client e parla i 12
    byte. E' quello che fa il Lua di mGBA, meno la grafica."""

    def __init__(self, nome, port):
        self.nome = nome
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(0.2)
        self.inbox = b""
        self.ricevuti = []
        self.seq = 0

    def cammina(self, x, y, kind=EV_STEP):
        self.seq = (self.seq + 1) % 256
        self.sock.sendall(evento(kind, x, y, self.seq))

    def drena(self, secondi=0.6):
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

    def slot_visti(self):
        """slot -> insieme delle posizioni ricevute su quello slot."""
        m = {}
        for ev in self.ricevuti:
            if kind_of(ev) in (EV_STEP, EV_SYNC):
                x, y = struct.unpack_from("<hh", ev, 6)
                m.setdefault(slot_of(ev), set()).add((x, y))
        return m

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def main():
    procs = []
    gba = []
    ok = True
    try:
        procs.append(subprocess.Popen(
            [PY, "relay.py", "--port", str(RELAY_PORT), "--verbose",
             # timeout corto: il client ucciso non manda nessun BYE (come un
             # cavo staccato), e il relay se ne accorge SOLO per silenzio. I
             # vivi mandano un PING al secondo, quindi 4 s non li tocca.
             "--timeout", "4"], cwd=HERE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace"))
        time.sleep(0.6)

        # I client si avviano QUANDO ENTRANO, non tutti insieme: nella stanza
        # entra chi arriva prima, e con cinque HELLO in volo insieme l'ordine
        # lo decide la rete - il test diventava intermittente (a volte il
        # quinto prendeva il posto del quarto). --copie 1 perche' qui si
        # contano gli eventi, non si prova la ridondanza (che ha il suo test).
        def avvia_client(i):
            procs.append(subprocess.Popen(
                [PY, "client.py", "--listen", str(8301 + i),
                 "--relay", "127.0.0.1:%d" % RELAY_PORT,
                 "--peer-id", str(i + 1), "--room", str(ROOM), "--copie", "1"],
                cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace"))

        for i in range(3):
            avvia_client(i)
            time.sleep(0.4)
        time.sleep(1.2)

        for i in range(3):
            gba.append(GbaFinto("G%d" % (i + 1), 8301 + i))
        time.sleep(0.5)

        # --- 1. ognuno si presenta da una posizione diversa -----------------
        for i, g in enumerate(gba):
            g.cammina(10 + i * 5, 20, kind=EV_SYNC)
        time.sleep(0.8)

        # --- 2. ognuno cammina di tre passi, sulla SUA colonna --------------
        for passo in range(3):
            for i, g in enumerate(gba):
                g.cammina(10 + i * 5, 21 + passo)
            time.sleep(0.35)
        for g in gba:
            g.drena(0.8)

        for i, g in enumerate(gba):
            visti = g.slot_visti()
            altri = [j for j in range(3) if j != i]
            assert len(visti) == 2, (
                "%s vede %d slot invece di 2 (%s): o non riceve da tutti, o "
                "due amici finiscono sullo stesso avatar"
                % (g.nome, len(visti), sorted(visti)))
            # le colonne x devono essere quelle degli ALTRI due, mai la propria
            colonne = set()
            for pos in visti.values():
                colonne |= {x for x, _ in pos}
            mie = {10 + i * 5}
            assert not (colonne & mie), (
                "%s riceve i propri eventi (colonna %s): il relay li rimanda "
                "al mittente" % (g.nome, mie))
            attese = {10 + j * 5 for j in altri}
            assert colonne == attese, (
                "%s vede le colonne %s, attese %s" % (g.nome, sorted(colonne), sorted(attese)))
            # OGNI slot porta UNA sola colonna: e' la stabilita' dell'avatar
            for slot, pos in visti.items():
                cols = {x for x, _ in pos}
                assert len(cols) == 1, (
                    "%s: sullo slot %d arrivano DUE giocatori diversi (colonne "
                    "%s): l'avatar rimbalzerebbe fra i due"
                    % (g.nome, slot, sorted(cols)))
            # e il tipo resta leggibile sotto il timbro
            tipi = {kind_of(e) for e in g.ricevuti}
            assert tipi <= {EV_STEP, EV_SYNC, EV_LEAVE}, (
                "%s: tipi illeggibili sotto il timbro dello slot: %s" % (g.nome, tipi))
        print("PASSO 1: tre giocatori, ognuno vede gli ALTRI DUE su due slot "
              "distinti e stabili, e mai se stesso")

        # QUALE slot G1 ha dato a G2 lo decide l'ORDINE DI ARRIVO del primo
        # evento, che e' una gara fra due processi: G2 puo' avere lo 0 o l'1.
        # E' una proprieta' del protocollo, non un difetto - ogni ricevente
        # numera i propri amici per conto suo - ma il test non deve
        # ASSUMERLA: qui la si legge dai fatti (la colonna 15 e' di G2) e la
        # si usa dopo, per il VIA. Assumere lo 0 rendeva il test
        # intermittente: 2 fallimenti su 6 corse.
        slot_g2 = [s for s, pos in gba[0].slot_visti().items()
                   if any(x == 15 for x, _ in pos)]
        assert len(slot_g2) == 1, "G2 non ha uno slot suo presso G1"
        slot_g2 = slot_g2[0]

        # --- 3. il quarto entra --------------------------------------------
        avvia_client(3)
        time.sleep(1.5)
        g4 = GbaFinto("G4", 8304)
        gba.append(g4)
        time.sleep(0.4)
        g4.cammina(40, 20, kind=EV_SYNC)
        time.sleep(0.4)
        for passo in range(2):
            g4.cammina(40, 21 + passo)
            time.sleep(0.3)
        for g in gba:
            g.drena(0.6)

        visti0 = gba[0].slot_visti()
        assert len(visti0) == 3, (
            "col quarto giocatore G1 vede %d slot invece di 3 (%s)"
            % (len(visti0), sorted(visti0)))
        col_quarto = {s for s, pos in visti0.items() if any(x == 40 for x, _ in pos)}
        assert len(col_quarto) == 1, "il quarto giocatore non ha uno slot suo"
        print("PASSO 2: il QUARTO entra e prende il terzo slot (%d) senza "
              "spostare gli altri" % col_quarto.pop())

        # --- 4. il quinto resta fuori --------------------------------------
        avvia_client(4)
        time.sleep(1.5)
        g5 = GbaFinto("G5", 8305)
        gba.append(g5)
        time.sleep(0.4)
        g5.cammina(60, 20, kind=EV_SYNC)
        time.sleep(0.5)
        for g in gba[:4]:
            g.ricevuti.clear()
            g.drena(0.5)
        for g in gba[:4]:
            colonne = set()
            for pos in g.slot_visti().values():
                colonne |= {x for x, _ in pos}
            assert 60 not in colonne, (
                "%s vede il QUINTO giocatore: il tetto della stanza non morde"
                % g.nome)
        g5.drena(0.4)
        assert not g5.ricevuti, "il quinto giocatore riceve: dovrebbe essere fuori"
        print("PASSO 3: il QUINTO resta fuori dalla stanza, nei due sensi")

        # --- 5. uno se ne va: VIA timbrato e slot liberato ------------------
        # G2 chiude il suo lato gioco E il suo client: il relay manda il BYE.
        gba[1].close()
        procs[2].terminate()          # procs[0] = relay, procs[1] = client 1
        gba[0].ricevuti.clear()
        # il relay lo dichiara morto dopo 4 s di silenzio, poi il BYE deve
        # attraversare relay -> client -> ponte TCP
        gba[0].drena(7.0)
        vie = [e for e in gba[0].ricevuti if kind_of(e) == EV_LEAVE]
        assert vie, "nessun VIA dopo la partenza di G2"
        slot_via = slot_of(vie[0])
        assert slot_via == slot_g2, (
            "il VIA arriva sullo slot %d invece che su quello di G2 (%d): "
            "despawnerebbe l'avatar SBAGLIATO" % (slot_via, slot_g2))
        print("PASSO 4: chi se ne va produce un VIA timbrato con IL SUO slot (%d)"
              % slot_via)

    except Exception as exc:
        ok = False
        print("FALLITO: %s" % exc)
    finally:
        for g in gba:
            g.close()
        for p in procs:
            p.terminate()
        log_relay = ""
        for p in procs:
            try:
                out = p.communicate(timeout=2)[0]
            except subprocess.TimeoutExpired:
                p.kill()
                out = p.communicate()[0]
            if not log_relay:
                log_relay = out or ""

    if ok and "PIENA" not in log_relay:
        ok = False
        print("FALLITO: il relay non ha registrato la stanza piena")
    elif ok:
        print("PASSO 5: il relay ha registrato il rifiuto del quinto")

    if not ok:
        print("--- log relay ---")
        print(log_relay[-3000:])

    print("\nPARTITA A TRE (e a quattro): " + ("PASSATA" if ok else "FALLITA"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
