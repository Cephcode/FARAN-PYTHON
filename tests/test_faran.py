"""Tests du côté PC, sans carte ni port série.   Depuis python/ :  python3 -m unittest discover -s tests
Si FARAN_E2E pointe sur le firmware compilé pour PC (run_tests.sh le fait), la chaîne complète
carte -> main.py est aussi testée."""
import io
import os
import random
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import adc  # noqa: E402
import faran_link as link  # noqa: E402
import listener  # noqa: E402
import rejouer  # noqa: E402
from board import Board  # noqa: E402
from classifier import EventClassifier  # noqa: E402
import diagnostic as dg  # noqa: E402
from dataset import charger_appareils, constituer  # noqa: E402
from registre import Registre, horloge  # noqa: E402


class FakeSerial:
    """Rejoue des lignes comme si elles venaient de la carte."""
    def __init__(self, lines):
        self.lines = [l if isinstance(l, bytes) else l.encode() for l in lines]
        self.sent, self.eof = [], False

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        self.eof = True
        return b""

    def write(self, b):
        self.sent.append(b.decode())

    def close(self):
        pass


def evt(seq, typ, delta, dur=240):
    return link.encode("EVT,%d,%s,%.3f,100,%.3f,10,22,22,%d,e_power,0,1000" % (seq, typ, delta, 100 + delta, dur))


def evt8(seq, typ, delta, dur=240, ds=None, dpf=0.1, paliers=1, chaine=400):
    """Ligne EVT du firmware >= 0.8 (7 champs de plus)."""
    ds = delta * 1.2 if ds is None else ds
    return link.encode("EVT,%d,%s,%.3f,100,%.3f,10,22,22,%d,e_power,0,1000,%.3f,%.4f,%d,%d,%.3f,P,ameliore" % (
        seq, typ, delta, 100 + delta, dur, ds, dpf, paliers, chaine, abs(delta)))


def fake_board(lines):
    b = Board.__new__(Board)
    b.serial, b.afficher_infos = FakeSerial(lines), False
    return b


class TestAdcDifferentiel(unittest.TestCase):
    """Courant en différentiel A1 - A3 : calibre propre à chaque voie, centrage à 0 mV pour le courant."""
    ADC_DIFF = "ADC,1,0.125000,0.015625,4096,512,A0,A1-A3"
    ADC_SIMPLE = "ADC,0,0.125000,0.125000,4096,4096,A0,A1"

    def tearDown(self):
        adc.reinitialiser()

    def test_ligne_adc_appliquee_au_passage(self):
        link.parse_line(link.encode(self.ADC_SIMPLE))
        self.assertEqual((adc.voie("I").entree, adc.lsb_mv("I"), adc.voie("I").centre_mv), ("A1", 0.125, 1650.0))
        link.parse_line(link.encode(self.ADC_DIFF))
        self.assertEqual((adc.voie("I").entree, adc.lsb_mv("I"), adc.voie("I").centre_mv), ("A1-A3", 0.015625, 0.0))
        self.assertEqual(adc.lsb_mv("V"), 0.125)
        self.assertIsNone(link.parse_adc(link.parse_line(link.encode("ADC,1,0,0.01,4096,512,A0,A1-A3"))))

    def test_raw_converti_avec_le_calibre_de_la_voie(self):
        link.parse_line(link.encode(self.ADC_DIFF))
        blk_i = link.parse_raw(link.parse_line(link.encode("RAW,I,0,7000,2,640,-640")))
        blk_v = link.parse_raw(link.parse_line(link.encode("RAW,V,0,7000,2,13200,13200")))
        self.assertEqual([mv for _, mv in blk_i.samples()], [10.0, -10.0])     # 640 x 0,015625 mV
        self.assertEqual([mv for _, mv in blk_v.samples()], [1650.0, 1650.0])  # 13200 x 0,125 mV

    def test_diagnostic_centrage_selon_le_cablage(self):
        link.parse_line(link.encode(self.ADC_DIFF))
        bon = {"offset_mv": 0.3, "vpp_mv": 12.0}
        self.assertEqual(listener.diagnostic("ISTAT", bon), [])
        mauvais = {"offset_mv": 1650.0, "vpp_mv": 12.0}        # A3 non reliée : le 1,65 V ne s'annule plus
        txt = " ".join(listener.diagnostic("ISTAT", mauvais))
        self.assertIn("centrage", txt)
        self.assertIn("ecrete", txt)                          # 1650 mV dépasse le calibre +-512 mV
        c = dg.analyser_adc({"vstat": [], "istat": [dict(mauvais, f_hz=0, value=0, vrms_mv=1)]})
        self.assertTrue(any("courant (A1-A3) : centrage" in t for _, t, _ in c))
        link.parse_line(link.encode(self.ADC_SIMPLE))         # ancien câblage : 1650 mV redevient normal
        self.assertEqual(listener.diagnostic("ISTAT", mauvais), [])


