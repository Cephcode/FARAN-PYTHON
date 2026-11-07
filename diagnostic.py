"""Diagnostic de la chaîne de mesure : d'où viennent les puissances incohérentes et les rafales d'événements ?

    python main.py diagnostic            tous les examens, l'un après l'autre (~1 min 30), puis un rapport
    python main.py diagnostic --a-vide   idem, en déclarant qu'AUCUN appareil ne consomme (plus sévère)
    python main.py tension               seulement la tension, période par période, comparée au normal
    python main.py courant [--charge W]  seulement l'intensité, comparée au normal (à vide, ou à W watts)
    (ajouter --adc à tension/courant : valeurs vues par l'ADC, centrage, écrêtage, au lieu des périodes)

Les examens :
  1. liaison et version du firmware
  2. bus I2C, ADS1115, cadence de chaque voie, fil RDY            (MODE 0 de la carte)
  3. tension et courant au niveau de l'ADC : centrage, amplitude, écrêtage, fréquence   (MODE 2)
  4. puissance de CHAQUE période (20 ms) avec la lecture protégée (VERIFY 1) : pics, bruit, rafales
  5. la même chose avec l'ancienne lecture (VERIFY 0) : si les pics n'apparaissent qu'ici,
     la cause est prouvée (mesures rangées dans la mauvaise voie à cause de parasites)
Le rapport est affiché et écrit dans diagnostic_<date>.txt. Les causes possibles sont détaillées
dans DEPANNAGE_RAFALES.md (à la racine du projet).
"""
from __future__ import annotations

import statistics
import time
from datetime import datetime

import adc
import faran_link as link
from constants import (COURANT_A_VIDE_MAX, FREQ_MAX, FREQ_MIN, SAUT_TENSION_MAX, SEUIL_PIC_VA,
                       TENSION_NOMINALE, TOLERANCE_COURANT, TOLERANCE_TENSION)

V_MIN = TENSION_NOMINALE * (1 - TOLERANCE_TENSION)
V_MAX = TENSION_NOMINALE * (1 + TOLERANCE_TENSION)
DUREE_MIN_MS, DUREE_MAX_MS = 1000.0 / FREQ_MAX, 1000.0 / FREQ_MIN
OK, ATTENTION, PROBLEME = "OK", "ATTENTION", "PROBLEME"


# ====================================================================== collecte
def collecter(board, secondes, stop_si=None):
    """Lit tout ce que la carte envoie pendant `secondes` (ou jusqu'à stop_si(msg)). Messages décodés."""
    msgs = []
    fin = time.time() + secondes
    while time.time() < fin:
        msg = board.lire_message()
        if msg is None:
            if getattr(board.serial, "eof", False):
                break
            continue
        msgs.append(msg)
        if stop_si and stop_si(msg):
            break
    return msgs


def trier(msgs):
    """Range les messages par type, déjà décodés."""
    return {
        "pwr": [d for d in (link.parse_pwr(m) for m in msgs if m.kind == "PWR") if d],
        "stat": [d for d in (link.parse_stat(m) for m in msgs if m.kind == "STAT") if d],
        "evt": [d for d in (link.parse_event(m) for m in msgs if m.kind == "EVT") if d],
        "vstat": [d for d in (link.parse_stats(m) for m in msgs if m.kind == "VSTAT") if d],
        "istat": [d for d in (link.parse_stats(m) for m in msgs if m.kind == "ISTAT") if d],
        "info": [m.fields[0] if m.fields else "" for m in msgs if m.kind in ("INFO", "HELLO")],
        "err": [m.fields[0] if m.fields else "" for m in msgs if m.kind == "ERR"],
    }


# ====================================================================== analyse (fonctions pures, testées)
def mediane_locale(x, k, demi=5):
    """Médiane des voisins de x[k] (k exclu) : la référence pour dire si x[k] est un pic."""
    voisins = x[max(0, k - demi):k] + x[k + 1:k + 1 + demi]
    return statistics.median(voisins) if voisins else x[k]


