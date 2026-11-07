"""Analyse des MESURES BRUTES de l'ADS1115 (aucun calcul de puissance, aucune simulation).

1. Enregistrer 10 s de mesures brutes (la carte envoie les codes de l'ADC, tels quels) :
       python listener.py --mode 1 --seconds 10 --csv brut.csv
2. Analyser :
       python analyse_brut.py brut.csv
   (options --vgain / --igain si vous avez étalonné : V par mV et A par mV, voir STATUS)

Faire l'enregistrement avec la pince BRANCHÉE mais autour d'AUCUN fil : le courant doit être ~0.
Le programme dit si la voie courant (A1 - A3 en différentiel) voit :
  - presque rien (normal),
  - du 50 Hz capté (fils de la pince, résistance de charge),
  - une COPIE de la tension (A0 et A1 reliées, entrée A1 ou A3 en l'air, ou voies inversées),
et si l'alternance tension/courant est régulière (cadence, trous, sauts brusques).

Ce qui est « normal » (centrage, limites d'écrêtage) dépend du câblage de chaque voie : le fichier
contient la ligne ADC de la carte (enregistrée par listener.py), sinon on prend constants.py (adc.py).
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys

import adc
from constants import COURANT_A_VIDE_MAX


def lire(path):
    """{'V': [(t_us, mv)], 'I': [...]} depuis le CSV de listener.py (horloge 32 bits dépliée)."""
    voies = {"V": [], "I": []}
    for row in csv.reader(open(path, newline="")):
        if len(row) >= 6 and row[1] == "ADC":   # pc_time, ADC, differentiel, entree_i, lsb_i_mv, pleine_echelle_i_mv
            v = adc.voie("V")
            adc.appliquer({"differentiel": row[2] == "1", "entree_i": row[3], "lsb_i_mv": float(row[4]),
                           "pleine_echelle_i_mv": float(row[5]), "entree_v": v.entree, "lsb_v_mv": v.lsb_mv,
                           "pleine_echelle_v_mv": v.pleine_echelle_mv})
            continue
        if len(row) < 4 or row[1] not in ("RAW_V", "RAW_I"):
            continue
        voies[row[1][-1]].append((float(row[2]), float(row[3])))
    for ch, pts in voies.items():
        out, base, prev = [], 0.0, None
        for t, mv in pts:
            if prev is not None and t < prev - 2**31:
                base += 2**32                        # micros() a débordé
            prev = t
            out.append((t + base, mv))
        voies[ch] = out
    return voies


def resume(pts):
    t = [p[0] for p in pts]
    x = [p[1] for p in pts]
    n = len(x)
    off = statistics.fmean(x)
    ac = [v - off for v in x]
    rms = math.sqrt(sum(a * a for a in ac) / n)
    dts = [b - a for a, b in zip(t, t[1:])]
    dt = statistics.median(dts)
    # fréquence par passages par zéro montants (avec hystérésis de 20 % de l'amplitude)
    hyst = 0.2 * rms * math.sqrt(2)
    cross, armed = [], False
    for k in range(1, n):
        if ac[k] < -hyst:
            armed = True
        if armed and ac[k - 1] < 0 <= ac[k]:
            cross.append(t[k - 1] + (-ac[k - 1]) / (ac[k] - ac[k - 1]) * (t[k] - t[k - 1]))
            armed = False
    freq = (len(cross) - 1) / ((cross[-1] - cross[0]) / 1e6) if len(cross) > 2 else 0.0
    # sauts : un point très loin de la moyenne de ses deux voisins (un 50 Hz à ~8 points/période
    # ne fait pas ça : l'écart normal reste < ~30 % de l'amplitude crête)
    crete = rms * math.sqrt(2)
    sauts = [k for k in range(1, n - 1)
             if abs(x[k] - (x[k - 1] + x[k + 1]) / 2) > max(0.6 * crete, 40.0)]
    return {"n": n, "sps": 1e6 / dt if dt > 0 else 0, "offset": off, "min": min(x), "max": max(x),
            "rms": rms, "freq": freq, "trous": sum(1 for d in dts if d > 1.8 * dt), "sauts": len(sauts),
            "dt_max_ms": max(dts) / 1000.0, "t": t, "ac": ac}


def correlation(v, i):
    """Corrélation entre le courant et la tension interpolée aux mêmes instants (+ rapport des amplitudes)."""
    tv, av = v["t"], v["ac"]
    paires, j = [], 0
    for t, a in zip(i["t"], i["ac"]):
        while j < len(tv) - 2 and tv[j + 1] < t:
            j += 1
        if tv[j] <= t <= tv[j + 1]:
            f = (t - tv[j]) / (tv[j + 1] - tv[j])
            paires.append((av[j] + f * (av[j + 1] - av[j]), a))
    if len(paires) < 20:
        return 0.0, 0.0
    xs, ys = [p[0] for p in paires], [p[1] for p in paires]
    sxx = sum(x * x for x in xs)
    syy = sum(y * y for y in ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    r = sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0
    return r, (sxy / sxx if sxx > 0 else 0.0)


def analyser(voies, vgain=0.30, igain=0.1, out=print):
    constats = []
    if len(voies["V"]) < 100 or len(voies["I"]) < 100:
        out("Pas assez de mesures (V : %d, I : %d). Le fichier vient-il de 'listener.py --mode 1 --csv' ?"
            % (len(voies["V"]), len(voies["I"])))
        return ["pas de donnees"]
    v, i = resume(voies["V"]), resume(voies["I"])
    vv, vi = adc.voie("V"), adc.voie("I")
    for nom, r, g, u in ((vv.libelle().upper(), v, vgain, "V"), (vi.libelle().upper(), i, igain, "A")):
        out("%s : %d points, %.0f points/s, centrage %.1f mV, de %.1f a %.1f mV, efficace %.2f mV (= %.3f %s), "
            "%.2f Hz, trous %d (plus long %.1f ms), sauts brusques %d"
            % (nom, r["n"], r["sps"], r["offset"], r["min"], r["max"], r["rms"], r["rms"] * g, u, r["freq"],
               r["trous"], r["dt_max_ms"], r["sauts"]))
    corr, pente = correlation(v, i)
    out("Corrélation courant / tension : %.2f (amplitude courant = %.2f x tension)" % (corr, pente))
    out("")

    # --- alternance
    if abs(v["n"] - i["n"]) > 0.02 * max(v["n"], i["n"]) + 5:
        constats.append("Alternance déséquilibrée : %d points tension pour %d points courant (doivent être égaux)."
                        % (v["n"], i["n"]))
    if v["sps"] < 300 or i["sps"] < 300:
        constats.append("Cadence faible (%.0f / %.0f points/s, attendu ~360 à 430) : fil RDY ou bus I2C."
                        % (v["sps"], i["sps"]))
    if v["trous"] + i["trous"] > 0.01 * (v["n"] + i["n"]):
        constats.append("Beaucoup de trous dans les mesures (%d) : mesures jetées ou perdues, parasites RDY/I2C."
                        % (v["trous"] + i["trous"]))

    # --- tension
    if not vv.centrage_ok(v["offset"]):
        constats.append("Tension : centrage %.0f mV au lieu de ~%.0f mV." % (v["offset"], vv.centre_mv))
    if vv.ecrete(v["min"], v["max"]):
        constats.append("Tension : signal écrêté (%.0f à %.0f mV)." % (v["min"], v["max"]))
    if not 49 <= v["freq"] <= 51:
        constats.append("Tension : fréquence %.2f Hz, le 50 Hz n'est pas reconnu (signal absent ou très bruité)."
                        % v["freq"])
    if v["sauts"]:
        constats.append("Tension : %d sauts brusques. Un vrai 50 Hz n'en fait pas : faux contact, parasites, "
                        "ou mesures de l'autre voie mélangées." % v["sauts"])

    # --- courant (pince branchée, autour d'aucun fil)
    if not vi.centrage_ok(i["offset"]):
        constats.append("Courant : centrage %.1f mV au lieu de ~%.0f mV -> %s." % (i["offset"], vi.centre_mv, vi.cause_centrage()))
    if vi.ecrete(i["min"], i["max"]):
        constats.append("Courant : signal écrêté (%.1f à %.1f mV)." % (i["min"], i["max"]))
    # au-delà du courant « à vide » normal (constants.py), le signal n'est plus du bruit de fond
    if i["rms"] > COURANT_A_VIDE_MAX / igain:
        if corr > 0.8 and pente > 0.2:
            constats.append(
                "Courant : la voie courant voit une COPIE DE LA TENSION (corrélation %.2f, %.0f %% de son amplitude). "
                "Si aucun fil ne passe dans la pince, ce n'est pas un courant : A0 et A1 se touchent (pont de "
                "soudure, fils mélangés), A1 ou A3 n'est pas vraiment reliée (entrée en l'air), ou la masse/le 1,65 V "
                "est partagé avec le ZMPT101B. Vérifier au multimètre : résistance A1 <-> A3 ~20 ohms, "
                "A0 et A1 jamais reliées." % (corr, 100 * pente))
        elif 45 <= i["freq"] <= 55:
            constats.append(
                "Courant : %.1f mV efficaces de 50 Hz sans aucun fil dans la pince (%.2f A affichés). C'est du 50 Hz "
                "CAPTÉ : résistance de 20 ohms absente ou mal soudée (vérifier ~20 ohms entre les 2 fils de la "
                "pince), fils de la pince longs/non torsadés, ou montage collé à un câble secteur."
                % (i["rms"], i["rms"] * igain))
        else:
            constats.append(
                "Courant : %.1f mV efficaces de bruit non périodique (%.2f A affichés) : faux contact, A1 ou A3 en l'air, "
                "alimentation 3,3 V bruitée." % (i["rms"], i["rms"] * igain))
    if i["sauts"]:
        constats.append("Courant : %d sauts brusques (faux contact, parasites)." % i["sauts"])

    out("VERDICT")
    if not constats:
        out("  Mesures brutes saines : courant ~0, tension propre, alternance régulière.")
        out("  Si des rafales apparaissent quand même, enregistrer de la même façon PENDANT une rafale.")
    for c in constats:
        out("  - " + c)
    return constats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--vgain", type=float, default=0.30, help="V par mV (STATUS : gain)")
    ap.add_argument("--igain", type=float, default=0.1, help="A par mV (STATUS : igain)")
    a = ap.parse_args(argv)
    analyser(lire(a.csv), a.vgain, a.igain)


if __name__ == "__main__":
    sys.exit(main())