class TestLink(unittest.TestCase):
    def test_checksum_et_evenement(self):
        ev = link.parse_event(link.parse_line(evt(3, "ON", 78.2, 860)))
        self.assertEqual((ev["type"], ev["delta"], ev["transient_duration"], ev["seq"]), ("ON", 78.2, 860, 3))
        self.assertIsNone(link.parse_line(evt(3, "ON", 78.2).replace("ON", "OF")))   # checksum faux

    def test_evenement_firmware_08(self):
        ev = link.parse_event(link.parse_line(evt8(4, "ON", 225.0, 45000, ds=228.0, dpf=0.99, paliers=3, chaine=40000)))
        self.assertEqual((ev["delta_s"], ev["delta_pf"], ev["n_paliers"], ev["duree_chaine_ms"], ev["puissance"],
                          ev["algo"]), (228.0, 0.99, 3, 40000, "P", "ameliore"))
        ancien = link.parse_event(link.parse_line(evt(3, "ON", 78.2)))
        self.assertEqual((ancien["n_paliers"], ancien["delta_s"], ancien["puissance"], ancien["algo"]), (None, None, "S", "ref"))
        nan = link.parse_event(link.parse_line(link.encode(
            "EVT,1,OFF,-50.000,100,50,10,22,22,240,d_min,0,1000,nan,nan,1,200,50.000,P,ref")))
        self.assertTrue(nan["delta_s"] != nan["delta_s"])   # « nan » : delta de S inconnu
        self.assertIsNone(link.parse_event(link.parse_line(link.encode(
            "EVT,1,OFF,-50.000,100,50,10,22,22,240,d_min,0,1000,nan,nan,1,200,50.000,X,ref"))))

    def test_stat(self):
        d = link.parse_stat(link.parse_line(link.encode("STAT,1000,50,1,0,0,417,416,0,0")))
        self.assertEqual((d["sps_v"], d["sps_i"], d["rdy_timeouts"]), (417, 416, 0))
        self.assertIsNone(d["spurious_rdy"])   # ancien firmware : compteurs absents
        d = link.parse_stat(link.parse_line(link.encode("STAT,1000,50,1,0,0,380,380,0,2,7,1,0,3,0,1")))
        self.assertEqual((d["spurious_rdy"], d["desync"], d["clip_i"], d["verify"]), (7, 1, 3, 1))
        self.assertIsNone(d["bruit_w"])
        d = link.parse_stat(link.parse_line(link.encode("STAT,1000,50,1,0,0,330,330,0,0,0,0,0,0,0,1,0.37,4.00")))
        self.assertEqual((d["bruit_w"], d["seuil_w"], d["verify"]), (0.37, 4.0, 1))

    def test_pwr(self):
        # firmware >= 0.7 : la période seule, puis ce que reçoit le détecteur (moyenne sur 10 périodes)
        d = link.parse_pwr(link.parse_line(link.encode("PWR,12,35.100,220.000,0.1595,20.000,101.200,0.4600,10")))
        self.assertEqual((d["s_va"], d["s_detecteur"], d["i_detecteur"], d["n_moyenne"]), (35.1, 101.2, 0.46, 10))
        # ancien firmware (5 champs) : le détecteur recevait la période seule
        d = link.parse_pwr(link.parse_line(link.encode("PWR,12,35.100,220.000,0.1595,20.000")))
        self.assertEqual((d["s_detecteur"], d["i_detecteur"], d["n_moyenne"]), (35.1, 0.1595, 1))
        self.assertIsNone(link.parse_pwr(link.parse_line(link.encode("PWR,12,35.1,220,0.16,20,101.2"))))
        self.assertIsNone(d["p_w"])
        # firmware >= 0.8 : puissance active et facteur de puissance
        d = link.parse_pwr(link.parse_line(link.encode(
            "PWR,12,106.4,220.000,0.4837,20.000,106.300,0.4830,10,100.000,0.9400,99.900")))
        self.assertEqual((d["p_w"], d["pf"], d["p_detecteur"], d["s_detecteur"]), (100.0, 0.94, 99.9, 106.3))

    def test_listener_console(self):
        out = []
        args = listener.make_parser().parse_args([])
        lines = [link.encode("STAT,1000,50,1,0,0,417,416,3,0"), b"\x00\xffbruit", evt(0, "ON", 60)]
        listener.Listener(FakeSerial(lines), args, out=out.append).run()
        txt = "\n".join(out)
        self.assertIn("V=417 I=416 sans_RDY=3", txt)
        self.assertIn("EVENEMENT ON +60.0 W", txt)
        self.assertIn("1 ligne(s) invalide", txt)