def trouver_pics(s, seuil=SEUIL_PIC_VA):
    """Indices des périodes dont la puissance s'écarte de ses voisines de plus de max(seuil, 20 %)."""
    pics = []
    for k in range(len(s)):
        ref = mediane_locale(s, k)
        if abs(s[k] - ref) > max(seuil, 0.2 * abs(ref)):
            pics.append(k)
    return pics


def grouper(indices, ecart=25):
    """Regroupe des indices proches (25 périodes = 0,5 s) : chaque groupe est une « rafale »."""
    groupes = []
    for i in indices:
        if groupes and i - groupes[-1][-1] <= ecart:
            groupes[-1].append(i)
        else:
            groupes.append([i])
    return groupes


def rafales_evenements(evts, ecart_periodes=100):
    """Événements arrivés à moins de 2 s (100 périodes) les uns des autres."""
    if not evts:
        return []
    return [g for g in grouper([e["confirmed"] for e in evts], ecart_periodes) if len(g) >= 2]


def somme(stats, cle):
    vals = [s[cle] for s in stats if s.get(cle) is not None]
    return sum(vals) if vals else None


def analyser_puissance(d, a_vide=False):
    """Analyse les lignes PWR / STAT / EVT d'une séance MODE 4. Renvoie (resume, constats).
    constats = [(niveau, titre, detail)]."""
    pwr, stats, evts = d["pwr"], d["stat"], d["evt"]
    constats = []
    if len(pwr) < 50:
        constats.append((PROBLEME, "pas assez de périodes mesurées (%d)" % len(pwr),
                         "la tension n'est pas vue (ZMPT101B, A0) ou le firmware n'envoie pas les lignes PWR"))
        return {"periodes": len(pwr)}, constats

    s = [p["s_va"] for p in pwr]            # période seule : c'est là qu'un pic de mesure se voit
    s_det = [p["s_detecteur"] for p in pwr]  # moyenne sur N périodes : ce que reçoit le détecteur
    v = [p["vrms"] for p in pwr]
    i = [p["irms"] for p in pwr]
    dur = [p["duration_ms"] for p in pwr]
    pics = trouver_pics(s)
    rafales = grouper(pics)
    hors_pics = set(pics)
    # Bruit : sur ce que voit le détecteur (une télé fait varier S_va de ±35 % d'une période à l'autre,
    # mais la moyenne sur N périodes, elle, reste stable).
    residus = [s_det[k] - mediane_locale(s_det, k) for k in range(len(s_det)) if k not in hors_pics]
    bruit = 1.4826 * statistics.median([abs(r) for r in residus]) if residus else 0.0
    # Puissance active (firmware >= 0.8) : P médiane, facteur de puissance, sens de la pince
    p_act = [p["p_detecteur"] for p in pwr if p.get("p_detecteur") is not None]
    bruits = [st["bruit_w"] for st in stats if st.get("bruit_w")]
    r = {
        "p_mediane": statistics.median(p_act) if p_act else None,
        "pf_mediane": statistics.median([p["pf"] for p in pwr if p.get("pf") is not None]) if p_act else None,
        "bruit_detecteur": statistics.median(bruits) if bruits else None,
        "periodes": len(pwr), "s_mediane": statistics.median(s), "s_min": min(s), "s_max": max(s),
        "n_moyenne": pwr[-1]["n_moyenne"],
        "bruit_va": bruit, "pics": len(pics), "rafales_pics": len(rafales),
        "pic_max": max((abs(s[k] - mediane_locale(s, k)) for k in pics), default=0.0),
        "v_mediane": statistics.median(v), "v_min": min(v), "v_max": max(v),
        "i_mediane": statistics.median(i), "i_max": max(i),
        "duree_min": min(dur), "duree_max": max(dur),
        "evenements": len(evts), "rafales_evt": rafales_evenements(evts),
        "rdy_parasites": somme(stats, "spurious_rdy"), "jetees": somme(stats, "desync"),
        "i2c": somme(stats, "i2c_errors"), "sans_rdy": somme(stats, "rdy_timeouts"),
        "reinit_i2c": max((st["i2c_recoveries"] or 0) for st in stats) if stats else None,
        "ecretage_v": somme(stats, "clip_v"), "ecretage_i": somme(stats, "clip_i"),
        "rejetees": max((st["bad_periods"] for st in stats), default=0),
        "sps_v": statistics.median([st["sps_v"] for st in stats]) if stats else None,
        "sps_i": statistics.median([st["sps_i"] for st in stats]) if stats else None,
    }

    # --- tension
    if not V_MIN <= r["v_mediane"] <= V_MAX:
        constats.append((ATTENTION, "tension mesurée %.0f V (normal : %.0f à %.0f V)" % (r["v_mediane"], V_MIN, V_MAX),
                         "étalonnage : python listener.py --mode 2 --interactive, puis CAL <tension du multimètre>, SAVE"))
    sauts_v = sum(1 for a, b in zip(v, v[1:]) if a > 0 and abs(b - a) / a > SAUT_TENSION_MAX)
    if sauts_v > len(v) * 0.01:
        constats.append((PROBLEME, "tension instable : %d sauts de plus de %.0f %% d'une période à l'autre"
                         % (sauts_v, 100 * SAUT_TENSION_MAX),
                         "le secteur ne bouge pas comme ça : mesures de courant mélangées à la tension, ZMPT101B "
                         "mal alimenté, ou fil A0 qui capte des parasites"))
    if r["duree_min"] < DUREE_MIN_MS or r["duree_max"] > DUREE_MAX_MS:
        constats.append((ATTENTION, "durée de période de %.2f à %.2f ms (normal %.1f à %.1f)"
                         % (r["duree_min"], r["duree_max"], DUREE_MIN_MS, DUREE_MAX_MS),
                         "passages par zéro de la tension mal détectés (signal bruité ou trop faible)"))

    # --- courant / puissance
    if a_vide and r["i_mediane"] > COURANT_A_VIDE_MAX:
        constats.append((ATTENTION, "à vide, le courant vaut %.3f A (%.0f VA) au lieu de < %.2f A"
                         % (r["i_mediane"], r["s_mediane"], COURANT_A_VIDE_MAX),
                         "soit un appareil consomme vraiment (veille, frigo, box...), soit les fils de la pince "
                         "captent du 50 Hz : torsader les deux fils, les raccourcir, éloigner des câbles secteur"))
    if pics:
        niveau = PROBLEME if r["pic_max"] > 200 or len(pics) > 5 else ATTENTION
        constats.append((niveau, "%d période(s) aberrante(s) en %d rafale(s), écart maximal %.0f VA"
                         % (len(pics), len(rafales), r["pic_max"]),
                         "pics de plus de ~1000 VA sans appareil : signature d'une mesure de TENSION rangée comme "
                         "COURANT (voir examen 5 et les compteurs ci-dessous)"))
    if r["p_mediane"] is not None and r["p_mediane"] < -max(5.0, 0.2 * r["s_mediane"]):
        constats.append((ATTENTION, "puissance active négative (%.1f W) : la pince est posée à l'envers" % r["p_mediane"],
                         "sans gravité (le détecteur prend |P|), mais retourner la pince donne des lignes PWR plus lisibles"))
    if bruit > 10:
        constats.append((ATTENTION, "bruit de puissance %.1f VA (normal : < 10 VA)" % bruit,
                         "fils de la pince trop longs / non torsadés, alimentation 3,3 V bruitée, masse commune mal faite"))

    # --- compteurs du firmware
    if r["rdy_parasites"] is None:
        constats.append((ATTENTION, "firmware ancien (0.4) : pas de compteurs de parasites",
                         "flasher le firmware 0.5 (lecture protégée de l'ADC)"))
    else:
        if r["rdy_parasites"]:
            constats.append((PROBLEME, "%d fronts parasites sur le fil ALERT/RDY (GPIO4)" % r["rdy_parasites"],
                             "fil RDY trop long ou près du 220 V : le raccourcir, ajouter un pull-up 10 kOhms vers 3,3 V"))
        if r["jetees"]:
            constats.append((ATTENTION, "%d mesures jetées (voie tension/courant incertaine)" % r["jetees"],
                             "parasites sur le bus I2C : fils SDA/SCL courts, pull-ups 4,7 kOhms, essayer I2C à 100 kHz"))
        if r["i2c"]:
            constats.append((ATTENTION, "%d erreurs I2C, %s réinitialisation(s) du bus" % (r["i2c"], r["reinit_i2c"]),
                             "masse commune ESP32/ADS1115/capteurs, condensateur 100 nF sur VDD de l'ADS1115, fils courts"))
        if r["ecretage_v"]:
            constats.append((PROBLEME, "%d mesures de tension écrêtées (A0 collée à 0 ou 3,3 V)" % r["ecretage_v"],
                             "baisser le potentiomètre du ZMPT101B (crête-à-crête < 2500 mV)"))
        if r["ecretage_i"]:
            constats.append((PROBLEME, "%d mesures de courant écrêtées (A1)" % r["ecretage_i"],
                             "centrage 1,65 V de la pince absent, ou résistance de charge 20 Ohms débranchée"))
    if r["sans_rdy"]:
        constats.append((ATTENTION, "%d conversions sans impulsion RDY" % r["sans_rdy"], "fil ALERT/RDY -> GPIO4 à vérifier"))
    if r["sps_v"] is not None and min(r["sps_v"], r["sps_i"]) < 300:
        constats.append((ATTENTION, "cadence %.0f / %.0f points/s par voie (normal ~360 à 430)" % (r["sps_v"], r["sps_i"]),
                         "bus I2C lent ou en erreur, fil RDY absent"))

    # --- événements
    if r["rafales_evt"]:
        constats.append((PROBLEME if a_vide else ATTENTION,
                         "%d rafale(s) d'événements (plusieurs événements à moins de 2 s)" % len(r["rafales_evt"]),
                         "si des pics sont signalés au même moment : ce sont eux. Sinon : transitoire réel de "
                         "l'appareil (moteur, compresseur) découpé en plusieurs marches"))
    elif a_vide and evts:
        constats.append((PROBLEME, "%d événement(s) détecté(s) alors que rien ne change" % len(evts),
                         "faux événements : voir les pics et les compteurs ci-dessus"))
    return r, constats


