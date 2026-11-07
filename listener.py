"""CONSOLE BAS NIVEAU (installation, étalonnage, dépannage). Pour l'usage normal : main.py.

Écoute la carte ESP32 sur le port série (pyserial), décode les messages et :
  - affiche chaque message avec l'heure du PC (comme votre ancien listener.py) ;
  - pour les flux bruts (MODE 1 et 6), calcule ICI, sur le PC, le résumé de chaque seconde
    (cadence, moyenne, crête à crête, valeur efficace) au lieu de le demander à la carte ;
  - peut enregistrer les échantillons / puissances / événements dans un fichier CSV ;
  - peut envoyer des commandes à la carte, depuis la ligne de commande ou au clavier.

Exemples (port Linux : /dev/ttyUSB0, parfois /dev/ttyUSB1 ; Mac : /dev/cu.usbserial-...) :
  python listener.py --port /dev/ttyUSB0                          # écouter seulement
  python listener.py --port /dev/ttyUSB0 --interactive            # écouter + taper MODE 0, HELP...
  python listener.py --port /dev/ttyUSB0 --mode 0                 # diagnostic de l'ADS1115
  python listener.py --port /dev/ttyUSB0 --mode 1 --csv raw.csv   # tension + courant bruts, enregistrés
  python listener.py --port /dev/ttyUSB0 --mode 2 --interactive   # étalonnage : taper CAL 229 puis CALI 4.3, SAVE
  python listener.py --port /dev/ttyUSB0 --cmd "SET PWR 1" --mode 4 --csv puissance.csv

Un seul programme peut utiliser le port à la fois : fermez le moniteur PlatformIO avant.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import threading
import time
from datetime import datetime

import adc
import faran_link as link

BAUD = 460800  # identique à SERIAL_BAUD dans config.h


class SecondSummary:
    """Résumé par seconde des flux bruts, calculé sur le PC (une voie)."""

    def __init__(self, name: str, unit: str, gain_per_mv: float):
        self.name, self.unit, self.gain = name, unit, gain_per_mv
        self.reset()

    def reset(self):
        self.mv = []
        self.t0 = None

    def add(self, t_us: float, mv: float):
        if self.t0 is None:
            self.t0 = t_us
        self.mv.append(mv)

    def ready(self, t_us: float) -> bool:
        return self.t0 is not None and (t_us - self.t0) >= 1e6

    def line(self, t_us: float) -> str:
        n = len(self.mv)
        mean = sum(self.mv) / n
        ac = [x - mean for x in self.mv]
        rms = math.sqrt(sum(x * x for x in ac) / n)
        fs = (n - 1) / ((t_us - self.t0) / 1e6) if t_us > self.t0 else 0
        s = "%s : %d points, %.0f SPS, offset %.1f mV, crete-a-crete %.1f mV, efficace %.2f mV (~%.3f %s)" % (
            self.name, n, fs, mean, max(self.mv) - min(self.mv), rms, rms * self.gain, self.unit)
        self.reset()
        return s


def diagnostic(kind, d):
    """Repère les branchements faux à partir d'une ligne VSTAT / ISTAT (mode 2).
    Ce qui est normal dépend du câblage de la voie (entrée simple ou différentielle) : voir adc.py."""
    v = adc.voie(kind)
    nom = v.libelle().upper()
    bas = d["offset_mv"] - d["vpp_mv"] / 2      # point le plus bas du signal
    haut = d["offset_mv"] + d["vpp_mv"] / 2
    out = []
    if not v.centrage_ok(d["offset_mv"]):
        out.append("%s : centrage a %.1f mV au lieu de ~%.0f mV (%s)" % (nom, d["offset_mv"], v.centre_mv, v.cause_centrage()))
    if v.ecrete(bas, haut):
        b, h = v.bornes_mv()
        out.append("%s : signal ecrete (de %.0f a %.0f mV, il doit rester entre %.0f et %.0f)" % (nom, bas, haut, b, h))
    if kind == "VSTAT" and d["vpp_mv"] < 300:
        out.append("%s : presque pas de signal (%.0f mV crete-a-crete, il faut ~1000 a 2000)" % (nom, d["vpp_mv"]))
    return out


class Listener:
    def __init__(self, ser, args, classifier=None, out=print):
        self.ser, self.args, self.classifier, self.out = ser, args, classifier, out
        self.summ = {"V": SecondSummary("tension", "V", args.vgain), "I": SecondSummary("courant", "A", args.igain)}
        self.invalid = 0
        self.csv_file = None
        self.csv = None
        self.stop = False
        self.n_events = 0

    # -------------------------------------------------------------- envoi
    def send(self, command: str):
        self.ser.write((command.strip() + "\n").encode("ascii"))

    # -------------------------------------------------------------- CSV
    def open_csv(self, path):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)   # ex. donnees_reelles/ (voir rejouer.py)
        self.csv_file = open(path, "w", newline="")
        self.csv = csv.writer(self.csv_file)
        # PWR : a = S de la période, b = Vrms, c = Irms, d = n° de période, e = S moyennée, f = N (moyenne),
        #       g = P de la période (W), h = PF, i = P moyennée (le détecteur reçoit |i| ou e) ; voir rejouer.py
        # EVT : a = ON/OFF, b = delta, c = durée, d = confirmé par, e = n_paliers, f = delta_S, g = delta_PF
        self.csv.writerow(["pc_time", "kind", "a", "b", "c", "d", "e", "f", "g", "h", "i"])

    def log(self, kind, *vals):
        if self.csv:
            self.csv.writerow([datetime.now().isoformat(timespec="milliseconds"), kind, *vals])

    # -------------------------------------------------------------- traitement d'une ligne
    def handle(self, raw):
        msg = link.parse_line(raw)
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if msg is None:
            self.invalid += 1
            if self.args.verbose:
                self.out("(ligne ignoree) %r" % (raw[:60] if isinstance(raw, (bytes, str)) else raw))
            return
        k = msg.kind
        if k == "RAW":
            blk = link.parse_raw(msg)
            if blk is None:
                self.invalid += 1
                return
            s = self.summ[blk.channel]
            for t, mv in blk.samples():
                s.add(t, mv)
                if self.csv:
                    self.csv.writerow([stamp, "RAW_" + blk.channel, "%.0f" % t, "%.3f" % mv, "", ""])
            if s.ready(blk.t_last_us):
                self.out("%s  %s" % (stamp, s.line(blk.t_last_us)))
        elif k == "EVT":
            ev = link.parse_event(msg)
            if ev is None:
                self.invalid += 1
                return
            self.n_events += 1
            self.out("%s  EVENEMENT %s %+.1f W  (avant %.1f -> apres %.1f, duree %d ms, confirme par %s%s%s)" % (
                stamp, ev["type"], ev["delta"], ev["s_before"], ev["s_after"], ev["transient_duration"],
                ev["confirmed_by"], ", paire" if ev["is_pair_net_event"] else "",
                "" if ev["n_paliers"] is None else ", %d palier(s) en %.1f s, dS %+.1f VA" % (
                    ev["n_paliers"], ev["duree_chaine_ms"] / 1000.0, ev["delta_s"])))
            self.log("EVT", ev["type"], ev["delta"], ev["transient_duration"], ev["confirmed_by"],
                     *(() if ev["n_paliers"] is None else (ev["n_paliers"], ev["delta_s"], ev["delta_pf"])))
        elif k == "PWR":
            d = link.parse_pwr(msg)
            if d:
                actif = () if d["p_w"] is None else (d["p_w"], d["pf"], d["p_detecteur"])
                self.log("PWR", d["s_va"], d["vrms"], d["irms"], d["period"], d["s_detecteur"], d["n_moyenne"], *actif)
                if self.args.verbose:
                    self.out("%s  S = %.1f VA (Vrms %.1f V, Irms %.3f A)%s ; moyenne sur %d periodes : S %.1f VA%s"
                             % (stamp, d["s_va"], d["vrms"], d["irms"],
                                "" if not actif else ", P = %.1f W, PF %.3f" % (d["p_w"], d["pf"]), d["n_moyenne"],
                                d["s_detecteur"], "" if not actif else ", P %.1f W" % d["p_detecteur"]))
        elif k == "STAT":
            d = link.parse_stat(msg)
            if d:
                warn = ""
                if d["rdy_timeouts"] or d["i2c_errors"] or d["spurious_rdy"] or d["desync"]:
                    warn = "  <- fil RDY ou bus I2C a verifier !"
                extra = ""
                if d["spurious_rdy"] is not None:
                    extra = " rdy_parasites=%d jetees=%d ecretage_V=%d ecretage_I=%d reinit_i2c=%d verify=%d" % (
                        d["spurious_rdy"], d["desync"], d["clip_v"], d["clip_i"], d["i2c_recoveries"], d["verify"])
                if d["bruit_w"] is not None and d["bruit_w"] > 0:
                    extra += " bruit=%.2fW seuil=%.1fW" % (d["bruit_w"], d["seuil_w"])
                self.out("%s  STAT periodes=%d evenements=%d ecartes=%d rejetees=%d points/s V=%d I=%d sans_RDY=%d i2c=%d%s%s" % (
                    stamp, d["periods"], d["events"], d["discarded"], d["bad_periods"], d["sps_v"], d["sps_i"],
                    d["rdy_timeouts"], d["i2c_errors"], extra, warn))
        elif k in ("VSTAT", "ISTAT"):
            d = link.parse_stats(msg)
            if d:
                for w in diagnostic(k, d):
                    self.out("%s  !! %s" % (stamp, w))
                self.out("%s  %s offset %.1f mV, crete-a-crete %.1f mV, efficace %.2f mV (= %.3f %s), %.3f Hz, cadence %.1f SPS" % (
                    stamp, k, d["offset_mv"], d["vpp_mv"], d["vrms_mv"], d["value"], "V" if k == "VSTAT" else "A",
                    d["f_hz"], d["fs"]))
        elif k == "ADC":   # déjà appliqué par parse_line : on le dit en clair
            d = link.parse_adc(msg)
            if d:
                self.log("ADC", 1 if d["differentiel"] else 0, d["entree_i"], d["lsb_i_mv"], d["pleine_echelle_i_mv"])
                self.out("%s  ADC : tension sur %s (1 code = %g mV), courant sur %s%s (1 code = %g mV, pleine echelle +-%.0f mV)" % (
                    stamp, d["entree_v"], d["lsb_v_mv"], d["entree_i"], " en differentiel" if d["differentiel"] else "",
                    d["lsb_i_mv"], d["pleine_echelle_i_mv"]))
        else:  # HELLO, MODE, INFO, ERR
            self.out("%s  %s" % (stamp, msg.body.replace(",", " ", 1) if k in ("INFO", "ERR", "MODE") else msg.body))

    # -------------------------------------------------------------- boucle
    def run(self):
        for c in self.args.cmd or []:
            self.send(c)
        if self.args.mode is not None:
            self.send("MODE %s" % self.args.mode)
        t_end = time.time() + self.args.seconds if self.args.seconds else None
        while not self.stop:
            if t_end and time.time() >= t_end:
                break
            line = self.ser.readline()
            if not line:
                if getattr(self.ser, "eof", False):
                    break
                continue
            self.handle(line)
        self.out("-- fin : %d evenement(s), %d ligne(s) invalide(s) ignoree(s)" % (self.n_events, self.invalid))


def keyboard_thread(listener: Listener):
    """Permet de taper des commandes (HELP, MODE 0, CAL 230...) pendant l'écoute. 'q' pour quitter."""
    def loop():
        for line in sys.stdin:
            line = line.strip()
            if line.lower() in ("q", "quit", "exit"):
                listener.stop = True
                return
            if line:
                listener.send(line)
    threading.Thread(target=loop, daemon=True).start()


