#!/usr/bin/env python3
"""
test_pannello.py - il pannello web provato SENZA browser e SENZA hardware.

Il pannello avvia programmi: e' il pezzo dove uno sbaglio costa piu' caro (una
porta aperta al mondo, un comando costruito male, un figlio che resta acceso
dopo la chiusura). Qui si prova tutto quello che si puo' provare a secco:

- il server risponde e serve la pagina;
- ascolta SOLO su 127.0.0.1, mai sulla rete;
- la validazione della configurazione rifiuta l'immondizia PRIMA di
  costruire una riga di comando;
- l'avvio vero del relay funziona e il suo log arriva nel pannello (e' il
  giro completo: sottoprocesso -> pipe -> ring buffer -> API);
- la mutua esclusione sull'adattatore USB;
- la sintesi si aggiorna leggendo le righe di client.py (i formati sono un
  contratto: se cambiano, questo test cade e si scopre subito).

    python test_pannello.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)

# La configurazione del test va in un file usa-e-getta: senza, provare il
# pannello sovrascriveva quella VERA dell'utente, e alla partita dopo il relay
# puntava a una porta casuale (successo il 2026-08-09).
CONFIG_TEST = os.path.join(tempfile.gettempdir(), "pannello.test.config.json")
os.environ["PANNELLO_CONFIG"] = CONFIG_TEST

import pannello as P  # noqa: E402


def porta_libera():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestValidazione(unittest.TestCase):
    """La validazione gira in-process: nessun server, nessun figlio."""

    def setUp(self):
        self.p = P.Pannello.__new__(P.Pannello)   # senza __init__: niente file
        self.p.config = dict(P.CONFIG_DEFAULT)
        self.p.salva_config = lambda: None

    def test_relay_malformato_rifiutato(self):
        for cattivo in ("", "solo-host", "host:", ":9000", "host:99999",
                        "host 9000", "host:9000; rm -rf /", "a" * 300 + ":9000"):
            ok, msg = self.p.aggiorna_config({"relay": cattivo})
            self.assertFalse(ok, "accettato un relay non valido: %r" % cattivo)
            self.assertTrue(msg)

    def test_relay_buoni_accettati(self):
        for buono in ("127.0.0.1:9000", "93.42.251.212:9000",
                      "mionome.duckdns.org:9000"):
            ok, _ = self.p.aggiorna_config({"relay": buono})
            self.assertTrue(ok, "rifiutato un relay valido: %r" % buono)
            self.assertEqual(self.p.config["relay"], buono)

    def test_relay_websocket_accettato_e_ruolo_corretto(self):
        """Il relay via WebSocket (2026-08-25): e' la strada che passa verso
        la VPS, e chi la usa NON ospita niente - il ruolo deve tornare
        'amico' da solo, o il pannello offrirebbe di accendere un relay
        locale che nessuno userebbe."""
        url = "wss://gbcatrade.wired-ariel.it/passotile/ws"
        ok, _ = self.p.aggiorna_config({"relay": url, "ruolo": "ospite"})
        self.assertTrue(ok, "rifiutato un relay WebSocket valido")
        self.assertEqual(self.p.config["relay"], url)
        self.assertEqual(self.p.config["ruolo"], "amico")
        # anche ws:// in chiaro, per le prove in locale
        self.assertTrue(self.p.aggiorna_config({"relay": "ws://127.0.0.1:9001/"})[0])
        # ma non un URL storpio
        for cattivo in ("wss://", "ws:/host/ws", "wss://host con spazio/ws",
                        "http://gbcatrade.wired-ariel.it/passotile/ws"):
            ok, _ = self.p.aggiorna_config({"relay": cattivo})
            self.assertFalse(ok, "accettato un URL non valido: %r" % cattivo)

    def test_stanza_e_timing_nei_limiti(self):
        self.assertFalse(self.p.aggiorna_config({"stanza": 0})[0])
        self.assertFalse(self.p.aggiorna_config({"stanza": 70000})[0])
        self.assertFalse(self.p.aggiorna_config({"stanza": "ciao"})[0])
        self.assertFalse(self.p.aggiorna_config({"timing": 10})[0])
        self.assertTrue(self.p.aggiorna_config({"stanza": 58243})[0])

    def test_config_non_valida_non_tocca_quella_buona(self):
        self.p.aggiorna_config({"stanza": 4242})
        self.p.aggiorna_config({"relay": "spazzatura"})
        self.assertEqual(self.p.config["stanza"], 4242)


class TestSintesi(unittest.TestCase):
    """La sintesi si ricava dalle righe di client.py: e' un CONTRATTO con i
    formati di quel file, e questo test e' il posto dove si rompe di sicuro
    se qualcuno li cambia."""

    def setUp(self):
        self.p = P.Pannello.__new__(P.Pannello)
        import threading
        self.p.lock = threading.Lock()
        self.p.righe = []
        self.p.seq = 0
        self.p.sintesi = self.p._sintesi_vuota()

    def test_legge_mappa_e_contatori(self):
        self.p.riga("gioca", "[client 1] rtt 31 ms | inviati 42 | ricevuti 17 "
                             "| scartati 0 dup + 0 arretrati | ricuciti 0 (+0 "
                             "larghi) | persi dal simulatore 0 | VIA 0 | "
                             "stanza 58243 | io su mappa 10.6")
        self.assertEqual(self.p.sintesi["mappa"], "10.6")
        self.assertEqual(self.p.sintesi["inviati"], 42)
        self.assertEqual(self.p.sintesi["ricevuti"], 17)
        self.assertTrue(self.p.sintesi["amico"])

    def test_legge_parole_al_secondo(self):
        self.p.riga("gioca", "[client 1] [usb] tx 10 frame (0 errori) | rx 88 "
                             "frame, 226 parole/s (1131 tot, 40 pacchetti), "
                             "0 errori, 0 resync | status 0xff08")
        self.assertEqual(self.p.sintesi["parole_s"], "226")

    def test_multiboot_riuscito(self):
        self.assertFalse(self.p.sintesi["caricato"])
        self.p.riga("multiboot", "[mb  ] DONE!  CRC 0x1234")
        self.assertTrue(self.p.sintesi["caricato"])

    def test_avviso_porta_chiusa_e_club(self):
        self.p.riga("gioca", "[client 1]       il TUO GBA ha chiuso la porta "
                             "seriale: sei fuori dall'overworld")
        self.assertIn("fuori dal mondo di gioco", self.p.sintesi["avviso"])

        self.p.riga("gioca", "[client 1] [club ] il TUO GBA e' entrato al "
                             "Cable Club")
        self.assertEqual(self.p.sintesi["club"], "in corso")
        self.p.riga("gioca", "[client 1] [club ] Pico di nuovo in passthrough: "
                             "si ricammina")
        self.assertIsNone(self.p.sintesi["club"])


class TestServer(unittest.TestCase):
    """Il pannello VERO, in un processo separato, come lo lancia l'utente."""

    @classmethod
    def setUpClass(cls):
        cls.porta = porta_libera()
        cls.proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(QUI, "pannello.py"),
             "--porta", str(cls.porta), "--niente-browser"],
            cwd=QUI, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, errors="replace")
        # Si aspetta che la porta risponda, invece di dormire a caso.
        for _ in range(100):
            try:
                cls.get("/api/tick?da=0")
                return
            except Exception:
                time.sleep(0.1)
        raise AssertionError("il pannello non si e' aperto in 10 s")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    @classmethod
    def url(cls, rotta):
        return "http://127.0.0.1:%d%s" % (cls.porta, rotta)

    @classmethod
    def get(cls, rotta):
        with urllib.request.urlopen(cls.url(rotta), timeout=5) as r:
            return r.read()

    @classmethod
    def post(cls, rotta, corpo):
        req = urllib.request.Request(
            cls.url(rotta), data=json.dumps(corpo).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())

    def test_serve_la_pagina(self):
        html = self.get("/").decode("utf-8", "replace")
        self.assertIn("gen3-poke-multiplayer", html)
        # L'insegna e' il riquadro d'erba con le due orme (niente Pokeball CSS
        # dalla riscrittura del 2026-08-21): se sparisce quella, la testa e'
        # rimasta senza marchio.
        self.assertIn('class="marchio"', html)
        self.assertIn("img/orma-treecko-scuro-bordo.png", html)
        self.assertIn("img/orma-torchic-scuro-bordo.png", html)
        # Nessuna risorsa esterna: sarebbe un download all'apertura e una
        # pagina rotta offline (per non parlare degli asset Nintendo).
        for schema in ("http://", "https://", "//cdn"):
            self.assertNotIn('src="' + schema, html)
            self.assertNotIn('href="' + schema, html)

    def test_serve_le_immagini(self):
        """La testa della pagina e' fatta di PNG in net/img/: se il pannello
        non li serve, resta un rettangolo verde vuoto."""
        for nome in ("tile-erba-alta.png", "orma-treecko-scuro-bordo.png",
                     "orma-torchic-scuro-bordo.png", "favicon.png"):
            corpo = self.get("/img/" + nome)
            self.assertEqual(corpo[:8], bytes([137, 80, 78, 71, 13, 10, 26, 10]), nome)
        # Nessuna uscita da net/img/, e niente che non sia un .png.
        for cattivo in ("/img/../pannello.py", "/img/sotto/x.png", "/img/x.txt"):
            with self.assertRaises(urllib.error.HTTPError):
                self.get(cattivo)

    def test_solo_loopback(self):
        """Il pannello avvia programmi: NON deve rispondere sulla rete."""
        mio_ip = socket.gethostbyname(socket.gethostname())
        if mio_ip.startswith("127."):
            self.skipTest("questa macchina ha solo il loopback")
        s = socket.socket()
        s.settimeout(2)
        esito = s.connect_ex((mio_ip, self.porta))
        s.close()
        self.assertNotEqual(esito, 0,
                            "il pannello risponde su %s: e' esposto alla rete!"
                            % mio_ip)

    def test_config_rifiutata_via_api(self):
        r = self.post("/api/config", {"relay": "non-un-indirizzo"})
        self.assertFalse(r["ok"])
        self.assertIn("relay", r["messaggio"])

    def test_relay_si_avvia_e_il_log_arriva(self):
        porta_relay = porta_libera()
        r = self.post("/api/config", {"ruolo": "ospite",
                                      "relay": "127.0.0.1:%d" % porta_relay,
                                      "stanza": 58243})
        self.assertTrue(r["ok"], r.get("messaggio"))

        r = self.post("/api/avvia", {"cosa": "relay"})
        self.assertTrue(r["ok"], r.get("messaggio"))

        # Il giro completo: relay.py -> pipe -> ring buffer -> /api/tick.
        atteso = "in ascolto su UDP %d" % porta_relay
        trovato = False
        for _ in range(60):
            s = json.loads(self.get("/api/tick?da=0"))
            if any(atteso in riga["testo"] for riga in s["righe"]):
                trovato = True
                break
            time.sleep(0.1)
        self.assertTrue(trovato, "il log del relay non e' arrivato al pannello")

        s = json.loads(self.get("/api/tick?da=0"))
        self.assertTrue(s["processi"]["relay"]["vivo"])

        r = self.post("/api/ferma", {"cosa": "relay"})
        self.assertTrue(r["ok"])
        for _ in range(40):
            s = json.loads(self.get("/api/tick?da=0"))
            if not s["processi"]["relay"]["vivo"]:
                break
            time.sleep(0.1)
        self.assertFalse(s["processi"]["relay"]["vivo"],
                         "il relay e' rimasto acceso dopo lo stop")

    def test_relay_negato_a_chi_non_ospita(self):
        self.post("/api/config", {"ruolo": "amico", "relay": "1.2.3.4:9000"})
        r = self.post("/api/avvia", {"cosa": "relay"})
        self.assertFalse(r["ok"])
        self.assertIn("OSPITA", r["messaggio"])
        # si rimette com'era, per non lasciare il file di config sporco
        self.post("/api/config", {"ruolo": "ospite", "relay": "127.0.0.1:9000"})

    def test_solo_incrementale(self):
        """?da=N deve dare SOLO le righe nuove: e' cio' che tiene leggero il
        polling ogni mezzo secondo."""
        s1 = json.loads(self.get("/api/tick?da=0"))
        s2 = json.loads(self.get("/api/tick?da=%d" % s1["seq"]))
        self.assertEqual(s2["righe"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