def analyser_adc(d):
    """Examen 3 : lignes VSTAT / ISTAT (MODE 2). Renvoie des constats."""
    constats = []
    # Ce qui est normal dépend du câblage de chaque voie (A0 simple, A1-A3 différentiel) : voir adc.py.
    for kind, lignes in (("VSTAT", d["vstat"]), ("ISTAT", d["istat"])):
        v = adc.voie(kind)
        nom = v.libelle()
        if not lignes:
            constats.append((PROBLEME, "%s : aucune mesure reçue" % nom, "voie absente : MODE 0, câblage de %s" % v.entree))
            continue
        x = lignes[-1]
        bas, haut = x["offset_mv"] - x["vpp_mv"] / 2, x["offset_mv"] + x["vpp_mv"] / 2
        if not v.centrage_ok(x["offset_mv"]):
            constats.append((PROBLEME, "%s : centrage %.1f mV (normal ~%.0f)" % (nom, x["offset_mv"], v.centre_mv),
                             v.cause_centrage()))
        if v.ecrete(bas, haut):
            b, h = v.bornes_mv()
            constats.append((PROBLEME, "%s : signal écrêté (%.0f à %.0f mV, limites %.0f / %.0f)" % (nom, bas, haut, b, h),
                             "amplitude trop grande pour le calibre de la voie" if v.differentiel
                             else "amplitude trop grande pour 0-3,3 V"))
        if kind == "VSTAT":
            if x["vpp_mv"] < 300:
                constats.append((PROBLEME, "tension : presque pas de signal (%.0f mV crête-à-crête)" % x["vpp_mv"],
                                 "ZMPT101B non alimenté, potentiomètre au minimum, fil A0"))
            if not FREQ_MIN <= x["f_hz"] <= FREQ_MAX:
                constats.append((PROBLEME, "tension : fréquence %.2f Hz (normal 49 à 51)" % x["f_hz"],
                                 "signal trop bruité pour trouver les passages par zéro"))
            if not V_MIN <= x["value"] <= V_MAX:
                constats.append((ATTENTION, "tension : %.0f V (normal %.0f à %.0f V)" % (x["value"], V_MIN, V_MAX),
                                 "étalonner : CAL <volts du multimètre> puis SAVE"))
        vals = [l["vrms_mv"] for l in lignes]
        if len(vals) >= 3 and statistics.mean(vals) > 0 and \
                (max(vals) - min(vals)) / statistics.mean(vals) > (0.05 if kind == "VSTAT" else 0.5):
            constats.append((ATTENTION, "%s : valeur efficace instable d'une seconde à l'autre (%.1f à %.1f mV)"
                             % (nom, min(vals), max(vals)), "parasites, ou appareil qui varie pendant l'examen"))
    return constats


