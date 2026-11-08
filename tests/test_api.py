"""Tests de l'API (api.py) et du comptage de la consommation (consommation.py), sans carte.
Le temps est simulé : une fausse horloge que le test fait avancer."""
import csv
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

try:
    from fastapi.testclient import TestClient
    import api
    FASTAPI = True
except ImportError:   # fastapi / httpx pas installés : pip install -r requirements.txt
    FASTAPI = False

from service import Production  # noqa: E402

TARIF = 100.0
APPAREILS = {1: "tele", 2: "ventilateur", 3: "fer a repasser"}


class Horloge:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def a(annee, mois, jour, h=0, m=0, s=0):
    return datetime(annee, mois, jour, h, m, s).timestamp()


def on(delta):
    return {"type": "ON", "delta": float(delta)}


def off(delta):
    return {"type": "OFF", "delta": -float(delta)}


@unittest.skipUnless(FASTAPI, "fastapi non installé")
class TestApi(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.horloge = Horloge(a(2026, 11, 8, 10, 0, 0))

    def tearDown(self):
        self.dossier.cleanup()

    def production(self, demo=False, duree=120):
        p = Production(APPAREILS, tarif=TARIF, dossier=self.dossier.name, demo=demo, duree_demo_s=duree,
                       journal=None, horloge=self.horloge)
        return p, TestClient(api.creer_app(p))

    def test_sante_et_page(self):
        p, c = self.production()
        self.assertEqual(c.get("/api/sante").json()["ok"], True)
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Ce qui est allumé", r.text)

    def test_etat_seuls_les_allumes_parts_et_couts(self):
        p, c = self.production()
        p.evenement(1, on(60))
        self.horloge.t += 60
        p.evenement(2, on(240))
        self.horloge.t += 30
        p.signe_de_vie()                                                 # la carte vient d'envoyer une ligne
        e = c.get("/api/etat").json()
        self.assertTrue(e["carte_connectee"])
        self.assertEqual(e["puissance_totale_w"], 300.0)
        self.assertEqual(e["cout_heure_fcfa"], 30.0)                     # 0,3 kW x 100 FCFA
        noms = [x["nom"] for x in e["appareils_allumes"]]
        self.assertEqual(noms, ["ventilateur", "tele"])                  # du plus gros au plus petit
        v, t = e["appareils_allumes"]
        self.assertEqual((v["part_pct"], t["part_pct"]), (80.0, 20.0))
        self.assertEqual((v["cout_heure_fcfa"], t["cout_heure_fcfa"]), (24.0, 6.0))
        self.assertEqual((t["duree_s"], v["duree_s"]), (90, 30))
        self.assertEqual(t["depuis"], "2026-11-08T10:00:00")
        p.evenement(1, off(60))                                          # la télé s'éteint
        e = c.get("/api/etat").json()
        self.assertEqual([x["nom"] for x in e["appareils_allumes"]], ["ventilateur"])
        self.assertEqual(e["appareils_allumes"][0]["part_pct"], 100.0)
        self.horloge.t += 10
        self.assertFalse(c.get("/api/etat").json()["carte_connectee"])   # plus de nouvelles depuis 10 s

    def test_rapport_journalier_compte_jusqu_a_maintenant(self):
        p, c = self.production()
        p.evenement(3, on(1000))
        self.horloge.t += 1800                    # 30 min de fer, toujours allumé
        p.evenement(1, on(60))
        self.horloge.t += 600
        p.evenement(1, off(60))                   # télé 10 min
        self.horloge.t += 1200                    # le fer continue : 30 + 10 + 20 min = 1 h en tout
        e = c.get("/api/etat").json()
        self.assertAlmostEqual(e["energie_jour_kwh"], 1.0 + 0.01, places=4)
        self.assertAlmostEqual(e["cout_jour_fcfa"], 101.0, places=1)
        r = c.get("/api/rapports/journalier?format=csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        lignes = {l["appareil"]: l for l in csv.DictReader(io.StringIO(r.text))}
        self.assertEqual(lignes["fer a repasser"]["energie_kwh"], "1.0")
        self.assertEqual((lignes["fer a repasser"]["duree_s"], lignes["fer a repasser"]["puissance_moyenne_w"]),
                         ("3600", "1000.0"))
        self.assertEqual((lignes["tele"]["utilisations"], lignes["tele"]["duree_s"]), ("1", "600"))
        self.assertEqual(lignes["TOTAL"]["cout_fcfa"], "101.0")
        self.assertEqual(lignes["TOTAL"]["debut"], "2026-11-08T00:00:00")   # depuis minuit

    def test_minuit_archive_et_remise_a_zero(self):
        self.horloge.t = a(2026, 11, 8, 23, 0, 0)
        p, c = self.production()
        p.evenement(2, on(100))                   # 1 h avant minuit, reste allumé
        self.horloge.t = a(2026, 11, 9, 0, 30, 0)
        e = c.get("/api/etat").json()
        self.assertAlmostEqual(e["energie_jour_kwh"], 0.05, places=4)     # seulement la demi-heure d'aujourd'hui
        self.assertAlmostEqual(e["energie_mois_kwh"], 0.15, places=4)     # + l'heure d'hier, archivée
        self.assertEqual(e["appareils_allumes"][0]["duree_s"], 5400)      # il n'a pas été coupé
        archive = os.path.join(self.dossier.name, "jour_2026-11-08.csv")
        self.assertTrue(os.path.exists(archive))
        with open(archive, encoding="utf-8") as f:
            jour = {l["appareil"]: l for l in csv.DictReader(f)}
        self.assertEqual((jour["ventilateur"]["energie_kwh"], jour["ventilateur"]["duree_s"]), ("0.1", "3600"))
        self.assertEqual(jour["ventilateur"]["fin"], "2026-11-09T00:00:00")
        p.evenement(2, off(100))
        r = c.get("/api/rapports/mensuel?format=csv")
        lignes = {l["appareil"]: l for l in csv.DictReader(io.StringIO(r.text))}
        self.assertEqual(lignes["ventilateur"]["utilisations"], "1")      # une seule utilisation sur deux jours
        self.assertEqual(lignes["ventilateur"]["energie_kwh"], "0.15")

    def test_mode_demo_remise_a_zero_toutes_les_periodes(self):
        self.horloge.t = a(2026, 11, 8, 10, 0, 0)
        p, c = self.production(demo=True, duree=10)       # « journée » de 10 s pour le test
        p.evenement(3, on(3600))                           # 3,6 kW : 1 Wh par seconde
        self.horloge.t += 4
        p.evenement(3, off(3600))
        self.horloge.t += 3
        e = c.get("/api/etat").json()
        self.assertAlmostEqual(e["energie_jour_kwh"], 0.004, places=6)
        self.assertTrue(e["mode_demo"])
        self.horloge.t += 5                                # 10:00:12 : nouvelle période
        p.evenement(1, on(3600))
        self.horloge.t += 2
        e = c.get("/api/etat").json()
        self.assertAlmostEqual(e["energie_jour_kwh"], 0.002, places=6)     # remis à zéro, puis 2 s de télé
        self.assertAlmostEqual(e["energie_mois_kwh"], 0.006, places=6)     # le « mois » regroupe les périodes
        self.assertEqual(e["debut_jour"], "2026-11-08T10:00:10")
        dossier = os.path.join(self.dossier.name, "demo")
        self.assertIn("periode_2026-11-08_10-00-00.csv", os.listdir(dossier))
        self.horloge.t += 30                                # 10:00:44 : trois périodes passent d'un coup
        e = c.get("/api/etat").json()
        self.assertAlmostEqual(e["energie_jour_kwh"], 0.004, places=6)     # la télé, toujours allumée : 40 à 44 s
        r = c.get("/api/rapports/mensuel?format=csv")
        lignes = {l["appareil"]: l for l in csv.DictReader(io.StringIO(r.text))}
        self.assertEqual(lignes["tele"]["utilisations"], "1")
        self.assertAlmostEqual(float(lignes["TOTAL"]["energie_kwh"]), 0.004 + 0.032, places=4)   # fer 4 s, télé 32 s

    def test_mensuel_construit_a_partir_des_archives(self):
        # deux journées déjà archivées (fichiers), puis le programme démarre le 3
        self.horloge.t = a(2026, 11, 1, 20, 0, 0)
        p, c = self.production()
        p.evenement(3, on(1000))
        self.horloge.t += 3600
        p.evenement(3, off(1000))                 # 1 kWh le 1er
        self.horloge.t = a(2026, 11, 2, 12, 0, 0)
        p.evenement(1, on(100))
        self.horloge.t += 3600
        p.evenement(1, off(100))                  # 0,1 kWh le 2
        self.horloge.t = a(2026, 11, 3, 9, 0, 0)
        p.tic()
        del p, c
        p2, c2 = self.production()                 # redémarrage du programme : tout vient des fichiers
        p2.evenement(2, on(50))
        self.horloge.t += 3600                    # 0,05 kWh aujourd'hui, encore allumé
        e = c2.get("/api/etat").json()
        self.assertAlmostEqual(e["energie_mois_kwh"], 1.15, places=4)
        self.assertAlmostEqual(e["energie_jour_kwh"], 0.05, places=4)
        r = c2.get("/api/rapports/mensuel?format=csv")
        lignes = {l["appareil"]: l for l in csv.DictReader(io.StringIO(r.text))}
        self.assertEqual(lignes["TOTAL"]["debut"], "2026-11-01T00:00:00")
        self.assertEqual(lignes["fer a repasser"]["energie_kwh"], "1.0")
        self.assertEqual(lignes["TOTAL"]["cout_fcfa"], "115.0")
        # le mois d'avant n'est pas compté
        self.horloge.t = a(2026, 12, 1, 0, 0, 30)
        e = c2.get("/api/etat").json()
        self.assertLess(e["energie_mois_kwh"], 0.01)

    def test_redemarrage_garde_la_journee_en_cours(self):
        p, c = self.production()
        p.evenement(3, on(1000))
        self.horloge.t += 360
        p.evenement(3, off(1000))
        p2, c2 = self.production()
        self.assertAlmostEqual(c2.get("/api/etat").json()["energie_jour_kwh"], 0.1, places=4)

    def test_telechargements_pdf_ou_html(self):
        p, c = self.production()
        p.evenement(1, on(55))
        self.horloge.t += 120
        for quoi in ("journalier", "mensuel"):
            r = c.get("/api/rapports/%s?format=pdf" % quoi)
            self.assertEqual(r.status_code, 200)
            if r.headers["content-type"] == "application/pdf":
                self.assertTrue(r.content.startswith(b"%PDF"))
            else:
                self.assertIn("text/html", r.headers["content-type"])
                self.assertIn("Faran", r.text)
            r = c.get("/api/rapports/%s?format=html" % quoi)
            self.assertIn("tele", r.text)
        self.assertEqual(c.get("/api/rapports/annuel").status_code, 404)
        self.assertEqual(c.get("/api/rapports/journalier?format=doc").status_code, 422)


if __name__ == "__main__":
    unittest.main()