class TestRegistre(unittest.TestCase):
    def test_allumage_extinction_s_annulent(self):
        with tempfile.TemporaryDirectory() as d:
            journal = os.path.join(d, "journal.csv")
            r = Registre({3: "ventilateur mode 2", 4: "fer"}, journal)
            r.ajouter(3, {"type": "ON", "delta": 30.0}, maintenant=0)
            r.ajouter(4, {"type": "ON", "delta": 1000.0}, maintenant=10)
            self.assertEqual(r.puissance_totale(), 1030)
            self.assertIn("00:01:00", r.tableau(maintenant=60))          # horloge du ventilateur
            msg = r.ajouter(3, {"type": "OFF", "delta": -31.0}, maintenant=3600)
            self.assertIn("30.00 Wh", msg)                                # 30 W pendant 1 h
            self.assertEqual([s.nom for s in r.actifs], ["fer"])        # le ventilateur a disparu
            self.assertIn("ventilateur mode 2,3,", open(journal).read())

    def test_extinction_mal_classee_retrouvee_par_la_puissance(self):
        r = Registre({1: "lampe", 4: "fer"})
        r.ajouter(4, {"type": "ON", "delta": 1000.0}, maintenant=0)
        r.ajouter(1, {"type": "OFF", "delta": -980.0}, maintenant=5)   # classée "lampe" par erreur
        self.assertEqual(r.actifs, [])
        self.assertIn("ignoree", r.ajouter(1, {"type": "OFF", "delta": -60.0}, maintenant=6))

    def test_horloge(self):
        self.assertEqual(horloge(3725), "01:02:05")