def comparer_lectures(protegee, ancienne):
    """Examen 5 : même chose avec VERIFY 1 et VERIFY 0. Renvoie un constat (ou None)."""
    if not protegee or not ancienne or "pics" not in protegee or "pics" not in ancienne:
        return None
    p1, p0 = protegee["pics"], ancienne["pics"]
    e1, e0 = protegee["evenements"], ancienne["evenements"]
    if p0 > p1 + 2 or e0 > e1 + 1:
        return (PROBLEME, "CAUSE PROUVÉE : l'ancienne lecture donne %d pics / %d événements, la lecture protégée %d / %d"
                % (p0, e0, p1, e1),
                "des mesures étaient rangées dans la mauvaise voie (parasites RDY / I2C). Le firmware 0.5 le corrige ; "
                "réduire aussi les parasites à la source (DEPANNAGE_RAFALES.md, §A)")
    if p1 > 2:
        return (ATTENTION, "les pics existent avec les deux lectures (%d et %d)" % (p1, p0),
                "ils ne viennent PAS d'une confusion de voie : le signal lui-même est perturbé (câblage analogique, "
                "alimentation, vrai transitoire). Voir DEPANNAGE_RAFALES.md, §B et §C")
    return (OK, "aucune différence entre les deux lectures pendant l'examen", "")


