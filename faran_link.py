"""Liaison ESP32 <-> PC : décodage du protocole série (voir firmware/src/protocol.h).

Une ligne = `CORPS*HH` où HH est le XOR hexadécimal des octets du CORPS. Toute ligne tronquée,
bruitée ou sans checksum (messages de démarrage de la carte, par exemple) est rejetée.

La ligne ADC (description des deux voies : entrées, calibres) est appliquée AUTOMATIQUEMENT au
passage (voir adc.py) : les codes RAW sont ensuite convertis avec le bon calibre de chaque voie.
"""
from __future__ import annotations

from dataclasses import dataclass

import adc


def checksum(body: str) -> int:
    x = 0
    for b in body.encode("ascii", errors="replace"):
        x ^= b
    return x


def encode(body: str) -> str:
    """Fabrique une ligne valide (tests, ou commande à envoyer)."""
    return f"{body}*{checksum(body):02X}\n"


@dataclass
class Message:
    kind: str
    fields: list
    body: str


def parse_line(line) -> "Message | None":
    """Renvoie le message décodé, ou None si la ligne est invalide."""
    if isinstance(line, bytes):
        line = line.decode("ascii", errors="replace")
    line = line.strip()
    if "*" not in line:
        return None
    body, _, cs = line.rpartition("*")
    try:
        if int(cs, 16) != checksum(body):
            return None
    except ValueError:
        return None
    fields = body.split(",")
    msg = Message(kind=fields[0], fields=fields[1:], body=body)
    if msg.kind == "ADC":
        d = parse_adc(msg)
        if d:
            adc.appliquer(d)   # la carte dit comment elle mesure : on s'y aligne
    return msg


# ------------------------------------------------------------------ description des voies (ADC)
_ADC_KEYS = ["differentiel", "lsb_v_mv", "lsb_i_mv", "pleine_echelle_v_mv", "pleine_echelle_i_mv", "entree_v", "entree_i"]


def parse_adc(msg: Message) -> "dict | None":
    """ADC,<diff 0|1>,<lsb_v_mv>,<lsb_i_mv>,<pe_v_mv>,<pe_i_mv>,<entree_v>,<entree_i> -> dictionnaire."""
    if msg.kind != "ADC" or len(msg.fields) != len(_ADC_KEYS):
        return None
    try:
        d = dict(zip(_ADC_KEYS, msg.fields))
        d["differentiel"] = d["differentiel"] == "1"
        for k in ("lsb_v_mv", "lsb_i_mv", "pleine_echelle_v_mv", "pleine_echelle_i_mv"):
            d[k] = float(d[k])
        if d["lsb_v_mv"] <= 0 or d["lsb_i_mv"] <= 0:
            return None
        return d
    except ValueError:
        return None


# ------------------------------------------------------------------ flux brut (RAW)
# 1 code de l'ADS1115 = adc.lsb_mv(voie) : 0,125 mV pour la tension, 0,015625 mV pour le courant différentiel.


@dataclass
class RawBlock:
    channel: str          # "V" ou "I"
    t_first_us: int       # horloge de la carte (micros), 32 bits
    t_last_us: int
    codes: list

    def samples(self):
        """[(t_us, millivolts)] : les instants intermédiaires sont répartis régulièrement entre le premier
        et le dernier échantillon (l'écart est calculé modulo 2**32 : insensible au débordement de micros()).
        Les codes sont convertis avec le calibre de la voie (adc.py)."""
        n = len(self.codes)
        lsb = adc.lsb_mv(self.channel)
        span = (self.t_last_us - self.t_first_us) % 2**32
        out = []
        for k, c in enumerate(self.codes):
            t = self.t_first_us + (span * k / (n - 1) if n > 1 else 0)
            out.append((t, c * lsb))
        return out


def parse_raw(msg: Message) -> "RawBlock | None":
    if msg.kind != "RAW" or len(msg.fields) < 4:
        return None
    try:
        ch, t0, t1, n = msg.fields[:4]
        codes = [int(x) for x in msg.fields[4:]]
        if ch not in ("V", "I") or int(n) != len(codes) or not codes:
            return None
        return RawBlock(ch, int(t0), int(t1), codes)
    except ValueError:
        return None


# ------------------------------------------------------------------ événements
# Champs ajoutés par le firmware >= 0.8 (fin de la ligne EVT) : nouvelles caractéristiques pour le classifieur.
EVT_EXTRA = ["delta_s", "delta_pf", "n_paliers", "duree_chaine_ms", "marche_max", "puissance", "algo"]


