# Mode dataset : constitue dataset.csv en vous guidant.
#
# Pour chaque commande :
#   1. le programme tire au hasard un appareil de appareils.csv et vous dit quoi faire
#      ("ALLUMER : ventilateur mode 2"), en tenant compte de son état (on n'éteint pas un appareil éteint) ;
#   2. vous avez DUREE_COMMANDE secondes (30 s) pour le faire ; pendant ce temps il écoute la carte ;
#   3. si un événement du bon type arrive (ON pour allumer, OFF pour éteindre), il écrit une ligne :
#          label,delta,transient_duration,ON|OFF,delta_s,delta_pf,n_paliers,duree_chaine_ms,marche_max
#          (exemple : 3,69.9,4980,ON,57.9,0.245,1,400,71.1)
#      Les 5 dernières colonnes viennent du firmware >= 0.8 ; un ancien firmware n'écrit que les 4 premières.
#
# Le fichier est complété (jamais effacé) : on peut faire plusieurs séances.

import csv
import random
import time

from board import decrire
from constants import DUREE_COMMANDE, PAUSE_ENTRE_COMMANDES


def charger_appareils(path):
    """appareils.csv (id,nom) -> {id: nom}"""
    appareils = {}
    with open(path, 'r', newline='', encoding='utf-8') as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0].strip().isdigit():
                appareils[int(row[0])] = row[1].strip()
    if not appareils:
        raise ValueError("aucun appareil dans %s (format : id,nom)" % path)
    return appareils


COLONNES_EN_PLUS = ["delta_s", "delta_pf", "n_paliers", "duree_chaine_ms", "marche_max"]   # firmware >= 0.8


def ecrire_ligne(dataset_path, label, event):
    ligne = [label, event["delta"], event["transient_duration"], event["type"]]
    if event.get("n_paliers") is not None:   # firmware >= 0.8 : les nouvelles caractéristiques
        ligne += [event[k] for k in COLONNES_EN_PLUS]
    with open(dataset_path, 'a', newline='') as f:
        csv.writer(f).writerow(ligne)


def demander(question, repondre=input):
    return repondre(question).strip().lower().startswith("o")


def constituer(board, appareils, dataset_path, nombre, duree=DUREE_COMMANDE, pause=PAUSE_ENTRE_COMMANDES,
               repondre=input, hasard=random):
    """Lance `nombre` commandes. Renvoie le nombre de lignes écrites."""
    allume = {label: False for label in appareils}   # on suppose tout éteint au départ
    print("Mettez tous les appareils de la liste HORS TENSION avant de commencer.")
    ecrites = 0

    for n in range(nombre):
        label = hasard.choice(sorted(appareils))
        attendu = "OFF" if allume[label] else "ON"
        action = "ETEINDRE" if allume[label] else "ALLUMER"

        time.sleep(pause)
        board.vider()          # on oublie les événements arrivés avant la commande
        print("\n[%d/%d] %s : %s   (vous avez %d s)" % (n + 1, nombre, action, appareils[label], duree))

        event, fin = None, time.time() + duree
        while time.time() < fin:
            e = board.attendre_evenement(timeout=fin - time.time())
            if e is None:
                break
            if e["type"] == attendu:
                event = e
                break
            print("   evenement %s ignore (on attend un %s)" % (decrire(e), attendu))

        if event:
            ecrire_ligne(dataset_path, label, event)
            allume[label] = not allume[label]
            ecrites += 1
            print("   OK, ecrit : %s -> %s" % (appareils[label], decrire(event)))
        else:
            print("   rien detecte.")
            # si vous l'avez fait quand même, l'état de l'appareil a changé : on le note
            if demander("   Avez-vous quand meme %s l'appareil ? (o/n) " % action.lower(), repondre):
                allume[label] = not allume[label]

    print("\n%d ligne(s) ajoutee(s) a %s" % (ecrites, dataset_path))
    return ecrites
