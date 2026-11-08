"""Compte la consommation par appareil, jour par jour (ou par période de démonstration), pour l'API.

Une PÉRIODE = une journée, de minuit à minuit (heure locale). En mode démonstration (MODE_DEMO), une
période ne dure que DUREE_DEMO_S secondes (2 min) : tout se passe comme pour une journée, en accéléré.

Pendant la période, on additionne pour chaque appareil :
    utilisations   combien de fois il a été allumé (une utilisation qui chevauche minuit compte dans
                   les deux journées ; « reportees » dit combien venaient de la veille)
    duree_s        combien de temps il a tourné pendant CETTE période
    energie_wh     puissance x durée
Les appareils encore allumés sont comptés jusqu'à « maintenant » (sans être coupés).

À la fin de la période (minuit), elle est ARCHIVÉE dans un fichier (rapports/jour_AAAA-MM-JJ.csv, ou
rapports/demo/periode_AAAA-MM-JJ_HH-MM-SS.csv), puis les compteurs repartent de zéro. Le mois est la somme
des périodes archivées depuis le 1er, plus la période en cours. La période en cours est aussi écrite
dans rapports/en_cours.csv à chaque changement : un redémarrage du programme ne la perd pas.

Toutes les fonctions reçoivent l'heure (`maintenant`, secondes comme time.time()) : les tests peuvent
faire avancer le temps comme ils veulent.
"""
from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

COLONNES = ["debut", "fin", "label", "appareil", "utilisations", "reportees", "duree_s", "energie_kwh",
            "cout_fcfa", "puissance_moyenne_w"]


def iso(t):
    """secondes -> '2026-11-08T14:02:00' (heure locale, ISO 8601)"""
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


def depuis_iso(texte):
    return datetime.fromisoformat(texte).timestamp()