def parse_event(msg: Message) -> "dict | None":
    """EVT -> le même dictionnaire que ConfirmedEvent.dict() de votre event_detector.py
    (clés : type, delta, s_before, s_after, start, transient_duration, confirmed, confirmed_by,
    is_pair_net_event), plus quelques champs en plus : seq, end_of_transient, board_ms.
    On peut donc le donner tel quel à EventClassifier.identify().
    Firmware >= 0.8 : aussi delta_s (VA), delta_pf, n_paliers, duree_chaine_ms, marche_max (W),
    puissance ("P" ou "S") et algo ("ref" ou "ameliore"). Ancien firmware (12 champs) : ces clés valent
    None (puissance "S", algo "ref" : c'était l'ancien fonctionnement)."""
    if msg.kind != "EVT" or len(msg.fields) not in (12, 12 + len(EVT_EXTRA)):
        return None
    try:
        seq, typ, delta, sb, sa, start, end_tr, conf, dur, by, pair, t_ms = msg.fields[:12]
        if typ not in ("ON", "OFF") or by not in ("e_power", "d_min"):
            return None
        d = {
            "type": typ,
            "delta": float(delta),
            "s_before": float(sb),
            "s_after": float(sa),
            "start": int(start),
            "transient_duration": int(dur),
            "confirmed": int(conf),
            "confirmed_by": by,
            "is_pair_net_event": pair == "1",
            "seq": int(seq),
            "end_of_transient": int(end_tr),
            "board_ms": int(t_ms),
            "delta_s": None, "delta_pf": None, "n_paliers": None, "duree_chaine_ms": None, "marche_max": None,
            "puissance": "S", "algo": "ref",
        }
        if len(msg.fields) > 12:
            ds, dpf, n, dc, mm, puis, algo = msg.fields[12:]
            if puis not in ("P", "S") or algo not in ("ref", "ameliore"):
                return None
            d.update(delta_s=float(ds), delta_pf=float(dpf), n_paliers=int(n), duree_chaine_ms=int(dc),
                     marche_max=float(mm), puissance=puis, algo=algo)   # float("nan") : delta inconnu
        return d
    except ValueError:
        return None


# ------------------------------------------------------------------ autres messages
_STAT_KEYS = ["t_ms", "periods", "events", "discarded", "bad_periods", "sps_v", "sps_i", "rdy_timeouts", "i2c_errors"]
# firmware >= 0.5 : compteurs de robustesse en plus (voir firmware/src/sampler.h)
_STAT_KEYS_V05 = _STAT_KEYS + ["spurious_rdy", "desync", "clip_v", "clip_i", "i2c_recoveries", "verify"]
# firmware >= 0.8 : bruit mesuré par le détecteur amélioré et son seuil effectif (W, nombres à virgule)
_STAT_KEYS_V08 = _STAT_KEYS_V05 + ["bruit_w", "seuil_w"]


def parse_stat(msg: Message) -> "dict | None":
    """STAT -> dictionnaire. Ancien firmware (9 ou 15 champs) : les compteurs absents valent None."""
    tailles = {len(k): k for k in (_STAT_KEYS, _STAT_KEYS_V05, _STAT_KEYS_V08)}
    if msg.kind != "STAT" or len(msg.fields) not in tailles:
        return None
    keys = tailles[len(msg.fields)]
    try:
        d = {k: (float(x) if k in ("bruit_w", "seuil_w") else int(x)) for k, x in zip(keys, msg.fields)}
    except ValueError:
        return None
    for k in _STAT_KEYS_V08[len(_STAT_KEYS):]:
        d.setdefault(k, None)
    return d


_PWR_KEYS = ["period", "s_va", "vrms", "irms", "duration_ms"]
# firmware >= 0.7 : ce que reçoit VRAIMENT le détecteur (moyenne des n_moyenne dernières périodes)
_PWR_KEYS_V07 = _PWR_KEYS + ["s_detecteur", "i_detecteur", "n_moyenne"]
# firmware >= 0.8 : puissance ACTIVE (W) et facteur de puissance de la période, P moyennée (détecteur)
_PWR_KEYS_V08 = _PWR_KEYS_V07 + ["p_w", "pf", "p_detecteur"]


def parse_pwr(msg: Message) -> "dict | None":
    """PWR -> dictionnaire.
    s_va, vrms, irms, duration_ms (et p_w, pf) : la période seule ;
    s_detecteur, i_detecteur (et p_detecteur) : la moyenne des n_moyenne dernières périodes : le détecteur
    reçoit |p_detecteur| (SET PUISSANCE P) ou s_detecteur (SET PUISSANCE S).
    Ancien firmware : 5 champs (le détecteur recevait la période seule : s_detecteur = s_va, n_moyenne = 1)
    ou 8 champs (pas de puissance active : p_w, pf, p_detecteur valent None)."""
    tailles = {len(k): k for k in (_PWR_KEYS, _PWR_KEYS_V07, _PWR_KEYS_V08)}
    if msg.kind != "PWR" or len(msg.fields) not in tailles:
        return None
    try:
        d = dict(zip(tailles[len(msg.fields)], (float(x) for x in msg.fields)))
    except ValueError:
        return None
    d.setdefault("s_detecteur", d["s_va"])
    d.setdefault("i_detecteur", d["irms"])
    d["n_moyenne"] = int(d.get("n_moyenne", 1))
    for k in ("p_w", "pf", "p_detecteur"):
        d.setdefault(k, None)
    return d


_STATS_KEYS = ["t_ms", "fs", "offset_mv", "vpp_mv", "vrms_mv", "value", "f_hz", "crossings", "dropped"]


def parse_stats(msg: Message) -> "dict | None":
    """VSTAT / ISTAT : 'value' = tension efficace (V) ou courant efficace (A) selon le message."""
    if msg.kind not in ("VSTAT", "ISTAT") or len(msg.fields) != len(_STATS_KEYS):
        return None
    try:
        d = dict(zip(_STATS_KEYS, (float(x) for x in msg.fields)))
        d["kind"] = msg.kind
        return d
    except ValueError:
        return None
