"""Ce que l'ADC voit sur chaque voie, et ce qui est « normal » (miroir de firmware/src/adc_inputs.h).

Deux voies, deux câblages :
  - tension « V » : A0 par rapport à GND, calibre +-4,096 V, signal centré à ~1650 mV (0 à 3300 mV) ;
  - courant « I » : A1 - A3 en DIFFÉRENTIEL, calibre +-0,512 V, signal centré à ~0 mV.

La carte annonce sa configuration (ligne ADC, au démarrage, à chaque MODE et avec STATUS).
faran_link.parse_line() la transmet ici automatiquement : tous les programmes (listener, diagnostic,
main) utilisent donc la configuration RÉELLE de la carte. Tant qu'aucune ligne ADC n'est arrivée,
on prend les valeurs de constants.py.

Tout ce qui juge un centrage, un écrêtage ou convertit un code en mV passe par ce module :
pour un autre câblage, il n'y a rien à changer ailleurs.
"""
from __future__ import annotations

from dataclasses import dataclass

import constants as C

ALIMENTATION_MV = 3300.0      # alimentation de l'ADS1115 (entrées simples : signal entre 0 et 3300 mV)
MARGE_ECRETAGE_MV = 30.0      # entrée simple : à moins de 30 mV de 0 ou de 3300 mV, le signal est écrêté
MARGE_ECRETAGE_DIFF = 0.02    # différentiel : au-delà de 98 % de la pleine échelle, le signal est écrêté
TOLERANCE_CENTRAGE_SIMPLE = 350.0   # mV autour de 1650 (1300 à 2000 mV)
TOLERANCE_CENTRAGE_DIFF = 5.0       # mV autour de 0 : en différentiel, le 1,65 V s'annule


@dataclass
class Voie:
    nom: str                # "tension" ou "courant"
    entree: str             # "A0", "A1-A3"...
    differentiel: bool
    lsb_mv: float           # mV par code de l'ADC
    pleine_echelle_mv: float

    @property
    def centre_mv(self) -> float:
        """Centre attendu du signal alternatif."""
        return 0.0 if self.differentiel else ALIMENTATION_MV / 2

    @property
    def tolerance_centrage_mv(self) -> float:
        return TOLERANCE_CENTRAGE_DIFF if self.differentiel else TOLERANCE_CENTRAGE_SIMPLE

    def centrage_ok(self, offset_mv: float) -> bool:
        return abs(offset_mv - self.centre_mv) <= self.tolerance_centrage_mv

    def bornes_mv(self):
        """(bas, haut) : le signal doit rester strictement entre les deux, sinon il est écrêté."""
        if self.differentiel:
            m = self.pleine_echelle_mv * (1 - MARGE_ECRETAGE_DIFF)
            return -m, m
        return MARGE_ECRETAGE_MV, ALIMENTATION_MV - MARGE_ECRETAGE_MV

    def ecrete(self, bas_mv: float, haut_mv: float) -> bool:
        b, h = self.bornes_mv()
        return bas_mv < b or haut_mv > h

    def libelle(self) -> str:
        return "%s (%s)" % (self.nom, self.entree)

    def normal(self) -> str:
        """Une phrase : ce que l'on doit voir sur cette voie."""
        b, h = self.bornes_mv()
        return "centrage ~%.0f mV (+-%.0f), signal entre %.0f et %.0f mV" % (
            self.centre_mv, self.tolerance_centrage_mv, b, h)

    def cause_centrage(self) -> str:
        """Ce qu'il faut vérifier quand le centrage est faux."""
        if self.nom == "tension":
            return "alimentation du ZMPT101B (3,3 V) / fil A0"
        if self.differentiel:
            return ("un fil de la pince ou un jumper manque : A1 doit aller au côté signal de la pince, A3 au milieu "
                    "du pont 1,65 V, et ~20 ohms entre les deux")
        return "pont de résistances 1,65 V / fil A1"


def _depuis_constants():
    diff = C.COURANT_DIFFERENTIEL
    return {
        "V": Voie("tension", "A0", False, C.LSB_TENSION_MV, C.PLEINE_ECHELLE_TENSION_MV),
        "I": Voie("courant", "A1-A3" if diff else "A1", diff, C.LSB_COURANT_MV, C.PLEINE_ECHELLE_COURANT_MV),
    }


VOIES = _depuis_constants()


def voie(ch: str) -> Voie:
    """ch : "V" / "VSTAT" (tension) ou "I" / "ISTAT" (courant)."""
    return VOIES["V" if ch.upper().startswith("V") else "I"]


def lsb_mv(ch: str) -> float:
    return voie(ch).lsb_mv


def appliquer(d: dict):
    """Adopte la configuration annoncée par la carte (dictionnaire de faran_link.parse_adc)."""
    VOIES["V"] = Voie("tension", d["entree_v"], False, d["lsb_v_mv"], d["pleine_echelle_v_mv"])
    VOIES["I"] = Voie("courant", d["entree_i"], d["differentiel"], d["lsb_i_mv"], d["pleine_echelle_i_mv"])


def reinitialiser():
    """Revient aux valeurs de constants.py (tests)."""
    VOIES.update(_depuis_constants())