# ------------------------------------------------------------------ découpage du temps
class Periodes:
    """Où commence la période qui contient l'instant t ? (minuit, ou multiple de DUREE_DEMO_S en démo)"""

    def __init__(self, demo=False, duree_demo_s=120):
        self.demo = demo
        self.duree = float(duree_demo_s)

    def debut(self, t):
        if self.demo:
            return (t // self.duree) * self.duree      # 14:00:00, 14:02:00, 14:04:00...
        d = datetime.fromtimestamp(t)
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def fin(self, debut):
        if self.demo:
            return debut + self.duree
        d = datetime.fromtimestamp(debut) + timedelta(days=1, hours=2)   # +2 h : sûr même un jour de 25 h
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    @staticmethod
    def debut_mois(t):
        d = datetime.fromtimestamp(t)
        return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()

    def nom_fichier(self, debut):
        d = datetime.fromtimestamp(debut)
        return d.strftime("periode_%Y-%m-%d_%H-%M-%S.csv") if self.demo else d.strftime("jour_%Y-%m-%d.csv")


# ------------------------------------------------------------------ une ligne du bilan
@dataclass
class Ligne:
    label: int
    appareil: str
    utilisations: int = 0
    reportees: int = 0
    duree_s: float = 0.0
    energie_wh: float = 0.0

    def ajouter(self, autre):
        self.utilisations += autre.utilisations
        self.reportees += autre.reportees
        self.duree_s += autre.duree_s
        self.energie_wh += autre.energie_wh

    def puissance_moyenne_w(self):
        return self.energie_wh * 3600.0 / self.duree_s if self.duree_s > 0 else 0.0


@dataclass
class Bilan:
    """Ce que contient un rapport : une période (ou un mois), une ligne par appareil."""
    titre: str
    debut: float
    fin: float
    lignes: dict = field(default_factory=dict)   # {label: Ligne}

    def ligne(self, label, appareil):
        if label not in self.lignes:
            self.lignes[label] = Ligne(label, appareil)
        return self.lignes[label]

    def energie_kwh(self):
        return sum(l.energie_wh for l in self.lignes.values()) / 1000.0

    def triees(self):
        """du plus gros consommateur au plus petit"""
        return sorted(self.lignes.values(), key=lambda l: -l.energie_wh)


# ------------------------------------------------------------------ fichiers CSV (archives ET téléchargements)
def ecrire_csv(f, bilan, tarif):
    """Une ligne par appareil, puis TOTAL. Même format pour les archives et pour le rapport téléchargé."""
    w = csv.writer(f)
    w.writerow(COLONNES)
    tot = Ligne(-1, "TOTAL")
    for l in bilan.triees():
        tot.ajouter(l)
        w.writerow(_cellules(bilan, l, tarif))
    w.writerow(_cellules(bilan, tot, tarif, label=""))


def _cellules(bilan, l, tarif, label=None):
    kwh = l.energie_wh / 1000.0
    return [iso(bilan.debut), iso(bilan.fin), l.label if label is None else label, l.appareil, l.utilisations,
            l.reportees, round(l.duree_s), round(kwh, 4), round(kwh * tarif, 1), round(l.puissance_moyenne_w(), 1)]


def lire_csv(path):
    """Relit un fichier écrit par ecrire_csv -> Bilan (la ligne TOTAL est ignorée)."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    b = Bilan("archive", depuis_iso(rows[0]["debut"]), depuis_iso(rows[0]["fin"]))
    for r in rows:
        if r["appareil"] == "TOTAL" or r["label"] == "":
            continue
        l = b.ligne(int(r["label"]), r["appareil"])
        l.utilisations += int(r["utilisations"])
        l.reportees += int(r["reportees"])
        l.duree_s += float(r["duree_s"])
        l.energie_wh += float(r["energie_kwh"]) * 1000.0
    return b


# ------------------------------------------------------------------ le compteur
class Consommation:

    def __init__(self, dossier, periodes, tarif, maintenant=None):
        maintenant = time.time() if maintenant is None else maintenant
        self.periodes = periodes
        self.tarif = tarif
        self.dossier = os.path.join(dossier, "demo") if periodes.demo else dossier
        os.makedirs(self.dossier, exist_ok=True)
        self.en_cours = os.path.join(self.dossier, "en_cours.csv")
        self.debut = periodes.debut(maintenant)
        self.cumul = Bilan("periode", self.debut, self.debut)   # utilisations TERMINÉES de la période
        self._reprendre(maintenant)
        self._mois = None                                        # mois archivé, gardé en mémoire
        self._charger_mois(maintenant)

    # --- ce qui se passe
    def session_finie(self, session, fin):
        """Une utilisation se termine (appelée par le registre) : sa part dans la période est ajoutée."""
        self._ajouter_session(self.cumul, session, fin)
        self._sauver_en_cours(fin)

    def basculer_si_besoin(self, actifs, maintenant):
        """Minuit (ou fin de la période de démo) est passé : archiver, puis repartir de zéro.
        Les appareils allumés sont comptés jusqu'à la limite, et continuent dans la période suivante."""
        bascule = False
        while maintenant >= self.periodes.fin(self.debut):
            limite = self.periodes.fin(self.debut)
            self._archiver(self.bilan(actifs, limite))
            self.debut = limite
            self.cumul = Bilan("periode", self.debut, self.debut)
            bascule = True
        if bascule:
            self._sauver_en_cours(maintenant)
        return bascule

    # --- ce que l'on lit
    def bilan(self, actifs, maintenant):
        """La période en cours, appareils allumés compris (jusqu'à maintenant)."""
        b = Bilan("journalier", self.debut, maintenant)
        for l in self.cumul.lignes.values():
            b.ligne(l.label, l.appareil).ajouter(l)
        for s in actifs:
            self._ajouter_session(b, s, maintenant)
        return b

    def bilan_mois(self, actifs, maintenant):
        """Depuis le 1er du mois : les périodes archivées, plus la période en cours. Une utilisation qui
        chevauche deux périodes du même mois n'est comptée qu'une fois."""
        debut_mois = self.periodes.debut_mois(maintenant)
        if self._mois is None or self._mois.debut != debut_mois:
            self._charger_mois(maintenant)
        b = Bilan("mensuel", debut_mois, maintenant)
        for l in self._mois.lignes.values():
            b.ligne(l.label, l.appareil).ajouter(l)
        jour = self.bilan(actifs, maintenant)
        for l in jour.lignes.values():
            dst = b.ligne(l.label, l.appareil)
            dst.ajouter(l)
            if self.debut > debut_mois:           # venue d'une période de ce mois : déjà comptée
                dst.utilisations -= l.reportees
            dst.reportees = 0
        return b

    # --- détails
    def _ajouter_session(self, bilan, s, fin):
        debut = max(s.depuis, self.debut)
        if fin <= debut:
            return
        l = bilan.ligne(s.label, s.nom)
        l.utilisations += 1
        if s.depuis < self.debut:
            l.reportees += 1        # allumée avant cette période (la veille)
        l.duree_s += fin - debut
        l.energie_wh += s.puissance * (fin - debut) / 3600.0

    def _archiver(self, bilan):
        path = os.path.join(self.dossier, self.periodes.nom_fichier(bilan.debut))
        with open(path, "w", newline="", encoding="utf-8") as f:
            ecrire_csv(f, bilan, self.tarif)
        if self._mois is not None and self.periodes.debut_mois(bilan.debut) == self._mois.debut:
            self._ajouter_au_mois(bilan)
        else:
            self._mois = None   # nouveau mois : relu au prochain bilan_mois

    def _ajouter_au_mois(self, bilan):
        for l in bilan.lignes.values():
            dst = self._mois.ligne(l.label, l.appareil)
            dst.ajouter(l)
            if bilan.debut > self._mois.debut:
                dst.utilisations -= l.reportees
            dst.reportees = 0

    def _charger_mois(self, maintenant):
        debut_mois = self.periodes.debut_mois(maintenant)
        self._mois = Bilan("mois archivé", debut_mois, debut_mois)
        for nom in sorted(os.listdir(self.dossier)):
            if nom == "en_cours.csv" or not nom.endswith(".csv"):
                continue
            b = lire_csv(os.path.join(self.dossier, nom))
            if b and debut_mois <= b.debut < self.debut:
                self._ajouter_au_mois(b)

    def _sauver_en_cours(self, maintenant):
        b = Bilan("en cours", self.debut, maintenant)
        b.lignes = self.cumul.lignes
        with open(self.en_cours, "w", newline="", encoding="utf-8") as f:
            ecrire_csv(f, b, self.tarif)

    def _reprendre(self, maintenant):
        """Au démarrage : reprendre la période en cours (programme relancé), ou archiver une période finie
        pendant que le programme était arrêté."""
        if not os.path.exists(self.en_cours):
            return
        b = lire_csv(self.en_cours)
        if b is None:
            return
        if b.debut == self.debut:
            self.cumul.lignes = b.lignes
        elif b.debut < self.debut:
            b.fin = self.periodes.fin(b.debut)
            path = os.path.join(self.dossier, self.periodes.nom_fichier(b.debut))
            if not os.path.exists(path):
                with open(path, "w", newline="", encoding="utf-8") as f:
                    ecrire_csv(f, b, self.tarif)
