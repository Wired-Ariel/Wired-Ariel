#!/usr/bin/env python3
"""
test_finto_club.py - il partner finto (finto_club.py) provato A SECCO.

Un ClubSession master con device finto (il lato di Lain) e un ClubSession
slave pilotato da FintoGba (il partner finto), collegati dalla FakeNet di
test_club: e' l'intera prova della procedura V senza hardware. Si verifica
la recita a livello del protocollo di link della Gen 3:

- la scala: AwaitMode -> ruolo -> handshake -> Connected, coi comandi giusti;
- la raffica del master: SEND_LINK_TYPE + INIT_BLOCK + 5 CONT_BLOCK, e il
  LinkPlayerBlock ricostruito DAI CONT deve essere identico al sorgente
  (firma GameFreak compresa: sbagliarla = CB2_LinkError sul GBA vero);
- il conteggio del blocco del GBA vero e la richiesta schede che ne segue;
- la scheda finta da 100 byte (BLOCK_REQ_SIZE_100);
- la vittoria dichiarata solo a scheda intera;
- l'eco della chiusura e la fine pulita da entrambi i lati.

    python test_finto_club.py
"""

import struct
import unittest

import finto_club
from finto_club import (
    BLOCCO_GIOCATORE, FintoGba, LINKCMD_CONT_BLOCK, LINKCMD_INIT_BLOCK,
    LINKCMD_READY_CLOSE_LINK, LINKCMD_SEND_BLOCK_REQ, LINKCMD_SEND_HELD_KEYS,
    LINKCMD_SEND_LINK_TYPE, LINKTYPE_TRADE, LINKTYPE_TRADE_SETUP,
    LINK_KEY_CODE_EXIT_ROOM, blocco_giocatore, blocco_in_cont, pacchetto,
)
from club_link import ClubSession
from test_club import FakeDev, FakeNet
from usb_link import (
    CMD_CONNECT_LINK, CMD_SET_MODE_MASTER, CMD_SET_MODE_SLAVE,
    CMD_START_HANDSHAKE,
    LINK_ST_AWAIT_MODE, LINK_ST_CLOSED, LINK_ST_CONNECTED,
    LINK_ST_HANDSHAKE_RX, LINK_ST_RECONNECTING,
)


def parole(block64):
    return struct.unpack("<8H", block64[:16])