class TestClassifier(unittest.TestCase):
    def test_nouvelles_caracteristiques(self):
        """Dataset du firmware 0.8 : delta de S, delta de PF, paliers... distinguent deux appareils de même P."""
        lignes = []
        for k in range(8):   # télé (PF 0,55) et lampe (PF 1) : même puissance active, S très différente
            lignes.append("1,%.1f,500,ON,%.1f,0.55,1,400,%.1f" % (55 + k % 3, 100 + k % 3, 55 + k % 3))
            lignes.append("2,%.1f,500,ON,%.1f,1.00,1,400,%.1f" % (55 + k % 3, 55 + k % 3, 55 + k % 3))
            lignes.append("5,%.1f,45000,ON,228,0.99,4,40000,82" % (225 + k % 3))
        with tempfile.TemporaryDirectory() as d:
            data, modele = os.path.join(d, "d.csv"), os.path.join(d, "m.pkl")
            with open(data, "w") as f:
                f.write("\n".join(lignes) + "\n")
            c = EventClassifier()
            c.entrainer(data, modele)
            self.assertIn("delta_s", c.features)
            c = EventClassifier().charger(modele)
            base = {"delta": 56.0, "transient_duration": 500, "n_paliers": 1, "duree_chaine_ms": 400, "marche_max": 56}
            self.assertEqual(c.identify(dict(base, delta_s=101.0, delta_pf=0.55)), (1, "ON"))
            self.assertEqual(c.identify(dict(base, delta_s=56.0, delta_pf=1.0)), (2, "ON"))
            # un événement d'un ancien firmware (sans les nouvelles clés) ne fait pas planter
            self.assertIn(c.identify({"delta": 226.0, "transient_duration": 45000})[1], ("ON", "OFF"))

    def test_ancien_dataset_et_ancien_modele(self):
        """Un dataset à 4 colonnes (même mélangé) n'utilise que delta et transient_duration ; un modèle
        enregistré par l'ancienne version (2 éléments) se charge encore."""
        import pickle
        with tempfile.TemporaryDirectory() as d:
            data, modele = os.path.join(d, "d.csv"), os.path.join(d, "m.pkl")
            with open(os.path.join(HERE, "exemple_dataset.csv")) as f:
                ancien = f.read()
            with open(data, "w") as f:
                f.write(ancien + "111,78.0,880,ON,95.0,0.1,1,400,78.0\n")
            c = EventClassifier()
            c.entrainer(data, modele)
            self.assertEqual(c.features, ["delta", "transient_duration"])
            with open(modele, "wb") as f:
                pickle.dump((c.scaler, c.model), f)
            c2 = EventClassifier().charger(modele)
            self.assertEqual(c2.features, ["delta", "transient_duration"])
            self.assertEqual(c2.identify({"delta": 78.5, "transient_duration": 900}), (111, "ON"))

    def test_entrainer_et_reconnaitre_sur_l_exemple(self):
        with tempfile.TemporaryDirectory() as d:
            modele = os.path.join(d, "m.pkl")
            EventClassifier().entrainer(os.path.join(HERE, "exemple_dataset.csv"), modele)
            c = EventClassifier().charger(modele)
            self.assertEqual(c.identify({"delta": 78.5, "transient_duration": 900}), (111, "ON"))
            self.assertEqual(c.identify({"delta": -72.0, "transient_duration": 240}), (111, "OFF"))
            self.assertEqual(c.identify({"delta": 1190.0, "transient_duration": 19800}), (127, "ON"))


class TestDataset(unittest.TestCase):
    def test_commandes_guidees(self):
        # la "carte" envoie : un OFF parasite, puis le ON attendu ; puis un OFF ; puis rien
        board = fake_board([evt(0, "OFF", -40), evt(1, "ON", 45.5, 5240), evt(2, "OFF", -45.0, 5230)])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dataset.csv")
            with redirect_stdout(io.StringIO()) as out:
                n = constituer(board, {3: "ventilateur mode 2"}, path, 3, duree=1, pause=0,
                               repondre=lambda q: "n", hasard=random.Random(1))
            rows = open(path).read().splitlines()
        self.assertEqual(n, 2)
        self.assertEqual(rows, ["3,45.5,5240,ON", "3,-45.0,5230,OFF"])   # même format que l'exemple
        self.assertIn("ALLUMER : ventilateur mode 2", out.getvalue())
        self.assertIn("ETEINDRE : ventilateur mode 2", out.getvalue())
        self.assertIn("rien detecte", out.getvalue())

    def test_appareils(self):
        # (un fichier à part : votre appareils.csv change au fil des séances)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "appareils.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("id,nom\n1,tele\n3,ventilateur mode 2\n\n")
            self.assertEqual(charger_appareils(path), {1: "tele", 3: "ventilateur mode 2"})

    def test_dataset_firmware_08(self):
        """Firmware >= 0.8 : les 5 nouvelles caractéristiques sont écrites à la suite des 4 anciennes."""
        board = fake_board([evt8(0, "ON", 69.9, 4980, ds=57.9, dpf=0.245, paliers=1, chaine=400)])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dataset.csv")
            with redirect_stdout(io.StringIO()):
                constituer(board, {3: "ventilateur"}, path, 1, duree=1, pause=0, repondre=lambda q: "n",
                           hasard=random.Random(1))
            with open(path) as f:
                self.assertEqual(f.read().splitlines(), ["3,69.9,4980,ON,57.9,0.245,1,400,69.9"])


def pwr_lines(puissances, vrms=220.0):
    return [link.encode("PWR,%d,%.3f,%.3f,%.4f,20.000" % (k + 1, s, vrms, s / vrms)) for k, s in enumerate(puissances)]


