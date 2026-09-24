#!/usr/bin/env python3
"""
test_ruolo_relay.py - il ruolo "relay" del Lua provato FUORI da mGBA
(2026-08-26).

Il ruolo relay e' il client dentro lo script Lua: parla WebSocket col relay
senza Python accanto all'emulatore (nato per il Trimui Brick con Knulli). Fin
qui l'unico modo di provarlo era caricarlo in mGBA e guardare il log - cioe'
un giro da minuti, a mano, che nessuna regressione poteva ripetere.

Qui si estraggono dallo script generato le funzioni PURE (quelle che non
toccano ne' socket ne' emulatore) e si provano con `lupa`, lo stesso
interprete Lua che tools/check_lua.py usa gia' per compilare lo script:

  - wsParseUrl      : ws://host[:porta][/path] -> pezzi (e rifiuta wss://)
  - wsFrame         : il frame WebSocket mascherato che il server si aspetta
  - owlSend         : l'intestazione OWL1, byte per byte, contro protocol.py
  - il timbro dello SLOT nel nibble alto del type (fino a 4 giocatori)
  - relaySlotFor    : assegnazione 0,1,2 e il quarto amico senza avatar
  - la lettura di passotile-config.lua (stanza/peer senza toccare lo script)

Non prova la rete: quella e' la procedura in mGBA. Prova la LOGICA, che e'
esattamente cio' che si rompe in silenzio.

    D:\\Progettini\\Python313\\python.exe tools\\test_ruolo_relay.py
"""

import io
import os
import re
import struct
import sys
import unittest

QUI = os.path.dirname(os.path.abspath(__file__))
RADICE = os.path.dirname(QUI)
sys.path.insert(0, os.path.join(RADICE, "net"))

from protocol import T_EVENT, T_WATCH, pack  # noqa: E402

CORPO = os.path.join(RADICE, "mgba", "inject_body.lua")

# Le funzioni che si possono provare da sole: si ritagliano dal sorgente per
# testare IL FILE VERO, non una copia che domani diverge da lui.
DA_ESTRARRE = ("wsParseUrl", "wsFrame", "owlSend", "relaySlotFor",
               "relaySend")


def ritaglia(sorgente, nome):
    """Il corpo di `local function nome(...)` fino al suo `end` a colonna 0."""
    m = re.search(r"^local function %s\(.*?^end$" % re.escape(nome),
                  sorgente, re.S | re.M)
    if not m:
        raise AssertionError("funzione %s non trovata in inject_body.lua "
                             "(rinominata? il test va aggiornato con lei)" % nome)
    return m.group(0)


