# Classification des événements : même idée que votre classifier.py (kNN, prédit l'appareil ET l'état
# ON/OFF), sur delta et transient_duration, plus (firmware >= 0.8) les nouvelles caractéristiques de
# constants.FEATURES : delta de S, delta de PF, nombre de paliers... Découpée en deux temps :
#   - entrainer() : lit dataset.csv, mesure la précision, puis apprend sur TOUT le dataset et
#                   enregistre le modèle dans modele.pkl (mode "entrainer") ;
#   - charger() puis identify(event) : reconnaît un événement (modes "classer" et "production").

import csv
import math
import pickle

import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import StandardScaler

from constants import FEATURES, FEATURES_ANCIENNES, N_NEIGHBORS, TEST_SIZE

ETATS = ["OFF", "ON"]   # l'état est codé 0 / 1
# colonnes de dataset.csv : label, delta, transient_duration, ON|OFF, puis (firmware >= 0.8) :
COLONNES = ["delta", "transient_duration", None, "delta_s", "delta_pf", "n_paliers", "duree_chaine_ms", "marche_max"]


def valeur(x):
    """Une caractéristique en nombre ; "nan" ou vide (delta de S inconnu) -> 0."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(v) else v


def lire_dataset(dataset_path, features=FEATURES):
    """Lit dataset.csv -> (X, y, features utilisées) ; y = [label, etat].
    Si une seule ligne est à l'ancien format (4 colonnes), on ne garde que FEATURES_ANCIENNES pour tout le
    monde (et entrainer() le signale : il faut refaire le dataset)."""
    lignes = []
    with open(dataset_path, 'r', newline='') as f:
        for r in csv.reader(f):
            if len(r) < 4 or not r[0].strip().lstrip("-").isdigit():
                continue   # ligne vide ou en-tête
            d = {nom: r[k + 1] for k, nom in enumerate(COLONNES) if nom and k + 1 < len(r)}
            lignes.append((int(r[0]), ETATS.index(r[3].strip()), d))
    if any(len(d) < len(COLONNES) - 1 for _, _, d in lignes):
        features = [f for f in features if f in FEATURES_ANCIENNES]
    x = np.array([[valeur(d[f]) for f in features] for _, _, d in lignes])
    y = np.array([(label, etat) for label, etat, _ in lignes])
    return x, y, list(features)


class EventClassifier():

    def __init__(self) -> None:

        self.scaler = StandardScaler()

        self.model = KNeighborsClassifier(n_neighbors=N_NEIGHBORS)

        self.features = list(FEATURES_ANCIENNES)   # caractéristiques que le modèle attend

    def entrainer(self, dataset_path, modele_path):
        """Renvoie la précision mesurée (ou None si le dataset est trop petit pour la mesurer).
        self.features dit quelles caractéristiques ont servi (moins que FEATURES : ancien dataset)."""
        x_array, y_array, self.features = lire_dataset(dataset_path)
        if len(x_array) < 2:
            raise ValueError("dataset trop petit (%d ligne(s)) : lancez d'abord le mode dataset" % len(x_array))

        precision = None
        if len(x_array) >= 10:
            # 1) on met de côté une partie du dataset pour vérifier que le modèle reconnaît
            #    des événements qu'il n'a jamais vus (random_state : même découpage à chaque fois)
            X_train, X_test, y_train, y_test = train_test_split(x_array, y_array, test_size=TEST_SIZE, random_state=0)
            scaler = StandardScaler().fit(X_train)
            model = KNeighborsClassifier(n_neighbors=N_NEIGHBORS).fit(scaler.transform(X_train), y_train)
            prediction = model.predict(scaler.transform(X_test))
            precision = float(np.mean(np.all(prediction == y_test, axis=1)))

        # 2) le modèle utilisé en production apprend sur TOUT le dataset
        self.model.fit(self.scaler.fit_transform(x_array), y_array)
        with open(modele_path, 'wb') as f:
            pickle.dump((self.scaler, self.model, self.features), f)
        return precision

    def charger(self, modele_path):
        with open(modele_path, 'rb') as f:
            contenu = pickle.load(f)
        if len(contenu) == 2:   # modèle entraîné avant le firmware 0.8 : delta et transient_duration
            (self.scaler, self.model), self.features = contenu, list(FEATURES_ANCIENNES)
        else:
            self.scaler, self.model, self.features = contenu
        return self

    def identify(self, event):
        """event (dictionnaire envoyé par la carte) -> (label de l'appareil, "ON" ou "OFF").
        Un événement d'un ancien firmware n'a pas les nouvelles caractéristiques : elles valent 0."""
        x = self.scaler.transform([[valeur(event.get(f)) for f in self.features]])
        appliance, state = self.model.predict(x)[0]
        return int(appliance), ETATS[int(state)]
