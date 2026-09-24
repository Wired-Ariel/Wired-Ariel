#!/usr/bin/env python3
"""
test_club.py - la sessione del Cable Club (club_link.py) provata A SECCO.

Due ClubSession (master e slave) collegate da una rete finta che PERDE e
RIORDINA come l'UDP vero, e due device finti che recitano il copione degli
stati che il firmware Celio emette (linkdevice.service.ts). Sono i casi che
sull'hardware costerebbero una serata con Lain e qui costano un decimo di
secondo:

- la negoziazione collassata: AwaitMode -> master/slave dal peer-id;
- StartHandshake SOLO quando entrambi i lati sono HandshakeReceived,
  e una volta sola per lato;
- LinkConnected di la' -> ConnectLink di qua;
- blocchi in ordine nonostante perdita e riordino (REQ + rilancio);
- statusi che sopravvivono alla perdita (3 copie, dedup sul serial);
- chiusura: entrambi LinkClosed -> finita, da tutti e due i lati.

    python test_club.py
"""

import struct
import unittest

import club_link
from club_link import ClubSession
from protocol import (
    CLUB_DATA, CLUB_ENTER, CLUB_LEAVE, CLUB_REQ, CLUB_STATUS, club_data,
    club_unpack,
)
from usb_link import (
    CMD_CONNECT_LINK, CMD_SET_MODE_MASTER, CMD_SET_MODE_SLAVE,
    CMD_START_HANDSHAKE,
    LINK_ST_AWAIT_MODE, LINK_ST_CLOSED, LINK_ST_CONNECTED,
    LINK_ST_HANDSHAKE_RX, LINK_ST_READY, LINK_ST_RECONNECTING,
)


class FakeDev:
    def __init__(self):
        self.commands = []
        self.blocks = []

    def command(self, cmd, label):
        self.commands.append(cmd)

    def send_block(self, block):
        self.blocks.append(block)


class FakeNet:
    """Consegna i corpi T_CLUB all'altra sessione, come farebbe il relay.
    drop_pattern: indici (0-based) dei datagrammi da PERDERE, una volta sola.
    reorder: se vero, consegna a coppie invertite."""

    def __init__(self):
        self.a_to_b = []
        self.b_to_a = []
        self.drop = set()
        self.reorder = False
        self._count = {"a": 0, "b": 0}

    def sender(self, lato):
        coda = self.a_to_b if lato == "a" else self.b_to_a

        def send(body):
            i = self._count[lato]
            self._count[lato] += 1
            if i in self.drop:
                return                      # perso per sempre
            coda.append(body)
        return send

    def consegna(self, sessione, coda):
        """Svuota una coda dentro una sessione, col riordino se richiesto."""
        pend = list(coda)
        del coda[:]
        if self.reorder and len(pend) >= 2:
            pend[0], pend[1] = pend[1], pend[0]
        for body in pend:
            parsed = club_unpack(body)
            if parsed is None:
                continue
            sub, epoca, payload = parsed
            if sub == CLUB_STATUS:
                sessione.on_net_status(epoca, payload[0], payload[1])
            elif sub == CLUB_DATA:
                sessione.on_net_block(epoca, payload[0], payload[1])
            elif sub == CLUB_REQ:
                sessione.on_net_req(epoca, payload)
            elif sub == CLUB_ENTER:
                sessione.nota_partner(epoca)
            elif sub == CLUB_LEAVE:
                sessione.on_net_leave(epoca)


def blocco(n):
    return struct.pack("<H", n) * 32   # 64 byte riconoscibili