class TestRuoloRelay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import lupa
        except ImportError:
            raise unittest.SkipTest("lupa non installato (pip install lupa)")

        with open(CORPO, encoding="utf-8") as f:
            src = f.read()

        # encoding=None: i byte del protocollo non sono testo - e' la stessa
        # scelta di tools/check_lua.py, per gli stessi motivi.
        cls.lua = lupa.LuaRuntime(encoding=None, unpack_returned_tuples=True)
        # L'impalcatura: le poche cose del mondo esterno che le funzioni
        # ritagliate toccano. `ws` e' lo stato del client; console e emu sono
        # finti perche' qui non c'e' ne' mGBA ne' un gioco.
        cls.lua.execute("""
            console = { log = function() end, error = function() end }
            ws = { outSeq = 0, sock = true, peers = {}, slotsFree = {0,1,2},
                   scartati = 0, arretrati = 0, bye = 0 }
            RELAY_PEER = 42
            RELAY_ROOM = 4242
            link = { sent = 0 }
            inviati = {}
            function wsSendRaw(op, payload) inviati[#inviati+1] = {op=op, p=payload}; return true end
        """)
        for nome in DA_ESTRARRE:
            cls.lua.execute(ritaglia(src, nome).replace("local function", "function", 1))
        cls.src = src

    # --- l'URL ------------------------------------------------------------
    def test_url_ws_scomposto(self):
        f = self.lua.globals().wsParseUrl
        self.assertEqual(tuple(f(b"ws://127.0.0.1:9001/passotile/ws")),
                         (b"127.0.0.1", 9001, b"/passotile/ws"))
        self.assertEqual(tuple(f(b"ws://host/ws")), (b"host", 80, b"/ws"))
        # senza path: la RFC vuole almeno "/"
        self.assertEqual(tuple(f(b"ws://host"))[2], b"/")

    def test_wss_rifiutato(self):
        """Il Lua di mGBA non ha TLS: accettare wss:// vorrebbe dire aprire
        una TCP in chiaro verso la 443 e restare muti senza dire perche'."""
        self.assertIsNone(self.lua.globals().wsParseUrl(b"wss://host/ws"))
        self.assertIsNone(self.lua.globals().wsParseUrl(b"http://host/ws"))

    # --- il frame WebSocket ------------------------------------------------
    def test_frame_mascherato_come_vuole_la_rfc(self):
        """Un frame dal CLIENT deve avere il bit di mask (0x80 sul secondo
        byte): relay_ws.py lo pretende, e senza il server chiude."""
        b = bytes(self.lua.globals().wsFrame(2, b"ciao"))
        self.assertEqual(b[0], 0x82)          # FIN + opcode 2 (binario)
        self.assertEqual(b[1], 0x80 | 4)      # mask + lunghezza
        self.assertEqual(b[2:6], b"\x00\x00\x00\x00")   # chiave nulla
        self.assertEqual(b[6:], b"ciao")      # XOR con zero = testo intatto

    def test_frame_lungo_usa_i_16_bit(self):
        b = bytes(self.lua.globals().wsFrame(2, b"x" * 200))
        self.assertEqual(b[1], 0x80 | 126)
        self.assertEqual(struct.unpack(">H", b[2:4])[0], 200)

    # --- l'intestazione OWL1 ----------------------------------------------
    def test_owl_header_identico_a_protocol_py(self):
        """La prova che conta: i byte che il Lua mette sul filo devono essere
        gli stessi che protocol.py mette. Se un giorno divergono, si vede
        qui e non con una partita muta."""
        g = self.lua.globals()
        g.ws.outSeq = 0
        g.owlSend(T_WATCH, b"")
        primo = bytes(g.inviati[1].p)
        atteso = pack(T_WATCH, 42, 4242, 1)
        self.assertEqual(primo, atteso)

        corpo = bytes(range(12))
        g.owlSend(T_EVENT, corpo)
        secondo = bytes(g.inviati[2].p)
        self.assertEqual(secondo, pack(T_EVENT, 42, 4242, 2, corpo))

    def test_owl_a_nome_di_un_altro_peer(self):
        """Il BYE alla stanza VECCHIA (cambio identita' dal pannello o da
        peer(N)): deve uscire col peer e la stanza di PRIMA, o il relay lo
        attribuisce al peer nuovo e i compagni di prima restano con un nostro
        avatar piantato sulla mappa fino al timeout."""
        g = self.lua.globals()
        g.ws.outSeq = 0
        n = len(g.inviati)
        g.owlSend(4, b"", 7, 1234)          # 4 = T_BYE
        self.assertEqual(bytes(g.inviati[n + 1].p), pack(4, 7, 1234, 1))
        # e senza i due argomenti in piu' resta com'era: peer/stanza in uso
        g.owlSend(4, b"")
        self.assertEqual(bytes(g.inviati[n + 2].p), pack(4, 42, 4242, 2))
        # `inviati` e' condiviso fra i test e c'e' chi lo legge per indice:
        # si lascia come lo si e' trovato.
        g.inviati = self.lua.table()

    def test_ultimo_stato_e_una_fotografia_non_un_passo(self):
        """relaySend conserva l'ultimo evento per rispedirlo dopo un
        riaggancio. Se conservasse il PASSO, la ripetizione muoverebbe il
        remoto una seconda volta: va forzato a SYNC (2), lasciando intatto il
        nibble alto - che e' lo slot, non il tipo."""
        g = self.lua.globals()
        g.ws.handshaken = False             # non serve la rete per questa prova
        passo = bytes([0x31, 2, 0, 5, 0, 32, 1, 2, 3, 4, 5, 6])  # slot 3, tipo 1
        g.relaySend(passo)
        salvato = bytes(g.ws.lastEvent)
        self.assertEqual(salvato[0], 0x32)  # slot intatto, tipo -> SYNC
        self.assertEqual(salvato[1:], passo[1:])

        # Un VIA (4) o una scheda (6) NON sono posizioni: non si conservano.
        g.ws.lastEvent = None
        g.relaySend(bytes([4] + [0] * 11))
        self.assertIsNone(g.ws.lastEvent)

    def test_seq_gira_a_255(self):
        g = self.lua.globals()
        g.ws.outSeq = 255
        n = len(g.inviati)
        g.owlSend(T_WATCH, b"")
        self.assertEqual(bytes(g.inviati[n + 1].p)[10], 0)

    # --- gli slot avatar ---------------------------------------------------
    def test_slot_in_ordine_e_quarto_senza_avatar(self):
        g = self.lua.globals()
        g.ws.peers = self.lua.table()
        g.ws.slotsFree = self.lua.table(0, 1, 2)
        self.assertEqual(g.relaySlotFor(11), 0)
        self.assertEqual(g.relaySlotFor(22), 1)
        self.assertEqual(g.relaySlotFor(33), 2)
        # il quarto: false, non 0 - altrimenti finirebbe sopra il primo amico
        self.assertIs(g.relaySlotFor(44), False)
        # e lo slot di un amico gia' visto non cambia
        self.assertEqual(g.relaySlotFor(22), 1)

    def test_timbro_dello_slot_nel_nibble_alto(self):
        """Il contratto con EVENT_SLOT del payload: tipo nel nibble basso,
        slot in quello alto. E' la riga che il ruolo relay esegue prima di
        consegnare l'evento al gioco."""
        # La riga vera del ruolo relay, non una sua imitazione.
        self.assertIn("string.char(string.byte(ev, 1) | (slot << 4))", self.src)
        f = self.lua.eval(
            b"function(tipo, slot) return string.byte(string.char(tipo | (slot << 4))) end")
        self.assertEqual(f(1, 0), 0x01)   # PASSO slot 0 = nudo
        self.assertEqual(f(1, 1), 0x11)
        self.assertEqual(f(4, 2), 0x24)   # VIA slot 2
        self.assertEqual(f(3, 2) & 0x0F, 3)   # il tipo si rilegge intatto

    # --- il file di configurazione ----------------------------------------
    def test_config_letta_come_la_scrive_il_lua(self):
        """Quello che cfgScrivi() scrive, cfgLeggi() deve rileggerlo: e' un
        contratto con se stesso, e si rompe in silenzio (config ignorata =
        stanza sbagliata = 'non ci vediamo')."""
        scritto = ('-- scritto dal ruolo relay di gen3-poke-multiplayer\n'
                   'return { stanza = 4242, peer = 7, relay = "ws://host/ws" }\n')
        dati = self.lua.execute(
            ('local f = load(%r, "cfg", "t", {}); return f()' % scritto).encode())
        self.assertEqual(dati[b"stanza"], 4242)
        self.assertEqual(dati[b"peer"], 7)
        self.assertEqual(dati[b"relay"], b"ws://host/ws")

    def test_config_in_sandbox_non_esegue_codice(self):
        """La config e' un file che l'utente puo' modificare: si carica in un
        ambiente VUOTO (load con env {}), quindi anche una config maligna o
        sbagliata non puo' toccare l'iniettore."""
        self.assertIn('load(testo, CONFIG_FILE, "t", {})', self.src)
        # Una config che prova a riscrivere una globale dell'iniettore: con
        # l'ambiente vuoto scrive dentro la SUA tabella, non nella nostra.
        # (Non solleva un errore - non deve: deve solo non arrivare a noi.)
        cattiva = ('return { stanza = (function() RELAY_ROOM = 999; '
                   'return 1 end)() }')
        prima = self.lua.globals().RELAY_ROOM
        self.lua.execute(
            ('local f = load(%r, "cfg", "t", {}); return f()' % cattiva).encode())
        self.assertEqual(self.lua.globals().RELAY_ROOM, prima)


