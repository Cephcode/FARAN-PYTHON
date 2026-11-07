# Fausse carte pour le mode développement (PORT = "sim") : se comporte comme un port série
# qui recevrait les événements d'une vraie carte. Toutes les ~6 s, un des appareils simulés
# change d'état (allumé <-> éteint). Aucun matériel nécessaire.

import random
import time

import faran_link as link

# nom, puissance (W), duree du transitoire (ms) : les petits appareils sont confirmés par D_min (5 s)
APPAREILS_SIMULES = [
    ("lampe", 60.0, 240),
    ("ventilateur mode 2", 45.0, 5240),
    ("fer a repasser", 1000.0, 260),
]


class CarteSimulee():

    def __init__(self, intervalle=6.0, seed=None) -> None:
        self.random = random.Random(seed)
        self.intervalle = intervalle
        self.prochain = time.time() + 2
        self.allumes = set()
        self.total = 80.0          # consommation de fond de la maison
        self.seq = 0
        self.eof = False

    def write(self, data):         # les commandes (MODE 4...) sont acceptées sans effet
        pass

    def readline(self):
        time.sleep(0.05)
        if time.time() < self.prochain:
            return b""
        self.prochain = time.time() + self.intervalle
        nom, puissance, duree = self.random.choice(APPAREILS_SIMULES)
        allumage = nom not in self.allumes
        delta = (puissance if allumage else -puissance) * self.random.uniform(0.97, 1.03)
        (self.allumes.add if allumage else self.allumes.discard)(nom)
        avant = self.total
        self.total += delta
        by = "e_power" if abs(delta) >= 49 else "d_min"
        pf = {"lampe": 1.0, "ventilateur mode 2": 0.85, "fer a repasser": 1.0}[nom]
        s_delta = delta / pf
        # format du firmware >= 0.8 : delta_S, delta_PF, paliers, durée de la chaîne, plus grande marche, P, ameliore
        body = "EVT,%d,%s,%.3f,%.3f,%.3f,0,12,12,%d,%s,0,%d,%.3f,%.4f,1,%d,%.3f,P,ameliore" % (
            self.seq, "ON" if allumage else "OFF", delta, avant, self.total,
            duree + self.random.randint(-20, 20), by, int(time.time() * 1000) % 10**9,
            s_delta, (pf - 0.9) * (1 if allumage else -1), duree // 2, abs(delta))
        self.seq += 1
        return link.encode(body).encode()

    def reset_input_buffer(self):
        pass

    def close(self):
        pass