# ====================================================================== examen complet
def _etape(titre, out):
    out("\n=== %s" % titre)


def examen_complet(board, duree=30.0, a_vide=False, comparer=True, out=print, fichier=None):
    rapport = []

    def dire(txt=""):
        out(txt)
        rapport.append(txt)

    def constats_vers_rapport(constats):
        for niveau, titre, detail in constats:
            dire("  [%-9s] %s" % (niveau, titre))
            if detail:
                dire("              -> %s" % detail)
        if not constats:
            dire("  [%-9s] rien d'anormal" % OK)

    tous = []
    dire("DIAGNOSTIC FARAN  %s  (%s)" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                          "à vide déclaré" if a_vide else "charges inconnues"))
    if a_vide:
        dire("Ne rien allumer ni éteindre pendant l'examen.")
    else:
        dire("Conseil : relancer avec --a-vide après avoir tout débranché, pour l'examen le plus sévère.")

    # 1. liaison
    _etape("1. liaison et version", dire)
    board.envoyer("MODE 5")
    board.envoyer("STATUS")
    d = trier(collecter(board, 3, stop_si=lambda m: m.kind == "INFO" and "mode=" in m.body))
    status = next((t for t in d["info"] if "mode=" in t), None)
    if status is None:
        c = [(PROBLEME, "la carte ne répond pas", "port, câble USB, moniteur PlatformIO encore ouvert, baud 460800")]
    elif "verify=" not in status:
        c = [(ATTENTION, "firmware ancien : " + status, "flasher le firmware 0.5 pour la lecture protégée et les compteurs")]
    else:
        c = []
        dire("  " + status)
    constats_vers_rapport(c)
    tous += c

    # 2. bus et ADC
    _etape("2. bus I2C, ADS1115, fil RDY (MODE 0)", dire)
    board.envoyer("MODE 0")
    d = trier(collecter(board, 6, stop_si=lambda m: m.kind == "INFO" and "scan termine" in m.body))
    for t in d["info"]:
        if "scan termine" not in t:
            dire("  " + t)
    c = [(PROBLEME, e, "") for e in d["err"]]
    constats_vers_rapport(c)
    tous += c

    # 3. ADC
    _etape("3. tension et courant vus par l'ADC (MODE 2, 8 s)", dire)
    board.envoyer("MODE 2")
    d = trier(collecter(board, 8))
    for x in d["vstat"][-1:] + d["istat"][-1:]:
        dire("  %s : centrage %.0f mV, crête-à-crête %.0f mV, efficace %.1f mV = %.3f %s, %.2f Hz" % (
            "tension" if x["kind"] == "VSTAT" else "courant", x["offset_mv"], x["vpp_mv"], x["vrms_mv"], x["value"],
            "V" if x["kind"] == "VSTAT" else "A", x["f_hz"]))
    c = analyser_adc(d)
    constats_vers_rapport(c)
    tous += c

    # 4. et 5. puissance période par période
    resultats = {}
    lectures = [("4. puissance de chaque période, lecture PROTÉGÉE (VERIFY 1)", 1)]
    if comparer:
        lectures.append(("5. même chose avec l'ANCIENNE lecture (VERIFY 0), pour comparer", 0))
    for titre, verify in lectures:
        _etape("%s, %.0f s" % (titre, duree), dire)
        board.envoyer("MODE 5")
        board.envoyer("SET VERIFY %d" % verify)
        board.envoyer("SET PWR 1")
        board.envoyer("MODE 4")
        d = trier(collecter(board, duree + 3))   # ~3 s de mise en route (centrage)
        r, c = analyser_puissance(d, a_vide)
        resultats[verify] = r
        if "s_mediane" in r:
            dire("  %d périodes : S médiane %.1f VA (de %.1f à %.1f), bruit %.1f VA ; V %.1f V (%.1f à %.1f) ; "
                 "I %.3f A (max %.3f)" % (r["periodes"], r["s_mediane"], r["s_min"], r["s_max"], r["bruit_va"],
                                          r["v_mediane"], r["v_min"], r["v_max"], r["i_mediane"], r["i_max"]))
            if r.get("p_mediane") is not None:
                dire("  puissance active P médiane %.1f W, facteur de puissance %.2f%s" % (
                    r["p_mediane"], r["pf_mediane"], "" if r["bruit_detecteur"] is None else
                    " ; bruit mesuré par le détecteur amélioré %.2f W" % r["bruit_detecteur"]))
            dire("  événements %d ; pics %d ; fronts RDY parasites %s ; mesures jetées %s ; erreurs I2C %s ; "
                 "écrêtage V/I %s/%s" % (r["evenements"], r["pics"], r["rdy_parasites"], r["jetees"], r["i2c"],
                                          r["ecretage_v"], r["ecretage_i"]))
        constats_vers_rapport(c)
        tous += c

    board.envoyer("MODE 5")
    board.envoyer("SET PWR 0")
    board.envoyer("SET VERIFY 1")   # on remet toujours la lecture protégée

    if comparer:
        c = comparer_lectures(resultats.get(1), resultats.get(0))
        if c:
            _etape("comparaison des deux lectures", dire)
            constats_vers_rapport([c] if c[0] != OK else [])
            if c[0] != OK:
                tous.append(c)

    _etape("BILAN", dire)
    graves = [t for t in tous if t[0] == PROBLEME]
    moyens = [t for t in tous if t[0] == ATTENTION]
    dire("  %d problème(s), %d point(s) d'attention." % (len(graves), len(moyens)))
    for niveau, titre, _ in graves + moyens:
        dire("  - [%s] %s" % (niveau, titre))
    if not graves and not moyens:
        dire("  Chaîne de mesure saine pendant l'examen. Si les rafales reviennent, relancer l'examen PENDANT "
             "qu'elles se produisent (allumer/éteindre l'appareil suspect pendant l'examen 4).")
    dire("  Causes possibles et remèdes détaillés : DEPANNAGE_RAFALES.md")

    if fichier is None:
        fichier = "diagnostic_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S")
    if fichier:
        with open(fichier, "w", encoding="utf-8") as f:
            f.write("\n".join(rapport) + "\n")
        out("\nRapport écrit dans %s" % fichier)
    return tous, resultats


