#!/usr/bin/env python3
"""
test_mappa_rematch.py - le squadre delle RIVINCITE sulla mappa (2026-08-28).

Quando un allenatore ti richiama col PokeNav non rimette in campo la squadra
di prima: ne ha altre quattro, piu' forti (gRematchTable, src/battle_setup.c).
Sulla mappa non c'erano. Adesso stanno in una tendina chiusa sotto la squadra
base, e questo test guarda le tre cose che possono rompersi in silenzio:

  1. la TABELLA si legge ancora (78 voci: se la decomp cambia forma, la regex
     smette di trovarle e l'unico sintomo sarebbe una tendina che non compare);
  2. i DATI generati sono giusti nei casi che hanno una trappola dentro -
     Cindy salta il _2, Wally VR ripete il _5, la Superquattro ripete lo
     stesso allenatore cinque volte (nessuna squadra nuova: niente tendina),
     Norman e' raggiungibile solo seguendo un salto nello script;
  3. la PAGINA sa disegnarle: i due punti che mostrano una squadra hanno il
     ramo delle rivincite, la tendina nasce CHIUSA, e le etichette esistono in
     italiano e in inglese.

Non prova l'aspetto a schermo: quello si guarda con gli occhi (procedura in
NOTES). Prova che i dati ci siano e che il codice che li disegna esista.

    D:\\Progettini\\Python313\\python.exe tools\\test_mappa_rematch.py
"""

import json
import os
import re
import sys
import unittest

QUI = os.path.dirname(os.path.abspath(__file__))
RADICE = os.path.dirname(QUI)
sys.path.insert(0, QUI)

DATI = os.path.join(RADICE, "net", "mappa", "dati.json")
PAGINA = os.path.join(RADICE, "net", "mappa.html")