class TestDiagnostic(unittest.TestCase):
    def test_pics_et_rafales(self):
        s = [100.0] * 200
        s[50] = s[51] = 5000.0          # deux périodes « tension lue comme courant »
        s[150] = 130.0                  # +30 VA : sous le seuil (max(30, 20 %))
        self.assertEqual(dg.trouver_pics(s), [50, 51])
        self.assertEqual(dg.grouper([50, 51, 120]), [[50, 51], [120]])

    def test_analyse_a_vide(self):
        s = [2.0] * 300
        for k in (100, 101, 102):
            s[k] = 4800.0
        msgs = [link.parse_line(l) for l in pwr_lines(s) + [link.encode("STAT,1000,50,0,0,0,380,380,0,0,4,0,0,0,0,1")]]
        r, constats = dg.analyser_puissance(dg.trier(msgs), a_vide=True)
        titres = " | ".join(t for _, t, _ in constats)
        self.assertEqual(r["pics"], 3)
        self.assertIn("aberrante", titres)
        self.assertIn("fronts parasites", titres)

    def test_mesure_saine(self):
        msgs = [link.parse_line(l) for l in pwr_lines([100.0 + 0.5 * (k % 3) for k in range(300)])
                + [link.encode("STAT,1000,50,0,0,0,380,380,0,0,0,0,0,0,0,1")]]
        r, constats = dg.analyser_puissance(dg.trier(msgs), a_vide=False)
        self.assertEqual(constats, [])

    def test_suivre_courant(self):
        s = [2.0] * 100
        s[40] = 5000.0
        lignes = pwr_lines(s) + [link.encode("STAT,1000,50,0,0,0,380,380,0,0,0,0,0,0,0,1")]
        sortie = []
        total, anormales = dg.suivre_voie(fake_board(lignes), "courant", out=sortie.append)
        self.assertEqual((total, anormales), (100, 1))
        self.assertTrue(any("!! période 41" in l for l in sortie))
        self.assertTrue(any("ANORMAL" in l and "courant" in l for l in sortie))

    def test_suivre_tension(self):
        lignes = pwr_lines([100.0] * 50, vrms=221.0) + pwr_lines([100.0] * 5, vrms=150.0)
        total, anormales = dg.suivre_voie(fake_board(lignes), "tension", out=lambda *_: None)
        self.assertEqual((total, anormales), (55, 5))


class TestRejeu(unittest.TestCase):
    """python rejouer.py : un enregistrement passe dans le VRAI détecteur (C++ compilé pour PC)."""

    def _csv(self, d, lignes, nouveau=True):
        path = os.path.join(d, "enr.csv")
        with open(path, "w", newline="") as f:
            f.write("pc_time,kind,a,b,c,d,e,f,g,h,i\n" if nouveau else "pc_time,kind,a,b,c,d,e,f\n")
            f.write("2026-11-06T21:59:05.235,ADC,1,A1-A3,0.015625,512.0\n")
            for k, (s, p) in enumerate(lignes):
                t = "2026-11-06T22:00:%06.3f" % (k * 0.02 % 60)
                f.write("%s,PWR,%.3f,220,%.4f,%d,%.3f,10%s\n" % (t, s, s / 220, k + 1, s,
                                                                 ",%.3f,%.3f,%.3f" % (p, p / s, p) if nouveau else ""))
        return path

    @staticmethod
    def _marches():
        """20 W de fond ; un appareil de 60 W (PF 0,5 : S = 120 VA) de 10 s à 25 s ; bruit léger."""
        rnd = random.Random(3)
        out = []
        for k in range(2000):
            on = 500 <= k < 1250
            p = 20 + (60 if on else 0) + rnd.gauss(0, 0.3)
            s = 22 + (120 if on else 0) + rnd.gauss(0, 0.3)
            out.append((s, p))
        return out

    def test_rejeu_nouveau_et_ancien_format(self):
        with tempfile.TemporaryDirectory() as d:
            enr = rejouer.Enregistrement(self._csv(d, self._marches()))
            self.assertTrue(enr.a_p())
            ev, bilan, _ = rejouer.rejouer(enr, "ameliore", "P")
            self.assertEqual([(e["type"], round(e["delta"])) for e in ev], [("ON", 60), ("OFF", -60)])
            self.assertAlmostEqual(ev[0]["delta_s"], 120, delta=3)
            self.assertIn("algo=ameliore puissance=P", bilan)
            ev, _, _ = rejouer.rejouer(enr, "ref", "S")
            self.assertEqual([(e["type"], round(e["delta"], -1)) for e in ev], [("ON", 120), ("OFF", -120)])
            ancien = rejouer.Enregistrement(self._csv(d, self._marches(), nouveau=False))
            self.assertFalse(ancien.a_p())
            with self.assertRaises(ValueError):   # pas de P dans un ancien fichier
                rejouer.rejouer(ancien, "ameliore", "P")
            ev, _, _ = rejouer.rejouer(ancien, "ameliore", "S")
            self.assertEqual(len(ev), 2)

    def test_moyenne_glissante(self):
        self.assertEqual(rejouer.moyenne_glissante([0, 10, 20, 30], 2), [0, 5, 15, 25])


