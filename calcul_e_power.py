"""Calcule E_POWER à partir de ce que la carte mesure VRAIMENT chez vous.

E_POWER (firmware/src/constants.h, 49 W par défaut) est le seuil au-delà duquel un événement est
confirmé TOUT DE SUITE, sans attendre D_MIN (5 s). Un événement aussi rapide ne doit jamais pouvoir
venir du bruit de mesure : E_POWER doit donc rester bien au-dessus des fausses « marches » que le
bruit fabrique dans la fenêtre du détecteur.

Ce script :
  1. écoute la puissance de chaque période (MODE 4 + SET PWR 1), sans que rien ne change : celle que
     reçoit le détecteur (champ S_detecteur, moyennée sur SET MOYENNE périodes, firmware >= 0.7) ;
  2. rejoue EXACTEMENT ce que voit le détecteur : fenêtres de 25 périodes, filtre médian de 5,
     région du début (w_s), du milieu (w_m) et de la fin (w_e), comme functions.py / window.py ;
  3. mesure la plus grande fausse marche due au bruit, et propose
        E_POWER = max(P_TH + 1, MARGE x plus grande fausse marche), arrondi aux 5 W au-dessus ;
  4. dit si les seuils actuels (P_TH, SIDE_TH, MID_TH, E_POWER) laissent passer le bruit.

Le bruit grandit avec la charge (l'ondulation de mesure est de quelques % de la puissance) :
lancez-le une fois À VIDE, puis une fois avec le PLUS GROS appareil allumé en continu, et gardez
la plus grande des deux valeurs.

Exemples (depuis python/) :
  python calcul_e_power.py                       # 60 s d'écoute sur le port de constants.py
  python calcul_e_power.py --secondes 120 --port /dev/ttyUSB1
  python calcul_e_power.py --csv puissance.csv   # relit un enregistrement de listener.py :
        python listener.py --cmd "SET PWR 1" --mode 4 --seconds 60 --csv puissance.csv

Pendant l'écoute : n'allumez et n'éteignez RIEN. Ctrl+C arrête (la carte se remet au repos).
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import statistics
import sys
import time

import faran_link as link
from constants import PORT

# ------------------------------------------------------------ réglages du calcul
MARGE = 3.0              # E_POWER >= 3 x la plus grande fausse marche due au bruit
ARRONDI_W = 5.0          # la proposition est arrondie aux 5 W au-dessus
PERIODES_MINI = 500      # ~10 s de mesures : en dessous, le calcul n'est pas fiable
WINDOW_LEN = 25          # fenêtre du détecteur (window.h / window.py)

# Seuils du détecteur : relus dans firmware/src/constants.h (ces valeurs ne servent que s'il est absent).
SEUILS_DEFAUT = {"P_TH": 25.0, "SIDE_TH": 25.0, "MID_TH": 20.0, "E_POWER": 49.0}
CONSTANTS_H = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware", "src", "constants.h")


# ====================================================================== seuils actuels du firmware
def lire_seuils(chemin=CONSTANTS_H):
    """Les seuils écrits dans constants.h (constexpr double NOM = valeur;)."""
    seuils = dict(SEUILS_DEFAUT)
    try:
        texte = open(chemin, encoding="utf-8").read()
    except OSError:
        return seuils
    for nom in seuils:
        m = re.search(r"constexpr\s+double\s+%s\s*=\s*([0-9.]+)" % nom, texte)
        if m:
            seuils[nom] = float(m.group(1))
    return seuils


# ====================================================================== écoute
def ecouter_carte(board, secondes, out=print):
    """Puissances (VA) de chaque période, pendant `secondes`. Renvoie (puissances, evenements_recus)."""
    board.envoyer("SET VERIFY 1")
    board.envoyer("SET PWR 1")
    board.envoyer("MODE 4")
    out("Écoute pendant %.0f s (+ ~3 s de mise en route de la carte). Ne touchez à rien..." % secondes)
    puissances, evenements = [], []
    debut, dernier_affichage = None, time.time()
    while True:
        msg = board.lire_message()
        if msg is None:
            if getattr(board.serial, "eof", False):
                break
            continue
        if msg.kind == "PWR":
            p = link.parse_pwr(msg)
            if p:
                if debut is None:
                    debut = time.time()
                puissances.append(p["s_detecteur"])   # ce que reçoit le détecteur (moyenne sur N périodes)
        elif msg.kind == "EVT":
            ev = link.parse_event(msg)
            if ev:
                evenements.append(ev)
                out("   !! événement reçu pendant l'écoute : %s %+.1f W" % (ev["type"], ev["delta"]))
        elif msg.kind == "ERR":
            out("   [carte] " + msg.body.replace(",", " ", 1))
        if debut is not None and time.time() - dernier_affichage >= 10:
            dernier_affichage = time.time()
            out("   %3.0f s : %d périodes, %.1f VA en moyenne" % (time.time() - debut, len(puissances),
                                                                     statistics.fmean(puissances)))
        if debut is not None and time.time() - debut >= secondes:
            break
    return puissances, evenements


def lire_csv(chemin):
    """Puissances (VA) depuis un CSV de listener.py (lignes PWR : pc_time, PWR, s_va, vrms, irms, période,
    s_detecteur, n_moyenne). On prend s_detecteur (ce que reçoit le détecteur) ; ancien fichier : s_va."""
    puissances = []
    for row in csv.reader(open(chemin, newline="")):
        if len(row) >= 3 and row[1] == "PWR":
            try:
                puissances.append(float(row[6]) if len(row) >= 7 and row[6] else float(row[2]))
            except ValueError:
                pass
    return puissances


# ====================================================================== ce que voit le détecteur
def filtre_median5(x):
    """Filtre médian de taille 5, bords réfléchis : identique à window.h et à scipy median_filter(x, 5)."""
    n, out = len(x), []
    for k in range(n):
        v = []
        for o in range(-2, 3):
            j = k + o
            if j < 0:
                j = -j - 1
            if j >= n:
                j = 2 * n - j - 1
            v.append(x[j])
        out.append(sorted(v)[2])
    return out


def regions(fenetre):
    """w_s, w_m, w_e : comme select_window_region_to_analyse() de functions.py."""
    n = len(fenetre)
    return fenetre[1:n // 3], fenetre[n // 2 - 2:n // 2 + 3], fenetre[n - n // 3 + 1:]


def fausses_marches(puissances):
    """Pour chaque fenêtre de 25 périodes (filtrée), les deux grandeurs que le détecteur compare à ses seuils :
       - cote : |moyenne(w_s) - moyenne(w_e)|  (la « marche » vue par SIDE_TH, P_TH et E_POWER) ;
       - milieu : plus grand écart dans w_m   (vu par MID_TH).
    Sur un signal où rien ne change, ce sont de FAUSSES marches : uniquement du bruit."""
    cotes, milieux = [], []
    for k in range(len(puissances) - WINDOW_LEN + 1):
        w_s, w_m, w_e = regions(filtre_median5(puissances[k:k + WINDOW_LEN]))
        cotes.append(abs(statistics.fmean(w_s) - statistics.fmean(w_e)))
        milieux.append(max(w_m) - min(w_m))
    return cotes, milieux


def centile(x, q):
    s = sorted(x)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


# ====================================================================== calcul de E_POWER
def calculer(puissances, seuils, marge=MARGE):
    """Renvoie un dictionnaire : bruit mesuré, E_POWER proposé, et ce qui ne va pas avec les seuils actuels."""
    moyenne = statistics.fmean(puissances)
    cotes, milieux = fausses_marches(puissances)
    pire_cote, pire_milieu = max(cotes), max(milieux)
    e_min_bruit = marge * pire_cote
    e_propose = max(seuils["P_TH"] + 1.0, e_min_bruit)
    e_propose = math.ceil(e_propose / ARRONDI_W) * ARRONDI_W

    alertes = []
    if pire_cote >= seuils["P_TH"] or pire_cote >= seuils["SIDE_TH"]:
        alertes.append("le bruit seul fait des marches de %.1f W, au-dessus de P_TH/SIDE_TH (%.0f/%.0f W) : "
                       "faux événements probables. Revoir le câblage (DEPANNAGE_RAFALES.md) avant de toucher aux seuils"
                       % (pire_cote, seuils["P_TH"], seuils["SIDE_TH"]))
    if pire_milieu >= seuils["MID_TH"]:
        alertes.append("le milieu de fenêtre bouge de %.1f W sans raison (MID_TH = %.0f W) : candidats parasites"
                       % (pire_milieu, seuils["MID_TH"]))
    if seuils["E_POWER"] < e_min_bruit:
        alertes.append("E_POWER actuel (%.0f W) < %.0f x bruit (%.1f W) : un pic de bruit pourrait être confirmé "
                       "tout de suite" % (seuils["E_POWER"], marge, e_min_bruit))
    return {
        "periodes": len(puissances),
        "moyenne_va": moyenne,
        "ondulation_pct": 100 * statistics.pstdev(puissances) / moyenne if moyenne > 0 else 0.0,
        "pire_cote": pire_cote, "cote_99": centile(cotes, 0.99),
        "pire_milieu": pire_milieu,
        "e_min_bruit": e_min_bruit,
        "e_power_propose": e_propose,
        "e_power_actuel": seuils["E_POWER"],
        "alertes": alertes,
    }


def afficher(r, seuils, evenements=(), out=print):
    out("")
    out("RÉSULTAT (%d périodes, soit %.0f s)" % (r["periodes"], r["periodes"] * 0.02))
    out("  puissance pendant l'écoute : %.1f VA en moyenne (ondulation %.1f %% d'une période à l'autre)"
        % (r["moyenne_va"], r["ondulation_pct"]))
    out("  plus grande fausse marche due au bruit : %.1f W (99 %% des fenêtres sous %.1f W)"
        % (r["pire_cote"], r["cote_99"]))
    out("  plus grand écart au milieu de fenêtre   : %.1f W" % r["pire_milieu"])
    out("")
    out("  E_POWER minimal sûr = max(P_TH + 1 = %.0f, %.0f x %.1f = %.1f)  ->  proposé : %.0f W"
        % (seuils["P_TH"] + 1, MARGE, r["pire_cote"], r["e_min_bruit"], r["e_power_propose"]))
    out("  E_POWER actuel (constants.h) : %.0f W" % r["e_power_actuel"])
    if evenements:
        out("")
        out("  ATTENTION : %d événement(s) pendant l'écoute. Si personne n'a rien allumé/éteint, ce sont de faux "
            "événements ; sinon, refaites l'écoute sans rien toucher." % len(evenements))
    out("")
    if r["alertes"]:
        out("VERDICT : à revoir")
        for a in r["alertes"]:
            out("  - " + a)
    elif r["e_power_actuel"] >= r["e_power_propose"]:
        out("VERDICT : E_POWER = %.0f W est sûr ici (il pourrait descendre jusqu'à %.0f W)."
            % (r["e_power_actuel"], r["e_power_propose"]))
    else:
        out("VERDICT : passer E_POWER à %.0f W." % r["e_power_propose"])
    out("")
    out("Pour changer E_POWER : firmware/src/constants.h, puis Upload. Un nouveau E_POWER change la façon dont")
    out("certains événements sont confirmés : refaire le dataset puis 'python main.py entrainer'.")
    out("Refaire ce calcul avec le plus gros appareil allumé en continu : le bruit grandit avec la charge.")


# ====================================================================== programme
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=PORT, help="port série de la carte (défaut : %s)" % PORT)
    ap.add_argument("--secondes", type=float, default=60.0, help="durée d'écoute (défaut : 60)")
    ap.add_argument("--csv", help="relire un enregistrement de listener.py au lieu d'écouter la carte")
    ap.add_argument("--marge", type=float, default=MARGE, help="E_POWER >= marge x bruit (défaut : %.0f)" % MARGE)
    a = ap.parse_args(argv)

    seuils = lire_seuils()
    print("Seuils du détecteur (constants.h) : P_TH %.0f W, SIDE_TH %.0f W, MID_TH %.0f W, E_POWER %.0f W"
          % (seuils["P_TH"], seuils["SIDE_TH"], seuils["MID_TH"], seuils["E_POWER"]))

    evenements = []
    if a.csv:
        puissances = lire_csv(a.csv)
    else:
        if a.port == "sim":
            print("La carte simulée (--port sim) n'envoie pas de puissances : utilisez la vraie carte, ou --csv.")
            return 1
        from board import Board
        board = Board(a.port, afficher_infos=False)
        try:
            puissances, evenements = ecouter_carte(board, a.secondes)
        except KeyboardInterrupt:
            print("\nArrêt demandé.")
            puissances = []
        finally:
            board.envoyer("SET PWR 0")
            board.envoyer("MODE 5")   # la carte se met au repos
            board.fermer()

    if len(puissances) < PERIODES_MINI:
        print("Pas assez de mesures (%d périodes, il en faut au moins %d, soit ~10 s)." % (len(puissances), PERIODES_MINI))
        return 1
    afficher(calculer(puissances, seuils, a.marge), seuils, evenements)
    return 0


if __name__ == "__main__":
    sys.exit(main())