class TestRivincite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DATI):
            raise unittest.SkipTest("net/mappa/dati.json non c'e': "
                                    "lancia prima tools/gen_mappa.py")
        with open(DATI, encoding="utf-8") as f:
            cls.dati = json.load(f)
        with open(PAGINA, encoding="utf-8") as f:
            cls.html = f.read()
        cls.per_tc = {}
        for chiave, mappa in cls.dati["mappe"].items():
            for pe in mappa.get("personaggi", []):
                if pe.get("tc"):
                    cls.per_tc.setdefault(pe["tc"], []).append((chiave, pe))

    # --- 1. la tabella nella decomp ---------------------------------------

    def test_la_tabella_si_legge_ancora(self):
        import gen_mappa
        tabella = gen_mappa.carica_rematch()
        self.assertGreaterEqual(
            len(tabella), 70,
            "gRematchTable letta male: attese ~78 voci, trovate %d. Se la "
            "decomp ha cambiato forma, la regex di carica_rematch va con lei"
            % len(tabella))
        self.assertEqual(tabella.get("TRAINER_ROSE_1"),
                         ["TRAINER_ROSE_2", "TRAINER_ROSE_3",
                          "TRAINER_ROSE_4", "TRAINER_ROSE_5"])
        # Cindy salta il _2: e' cosi' nel gioco, non un errore di lettura.
        self.assertEqual(tabella.get("TRAINER_CINDY_1"),
                         ["TRAINER_CINDY_3", "TRAINER_CINDY_4",
                          "TRAINER_CINDY_5", "TRAINER_CINDY_6"])

    # --- 2. i dati generati ------------------------------------------------

    def test_quanti_allenatori_hanno_la_tendina(self):
        con = {tc for tc, voci in self.per_tc.items()
               if any(pe.get("rematch") for _, pe in voci)}
        self.assertGreaterEqual(
            len(con), 70,
            "solo %d allenatori con rivincite: la tabella e la mappa non si "
            "stanno piu' agganciando" % len(con))

    def test_i_casi_con_la_trappola(self):
        attesi = {
            "TRAINER_ROSE_1": 4,
            "TRAINER_CINDY_1": 4,      # salta il _2, restano comunque 4
            "TRAINER_WALLY_VR_2": 3,   # il _5 e' ripetuto: 4 slot, 3 squadre
            "TRAINER_NORMAN_1": 4,     # raggiungibile solo dietro un salto
            "TRAINER_ROXANNE_1": 4,    # capopalestra: la tabella dice CITTA'
        }
        for tc, quante in attesi.items():
            with self.subTest(tc=tc):
                voci = self.per_tc.get(tc)
                self.assertTrue(voci, "%s non e' sulla mappa" % tc)
                for _, pe in voci:
                    self.assertEqual(len(pe.get("rematch", [])), quante)

    def test_la_superquattro_non_ha_tendina(self):
        """Sidney, Phoebe, Glacia, Drake e Wallace hanno lo STESSO trainer
        ripetuto cinque volte: non c'e' nessuna squadra nuova da mostrare, e
        una tendina con cinque copie identiche sarebbe peggio di niente."""
        for tc in ("TRAINER_SIDNEY", "TRAINER_PHOEBE", "TRAINER_GLACIA",
                   "TRAINER_DRAKE", "TRAINER_WALLACE"):
            for _, pe in self.per_tc.get(tc, []):
                self.assertNotIn("rematch", pe,
                                 "%s mostra rivincite identiche alla base" % tc)

    def test_nessuna_squadra_ripetuta(self):
        """Il dedup vale per TUTTA la voce, non solo per le adiacenti: due
        tendine con la stessa squadra due volte non aggiungono niente."""
        for tc, voci in self.per_tc.items():
            for _, pe in voci:
                rem = pe.get("rematch") or []
                impronte = [json.dumps(pe["squadra"], sort_keys=True)]
                for r in rem:
                    imp = json.dumps(r["squadra"], sort_keys=True)
                    self.assertNotIn(imp, impronte,
                                     "%s ha una squadra ripetuta" % tc)
                    impronte.append(imp)

    def test_gli_sprite_dei_rematch_ci_sono(self):
        """Ogni Pokemon che la tendina disegna deve avere il suo sprite: le
        rivincite tirano dentro ~45 specie che sulla mappa non compaiono
        altrove (Blissey, Dragonite, Alakazam...)."""
        mancanti = set()
        for tc, voci in self.per_tc.items():
            for _, pe in voci:
                for r in (pe.get("rematch") or []):
                    for mon in r["squadra"]:
                        if mon["cost"] not in self.dati["front"]:
                            mancanti.add(mon["cost"])
        self.assertFalse(mancanti,
                         "specie senza sprite anteriore: %s" % sorted(mancanti))

    # --- 3. la pagina ------------------------------------------------------

    def test_i_due_punti_disegnano_le_rivincite(self):
        """La squadra si mostra in due posti - la schedina al volo e il
        pannello del fianco - e le rivincite devono esserci in tutti e due."""
        rami = re.findall(r"if \(pe\.rematch && pe\.rematch\.length\)", self.html)
        self.assertEqual(len(rami), 2,
                         "attesi 2 punti che disegnano le rivincite, trovati %d"
                         % len(rami))

    def test_la_tendina_nasce_chiusa(self):
        """<details> senza `open`: chiusa di base, come chiesto. Se qualcuno
        aggiungesse `open` la scheda di ogni allenatore diventerebbe un muro
        di cinque squadre."""
        for m in re.finditer(r"<details class='sez'[^>]*>", self.html):
            self.assertNotIn(" open", m.group(0))

    def test_le_etichette_in_tutte_e_due_le_lingue(self):
        for chiave in ("rivincite", "rivincita", "rivinciteNota"):
            with self.subTest(chiave=chiave):
                self.assertEqual(
                    len(re.findall(r"\b%s:" % chiave, self.html)), 2,
                    "la chiave %s deve stare in UI.it e in UI.en" % chiave)
        # e il numero delle squadre entra nel sommario
        self.assertIn('rivincite: "rivincite (%s squadre)"', self.html)
        self.assertIn('rivincite: "rematches (%s parties)"', self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
