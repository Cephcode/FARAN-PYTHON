"""Le mode production, partagé entre deux fils d'exécution (threads) :

    LecteurCarte (thread)  : lit la carte sans arrêt, classe chaque événement, met le registre à jour ;
    l'API (api.py)         : lit l'état et fabrique les rapports quand la page le demande.

Les deux passent par un objet Production, protégé par un VERROU (threading.Lock) : chacun ne le tient
que le temps de copier ou de modifier quelques valeurs en mémoire (quelques microsecondes). La lecture
de la carte (qui peut attendre une seconde) se fait EN DEHORS du verrou : l'API ne bloque jamais la
carte, et la carte ne bloque jamais l'API.
"""
from __future__ import annotations

import threading
import time

from consommation import Consommation, Periodes, iso
from constants import (CARTE_MUETTE_S, DOSSIER_RAPPORTS, DUREE_DEMO_S, JOURNAL, MODE_DEMO, TARIF_FCFA_KWH,
                       VERSION_API)
from registre import Registre


class Production:

    def __init__(self, appareils, tarif=TARIF_FCFA_KWH, dossier=DOSSIER_RAPPORTS, demo=MODE_DEMO,
                 duree_demo_s=DUREE_DEMO_S, journal=JOURNAL, horloge=time.time):
        self.horloge = horloge                 # time.time (les tests en donnent une autre)
        self.tarif = tarif
        self.demo, self.duree_demo_s = demo, duree_demo_s
        self.verrou = threading.Lock()
        maintenant = horloge()
        self.consommation = Consommation(dossier, Periodes(demo, duree_demo_s), tarif, maintenant)
        self.registre = Registre(appareils, journal, quand_fini=self.consommation.session_finie)
        self.dernier_contact = None            # heure de la dernière ligne valide reçue de la carte

    # ------------------------------------------------------------ appelé par le lecteur de la carte
    def signe_de_vie(self):
        with self.verrou:
            self.dernier_contact = self.horloge()

    def evenement(self, label, event):
        """Un événement classé : le registre est mis à jour. Renvoie la phrase du registre."""
        with self.verrou:
            maintenant = self.horloge()
            self.consommation.basculer_si_besoin(self.registre.actifs, maintenant)
            return self.registre.ajouter(label, event, maintenant=maintenant)

    def tic(self):
        """À appeler souvent (chaque seconde) : archive la journée à minuit."""
        with self.verrou:
            self.consommation.basculer_si_besoin(self.registre.actifs, self.horloge())

    # ------------------------------------------------------------ appelé par l'API
    def carte_connectee(self, maintenant):
        return self.dernier_contact is not None and maintenant - self.dernier_contact <= CARTE_MUETTE_S

    def etat(self):
        """Tout ce qu'affiche la page (GET /api/etat)."""
        with self.verrou:
            maintenant = self.horloge()
            self.consommation.basculer_si_besoin(self.registre.actifs, maintenant)
            actifs = list(self.registre.actifs)
            jour = self.consommation.bilan(actifs, maintenant)
            mois = self.consommation.bilan_mois(actifs, maintenant)
            connectee = self.carte_connectee(maintenant)
            debut_periode = self.consommation.debut
        total = sum(s.puissance for s in actifs)
        allumes = [{
            "label": s.label,
            "nom": s.nom,
            "puissance_w": round(s.puissance, 1),
            "part_pct": round(100.0 * s.puissance / total, 1) if total > 0 else 0.0,
            "cout_heure_fcfa": round(s.puissance / 1000.0 * self.tarif, 1),
            "depuis": iso(s.depuis),
            "duree_s": round(maintenant - s.depuis),
        } for s in sorted(actifs, key=lambda s: -s.puissance)]
        return {
            "maintenant": iso(maintenant),
            "carte_connectee": connectee,
            "puissance_totale_w": round(total, 1),
            "cout_heure_fcfa": round(total / 1000.0 * self.tarif, 1),
            "energie_jour_kwh": round(jour.energie_kwh(), 4),
            "cout_jour_fcfa": round(jour.energie_kwh() * self.tarif, 1),
            "energie_mois_kwh": round(mois.energie_kwh(), 4),
            "cout_mois_fcfa": round(mois.energie_kwh() * self.tarif, 1),
            "appareils_allumes": allumes,
            # en plus, pour la page :
            "tarif_fcfa_kwh": self.tarif,
            "mode_demo": self.demo,
            "duree_demo_s": self.duree_demo_s,
            "debut_jour": iso(debut_periode),
            "debut_mois": iso(mois.debut),
            "version": VERSION_API,
        }

    def rapport(self, quoi):
        """'journalier' ou 'mensuel' -> Bilan, appareils allumés comptés jusqu'à maintenant."""
        with self.verrou:
            maintenant = self.horloge()
            self.consommation.basculer_si_besoin(self.registre.actifs, maintenant)
            actifs = list(self.registre.actifs)
            if quoi == "mensuel":
                return self.consommation.bilan_mois(actifs, maintenant)
            return self.consommation.bilan(actifs, maintenant)


class LecteurCarte(threading.Thread):
    """Lit la carte en continu (thread à part). Si le port se ferme (carte débranchée), il réessaie toutes
    les 3 s : l'API, elle, continue de répondre (carte_connectee devient false)."""

    def __init__(self, production, ouvrir_carte, classifieur, afficher=print):
        super().__init__(daemon=True, name="lecteur-carte")
        self.production = production
        self.ouvrir_carte = ouvrir_carte       # fonction sans argument -> Board démarrée
        self.classifieur = classifieur
        self.afficher = afficher
        self.arret = threading.Event()
        self.board = None

    def run(self):
        import faran_link as link
        while not self.arret.is_set():
            try:
                if self.board is None:
                    self.board = self.ouvrir_carte()
                msg = self.board.lire_message()        # attend au plus ~0,2 s (hors verrou)
                if msg is not None:
                    self.production.signe_de_vie()
                    if msg.kind == "EVT":
                        event = link.parse_event(msg)
                        if event:
                            label, _ = self.classifieur.identify(event)
                            self.afficher("%s %s" % (time.strftime("%H:%M:%S"), self.production.evenement(label, event)))
                elif getattr(self.board.serial, "eof", False):
                    self.arret.wait(0.2)
                self.production.tic()
            except Exception as e:   # port fermé, carte débranchée... : on réessaie
                self.afficher("carte : %s (nouvel essai dans 3 s)" % e)
                self.fermer()
                self.arret.wait(3.0)

    def fermer(self):
        if self.board is not None:
            try:
                self.board.envoyer("MODE 5")
                self.board.fermer()
            except Exception:
                pass
            self.board = None

    def arreter(self):
        self.arret.set()
        self.join(timeout=3)
        self.fermer()
