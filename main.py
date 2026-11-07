# Point d'entrée du côté PC. Un mode par usage :
#
#   python main.py afficher                 affiche les événements envoyés par la carte
#   python main.py classer                  affiche + reconnaît l'appareil (il faut modele.pkl)
#   python main.py enregistrer              écrit chaque événement dans evenements.csv (tests)
#   python main.py dataset --nombre 20      constitue dataset.csv (20 commandes guidées)
#   python main.py entrainer                apprend dataset.csv -> modele.pkl
#   python main.py production               détecte, classe, tient le registre des appareils en marche
#   python main.py diagnostic [--a-vide]    TOUS les examens de la chaîne de mesure, puis un rapport
#   python main.py tension [--adc]          seulement la tension, comparée aux valeurs normales
#   python main.py courant [--charge W]     seulement l'intensité, comparée aux valeurs normales
#
# Options : --port /dev/ttyUSB1   (ou --port sim : carte simulée, sans matériel)
#           --sans-capteur        la vraie carte simule elle-même les capteurs (MODE 3)

import argparse
import csv
import os
import time
from datetime import datetime

from board import Board, decrire
from classifier import EventClassifier
from constants import *
from dataset import charger_appareils, constituer
from diagnostic import examen_complet, suivre_voie
from registre import Registre


def ouvrir_carte(args):
    board = Board(args.port)
    board.demarrer(MODE_CARTE_SIMULE if args.sans_capteur else MODE_CARTE_REEL)
    print("Carte en ecoute sur %s. La 1re detection arrive ~3 s apres le demarrage. Ctrl+C pour arreter." % args.port)
    return board


def heure():
    return datetime.now().strftime("%H:%M:%S")


def afficher(board):
    for event in board.evenements():
        print(heure(), decrire(event))


def classer(board, classifier, appareils):
    for event in board.evenements():
        label, etat = classifier.identify(event)
        print(heure(), decrire(event), "=>", appareils.get(label, label), etat)


def enregistrer(board, path):
    nouveau = not os.path.exists(path)
    with open(path, 'a', newline='') as f:
        fichier = csv.writer(f)
        if nouveau:
            fichier.writerow(["heure_pc", "type", "delta", "s_before", "s_after", "start", "end_of_transient",
                              "confirmed", "transient_duration", "confirmed_by", "is_pair_net_event", "seq", "board_ms",
                              "delta_s", "delta_pf", "n_paliers", "duree_chaine_ms", "marche_max", "puissance", "algo"])
        for event in board.evenements():
            fichier.writerow([datetime.now().isoformat(timespec="seconds"), event["type"], event["delta"], event["s_before"],
                              event["s_after"], event["start"], event["end_of_transient"], event["confirmed"],
                              event["transient_duration"], event["confirmed_by"], int(event["is_pair_net_event"]),
                              event["seq"], event["board_ms"], event["delta_s"], event["delta_pf"], event["n_paliers"],
                              event["duree_chaine_ms"], event["marche_max"], event["puissance"], event["algo"]])
            f.flush()   # écrit tout de suite : rien n'est perdu si on débranche
            print(heure(), "enregistre :", decrire(event))


def production(board, classifier, appareils):
    registre = Registre(appareils, JOURNAL)
    prochain_affichage = 0
    while True:
        event = board.attendre_evenement(timeout=1)
        if event:
            label, _ = classifier.identify(event)   # l'appareil vient du classifieur, ON/OFF du détecteur
            print("\n%s %s" % (heure(), registre.ajouter(label, event)))
            prochain_affichage = 0
        if time.time() >= prochain_affichage:
            print("\n--- registre a %s ---\n%s" % (heure(), registre.tableau()))
            prochain_affichage = time.time() + RAFRAICHISSEMENT


def main(argv=None):
    ap = argparse.ArgumentParser(description="Faran : detection, classification et registre des appareils")
    ap.add_argument("mode", choices=["afficher", "classer", "enregistrer", "dataset", "entrainer", "production",
                                     "diagnostic", "tension", "courant"])
    ap.add_argument("--port", default=PORT, help="port serie de la carte, ou 'sim' (defaut : %s)" % PORT)
    ap.add_argument("--sans-capteur", action="store_true", help="la carte simule les capteurs (MODE 3)")
    ap.add_argument("--nombre", type=int, default=10, help="mode dataset : nombre de commandes")
    ap.add_argument("--duree", type=float, default=DUREE_COMMANDE, help="mode dataset : secondes par commande")
    ap.add_argument("--fichier", default=EVENEMENTS, help="mode enregistrer : fichier de sortie")
    ap.add_argument("--a-vide", action="store_true", help="diagnostic : aucun appareil ne consomme (examen plus severe)")
    ap.add_argument("--secondes", type=float, default=30.0, help="diagnostic : duree de chaque examen de puissance")
    ap.add_argument("--sans-comparaison", action="store_true", help="diagnostic : ne pas refaire l'examen avec l'ancienne lecture")
    ap.add_argument("--charge", type=float, help="mode courant : puissance attendue (W) ; sans elle, on compare a vide")
    ap.add_argument("--adc", action="store_true", help="modes tension/courant : valeurs vues par l'ADC (MODE 2)")
    args = ap.parse_args(argv)

    if args.mode == "entrainer":
        classifieur = EventClassifier()
        precision = classifieur.entrainer(DATASET, MODELE)
        print("Modele enregistre dans %s (caracteristiques : %s)." % (MODELE, ", ".join(classifieur.features)))
        if len(classifieur.features) < len(FEATURES):
            print("ATTENTION : %s contient des lignes a l'ANCIEN format (4 colonnes, firmware < 0.8). Le modele n'utilise"
                  " que delta et transient_duration. Refaites le dataset (python main.py dataset) avec le firmware"
                  " 0.8 : les evenements sont maintenant mesures autrement (puissance active, demarrages regroupes)."
                  % DATASET)
        print("Precision sur des evenements jamais vus : %s" % ("%.0f %%" % (100 * precision) if precision is not None
                                                                 else "dataset trop petit pour la mesurer (< 10 lignes)"))
        return

    appareils = charger_appareils(APPAREILS) if args.mode in ("classer", "dataset", "production") else {}
    classifier = EventClassifier().charger(MODELE) if args.mode in ("classer", "production") else None
    if args.mode in ("diagnostic", "tension", "courant"):
        if args.port == "sim" or args.sans_capteur:
            print("Le diagnostic examine les VRAIS capteurs : il faut la carte (pas --port sim ni --sans-capteur).")
            return
        board = Board(args.port)
        try:
            if args.mode == "diagnostic":
                examen_complet(board, duree=args.secondes, a_vide=args.a_vide, comparer=not args.sans_comparaison)
            else:
                suivre_voie(board, args.mode, charge_w=args.charge, adc=args.adc)
        except KeyboardInterrupt:
            print("\narret.")
        finally:
            board.envoyer("SET VERIFY 1")
            board.envoyer("SET PWR 0")
            board.envoyer("MODE 5")
            board.fermer()
        return

    board = ouvrir_carte(args)
    try:
        if args.mode == "afficher":
            afficher(board)
        elif args.mode == "classer":
            classer(board, classifier, appareils)
        elif args.mode == "enregistrer":
            enregistrer(board, args.fichier)
        elif args.mode == "dataset":
            constituer(board, appareils, DATASET, args.nombre, duree=args.duree)
        elif args.mode == "production":
            production(board, classifier, appareils)
    except KeyboardInterrupt:
        print("\narret.")
    finally:
        board.envoyer("MODE 5")   # la carte se met au repos
        board.fermer()


if __name__ == "__main__":
    main()
