"""Non-régression sur les VRAIS enregistrements (python/donnees_reelles/, faits avec la carte le 09 et le 10/11).

Ce que la carte doit en tirer (détecteur amélioré, puissance P, réglages de config.h) :
  ventilo_vitesses.csv : ventilateur 50 W seul, 4 changements de vitesse de 3 à 4,5 W puis extinction ;
  tele_seule.csv       : télé allumée (~+33 W, puissance qui fluctue de ±8 W à chaque période) ;
  tele_ventilo.csv     : ventilo seul, +4 W (vitesse), télé allumée (+35 W), ventilo éteint (-48 W).
Les fichiers absents sont sautés (ils ne sont pas livrés avec le code).
"""
import os
import sys
import unittest

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ICI, ".."))
import rejouer  # noqa: E402

DONNEES = os.path.join(ICI, "..", "donnees_reelles")


def evenements(nom):
    chemin = os.path.join(DONNEES, nom)
    if not os.path.exists(chemin):
        raise unittest.SkipTest("%s absent" % nom)
    evts, _, _ = rejouer.rejouer(rejouer.Enregistrement(chemin), "ameliore", "P")
    return evts


def proche(x, y, tol):
    return abs(x - y) <= tol


class EnregistrementsReels(unittest.TestCase):
    def test_vitesses_du_ventilateur(self):
        d = [e["delta"] for e in evenements("ventilo_vitesses.csv")]
        attendu = (4.1, 3.1, -3.1, -4.3, -50.9)
        self.assertEqual(len(d), len(attendu), d)
        for x, y in zip(d, attendu):
            self.assertTrue(proche(x, y, 1.0), d)

    def test_tele_seule_un_seul_on_sans_latence(self):
        ev = evenements("tele_seule.csv")
        self.assertEqual([e["type"] for e in ev], ["ON"], ev)
        self.assertTrue(proche(ev[0]["delta"], 33, 5), ev)
        # firmware <= 0.9.1 : 105 s (bruit figé pendant la chaîne) ; il faut rester bien en dessous
        delai_s = (ev[0]["periode"] - ev[0]["end_of_transient"]) * 0.02
        self.assertLess(delai_s, 15, ev)

    def test_tele_et_ventilateur(self):
        ev = evenements("tele_ventilo.csv")
        d = [e["delta"] for e in ev]
        self.assertEqual(len(d), 3, d)
        self.assertTrue(proche(d[0], 4, 1) and proche(d[1], 35, 5) and proche(d[2], -48, 5), d)


if __name__ == "__main__":
    unittest.main()