class TestClub(unittest.TestCase):

    def setUp(self):
        # Il distanziamento del ConnectLink si azzera: questi test fanno la
        # scala in sincrono. Il ritardo vero ha il suo test dedicato
        # (test_connect_link_distanziato_da_start_handshake).
        vecchio = club_link.CONNECT_RITARDO_S
        club_link.CONNECT_RITARDO_S = 0.0
        self.addCleanup(setattr, club_link, "CONNECT_RITARDO_S", vecchio)
        self.net = FakeNet()
        self.dev_a = FakeDev()
        self.dev_b = FakeDev()
        self.a = ClubSession(True, self.dev_a, self.net.sender("a"),
                             lambda s: None)
        self.b = ClubSession(False, self.dev_b, self.net.sender("b"),
                             lambda s: None)
        # Una sessione vera comincia SEMPRE con AwaitMode (firmware,
        # module/link.cpp:13), quindi i test partono da li': cosi' provano il
        # flusso che esiste, non uno che il device non produce mai. Il caso
        # "prima di AwaitMode" ha il suo test dedicato, su una coppia pulita.
        # Si arma in SILENZIO: passare davvero da AwaitMode genererebbe
        # traffico di rete e comandi che falserebbero i conteggi degli altri
        # test. Il percorso completo dell'avvio ha i suoi due test dedicati
        # (test_ruoli_dal_peer_id, test_residuo_prima_di_awaitmode_non_chiude).
        self.a._avviata = True
        self.b._avviata = True
        # E si arma anche la SEZIONE, per lo stesso motivo: sul device vero
        # i dati non passano mai fuori da una sezione viva (fra una e
        # l'altra il firmware purga la coda). I test che quel confine lo
        # provano davvero se lo aprono e chiudono da soli
        # (test_blocchi_tenuti_da_parte_fra_due_sezioni).
        self.a._sezione_viva = True
        self.b._sezione_viva = True
        # ... e la sezione COLLEGATA, che dal 2026-08-27 e' il cancello vero
        # dei blocchi (i comandi della scala restano su _sezione_viva).
        self.a._sezione_pronta = True
        self.b._sezione_pronta = True

    def scambia(self):
        """Qualche giro di consegna incrociata, come il ciclo del client."""
        for _ in range(4):
            self.net.consegna(self.b, self.net.a_to_b)
            self.net.consegna(self.a, self.net.b_to_a)

    def test_ruoli_dal_peer_id(self):
        self.a.on_device_status(LINK_ST_AWAIT_MODE)
        self.b.on_device_status(LINK_ST_AWAIT_MODE)
        self.assertIn(CMD_SET_MODE_MASTER, self.dev_a.commands)
        self.assertIn(CMD_SET_MODE_SLAVE, self.dev_b.commands)
        self.assertNotIn(CMD_SET_MODE_SLAVE, self.dev_a.commands)

    # --- il ruolo che ARRIVA DOPO (2026-08-28) ---------------------------
    #
    # Chi arriva al bancone per primo non sa ancora con chi giochera': col
    # peer sorteggiato dal sito il ruolo si puo' decidere SOLO conoscendo il
    # numero dell'amico. Fino a quel momento il comando al Pico non deve
    # partire - mandarne uno a caso significa due master o due slave, e nel
    # secondo caso i due schermi restano su "in attesa" per sempre.

    def test_ruolo_ignoto_non_manda_nessun_comando(self):
        c = ClubSession(None, self.dev_a, self.net.sender("a"), lambda s: None)
        c._avviata = True
        c.on_device_status(LINK_ST_AWAIT_MODE)
        self.assertNotIn(CMD_SET_MODE_MASTER, self.dev_a.commands)
        self.assertNotIn(CMD_SET_MODE_SLAVE, self.dev_a.commands)

    def test_ruolo_deciso_dopo_awaitmode(self):
        c = ClubSession(None, self.dev_a, self.net.sender("a"), lambda s: None)
        c._avviata = True
        c.on_device_status(LINK_ST_AWAIT_MODE)
        c.set_master(True)
        self.assertEqual([x for x in self.dev_a.commands
                          if x in (CMD_SET_MODE_MASTER, CMD_SET_MODE_SLAVE)],
                         [CMD_SET_MODE_MASTER])
        # e non si ripete: il firmware tiene il modo nel modulo, la sezione
        # nuova nasce gia' col ruolo giusto (link.cpp:17-22).
        c.set_master(False)
        self.assertNotIn(CMD_SET_MODE_SLAVE, self.dev_a.commands)

    def test_ruolo_deciso_prima_di_awaitmode(self):
        c = ClubSession(None, self.dev_a, self.net.sender("a"), lambda s: None)
        c._avviata = True
        c.set_master(False)
        # Il device non ha ancora chiesto niente: il comando aspetta.
        self.assertNotIn(CMD_SET_MODE_SLAVE, self.dev_a.commands)
        c.on_device_status(LINK_ST_AWAIT_MODE)
        self.assertIn(CMD_SET_MODE_SLAVE, self.dev_a.commands)

    def test_ready_resta_locale(self):
        self.a.on_device_status(LINK_ST_READY)
        self.assertEqual(self.net.a_to_b, [])

    def test_handshake_quando_entrambi(self):
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.scambia()
        # solo A e' pronto: nessuno StartHandshake da nessuna parte
        self.assertNotIn(CMD_START_HANDSHAKE, self.dev_a.commands)
        self.assertNotIn(CMD_START_HANDSHAKE, self.dev_b.commands)

        self.b.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.scambia()
        self.assertEqual(self.dev_a.commands.count(CMD_START_HANDSHAKE), 1)
        self.assertEqual(self.dev_b.commands.count(CMD_START_HANDSHAKE), 1)

        # gli annunci ripetuti non lo fanno ripartire
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.scambia()
        self.assertEqual(self.dev_b.commands.count(CMD_START_HANDSHAKE), 1)

    def test_handshake_sopravvive_alla_perdita(self):
        # A annuncia HandshakeRx in 3 copie: le prime DUE si perdono.
        self.net.drop = {0, 1}
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.b.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.scambia()
        self.assertEqual(self.dev_a.commands.count(CMD_START_HANDSHAKE), 1)
        self.assertEqual(self.dev_b.commands.count(CMD_START_HANDSHAKE), 1)

    def test_connected_di_la_connectlink_di_qua(self):
        # La scala PRIMA: il ConnectLink non parte a freddo, vuole una
        # sezione viva sul device e lo StartHandshake gia' fatto (vedi
        # _forse_connect_link). E' l'ordine che tiene anche il firmware.
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.b.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.scambia()
        self.a.on_device_status(LINK_ST_CONNECTED)
        self.scambia()
        self.assertIn(CMD_CONNECT_LINK, self.dev_b.commands)
        self.assertNotIn(CMD_CONNECT_LINK, self.dev_a.commands)

    def test_blocchi_in_ordine_nonostante_il_riordino(self):
        self.net.reorder = True
        for n in range(6):
            self.a.on_device_block(blocco(n))
        self.scambia()
        self.assertEqual(self.dev_b.blocks, [blocco(n) for n in range(6)])

    def test_blocco_perso_recuperato_con_la_richiesta(self):
        # il datagramma 1 di A (blocco seq 1) si perde: B lo chiede indietro.
        self.net.drop = {1}
        for n in range(4):
            self.a.on_device_block(blocco(n))
        self.scambia()
        self.assertEqual(self.dev_b.blocks, [blocco(n) for n in range(4)])
        self.assertGreater(self.b.richiesti, 0)

    def test_ultimo_blocco_perso_recuperato_dal_rilancio(self):
        # L'ULTIMO blocco si perde e nessun successivo lo denuncia: e' il
        # buco che il rilancio periodico copre.
        self.net.drop = {2}   # 0,1 passano; il 2 (ultimo) si perde
        for n in range(3):
            self.a.on_device_block(blocco(n))
        self.scambia()
        self.assertEqual(len(self.dev_b.blocks), 2)

        club_link.RILANCIO_ULTIMO_S = 0.0   # subito, per il test
        self.a.tick()
        self.scambia()
        self.assertEqual(self.dev_b.blocks, [blocco(n) for n in range(3)])
        self.assertGreater(self.a.rilanci, 0)

    def test_residuo_prima_di_awaitmode_non_chiude(self):
        """Il difetto della prova sul campo del 2026-08-16: smontando il
        passthrough resta un LinkClosed nell'endpoint di stato, e veniva letto
        come primo stato del club - sessione dichiarata finita con `blocchi tx
        0 rx 0`, ogni scambio impossibile."""
        self.a._avviata = False      # come appena entrati nel modo link
        self.b._avviata = False
        self.a.on_device_status(LINK_ST_CLOSED)     # residuo
        self.scambia()
        self.assertFalse(self.a.finita)
        self.assertFalse(self.b.finita, "il residuo non deve nemmeno essere "
                                        "annunciato al partner")

        # Dopo AwaitMode la sessione e' viva e il ruolo e' stato chiesto.
        self.a.on_device_status(LINK_ST_AWAIT_MODE)
        self.assertTrue(self.a._avviata)
        self.assertFalse(self.a.finita)

    # --- la riapertura del link: il caso del 2026-08-20 --------------------

    def _giro(self):
        """Una salita completa della scala, come la fa il firmware a ogni
        UsbSection: handshake dai due lati, poi il cavo collegato."""
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.b.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.scambia()
        self.a.on_device_status(LINK_ST_CONNECTED)
        self.b.on_device_status(LINK_ST_CONNECTED)
        self.scambia()

    def test_riapertura_del_link_rifa_la_scala(self):
        """LO SCENARIO DEL CAMPO: entrati in saletta, i due si siedono alla
        macchina degli scambi. Il gioco CHIUDE e RIAPRE il link
        (cable_club.c, Task_ReestablishLink -> OpenLink) e il firmware apre
        una UsbSection nuova che riaspetta TUTTI i comandi della scala.
        Prima della correzione i gate erano per sessione: StartHandshake non
        ripartiva, i blocchi si congelavano e il gioco andava in errore dopo
        "un momento attendi"."""
        self._giro()
        self.assertEqual(self.dev_a.commands.count(CMD_START_HANDSHAKE), 1)
        self.assertEqual(self.dev_a.commands.count(CMD_CONNECT_LINK), 1)

        # ci si siede: i due device annunciano la riconnessione
        self.a.on_device_status(LINK_ST_RECONNECTING)
        self.b.on_device_status(LINK_ST_RECONNECTING)
        self.scambia()
        self.assertEqual(self.a.giri, 1)

        # la sezione nuova: la scala si rifa' TUTTA, sui due lati
        self._giro()
        self.assertEqual(self.dev_a.commands.count(CMD_START_HANDSHAKE), 2)
        self.assertEqual(self.dev_b.commands.count(CMD_START_HANDSHAKE), 2)
        self.assertEqual(self.dev_a.commands.count(CMD_CONNECT_LINK), 2)
        self.assertEqual(self.dev_b.commands.count(CMD_CONNECT_LINK), 2)

        # e i dati ricominciano a passare
        del self.dev_b.blocks[:]
        self.a.on_device_block(blocco(1))
        self.scambia()
        self.assertEqual(self.dev_b.blocks, [blocco(1)])

    def test_niente_comandi_fra_una_sezione_e_l_altra(self):
        """Fra due sezioni il firmware tiene m_currentSection a nullptr per
        400 ms e receiveCommand lo dereferenzia senza controllare
        (link.cpp:46-47): un comando capitato li' e' un puntatore nullo sul
        Pico. Quindi finche' il NOSTRO device non ha ridetto handshake, non
        esce niente - nemmeno se il partner e' gia' avanti."""
        self._giro()
        self.a.on_device_status(LINK_ST_RECONNECTING)
        prima = len(self.dev_a.commands)

        # il partner e' gia' ripartito e annuncia tutto: noi zitti
        self.b.on_device_status(LINK_ST_RECONNECTING)
        self.b.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.b.on_device_status(LINK_ST_CONNECTED)
        self.scambia()
        self.assertEqual(len(self.dev_a.commands), prima,
                         "nessun comando prima che la nostra sezione esista")

        # appena la nostra sezione c'e', i comandi in attesa partono, e
        # nell'ordine giusto: StartHandshake e poi ConnectLink.
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        nuovi = self.dev_a.commands[prima:]
        self.assertEqual(nuovi, [CMD_START_HANDSHAKE, CMD_CONNECT_LINK])

    def test_blocchi_tenuti_da_parte_fra_due_sezioni(self):
        """Il firmware PURGA la coda dei pacchetti quando apre la sezione
        nuova (usbLinkCommand, init): scrivere nel buco vuol dire buttare.
        E cio' che arriva proprio li' e' la raffica dei dati giocatore, che
        il gioco non ritenta mai."""
        self._giro()
        self.a.on_device_status(LINK_ST_RECONNECTING)
        del self.dev_a.blocks[:]

        self.b.on_device_block(blocco(7))
        self.scambia()
        self.assertEqual(self.dev_a.blocks, [],
                         "niente scritture mentre la sezione non c'e'")

        # la sezione nuova apre... ma l'handshake NON basta: i blocchi
        # spinti nel device prima di `cavo collegato` si perdono (campo del
        # 2026-08-27, GBA vs emulatore - 7 blocchi nel vuoto e il GBA appeso
        # ad "attendi" per sempre).
        self.b.on_device_block(blocco(8))
        self.scambia()
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.assertEqual(self.dev_a.blocks, [],
                         "all'handshake i blocchi NON si consegnano ancora")

        # a cavo COLLEGATO l'arretrato entra, e in ordine
        self.a.on_device_status(LINK_ST_CONNECTED)
        self.assertEqual(self.dev_a.blocks, [blocco(7), blocco(8)])
        self.assertEqual(self.a.blocchi_tenuti, 2)
        self.assertEqual(self.a.blocchi_persi_dev, 0)

    def test_riconnessione_del_partner_non_ci_fa_ricominciare(self):
        """Il giro nuovo lo decide il NOSTRO device (USB, non perde). Se lo
        decidesse la rete, il riannuncio di uno stato ci farebbe ricominciare
        la scala ogni 2 secondi, per sempre."""
        self._giro()
        self.a.on_net_status(self.a.epoca_partner, 900, LINK_ST_RECONNECTING)
        self.assertEqual(self.a.giri, 0)
        self.assertTrue(self.a._sezione_viva)

    def test_riapertura_riparte_anche_se_il_partner_e_avanti(self):
        """Asimmetria: il partner ha gia' rifatto la scala mentre noi siamo
        ancora a meta'. I flag in attesa devono farci arrivare comunque."""
        self._giro()
        self.a.on_device_status(LINK_ST_RECONNECTING)
        self.b.on_device_status(LINK_ST_RECONNECTING)
        self.b.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.b.on_device_status(LINK_ST_CONNECTED)
        self.scambia()
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.scambia()
        self.assertEqual(self.dev_a.commands.count(CMD_START_HANDSHAKE), 2)
        self.assertEqual(self.dev_a.commands.count(CMD_CONNECT_LINK), 2)

    # --- il link chiuso SENZA riapertura: il caso del 2026-08-27 -----------

    def test_link_chiuso_senza_riapertura_muore(self):
        """LO SCENARIO DEL CAMPO: il frullatore finisce (o si annulla al
        bancone). Il gioco chiude il link col doppio READY_CLOSE_LINK, il
        firmware dice "riconnessione" e aspetta una sezione nuova che non
        arrivera' MAI: quelle meccaniche non passano dalla porta della
        saletta (EXIT_ROOM), quindi niente LinkClosed. Prima della cura il
        club restava appeso per sempre e la camminata non tornava piu'."""
        self._giro()
        self.a.on_device_status(LINK_ST_RECONNECTING)
        self.b.on_device_status(LINK_ST_RECONNECTING)
        self.scambia()
        self._patch("RIAPERTURA_S", -1.0)
        self.a.tick()
        self.assertTrue(self.a.finita)
        self.assertTrue(self.a.abortita)
        self.assertEqual(self.a.motivo_fine, "link chiuso senza riapertura")

    def test_riapertura_completata_spegne_il_timer(self):
        """La riapertura VERA (sedersi alla macchina degli scambi): la scala
        si completa e il conto alla rovescia si spegne - anche con la
        costante a zero la sessione non muore."""
        self._giro()
        self.a.on_device_status(LINK_ST_RECONNECTING)
        self.b.on_device_status(LINK_ST_RECONNECTING)
        self.scambia()
        self._giro()                     # la scala nuova, completa
        self._patch("RIAPERTURA_S", -1.0)
        self.a.tick()
        self.b.tick()
        self.assertFalse(self.a.finita)
        self.assertFalse(self.b.finita)

    def test_riapertura_a_meta_non_basta(self):
        """L'asimmetria vista nei log del 2026-08-27 alle 01:44: dopo la
        chiusura UN giocatore riprova al bancone, il suo device arriva fino
        a "cavo collegato" (la scala parte grazie a _lui_hs tenuto), ma il
        partner non c'e' piu' e il suo Connected non arrivera' mai. Meta'
        scala non spegne il timer: la sessione muore lo stesso."""
        self._giro()
        self.a.on_device_status(LINK_ST_RECONNECTING)
        self.b.on_device_status(LINK_ST_RECONNECTING)
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.a.on_device_status(LINK_ST_CONNECTED)   # solo il MIO lato
        self._patch("RIAPERTURA_S", -1.0)
        self.a.tick()
        self.assertTrue(self.a.finita)
        self.assertEqual(self.a.motivo_fine, "link chiuso senza riapertura")

    def test_riannuncio_identico_non_e_progresso(self):
        """L'altro buco del 2026-08-27: gli stati riannunciati hanno sseq
        freschi e contavano come progresso - "partner: cavo collegato" ogni
        2 s teneva vivo il watchdog di una sessione gia' morta. Ora conta
        solo uno stato DIVERSO dall'ultimo visto; quello ripetuto si
        processa comunque (i flag sono idempotenti)."""
        self.a.on_net_status(0x1234, 0, LINK_ST_HANDSHAKE_RX)
        self.a._progresso = 0.0
        self.a.on_net_status(0x1234, 1, LINK_ST_HANDSHAKE_RX)   # riannuncio
        self.assertEqual(self.a._progresso, 0.0)
        self.a.on_net_status(0x1234, 2, LINK_ST_CONNECTED)      # novita'
        self.assertGreater(self.a._progresso, 0.0)

    # --- le epoche: i casi visti sul campo il 2026-08-19 -------------------

    def _patch(self, nome, valore):
        vecchio = getattr(club_link, nome)
        setattr(club_link, nome, valore)
        self.addCleanup(setattr, club_link, nome, vecchio)

    def test_connect_link_distanziato_da_start_handshake(self):
        """Il firmware trasmette la parola di slave solo fra StartHandshake e
        ConnectLink: se i due comandi partono nello stesso istante quel
        trasferimento non avviene mai e la sezione resta appesa (campo
        2026-08-27, partner in emulatore). Quindi il ConnectLink DEVE
        aspettare, e partire da solo al tick dopo il ritardo."""
        import time as _t
        self._patch("CONNECT_RITARDO_S", 0.05)
        # il partner e' gia' tutto pronto PRIMA della nostra scala: e' il
        # caso dell'emulatore, che annuncia handshake e collegato insieme
        self.a.on_net_status(0x77, 1, LINK_ST_HANDSHAKE_RX)
        self.a.on_net_status(0x77, 2, LINK_ST_CONNECTED)
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.assertIn(CMD_START_HANDSHAKE, self.dev_a.commands)
        self.assertNotIn(CMD_CONNECT_LINK, self.dev_a.commands,
                         "ConnectLink nello stesso istante = sezione appesa")
        # prima del ritardo il tick non lo manda...
        self.a.tick()
        self.assertNotIn(CMD_CONNECT_LINK, self.dev_a.commands)
        # ...dopo il ritardo si'
        _t.sleep(0.06)
        self.a.tick()
        self.assertIn(CMD_CONNECT_LINK, self.dev_a.commands)

    def test_congedo_accompagna_fuori_invece_di_staccare(self):
        """L'AMICO ESCE DALLA PORTA. Staccare il cavo qui lascia il nostro
        gioco dentro la coreografia d'uscita, senza partner: schermata nera e
        riavvio (campo 2026-08-27). Il firmware invece continua a fare da cavo
        finche' i due si sono scambiati READY_CLOSE_LINK (usbSection.cpp):
        qui si fa uguale, fabbricando noi le risposte del partner."""
        self.a.on_net_leave(self.a.epoca_partner)
        # NON e' finita: si accompagna fuori
        self.assertFalse(self.a.finita, "l'uscita dell'amico non stacca il cavo")
        self.assertTrue(self.a.congedo)
        # ...e al device e' andata la PORTA
        self.assertTrue(self.dev_a.blocks, "nessun blocco: il gioco resta appeso")
        porta = self.dev_a.blocks[-1]
        self.assertEqual(porta[0] | (porta[1] << 8), club_link.LINKCMD_HELD_KEYS)
        self.assertEqual(porta[2] | (porta[3] << 8), club_link.LINK_KEY_EXIT_ROOM)

        # il gioco chiede di chiudere: gli si risponde di si'
        self.a.on_device_block(
            club_link.blocco_da_cmd([club_link.LINKCMD_READY_CLOSE_LINK]))
        eco = self.dev_a.blocks[-1]
        self.assertEqual(eco[0] | (eco[1] << 8), club_link.LINKCMD_READY_CLOSE_LINK)

        # e quando il device dice "link chiuso", la sessione finisce PULITA
        self.a.on_device_status(LINK_ST_CLOSED)
        self.assertTrue(self.a.finita)
        self.assertFalse(self.a.abortita, "uscire dalla porta non e' un errore")

    def test_congedo_ha_un_tetto(self):
        """Se il gioco non chiude il link (dialogo aperto, schermata insolita)
        il congedo non puo' durare per sempre."""
        self._patch("CONGEDO_MAX_S", 0.05)
        self.a.on_net_leave(self.a.epoca_partner)
        self.assertFalse(self.a.finita)
        import time as _t
        _t.sleep(0.06)
        self.a.tick()
        self.assertTrue(self.a.finita)
        self.assertTrue(self.a.abortita, "scaduto il tetto e' un abbandono")

    def test_epoca_in_ogni_corpo(self):
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.a.on_device_block(blocco(0))
        for body in self.net.a_to_b:
            parsed = club_unpack(body)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed[1], self.a.epoca)

    def test_riavvio_del_partner_riagganciato_in_corsa(self):
        """LO SCENARIO DEL CAMPO: il client dell'amico si riavvia a meta'
        sessione. Prima della cura i suoi blocchi (sequenze da 0) e i nostri
        (sequenze avanti) si incrociavano in un mondo solo: `fuori-ordine 46
        richiesti 736`, device a secco, GBA appeso alla schermata di conferma.
        Ora: epoca nuova -> riaggancio, riannuncio, numerazioni riallineate."""
        self._patch("ANNUNCIO_S", 0.0)
        # ladder iniziale e 3 blocchi gia' scambiati
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.b.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.scambia()
        for n in range(3):
            self.a.on_device_block(blocco(n))
        self.scambia()
        self.assertEqual(len(self.dev_b.blocks), 3)

        # il client di B muore e rinasce: sessione NUOVA, epoca nuova
        self.dev_b = FakeDev()
        self.b = ClubSession(False, self.dev_b, self.net.sender("b"),
                             lambda s: None)
        self.b._avviata = True
        self.b.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.scambia()

        # A se n'e' accorto e ha riazzerato il lato-partner
        self.assertEqual(self.a.partner_riavvii, 1)
        # il riannuncio di A fa salire il ladder del B fresco
        self.a.tick()
        self.scambia()
        self.assertIn(CMD_START_HANDSHAKE, self.dev_b.commands)
        # ... e la scala del B fresco arriva fino a `cavo collegato`, che dal
        # 2026-08-27 e' il cancello dei blocchi (prima si consegnava gia'
        # all'handshake, e sul campo quei blocchi cadevano nel vuoto).
        self.b.on_device_status(LINK_ST_CONNECTED)
        self.scambia()

        # i blocchi ripartono: A continua dalla SUA numerazione, il B fresco
        # (attesa 0) chiede il pregresso e lo riceve dalla storia
        self.a.on_device_block(blocco(3))
        self.scambia()
        self.scambia()
        self.assertEqual(self.dev_b.blocks, [blocco(n) for n in range(4)])

    def test_epoca_morta_scartata(self):
        """I pacchetti in volo della vita precedente del partner non devono
        fare ping-pong fra le epoche: una volta abbandonata, si scarta."""
        self.a.on_net_status(0x1111, 0, LINK_ST_HANDSHAKE_RX)   # aggancio
        self.a.on_net_status(0x2222, 0, LINK_ST_HANDSHAKE_RX)   # riavvio
        self.assertEqual(self.a.partner_riavvii, 1)
        self.a.on_net_block(0x1111, 99, blocco(99))             # in volo
        self.assertEqual(self.a.epoca_scarti, 1)
        self.assertEqual(self.dev_a.blocks, [])
        # nemmeno un LEAVE della vita vecchia chiude la sessione nuova
        self.a.on_net_leave(0x1111)
        self.assertFalse(self.a.finita)

    def test_aggancio_a_sessione_vecchia_salta_la_numerazione(self):
        """Il caso simmetrico: siamo NOI i ripartiti, e ci agganciamo a una
        sessione del partner che ha gia' 700 blocchi alle spalle. Un buco piu'
        largo della storia (512) e' irrecuperabile per costruzione: si adotta
        la sua numerazione invece di chiedere l'impossibile per sempre."""
        self.a._tx_seq = 700
        for n in range(3):
            self.a.on_device_block(blocco(n))
        self.scambia()
        self.assertEqual(self.b.salti_numerazione, 1)
        self.assertEqual(self.dev_b.blocks, [blocco(n) for n in range(3)])
        self.assertEqual(self.b.richiesti, 0)

    def test_riannuncio_ingresso_finche_il_partner_non_si_vede(self):
        """L'ENTER partiva UNA volta: un amico riavviato dopo quell'attimo non
        sapeva del club (campo: 10 minuti di 'in attesa' a vuoto). Ora si
        riannuncia finche' il partner non si fa vivo."""
        self._patch("ANNUNCIO_S", 0.0)
        self.a.tick()
        self.a.tick()
        enter = [b for b in self.net.a_to_b
                 if club_unpack(b) and club_unpack(b)[0] == CLUB_ENTER]
        self.assertGreaterEqual(len(enter), 2)
        # appena il partner si vede, l'ENTER si spegne
        self.a.nota_partner(0x1234)
        del self.net.a_to_b[:]
        self.a.tick()
        enter = [b for b in self.net.a_to_b
                 if club_unpack(b) and club_unpack(b)[0] == CLUB_ENTER]
        self.assertEqual(enter, [])

    def test_club_fantasma_muore_da_solo(self):
        """Il caso del 2026-08-21: un EVENT_CLUB stantio apre un club dove
        NESSUN GBA e' al bancone. I due device restano ad "attesa ruolo" e
        la camminata non riparte mai. Il killer: nessun handshake da nessuna
        parte entro FANTASMA_S -> si chiude e si torna a camminare."""
        self._patch("FANTASMA_S", -1.0)
        self.a.tick()
        self.assertTrue(self.a.finita)
        self.assertEqual(self.a.motivo_fine, "fantasma")

    def test_club_vero_non_e_un_fantasma(self):
        """Basta UN handshake (di qua O di la') per non essere un fantasma:
        l'altro giocatore puo' metterci minuti ad arrivare al bancone."""
        self._patch("FANTASMA_S", -1.0)
        self._patch("PARTNER_MUTO_S", 9999.0)
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)   # il MIO GBA c'e'
        self.a.tick()
        self.assertFalse(self.a.finita)
        # e vale anche al contrario: solo il partner si e' presentato
        self.b._patch = None  # (b usa le stesse costanti patchate)
        self.b.on_net_status(0x1234, 0, LINK_ST_HANDSHAKE_RX)
        self.b.tick()
        self.assertFalse(self.b.finita)

    def test_partner_muto_chiude_la_sessione(self):
        """La sessione zombie del campo: il partner non e' MAI entrato e il
        client macinava rilanci all'infinito. Ora si chiude da sola."""
        self._patch("PARTNER_MUTO_S", -1.0)
        self.a.tick()
        self.assertTrue(self.a.finita)
        self.assertTrue(self.a.abortita)
        self.assertEqual(self.a.motivo_fine, "partner muto")

    def test_dup_non_e_progresso(self):
        """I dup dei rilanci NON toccano il watchdog: erano loro a tenere
        vive le sessioni zombie oltre ogni timeout."""
        self.a.on_net_block(0x1234, 0, blocco(0))
        self.a._progresso = 0.0
        self.a.on_net_block(0x1234, 0, blocco(0))    # rilancio -> dup
        self.assertEqual(self.a.duplicati, 1)
        self.assertEqual(self.a._progresso, 0.0)

    def test_riannuncio_stato_non_duplica_connectlink(self):
        """Gli stati riannunciati hanno sseq nuovi (il dedup non li ferma):
        il ConnectLink al device deve restare UNO per epoca del partner."""
        self.a.on_device_status(LINK_ST_HANDSHAKE_RX)
        self.a.on_net_status(0x1234, 0, LINK_ST_HANDSHAKE_RX)
        self.a.on_net_status(0x1234, 1, LINK_ST_CONNECTED)
        self.a.on_net_status(0x1234, 2, LINK_ST_CONNECTED)   # riannuncio
        self.assertEqual(self.dev_a.commands.count(CMD_CONNECT_LINK), 1)

    def test_chiusura_da_entrambi(self):
        self.a.on_device_status(LINK_ST_CLOSED)
        self.scambia()
        self.assertFalse(self.a.finita)
        self.assertFalse(self.b.finita)

        self.b.on_device_status(LINK_ST_CLOSED)
        self.scambia()
        self.assertTrue(self.b.finita)
        # B ha annunciato la chiusura: anche A deve vederla e finire.
        self.scambia()
        self.assertTrue(self.a.finita)


if __name__ == "__main__":
    unittest.main(verbosity=2)
