"""REJEU : fait passer un enregistrement de puissance dans le VRAI détecteur de la carte, sur le PC.

Sans carte et sans reflasher, on voit quels événements la carte aurait détectés sur un enregistrement, avec
le détecteur de référence (votre algorithme) ou le détecteur amélioré, en donnant au détecteur la puissance
apparente S ou la puissance active P. C'est le code C++ du firmware (firmware/src/detection.h et ce qu'il
inclut), compilé pour le PC la première fois (il faut g++), avec les réglages de config.h et constants.h.

Enregistrer (la carte en mode 4, une ligne PWR par période) :
    python listener.py --cmd "SET PWR 1" --mode 4 --csv donnees_reelles/tele_projecteur.csv
Rejouer :
    python rejouer.py donnees_reelles/tele_projecteur.csv                  # amélioré, P (ou S si pas de P)
    python rejouer.py donnees_reelles/*.csv --algo ref --puissance S        # comme le firmware 0.7
    python rejouer.py donnees_reelles/*.csv --comparer                      # référence/S contre amélioré/P
    python rejouer.py fichier.csv --bruit                                   # + le bruit mesuré, chaque seconde

Fichiers acceptés : un CSV de listener.py (lignes PWR ; ancien format sans P : rejeu en S seulement), ou
la sortie brute de la carte (lignes « PWR,...*HH »). Si le fichier contient aussi les lignes EVT que la
carte a envoyées pendant l'enregistrement, elles sont rappelées à la fin pour comparer.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime

import faran_link as link

ICI = os.path.dirname(os.path.abspath(__file__))
FIRMWARE = os.path.join(ICI, "..", "firmware")
SOURCES = [os.path.join(FIRMWARE, "test", "host", "rejeu.cpp"),
           os.path.join(FIRMWARE, "src", "event_detector.cpp"),
           os.path.join(FIRMWARE, "src", "improved_detector.cpp")]
BINAIRE = os.path.join(FIRMWARE, "test", "host", "build", "rejeu" + (".exe" if os.name == "nt" else ""))


# ------------------------------------------------------------------ compilation du détecteur pour PC
def compiler(force=False):
    """Compile firmware/test/host/rejeu.cpp si besoin (sources, config.h ou constants.h plus récents)."""
    src_dir = os.path.join(FIRMWARE, "src")
    deps = SOURCES + [os.path.join(src_dir, f) for f in os.listdir(src_dir) if f.endswith(".h")]
    if not force and os.path.exists(BINAIRE) and os.path.getmtime(BINAIRE) >= max(map(os.path.getmtime, deps)):
        return BINAIRE
    gpp = shutil.which("g++") or shutil.which("clang++")
    if not gpp:
        sys.exit("Il faut un compilateur C++ (g++) pour le rejeu : sudo apt install g++  (Windows : MSYS2 / MinGW)")
    os.makedirs(os.path.dirname(BINAIRE), exist_ok=True)
    cmd = [gpp, "-std=c++11", "-O2", "-I", src_dir, "-o", BINAIRE] + SOURCES
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit("compilation du détecteur impossible :\n" + r.stderr)
    return BINAIRE


# ------------------------------------------------------------------ lecture d'un enregistrement
class Enregistrement:
    """Les périodes d'un fichier : s[k], p[k] (None si absent), heure[k] (datetime ou None), et les EVT
    envoyés par la carte pendant l'enregistrement."""

    def __init__(self, path):
        self.path = path
        self.s, self.p, self.heure, self.evt_carte = [], [], [], []
        self.n_moyenne = None
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            premiere = f.readline()
            f.seek(0)
            if premiere.startswith("pc_time,"):
                self._lire_csv(f)
            else:
                self._lire_brut(f)

    def _lire_csv(self, f):
        # listener.py : pc_time,kind,a,b,c,d,e,f,g,h,i
        # PWR : a=S période, b=Vrms, c=Irms, d=n° période, e=S détecteur, f=N moyenne, g=P période, h=PF, i=P détecteur
        for row in csv.reader(f):
            if len(row) < 3:
                continue
            if row[1] == "PWR":
                try:
                    s = float(row[6]) if len(row) > 6 and row[6] else float(row[2])
                    p = float(row[10]) if len(row) > 10 and row[10] else None
                    if len(row) > 7 and row[7]:
                        self.n_moyenne = int(float(row[7]))
                except ValueError:
                    continue
                self.s.append(s)
                self.p.append(p)
                self.heure.append(_heure(row[0]))
            elif row[1] == "EVT" and len(row) >= 4:
                self.evt_carte.append((_heure(row[0]), row[2], row[3]))

    def _lire_brut(self, f):
        for ligne in f:
            m = link.parse_line(ligne)
            if m is None:
                continue
            if m.kind == "PWR":
                d = link.parse_pwr(m)
                if d:
                    self.s.append(d["s_detecteur"])
                    self.p.append(d["p_detecteur"])
                    self.heure.append(None)
                    self.n_moyenne = d["n_moyenne"]
            elif m.kind == "EVT":
                e = link.parse_event(m)
                if e:
                    self.evt_carte.append((None, e["type"], "%.3f" % e["delta"]))

    def a_p(self):
        return bool(self.p) and all(x is not None for x in self.p)

    def quand(self, periode):
        """Heure de la période n° `periode` (1 = la première) : heure du PC, sinon temps depuis le début."""
        k = min(max(periode, 1), len(self.s)) - 1
        if self.heure and self.heure[k] is not None:
            return self.heure[k].strftime("%H:%M:%S.%f")[:-3]
        return "t=%7.2f s" % (periode * 0.02)