def make_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--mode", help="envoie MODE <n> au démarrage (0 à 6 ou le nom)")
    ap.add_argument("--cmd", action="append", help="commande envoyée au démarrage (répétable), ex. --cmd 'SET PWR 1'")
    ap.add_argument("--seconds", type=float, help="s'arrête après n secondes")
    ap.add_argument("--csv", help="enregistre échantillons / puissances / événements dans ce fichier")
    ap.add_argument("--interactive", action="store_true", help="permet de taper des commandes au clavier")
    ap.add_argument("--vgain", type=float, default=0.30, help="V par mV, pour afficher des volts dans les résumés")
    ap.add_argument("--igain", type=float, default=0.1, help="A par mV, pour afficher des ampères dans les résumés")
    ap.add_argument("-v", "--verbose", action="store_true", help="affiche aussi les lignes ignorées et chaque puissance")
    return ap


def main(argv=None, ser=None):
    args = make_parser().parse_args(argv)
    if ser is None:
        import serial  # pip install pyserial
        ser = serial.Serial(args.port, args.baud, timeout=1)
    lis = Listener(ser, args)
    if args.csv:
        lis.open_csv(args.csv)
    if args.interactive:
        keyboard_thread(lis)
    try:
        lis.run()
    except KeyboardInterrupt:
        pass
    finally:
        if lis.csv_file:
            lis.csv_file.close()
        ser.close()
    return lis


if __name__ == "__main__":
    main()