class TestDiagnosi(unittest.TestCase):
    """Il 301 di nginx deve DIRSI, non lasciare l'emulatore muto.

    Il caso e' reale (2026-08-26): il sito distribuisce uno script che punta a
    ws:// in chiaro, ma nginx sulla 80 rimanda a https - e il Lua non ha TLS
    ne' segue i redirect. Senza un messaggio esplicito il sintomo sarebbe
    "non ci vediamo" senza nessuna causa visibile.
    """

    def test_il_301_spiega_il_problema_e_la_via_d_uscita(self):
        testo = TestRuoloRelay.src if hasattr(TestRuoloRelay, "src") else             io.open(CORPO, encoding="utf-8").read()
        # il ramo esiste...
        self.assertIn('if prima:find(" 30") then', testo)
        # ...e dice le tre cose che servono: che e' solo HTTPS, che manca il
        # TLS, e come uscirne.
        i = testo.index('if prima:find(" 30") then')
        blocco = testo[i:i + 900]
        for pezzo in ("HTTPS", "TLS", "relay("):
            self.assertIn(pezzo, blocco,
                          "il messaggio del 301 non dice %r: chi legge non "
                          "saprebbe cosa fare" % pezzo)


class TestScriptDalSito(unittest.TestCase):
    """Il file che il SITO fa scaricare deve essere Lua valido.

    Il generatore vive in web/js/app.js (configuraLua) e i suoi casi stanno in
    web/bridge_test.html; qui si prova la cosa che il browser NON puo'
    provare da solo - che il risultato compili davvero - rifacendo la stessa
    sostituzione sul template VERO. Se un giorno la generazione producesse un
    file rotto, l'unico sintomo per l'utente sarebbe mGBA che non dice niente
    (il parser Lua fallisce prima di eseguire: e' il difetto del 2026-07-30
    che ha fatto nascere check_lua.py).
    """

    TEMPLATE = os.path.join(RADICE, "web", "passotile-emulatore.lua")
    PAYLOAD = os.path.join(RADICE, "build", "payload.bin")

    # Le stesse tre righe di app.js: un CONTRATTO con build.ps1.
    RIGHE = (
        (r"^RELAY_URL\s*=.*$", 'RELAY_URL = "ws://host/passotile/ws"'),
        (r"^RELAY_ROOM\s*=.*$", "RELAY_ROOM = 4242"),
        (r"^RELAY_PEER\s*=.*$", "RELAY_PEER = 0"),
    )

    def test_il_file_scaricato_compila_col_payload_intatto(self):
        if not os.path.isfile(self.TEMPLATE):
            self.skipTest("manca web/passotile-emulatore.lua: lo genera "
                          "tools/prepara-sito-web.ps1 (o build.ps1 -LinkRole relay)")
        try:
            import lupa
        except ImportError:
            self.skipTest("lupa non installato")

        with open(self.TEMPLATE, encoding="latin-1") as f:
            testo = f.read()
        for pat, val in self.RIGHE:
            self.assertRegex(testo, re.compile(pat, re.M),
                             "il template non ha piu' la riga %s: il sito "
                             "scaricherebbe un file NON configurato" % pat)
            testo = re.sub(pat, val, testo, count=1, flags=re.M)

        lua = lupa.LuaRuntime(encoding=None)
        lua.compile(testo.encode("latin-1"))   # se non compila, alza qui

        # e il payload non deve essersi mosso di un byte
        if os.path.isfile(self.PAYLOAD):
            with open(self.PAYLOAD, "rb") as f:
                atteso = f.read()
            m = re.search(r"^PAYLOAD_BYTES_LEN\s*=\s*(\d+)", testo, re.M)
            if m:
                self.assertEqual(int(m.group(1)), len(atteso))


if __name__ == "__main__":
    unittest.main(verbosity=2)