# ====================================================================== modes « une seule voie »
def _verdict(ok):
    return "normal " if ok else "ANORMAL"


def suivre_voie(board, voie, charge_w=None, adc=False, out=print, secondes=None):
    """voie = 'tension' ou 'courant'. Affiche, chaque seconde, les valeurs de CETTE voie seulement,
    comparées aux valeurs normales ; chaque période anormale est signalée tout de suite (!!)."""
    if adc:
        return _suivre_voie_adc(board, voie, out, secondes)
    i_attendu = charge_w / TENSION_NOMINALE if charge_w else None
    if voie == "tension":
        out("TENSION seulement. Normal : %.0f à %.0f V, période %.1f à %.1f ms, < %.0f %% de saut entre périodes."
            % (V_MIN, V_MAX, DUREE_MIN_MS, DUREE_MAX_MS, 100 * SAUT_TENSION_MAX))
    elif i_attendu:
        out("COURANT seulement. Normal pour %.0f W : %.3f A +-%.0f %%." % (charge_w, i_attendu, 100 * TOLERANCE_COURANT))
    else:
        out("COURANT seulement, À VIDE. Normal : < %.2f A (soit < %.0f VA)."
            % (COURANT_A_VIDE_MAX, COURANT_A_VIDE_MAX * TENSION_NOMINALE))
    if voie == "courant":
        out("(courant vu par le détecteur : moyenne des N dernières périodes, SET MOYENNE ; firmware >= 0.7)")
    out("(Ctrl+C pour arrêter. ~3 s de mise en route.)")
    board.envoyer("SET VERIFY 1")
    board.envoyer("SET PWR 1")
    board.envoyer("MODE 4")

    def normal(p, prec):
        if voie == "tension":
            ok = V_MIN <= p["vrms"] <= V_MAX and DUREE_MIN_MS <= p["duration_ms"] <= DUREE_MAX_MS
            if prec and prec["vrms"] > 0:
                ok = ok and abs(p["vrms"] - prec["vrms"]) / prec["vrms"] <= SAUT_TENSION_MAX
            return ok
        if i_attendu:
            return abs(p["i_detecteur"] - i_attendu) <= TOLERANCE_COURANT * i_attendu + 0.02
        return p["i_detecteur"] <= COURANT_A_VIDE_MAX

    paquet, prec, anormales, total = [], None, 0, 0
    fin = time.time() + secondes if secondes else None
    while fin is None or time.time() < fin:
        msg = board.lire_message()
        if msg is None:
            if getattr(board.serial, "eof", False):
                break
            continue
        heure = datetime.now().strftime("%H:%M:%S")
        if msg.kind == "PWR":
            p = link.parse_pwr(msg)
            if not p:
                continue
            total += 1
            if not normal(p, prec):
                anormales += 1
                if voie == "tension":
                    out("%s  !! période %d : %.1f V, %.2f ms   <- ANORMAL" % (heure, p["period"], p["vrms"], p["duration_ms"]))
                else:
                    out("%s  !! période %d : %.3f A (%.0f VA)   <- ANORMAL" % (heure, p["period"], p["i_detecteur"],
                                                                         p["s_detecteur"]))
            paquet.append(p)
            prec = p
        elif msg.kind == "STAT" and paquet:
            st = link.parse_stat(msg)
            if voie == "tension":
                x = [p["vrms"] for p in paquet]
                ok = all(V_MIN <= a <= V_MAX for a in x) and max(x) - min(x) <= SAUT_TENSION_MAX * statistics.mean(x)
                out("%s  tension %.1f V (de %.1f à %.1f), %.2f Hz, %d périodes   [%s]" % (
                    heure, statistics.mean(x), min(x), max(x), 1000.0 / statistics.mean(p["duration_ms"] for p in paquet),
                    len(x), _verdict(ok)))
            else:
                x = [p["i_detecteur"] for p in paquet]
                ok = all(normal(p, None) for p in paquet)
                out("%s  courant %.3f A (de %.3f à %.3f), soit %.0f VA, %d périodes, moyenne sur %d   [%s]" % (
                    heure, statistics.mean(x), min(x), max(x), statistics.mean(p["s_detecteur"] for p in paquet), len(x),
                    paquet[-1]["n_moyenne"], _verdict(ok)))
            if st and (st.get("spurious_rdy") or st.get("desync") or st["i2c_errors"]):
                out("          (carte : %s fronts RDY parasites, %s mesures jetées, %d erreurs I2C cette seconde)"
                    % (st.get("spurious_rdy"), st.get("desync"), st["i2c_errors"]))
            paquet = []
        elif msg.kind in ("ERR", "INFO", "MODE"):
            out("   [carte] " + msg.body.replace(",", " ", 1))
    out("-- %d période(s), dont %d anormale(s)" % (total, anormales))
    board.envoyer("SET PWR 0")
    return total, anormales


