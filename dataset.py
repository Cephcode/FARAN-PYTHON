# Mode dataset : constitue dataset.csv en vous guidant.
#
# Au départ, le programme affiche la liste des appareils et vous demande lesquels sont ALLUMÉS en ce moment
# (Entrée : tout est éteint). Ensuite, pour chaque commande :
#   1. il tire au hasard un appareil et une commande POSSIBLE pour lui, d'après son état noté :
#        éteint            -> ALLUMER (dans un de ses modes)
#        allumé (mode 2)   -> ETEINDRE, ou PASSER A un autre de ses modes (« ventilateur mode 1 »)
#      Il ne demande donc jamais d'allumer un appareil allumé, ni d'éteindre un appareil éteint ;
#   2. vous avez DUREE_COMMANDE secondes (30 s) pour le faire ; pendant ce temps il écoute la carte ;
#   3. si un événement du bon type arrive (ON pour allumer, OFF pour éteindre, l'un ou l'autre pour un
#      changement de mode), il écrit une ligne et l'état de l'appareil change :
#          label,delta,transient_duration,ON|OFF,delta_s,delta_pf,n_paliers,duree_chaine_ms,marche_max
#          (exemple : 3,69.9,4980,ON,57.9,0.245,1,400,71.1)
#      Les 5 dernières colonnes viennent du firmware >= 0.8 ; un ancien firmware n'écrit que les 4 premières.
#      Un changement de mode est écrit avec le label du NOUVEAU mode : c'est l'événement que la carte enverra
#      en production quand vous changerez de vitesse ;
#   4. si rien n'est détecté, il demande si vous l'avez quand même fait : « o » -> l'état change, « n » -> non.
#
# Les MODES d'un même appareil (ventilateur mode 1, mode 2) sont des labels différents mais un seul appareil
# physique : la colonne facultative « appareil » de appareils.csv les regroupe :
#     id,nom,appareil
#     2,ventilateur mode 1,ventilateur
#     3,ventilateur mode 2,ventilateur
# Sans cette colonne (ancien fichier), chaque ligne est un appareil à part.
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


def charger_groupes(path):
    """appareils.csv -> {id: appareil physique}, d'après la colonne facultative « appareil » (en-tête
    id,nom,appareil). Sans cette colonne, ou si elle est vide, l'appareil physique est le nom lui-même."""
    groupes, col = {}, None
    with open(path, 'r', newline='', encoding='utf-8') as f:
        for row in csv.reader(f):
            cellules = [c.strip() for c in row]
            if not cellules or not cellules[0]:
                continue
            if not cellules[0].isdigit():           # ligne d'en-tête
                noms = [c.lower() for c in cellules]
                col = noms.index("appareil") if "appareil" in noms else None
                continue
            if len(cellules) >= 2:
                physique = cellules[col] if col is not None and col < len(cellules) and cellules[col] else cellules[1]
                groupes[int(cellules[0])] = physique
    return groupes


COLONNES_EN_PLUS = ["delta_s", "delta_pf", "n_paliers", "duree_chaine_ms", "marche_max"]   # firmware >= 0.8


def ecrire_ligne(dataset_path, label, event):
    ligne = [label, event["delta"], event["transient_duration"], event["type"]]
    if event.get("n_paliers") is not None:   # firmware >= 0.8 : les nouvelles caractéristiques
        ligne += [event[k] for k in COLONNES_EN_PLUS]
    with open(dataset_path, 'a', newline='') as f:
        csv.writer(f).writerow(ligne)


def demander(question, repondre=input):
    return repondre(question).strip().lower().startswith("o")


# ------------------------------------------------------------------ état des appareils
def commandes_possibles(etat, groupes):
    """Les commandes qui ont un sens dans l'état noté. etat = {appareil physique: label allumé ou None}.
    Renvoie [(appareil, action, label, type attendu)] ; type None = ON ou OFF (changement de mode)."""
    out = []
    for physique in sorted(etat):
        modes = sorted(l for l, g in groupes.items() if g == physique)
        allume = etat[physique]
        if allume is None:
            out += [(physique, "ALLUMER", l, "ON") for l in modes]
        else:
            out.append((physique, "ETEINDRE", allume, "OFF"))
            out += [(physique, "PASSER A", l, None) for l in modes if l != allume]
    return out


