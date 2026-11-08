# Registre des appareils en marche (mode production).
#
#   allumage  "ventilateur mode 2"  -> une ligne est ajoutée : appareil, puissance, depuis quand
#   extinction "ventilateur mode 2" -> les deux événements s'annulent : la ligne disparaît,
#                                      et l'utilisation terminée (durée, énergie) va dans journal.csv
#
# Pour chaque appareil en marche :
#   - horloge     : depuis combien de temps il est dans cet état ;
#   - puissance   : la puissance instantanée, celle de son allumage (+30 W à l'allumage => 30 W) ;
#   - énergie     : puissance x durée, en wattheures (Wh).

import csv
import os
import time

from constants import TOLERANCE_PUISSANCE


def horloge(secondes):
    """3725 -> '01:02:05'"""
    secondes = int(secondes)
    return "%02d:%02d:%02d" % (secondes // 3600, secondes % 3600 // 60, secondes % 60)


class Session():
    """Un appareil allumé."""

    def __init__(self, label, nom, puissance, depuis) -> None:
        self.label = label
        self.nom = nom
        self.puissance = puissance     # W
        self.depuis = depuis           # time.time() de l'allumage

    def duree(self, maintenant):
        return maintenant - self.depuis

    def energie_wh(self, maintenant):
        return self.puissance * self.duree(maintenant) / 3600.0


class Registre():

    def __init__(self, appareils, journal_path=None, quand_fini=None) -> None:
        self.appareils = appareils      # {label: nom}
        self.actifs = []                # liste de Session
        self.journal_path = journal_path
        self.quand_fini = quand_fini    # fonction(session, fin) appelée à chaque utilisation terminée (api.py)

    def nom(self, label):
        return self.appareils.get(label, "appareil %s" % label)

    def ajouter(self, label, event, maintenant=None):
        """Met le registre à jour avec un événement classé. Renvoie une phrase qui dit ce qui s'est passé."""
        maintenant = time.time() if maintenant is None else maintenant
        puissance = abs(event["delta"])

        if event["type"] == "ON":
            self.actifs.append(Session(label, self.nom(label), puissance, maintenant))
            return "%s allume (%.0f W)" % (self.nom(label), puissance)

        session = self.trouver(label, puissance)
        if session is None:
            return "extinction de %s (%.0f W) sans allumage connu : ignoree" % (self.nom(label), puissance)
        self.actifs.remove(session)          # allumage et extinction s'annulent
        self.noter_au_journal(session, maintenant)
        if self.quand_fini:
            self.quand_fini(session, maintenant)
        return "%s eteint apres %s : %.2f Wh consommes" % (
            session.nom, horloge(session.duree(maintenant)), session.energie_wh(maintenant))

    def trouver(self, label, puissance):
        """La session qu'une extinction annule : le même appareil d'abord (le plus ancien) ; sinon,
        si le classifieur s'est trompé d'appareil, celui dont la puissance est la plus proche (+-30 %)."""
        for session in self.actifs:
            if session.label == label:
                return session
        proches = [s for s in self.actifs if abs(s.puissance - puissance) <= TOLERANCE_PUISSANCE * s.puissance]
        return min(proches, key=lambda s: abs(s.puissance - puissance)) if proches else None

    def puissance_totale(self):
        return sum(s.puissance for s in self.actifs)

    def noter_au_journal(self, session, maintenant):
        if not self.journal_path:
            return
        nouveau = not os.path.exists(self.journal_path)
        with open(self.journal_path, 'a', newline='') as f:
            journal = csv.writer(f)
            if nouveau:
                journal.writerow(["appareil", "label", "debut", "fin", "duree_s", "puissance_w", "energie_wh"])
            journal.writerow([session.nom, session.label, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session.depuis)),
                              time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(maintenant)), round(session.duree(maintenant)),
                              round(session.puissance, 1), round(session.energie_wh(maintenant), 3)])

    def tableau(self, maintenant=None):
        """Le registre sous forme de texte, prêt à afficher."""
        maintenant = time.time() if maintenant is None else maintenant
        lignes = ["%-28s %10s %12s %12s" % ("APPAREIL EN MARCHE", "DEPUIS", "PUISSANCE", "ENERGIE")]
        for s in self.actifs:
            lignes.append("%-28s %10s %10.0f W %9.2f Wh" % (s.nom[:28], horloge(s.duree(maintenant)), s.puissance, s.energie_wh(maintenant)))
        if not self.actifs:
            lignes.append("(aucun appareil en marche)")
        lignes.append("%-28s %10s %10.0f W" % ("TOTAL", "", self.puissance_totale()))
        return "\n".join(lignes)