def _suivre_voie_adc(board, voie, out, secondes):
    """Variante --adc : valeurs brutes de l'ADC (MODE 2) pour une seule voie, comparées au normal."""
    kind = "VSTAT" if voie == "tension" else "ISTAT"
    out("%s vue par l'ADC (MODE 2). Normal : %s%s."
        % (adc.voie(kind).libelle().upper(), adc.voie(kind).normal(),
           ", 300 à 2500 mV crête-à-crête, 49 à 51 Hz" if voie == "tension" else ""))
    board.envoyer("MODE 2")
    fin = time.time() + secondes if secondes else None
    n = 0
    while fin is None or time.time() < fin:
        msg = board.lire_message()
        if msg is None:
            if getattr(board.serial, "eof", False):
                break
            continue
        if msg.kind != kind:
            continue
        x = link.parse_stats(msg)
        if not x:
            continue
        n += 1
        pbs = [t for _, t, _ in analyser_adc({"vstat": [x] if kind == "VSTAT" else [],
                                              "istat": [x] if kind == "ISTAT" else []})
               if t.startswith("tension" if kind == "VSTAT" else "courant")]
        out("%s  centrage %.1f mV, crête-à-crête %.1f mV, efficace %.3f mV = %.3f %s, %.2f Hz   [%s]%s" % (
            datetime.now().strftime("%H:%M:%S"), x["offset_mv"], x["vpp_mv"], x["vrms_mv"], x["value"],
            "V" if kind == "VSTAT" else "A", x["f_hz"], _verdict(not pbs), ("  " + " ; ".join(pbs)) if pbs else ""))
    return n
