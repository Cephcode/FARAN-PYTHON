"""Mode dataset (dataset.py) : jamais de commande impossible (allumer un appareil allumé, éteindre un
appareil éteint), modes d'un même appareil regroupés, état de départ demandé, réponses o/n respectées."""
import io
import os
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from dataset import charger_appareils, charger_groupes, commandes_possibles, constituer  # noqa: E402

APPAREILS_CSV = "id,nom,appareil\n1,tele,tele\n2,ventilateur mode 1,ventilateur\n3,ventilateur mode 2,ventilateur\n4,lampe,\n"


class CarteQuiObeit:
    """Fausse carte : envoie (ou non) l'événement de la commande en cours, selon ce que décide le test."""

    def __init__(self):
        self.a_envoyer = None

    def vider(self):
        pass

    def attendre_evenement(self, timeout=None):
        e, self.a_envoyer = self.a_envoyer, None
        return e


def evenement(typ, delta):
    return {"type": typ, "delta": delta, "transient_duration": 500, "s_before": 0, "s_after": delta,
            "confirmed_by": "e_power", "n_paliers": None}


class TestDataset(unittest.TestCase):
    def setUp(self):
        self.dossier = tempfile.TemporaryDirectory()
        self.csv = os.path.join(self.dossier.name, "appareils.csv")
        with open(self.csv, "w", encoding="utf-8") as f:
            f.write(APPAREILS_CSV)

    def tearDown(self):
        self.dossier.cleanup()

    def test_groupes_et_ancien_format(self):
        self.assertEqual(charger_groupes(self.csv), {1: "tele", 2: "ventilateur", 3: "ventilateur", 4: "lampe"})
        ancien = os.path.join(self.dossier.name, "ancien.csv")
        with open(ancien, "w", encoding="utf-8") as f:
            f.write("id,nom\n1,tele\n2,ventilateur mode 1\n")
        self.assertEqual(charger_groupes(ancien), {1: "tele", 2: "ventilateur mode 1"})   # un appareil par ligne
        self.assertEqual(charger_appareils(self.csv)[3], "ventilateur mode 2")

    def test_commandes_possibles(self):
        groupes = charger_groupes(self.csv)
        c = commandes_possibles({"tele": None, "ventilateur": 2, "lampe": 4}, groupes)
        self.assertIn(("tele", "ALLUMER", 1, "ON"), c)
        self.assertIn(("ventilateur", "ETEINDRE", 2, "OFF"), c)
        self.assertIn(("ventilateur", "PASSER A", 3, None), c)
        self.assertIn(("lampe", "ETEINDRE", 4, "OFF"), c)
        self.assertNotIn(("ventilateur", "ALLUMER", 3, "ON"), c)    # déjà allumé (en mode 1)
        self.assertNotIn(("lampe", "ALLUMER", 4, "ON"), c)
        self.assertEqual(len(c), 4)

    def test_aucune_commande_impossible(self):
        """200 commandes, réponses au hasard : l'état suivi par le test et celui du programme concordent,
        et aucune commande n'est impossible dans l'état réel."""
        appareils, groupes = charger_appareils(self.csv), charger_groupes(self.csv)
        carte, hasard = CarteQuiObeit(), random.Random(7)
        reel = {"tele": None, "ventilateur": 2, "lampe": None}   # au départ : ventilateur allumé en mode 1
        reponses = ["9", "2,3", "2"]                               # inconnu, deux modes du même appareil, puis bon
        demandees, vu = [], {"ALLUMER": 0, "ETEINDRE": 0, "PASSER A": 0, "o": 0, "n": 0, "detecte": 0}

        def sur_commande(action, label, etat_note):
            self.assertEqual(etat_note, reel)                     # le programme a le bon état
            physique = groupes[label]
            if action == "ALLUMER":
                self.assertIsNone(reel[physique], "allumer un appareil allumé")
            elif action == "ETEINDRE":
                self.assertEqual(reel[physique], label, "éteindre un appareil éteint (ou dans un autre mode)")
            else:
                self.assertIsNotNone(reel[physique], "changer le mode d'un appareil éteint")
                self.assertNotEqual(reel[physique], label)
            vu[action] += 1
            demandees.append((action, label))
            # ce que fait « l'utilisateur » : la carte voit l'événement, ou non (puis il répond o ou n)
            tirage = hasard.random()
            if tirage < 0.6:
                carte.a_envoyer = evenement("OFF" if action == "ETEINDRE" else "ON", 50.0)
                vu["detecte"] += 1
                fait = True
            else:
                fait = tirage < 0.8
                reponses.append("o" if fait else "n")
                vu["o" if fait else "n"] += 1
            if fait:
                reel[physique] = None if action == "ETEINDRE" else label

        dataset = os.path.join(self.dossier.name, "dataset.csv")
        with redirect_stdout(io.StringIO()) as sortie:
            ecrites = constituer(carte, appareils, dataset, 200, duree=1, pause=0, repondre=lambda q: reponses.pop(0),
                                 hasard=random.Random(3), groupes=groupes, sur_commande=sur_commande)
        self.assertEqual(len(demandees), 200)
        self.assertEqual(ecrites, vu["detecte"])
        self.assertTrue(all(vu[k] > 5 for k in vu), vu)              # tous les cas ont été essayés
        texte = sortie.getvalue()
        self.assertIn("numero(s) inconnu(s) : 9", texte)
        self.assertIn("un seul mode : ventilateur", texte)
        self.assertIn("(etat note : allumes : ventilateur mode 1)", texte)   # l'état est affiché avant la commande
        self.assertEqual(reponses, [])

    def test_tout_eteint_au_depart(self):
        appareils, groupes = charger_appareils(self.csv), charger_groupes(self.csv)
        commandes = []
        with redirect_stdout(io.StringIO()):
            constituer(CarteQuiObeit(), appareils, os.path.join(self.dossier.name, "d.csv"), 4, duree=1, pause=0,
                       repondre=lambda q: "" if "ALLUMES" in q else "n", hasard=random.Random(1), groupes=groupes,
                       sur_commande=lambda a, l, e: commandes.append(a))
        self.assertEqual(commandes, ["ALLUMER"] * 4)   # « n » à chaque fois : rien ne s'allume, on redemande ALLUMER


if __name__ == "__main__":
    unittest.main()