def decrire_etat(etat, appareils):
    allumes = [appareils[l] for _, l in sorted(etat.items()) if l is not None]
    return "allumes : " + (", ".join(allumes) if allumes else "aucun")


def demander_etat_initial(appareils, groupes, repondre=input):
    """Affiche la liste et demande ce qui est allumé. Renvoie {appareil physique: label allumé ou None}."""
    print("Appareils de la liste :")
    for label in sorted(appareils):
        print("   %3d  %s" % (label, appareils[label]))
    while True:
        rep = repondre("Numeros des appareils ALLUMES en ce moment (ex. 1,3), ou Entree si tout est eteint : ")
        rep = rep.strip().lower()
        etat = {g: None for g in groupes.values()}
        if rep in ("", "n", "non", "0", "aucun", "rien"):
            return etat
        try:
            labels = [int(x) for x in rep.replace(";", ",").replace(" ", ",").split(",") if x]
        except ValueError:
            print("   reponse incomprise : des numeros de la liste, separes par des virgules")
            continue
        inconnus = [l for l in labels if l not in groupes]
        doubles = [g for g in etat if sum(1 for l in labels if groupes.get(l) == g) > 1]
        if inconnus:
            print("   numero(s) inconnu(s) : %s" % ", ".join(map(str, inconnus)))
        elif doubles:
            print("   un appareil ne peut etre que dans un seul mode : %s" % ", ".join(doubles))
        else:
            for l in labels:
                etat[groupes[l]] = l
            return etat


def constituer(board, appareils, dataset_path, nombre, duree=DUREE_COMMANDE, pause=PAUSE_ENTRE_COMMANDES,
               repondre=input, hasard=random, groupes=None, sur_commande=None):
    """Lance `nombre` commandes. Renvoie le nombre de lignes écrites.
    groupes : {label: appareil physique} (charger_groupes) ; None = chaque label est un appareil.
    sur_commande(action, label, etat) : appelée avant chaque commande (pour les tests)."""
    if groupes is None:
        groupes = {label: appareils[label] for label in appareils}
    groupes = {l: g for l, g in groupes.items() if l in appareils}
    etat = demander_etat_initial(appareils, groupes, repondre)
    ecrites = 0

    for n in range(nombre):
        possibles = commandes_possibles(etat, groupes)
        physique = hasard.choice(sorted({p for p, _, _, _ in possibles}))   # d'abord l'appareil, au hasard
        _, action, label, attendu = hasard.choice([c for c in possibles if c[0] == physique])
        if sur_commande:
            sur_commande(action, label, dict(etat))

        time.sleep(pause)
        board.vider()          # on oublie les événements arrivés avant la commande
        print("\n   (etat note : %s)" % decrire_etat(etat, appareils))
        print("[%d/%d] %s : %s   (vous avez %d s)" % (n + 1, nombre, action, appareils[label], duree))

        event, fin = None, time.time() + duree
        while time.time() < fin:
            e = board.attendre_evenement(timeout=fin - time.time())
            if e is None:
                break
            if attendu is None or e["type"] == attendu:
                event = e
                break
            print("   evenement %s ignore (on attend un %s)" % (decrire(e), attendu))

        fait = False
        if event:
            ecrire_ligne(dataset_path, label, event)
            ecrites += 1
            fait = True
            print("   OK, ecrit : %s -> %s" % (appareils[label], decrire(event)))
        else:
            print("   rien detecte.")
            # si vous l'avez fait quand même, l'état de l'appareil a changé : on le note
            fait = demander("   Avez-vous quand meme fait « %s : %s » ? (o/n) " % (action.lower(), appareils[label]),
                            repondre)
        if fait:
            etat[physique] = None if action == "ETEINDRE" else label

    print("\n%d ligne(s) ajoutee(s) a %s" % (ecrites, dataset_path))
    return ecrites