class TestFintoClub(unittest.TestCase):

    def setUp(self):
        # Il distanziamento del ConnectLink (2026-08-27) si azzera: la scala
        # qui e' sincrona. Il ritardo vero ha il suo test in test_club.py.
        import club_link as _cl
        vecchio = _cl.CONNECT_RITARDO_S
        _cl.CONNECT_RITARDO_S = 0.0
        self.addCleanup(setattr, _cl, "CONNECT_RITARDO_S", vecchio)
        # La recita a tempo va a zero: i test non aspettano nessuno.
        self._ritmi = (finto_club.RITMO_PASSO_S, finto_club.RITMO_BURST_S,
                       finto_club.RITMO_RICONNESSIONE_S)
        finto_club.RITMO_PASSO_S = 0.0
        finto_club.RITMO_BURST_S = 0.0
        finto_club.RITMO_RICONNESSIONE_S = 0.0
        self.addCleanup(self._ripristina)

        self.net = FakeNet()
        self.dev_a = FakeDev()
        self.a = ClubSession(True, self.dev_a, self.net.sender("a"),
                             lambda s: None)
        self.finto = FintoGba(lambda s: None)
        self.b = ClubSession(False, self.finto, self.net.sender("b"),
                             lambda s: None)

    def _ripristina(self):
        (finto_club.RITMO_PASSO_S, finto_club.RITMO_BURST_S,
         finto_club.RITMO_RICONNESSIONE_S) = self._ritmi

    def pompa(self, giri=6):
        """Il ciclo principale della procedura V, in miniatura."""
        for _ in range(giri):
            self.finto.tick()
            while self.finto.stati:
                self.b.on_device_status(self.finto.stati.pop(0))
            while self.finto.blocchi:
                self.b.on_device_block(self.finto.blocchi.pop(0))
            self.net.consegna(self.b, self.net.a_to_b)
            self.net.consegna(self.a, self.net.b_to_a)

    def scala_completa(self):
        """Porta le due sessioni fino a Connected da entrambe le parti."""
        self.finto.avvia()
        self.a.on_device_status(LINK_ST_AWAIT_MODE)
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.pompa()
        # il lato A ha ricevuto il Connected del finto: ConnectLink al device
        self.assertIn(CMD_CONNECT_LINK, self.dev_a.commands)
        # il GBA vero si stabilisce
        self.a.on_device_status(LINK_ST_CONNECTED)
        self.pompa()

    def test_scala_e_ruoli(self):
        self.scala_completa()
        self.assertIn(CMD_SET_MODE_MASTER, self.dev_a.commands)
        self.assertEqual(self.finto.contatori, {})   # niente dati prima
        # il finto ha ricevuto ruolo slave e StartHandshake
        # (command() e' del finto: verifichiamo dagli effetti - la scala
        # e' arrivata in fondo, quindi entrambi i comandi sono passati)
        self.assertTrue(self.b._io_hs)
        self.assertTrue(self.b._hs_avviato)

    def test_raffica_del_master(self):
        self.scala_completa()
        blocchi = self.dev_a.blocks
        self.assertEqual(len(blocchi), 7, "LINK_TYPE + INIT + 5 CONT")
        self.assertEqual(parole(blocchi[0])[0], LINKCMD_SEND_LINK_TYPE)
        self.assertEqual(parole(blocchi[0])[1], LINKTYPE_TRADE_SETUP)
        self.assertEqual(parole(blocchi[1])[0], LINKCMD_INIT_BLOCK)
        self.assertEqual(parole(blocchi[1])[1], 60)
        # il LinkPlayerBlock ricostruito dai CONT deve essere IL blocco
        dati = b""
        for b in blocchi[2:]:
            w = parole(b)
            self.assertEqual(w[0], LINKCMD_CONT_BLOCK)
            dati += struct.pack("<7H", *w[1:])
        self.assertEqual(dati[:60], BLOCCO_GIOCATORE)
        self.assertTrue(dati.startswith(b"GameFreak inc.\x00"))

    def test_blocco_del_gba_vero_e_richiesta_schede(self):
        self.scala_completa()
        del self.dev_a.blocks[:]
        # il GBA vero risponde col SUO blocco giocatore (60 byte qualsiasi)
        for b in blocco_in_cont(bytes(range(60)), 1):
            self.a.on_device_block(b)
        self.pompa()
        self.assertTrue(self.finto._giocatore_ok)
        self.assertFalse(self.finto.vittoria)   # la scheda non c'e' ancora
        # il finto ha chiesto le schede e mandato la sua (100 byte: 8 CONT)
        w0 = [parole(b)[0] for b in self.dev_a.blocks]
        self.assertIn(LINKCMD_SEND_BLOCK_REQ, w0)
        self.assertEqual(w0.count(LINKCMD_CONT_BLOCK), 8)
        i_init = w0.index(LINKCMD_INIT_BLOCK)
        self.assertEqual(parole(self.dev_a.blocks[i_init])[1], 100)

    def test_vittoria_solo_a_scheda_intera(self):
        self.scala_completa()
        for b in blocco_in_cont(bytes(range(60)), 1):
            self.a.on_device_block(b)
        self.pompa()
        # la scheda del GBA vero, monca dell'ultimo CONT: niente vittoria
        scheda = blocco_in_cont(bytes(100), 1)
        for b in scheda[:-1]:
            self.a.on_device_block(b)
        self.pompa()
        self.assertFalse(self.finto.vittoria)
        # arriva l'ultimo pezzo: vittoria
        self.a.on_device_block(scheda[-1])
        self.pompa()
        self.assertTrue(self.finto.vittoria)

    def test_seduti_alla_macchina_il_link_si_riapre(self):
        """IL CASO DEL CAMPO (2026-08-20). READY_CLOSE_LINK non e' la fine:
        e' il gioco che chiude per cambiare meccanica. Il finto deve fare
        come il Pico vero - eco, riconnessione, sezione nuova - e la
        ClubSession dall altra parte deve rifare tutta la scala."""
        self.scala_completa()
        self.assertEqual(self.dev_a.commands.count(CMD_START_HANDSHAKE), 1)

        # il GBA vero si siede alla macchina degli scambi
        del self.dev_a.blocks[:]
        self.a.on_device_block(pacchetto(LINKCMD_READY_CLOSE_LINK, 0))
        self.pompa()

        # eco del comando e riapertura, come il firmware
        eco = [b for b in self.dev_a.blocks
               if parole(b)[0] == LINKCMD_READY_CLOSE_LINK]
        self.assertEqual(len(eco), 1)
        self.assertEqual(self.finto.giri, 1)
        self.assertFalse(self.b.finita, "riaprire non e' chiudere")

        # anche il nostro device riapre: la scala si rifa'
        self.a.on_device_status(LINK_ST_RECONNECTING)
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.pompa()
        self.a.on_device_status(LINK_ST_CONNECTED)
        self.pompa()
        self.assertEqual(self.dev_a.commands.count(CMD_START_HANDSHAKE), 2)

        # e il finto rimanda i dati giocatore, stavolta col linkType della
        # meccanica vera: se non combacia il gioco dice "scelte diverse"
        primo = [b for b in self.dev_a.blocks
                 if parole(b)[0] == LINKCMD_SEND_LINK_TYPE]
        # (i blocchi raccolti da quando il GBA si e' seduto: dentro c'e'
        #  l'eco della chiusura e poi tutta la raffica del giro nuovo)
        self.assertEqual(len(primo), 1)
        self.assertEqual(parole(primo[0])[1], LINKTYPE_TRADE)
        dati = b""
        for b in self.dev_a.blocks:
            if parole(b)[0] == LINKCMD_CONT_BLOCK:
                dati += struct.pack("<7H", *parole(b)[1:])
        self.assertEqual(dati[:60], blocco_giocatore(LINKTYPE_TRADE))

        # il GBA vero risponde: la riapertura e' riuscita
        for b in blocco_in_cont(blocco_giocatore(LINKTYPE_TRADE), 1):
            self.a.on_device_block(b)
        self.pompa()
        self.assertEqual(self.finto.riaperture_ok, 1)

    def test_uscire_dalla_porta_chiude_davvero(self):
        """L unica vera fine e' il tasto EXIT_ROOM: nel firmware spegne
        keepAlive (usbSection.cpp:54-57) e la sessione muore."""
        self.scala_completa()
        self.a.on_device_block(pacchetto(LINKCMD_SEND_HELD_KEYS,
                                         LINK_KEY_CODE_EXIT_ROOM))
        self.pompa()
        self.assertTrue(self.b._io_chiuso)
        self.assertEqual(self.finto.giri, 0, "uscire non e' riaprire")
        self.a.on_device_status(LINK_ST_CLOSED)
        self.pompa()
        self.assertTrue(self.a.finita)
        self.assertTrue(self.b.finita)

    def test_ruolo_sbagliato_avvisa(self):
        righe = []
        finto = FintoGba(righe.append)
        finto.command(CMD_SET_MODE_MASTER, "ruolo: master")
        self.assertTrue(any("peer 2" in r for r in righe))


if __name__ == "__main__":
    unittest.main(verbosity=2)