def _heure(texte):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%H:%M:%S.%f"):
        try:
            return datetime.strptime(texte, fmt)
        except ValueError:
            pass
    return None


def moyenne_glissante(x, n):
    """Moyenne des n dernières valeurs (pour rejouer un vieil enregistrement fait avec SET MOYENNE 1)."""
    if n <= 1:
        return list(x)
    out, somme = [], 0.0
    for k, v in enumerate(x):
        somme += v
        if k >= n:
            somme -= x[k - n]
        out.append(somme / min(k + 1, n))
    return out


# ------------------------------------------------------------------ rejeu
def rejouer(enr, algo="ameliore", puissance="P", moyenne=1, bruit=False):
    """Renvoie (événements, bilan, traces de bruit). Un événement = dictionnaire (mêmes clés que parse_event)."""
    s = moyenne_glissante(enr.s, moyenne)
    p = moyenne_glissante(enr.p, moyenne) if enr.a_p() else None
    if puissance == "P" and p is None:
        raise ValueError("%s n'a pas la puissance active P (ancien format) : rejouer avec --puissance S" % enr.path)
    lignes = "\n".join("%.4f %s" % (s[k], "%.4f" % p[k] if p else "nan") for k in range(len(s)))
    cmd = [compiler(), "--algo", algo, "--puissance", puissance] + (["--bruit"] if bruit else [])
    out = subprocess.run(cmd, input=lignes, capture_output=True, text=True, check=True).stdout
    evts, bilan, traces = [], "", []
    for l in out.splitlines():
        t = l.split()
        if t and t[0] == "EV":
            evts.append({
                "periode": int(t[1]), "type": t[2], "delta": float(t[3]), "s_before": float(t[4]),
                "s_after": float(t[5]), "start": int(t[6]), "end_of_transient": int(t[7]),
                "transient_duration": int(t[8]), "confirmed_by": t[9], "is_pair_net_event": t[10] == "1",
                "delta_s": float(t[11]), "delta_pf": float(t[12]), "n_paliers": int(t[13]),
                "duree_chaine_ms": int(t[14]), "marche_max": float(t[15])})
        elif t and t[0] == "NOISE":
            traces.append((int(t[1]), float(t[2]), float(t[3]), t[4] == "1"))
        elif l.startswith("#"):
            bilan = l[2:]
    return evts, bilan, traces


