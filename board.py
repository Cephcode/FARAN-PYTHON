# La carte ESP32 vue du PC : ouvre le port série, envoie les commandes, et rend les événements.
# Avec PORT = "sim", une carte simulée (carte_simulee.py) remplace la vraie : mode développement.

import time

import faran_link as link
from constants import BAUD


class Board():

    def __init__(self, port, baud=BAUD, afficher_infos=True) -> None:

        self.afficher_infos = afficher_infos

        if port == "sim":
            from carte_simulee import CarteSimulee
            self.serial = CarteSimulee()
        else:
            import serial  # pip install pyserial
            self.serial = serial.Serial(port, baud, timeout=0.2)

    def envoyer(self, commande):
        self.serial.write((commande.strip() + "\n").encode("ascii"))

    def demarrer(self, mode):
        """Met la carte dans un mode (ex. "4" = détection sur les vrais capteurs)."""
        self.envoyer("MODE %s" % mode)

    def lire_message(self):
        """Une ligne de la carte, décodée ; None si rien (ou ligne abîmée)."""
        line = self.serial.readline()
        if not line:
            return None
        return link.parse_line(line)

    def attendre_evenement(self, timeout=None):
        """Attend le prochain événement (dictionnaire) ; None si `timeout` secondes passent sans rien."""
        fin = None if timeout is None else time.time() + timeout
        while fin is None or time.time() < fin:
            msg = self.lire_message()
            if msg is None:
                if getattr(self.serial, "eof", False):
                    return None
                continue
            if msg.kind == "EVT":
                event = link.parse_event(msg)
                if event:
                    return event
            elif msg.kind in ("INFO", "ERR", "MODE", "HELLO") and self.afficher_infos:
                print("   [carte]", msg.body.replace(",", " ", 1))
        return None

    def evenements(self):
        """Tous les événements, un par un, sans fin (Ctrl+C pour arrêter)."""
        while True:
            event = self.attendre_evenement(timeout=1)
            if event:
                yield event
            elif getattr(self.serial, "eof", False):
                return

    def vider(self):
        """Oublie ce que la carte a déjà envoyé (événements anciens)."""
        if hasattr(self.serial, "reset_input_buffer"):
            self.serial.reset_input_buffer()

    def fermer(self):
        self.serial.close()


def decrire(event):
    """Une ligne lisible pour un événement."""
    txt = "%s %+.1f W (avant %.1f -> apres %.1f W, duree %d ms, confirme par %s)" % (
        event["type"], event["delta"], event["s_before"], event["s_after"],
        event["transient_duration"], event["confirmed_by"])
    if event.get("n_paliers") and event["n_paliers"] > 1:   # démarrage en plusieurs paliers (firmware >= 0.8)
        txt += " [%d paliers en %.1f s]" % (event["n_paliers"], event["duree_chaine_ms"] / 1000.0)
    return txt
