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
#
# APPAREILS À PLUSIEURS VITESSES (noms « <appareil> mode <n> » et « <appareil> mode <n> direct » dans
# appareils.csv, ex. « ventilateur mode 3 ») :
#   « ventilateur mode 1 »         allumage du ventilateur (0 -> 1, ~48 W)
#   « ventilateur mode 2 », « 3 »  une MARCHE de vitesse (1 -> 2, 2 -> 3 : ~3 W chacune)
#   « ventilateur mode 3 direct »  allumage directement en vitesse 3 (0 -> 3, ~53 W)
# Les marches 1 <-> 2 et 2 <-> 3 font la même puissance : le classifieur ne peut pas savoir laquelle c'est.
# Le registre, lui, connaît la vitesse en cours. Donc :
#   - une EXTINCTION classée « mode n » (n >= 2, pas « direct ») = le ventilateur descend d'UNE vitesse à
#     partir de la vitesse EN COURS, quel que soit n (vitesse 3 -> 2 même si le classifieur a dit « mode 2 ») ;
#   - un ALLUMAGE classé « mode n » pendant que le ventilateur tourne = il monte d'une vitesse ;
#   - l'extinction complète (classée « mode 1 » ou « mode n direct ») retire TOUTES ses lignes.
# Tout le reste (autres appareils, ventilateur éteint) suit la règle générale ci-dessus.

import csv
import os
import re
import time

from constants import TOLERANCE_PUISSANCE


_MODE = re.compile(r"^(?P<appareil>.+?)\s+mode\s+(?P<n>\d+)(?P<direct>\s+direct)?\s*$", re.IGNORECASE)


def lire_mode(nom):
    """« ventilateur mode 3 direct » -> ("ventilateur", 3, True) ; « tele » -> None."""
    m = _MODE.match(nom.strip())
    if not m:
        return None
    return m.group("appareil").strip().lower(), int(m.group("n")), m.group("direct") is not None


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

        vitesse = self.changer_vitesse(label, event, puissance, maintenant)
        if vitesse is not None:
            return vitesse

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

    # ------------------------------------------------------------------ appareils à plusieurs vitesses
    def label_du_mode(self, appareil, n, direct):
        for label, nom in self.appareils.items():
            if lire_mode(nom) == (appareil, n, direct):
                return label
        return None

    def sessions_de(self, appareil):
        return [s for s in self.actifs if (lire_mode(s.nom) or (None,))[0] == appareil]

    def terminer(self, session, maintenant):
        self.actifs.remove(session)
        self.noter_au_journal(session, maintenant)
        if self.quand_fini:
            self.quand_fini(session, maintenant)

    def changer_vitesse(self, label, event, puissance, maintenant):
        """Événement d'un appareil à plusieurs vitesses qui tourne déjà : changement de vitesse ou
        extinction complète. Renvoie la phrase, ou None si la règle générale s'applique."""
        mode = lire_mode(self.nom(label))
        if mode is None:
            return None
        appareil, n, direct = mode
        sessions = self.sessions_de(appareil)
        if not sessions:
            return None                          # éteint : règle générale (allumage, ou extinction ignorée)
        en_cours = max(lire_mode(s.nom)[1] for s in sessions)
        nom = sessions[0].nom.split(" mode ")[0]
        marche = n >= 2 and not direct           # une marche de vitesse, pas un allumage/extinction complet
        total = sum(s.puissance for s in sessions)

        if event["type"] == "OFF" and len(sessions) == 1 and not lire_mode(sessions[0].nom)[2]:
            return None                          # une seule ligne (allumé par une marche) : règle générale
        if event["type"] == "OFF" and (not marche or puissance >= (1 - TOLERANCE_PUISSANCE) * total):
            # extinction complète (classée allumage, ou qui retire presque toute sa puissance) :
            # toutes les lignes de l'appareil (allumage + marches de vitesse)
            debut = min(s.depuis for s in sessions)
            energie = sum(s.energie_wh(maintenant) for s in sessions)
            for s in list(sessions):
                self.terminer(s, maintenant)
            return "%s eteint (etait en mode %d) apres %s : %.2f Wh consommes" % (
                nom, en_cours, horloge(maintenant - debut), energie)

        if event["type"] == "OFF" and marche:
            if en_cours <= 1:
                return None                      # déjà en vitesse 1 : règle générale
            # la plus haute vitesse en cours ; une marche plutôt qu'un allumage direct, la plus récente
            haut = max((s for s in sessions if lire_mode(s.nom)[1] == en_cours),
                       key=lambda s: (not lire_mode(s.nom)[2], s.depuis))
            self.terminer(haut, maintenant)
            if lire_mode(haut.nom)[2]:
                # allumé directement dans cette vitesse : la ligne devient celle de la vitesse en dessous
                nouveau = self.label_du_mode(appareil, en_cours - 1, en_cours - 1 >= 2) \
                    or self.label_du_mode(appareil, en_cours - 1, False)
                if nouveau is not None:
                    self.actifs.append(Session(nouveau, self.nom(nouveau), max(haut.puissance - puissance, 0.0),
                                               maintenant))
            return "%s : mode %d -> mode %d (-%.0f W)" % (nom, en_cours, en_cours - 1, puissance)

        if event["type"] == "ON" and marche:
            # il tourne déjà : il monte d'une vitesse à partir de la vitesse en cours
            nouveau = self.label_du_mode(appareil, en_cours + 1, False)
            if nouveau is None:
                return None
            self.actifs.append(Session(nouveau, self.nom(nouveau), puissance, maintenant))
            return "%s : mode %d -> mode %d (+%.0f W)" % (nom, en_cours, en_cours + 1, puissance)

        return None                              # allumage complet alors qu'il tourne : règle générale

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