def decrire(enr, e):
    nan = lambda x: "  ?  " if math.isnan(x) else "%+6.1f" % x   # noqa: E731
    nanpf = lambda x: "  ?  " if math.isnan(x) else "%+.3f" % x  # noqa: E731
    return "%s  %-3s %+8.1f W  (%.1f -> %.1f)  dS %s VA  dPF %s  %d palier(s) en %.1f s, marche max %.1f W  [%s%s]" % (
        enr.quand(e["periode"]), e["type"], e["delta"], e["s_before"], e["s_after"], nan(e["delta_s"]),
        nanpf(e["delta_pf"]), e["n_paliers"], e["duree_chaine_ms"] / 1000.0, e["marche_max"], e["confirmed_by"],
        ", paire" if e["is_pair_net_event"] else "")


def afficher(enr, algo, puissance, moyenne, bruit, out=print):
    evts, bilan, traces = rejouer(enr, algo, puissance, moyenne, bruit)
    out("  -- détecteur %s, puissance %s%s : %d événement(s)" % (
        "AMÉLIORÉ" if algo == "ameliore" else "de RÉFÉRENCE", puissance,
        ", moyenne glissante %d" % moyenne if moyenne > 1 else "", len(evts)))
    for e in evts:
        out("     " + decrire(enr, e))
    out("     (%s)" % bilan)
    if traces:
        out("     bruit mesuré (W) : " + ", ".join("%s %.2f%s" % (enr.quand(k), b, "*" if c else "")
                                                for k, b, _, c in traces[::max(1, len(traces) // 12)]))
        out("     (* = une chaîne est ouverte à ce moment : le bruit n'est pas mis à jour)")
    return evts


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rejoue des enregistrements PWR dans le détecteur de la carte")
    ap.add_argument("fichiers", nargs="+")
    ap.add_argument("--algo", choices=["ref", "ameliore"], default="ameliore",
                    help="détecteur de référence (votre algorithme) ou amélioré (défaut)")
    ap.add_argument("--puissance", choices=["P", "S"], help="puissance donnée au détecteur (défaut : P si le fichier l'a)")
    ap.add_argument("--comparer", action="store_true", help="référence + S (firmware 0.7) puis amélioré + P")
    ap.add_argument("--moyenne", type=int, default=1, help="moyenne glissante en plus (vieux fichier fait avec SET MOYENNE 1)")
    ap.add_argument("--bruit", action="store_true", help="affiche aussi le bruit mesuré par le détecteur amélioré")
    ap.add_argument("--compiler", action="store_true", help="recompiler le détecteur même s'il est à jour")
    args = ap.parse_args(argv)
    if args.compiler:
        compiler(force=True)

    for path in args.fichiers:
        enr = Enregistrement(path)
        duree = len(enr.s) * 0.02
        print("\n== %s : %d périodes (%.0f s)%s%s" % (path, len(enr.s), duree,
              ", puissance active P présente" if enr.a_p() else ", sans P (ancien format : S seulement)",
              ", moyenne sur %d période(s) à l'enregistrement" % enr.n_moyenne if enr.n_moyenne else ""))
        if not enr.s:
            print("   aucune ligne PWR : enregistrer avec --cmd \"SET PWR 1\" --mode 4")
            continue
        if args.comparer:
            combis = [("ref", "S"), ("ameliore", "P" if enr.a_p() else "S")]
        else:
            combis = [(args.algo, args.puissance or ("P" if enr.a_p() else "S"))]
        for algo, puis in combis:
            if puis == "P" and not enr.a_p():
                print("   (pas de P dans ce fichier : rejeu en S)")
                puis = "S"
            afficher(enr, algo, puis, args.moyenne, args.bruit and algo == "ameliore")
        if enr.evt_carte:
            print("  -- pendant l'enregistrement, la carte avait envoyé %d événement(s) : %s" % (
                len(enr.evt_carte), ", ".join("%s %s" % (t, d) for _, t, d in enr.evt_carte[:12])
                + (" ..." if len(enr.evt_carte) > 12 else "")))


if __name__ == "__main__":
    main()