@unittest.skipUnless(os.environ.get("FARAN_E2E"), "firmware PC non compilé (lancer run_tests.sh)")
class TestChaineComplete(unittest.TestCase):
    def test_rejeu_identique_a_la_carte(self):
        """Rejouer la sortie de la carte (projecteur) redonne EXACTEMENT les événements que la carte a envoyés."""
        out = subprocess.run([os.environ["FARAN_E2E"], "proj"], capture_output=True, check=True).stdout
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "proj.txt")
            with open(path, "wb") as f:
                f.write(out)
            enr = rejouer.Enregistrement(path)
            ev, _, _ = rejouer.rejouer(enr, "ameliore", "P")
        carte = [link.parse_event(m) for m in (link.parse_line(l) for l in out.splitlines()) if m and m.kind == "EVT"]
        self.assertEqual([round(e["delta"], 1) for e in ev], [round(e["delta"], 1) for e in carte])
        self.assertEqual(len(ev), 2)

    def _analyse(self, scenario):
        out = subprocess.run([os.environ["FARAN_E2E"], scenario], capture_output=True, check=True).stdout
        msgs = [m for m in (link.parse_line(l) for l in out.splitlines()) if m]
        return dg.analyser_puissance(dg.trier(msgs), a_vide=True)

    def test_diagnostic_prouve_la_confusion_de_voie(self):
        """À vide avec parasites : la lecture protégée est propre, l'ancienne donne pics et événements."""
        protegee, _ = self._analyse("idle_glitch")
        ancienne, _ = self._analyse("idle_glitch_noverify")
        self.assertEqual((protegee["pics"], protegee["evenements"]), (0, 0))
        self.assertGreater(ancienne["pics"], 10)
        niveau, titre, _ = dg.comparer_lectures(protegee, ancienne)
        self.assertEqual(niveau, dg.PROBLEME)
        self.assertIn("CAUSE PROUV", titre)

    def test_tele_stable_pour_le_detecteur(self):
        """Télé simulée (pics de 1 ms) : S_va varie de ±35 % par période, mais pas ce que voit le détecteur."""
        out = subprocess.run([os.environ["FARAN_E2E"], "tv"], capture_output=True, check=True).stdout
        msgs = [m for m in (link.parse_line(l) for l in out.splitlines() if not l.startswith(b"#")) if m]
        r, constats = dg.analyser_puissance(dg.trier(msgs), a_vide=False)
        self.assertEqual((r["evenements"], r["n_moyenne"]), (0, 10))
        self.assertLess(r["bruit_va"], 2.0)
        self.assertGreater(r["s_max"] - r["s_min"], 50.0)   # la période seule, elle, bouge beaucoup

    def test_firmware_vers_registre(self):
        """Le firmware (ADS1115 simulé) détecte +1000/-1000/+60/-60 W ; le PC classe et tient le registre."""
        out = subprocess.run([os.environ["FARAN_E2E"], "live"], capture_output=True, check=True).stdout
        board = fake_board(out.splitlines(keepends=True))
        events = list(board.evenements())
        self.assertEqual([round(e["delta"], -1) for e in events], [1000, -1000, 60, -60])
        r = Registre({4: "fer", 1: "lampe"})
        for k, e in enumerate(events):
            r.ajouter(4 if abs(e["delta"]) > 500 else 1, e, maintenant=k * 10)
        self.assertEqual(r.actifs, [])


if __name__ == "__main__":
    unittest.main()
