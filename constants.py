# Définition des constantes du côté PC (comme constants.py de Faran custom code).
# Modifiez ici, pas dans le code.

# ------------------------------------------------------------ liaison avec la carte
PORT = "/dev/ttyUSB0"      # Linux : /dev/ttyUSB0 (ou ttyUSB1) ; Windows : "COM3" ; "sim" = carte simulée
BAUD = 460800              # identique à SERIAL_BAUD dans firmware/src/config.h
MODE_CARTE_REEL = "4"      # MODE 4 (LIVE) : détection sur les vrais capteurs
MODE_CARTE_SIMULE = "3"    # MODE 3 (DETECT_SIM) : détection sur un signal simulé par la carte

# ------------------------------------------------------------ ADC (doit correspondre à firmware/src/config.h)
# La carte annonce sa vraie configuration (ligne ADC) et elle remplace ces valeurs dès qu'elle arrive :
# elles ne servent que tant que la carte n'a rien dit (ou pour relire un ancien fichier brut).
COURANT_DIFFERENTIEL = True        # courant lu en différentiel A1 - A3 (A3 = milieu du pont 1,65 V)
LSB_TENSION_MV = 0.125             # 1 code de la voie tension (calibre +-4,096 V)
PLEINE_ECHELLE_TENSION_MV = 4096.0
LSB_COURANT_MV = 0.015625          # 1 code de la voie courant (calibre +-0,512 V) ; 0.125 en entrée simple A1
PLEINE_ECHELLE_COURANT_MV = 512.0  # 4096.0 en entrée simple A1

# ------------------------------------------------------------ fichiers
APPAREILS = "appareils.csv"        # liste des appareils : id,nom
DATASET = "dataset.csv"            # label,delta,transient_duration,ON|OFF[,delta_s,delta_pf,n_paliers,duree_chaine_ms,marche_max]
MODELE = "modele.pkl"              # classifieur entraîné (mode entrainer)
EVENEMENTS = "evenements.csv"      # mode enregistrer : tous les événements reçus (sur la carte SD si Raspberry Pi)
JOURNAL = "journal.csv"            # mode production : chaque utilisation terminée d'un appareil

# ------------------------------------------------------------ mode dataset
DUREE_COMMANDE = 30        # secondes pour exécuter une commande (écoute pendant ce temps)
PAUSE_ENTRE_COMMANDES = 3  # secondes de calme entre deux commandes

# ------------------------------------------------------------ mode entrainer
TEST_SIZE = 0.3            # part du dataset gardée pour mesurer la précision
N_NEIGHBORS = 1            # kNN, comme dans votre classifier.py
# Caractéristiques données au classifieur. Firmware >= 0.8 : le dataset a 5 colonnes de plus (delta de la
# puissance apparente S, delta du facteur de puissance, nombre de paliers du démarrage, sa durée, sa plus grande
# marche). Un ancien dataset (4 colonnes) n'a que delta et transient_duration : le classifieur se contente
# alors de FEATURES_ANCIENNES (et le dit). Retirez une caractéristique d'ici si elle gêne la reconnaissance.
FEATURES = ["delta", "transient_duration", "delta_s", "delta_pf", "n_paliers", "duree_chaine_ms", "marche_max"]
FEATURES_ANCIENNES = ["delta", "transient_duration"]

# ------------------------------------------------------------ mode production
RAFRAICHISSEMENT = 5       # secondes entre deux affichages du registre
TOLERANCE_PUISSANCE = 0.3  # une extinction peut annuler un allumage de puissance +-30 % si le label diffère

# ------------------------------------------------------------ diagnostic, modes tension / courant
# Ce qui est « normal » : chaque mesure est comparée à ces plages (python main.py diagnostic|tension|courant).
TENSION_NOMINALE = 220.0   # V efficaces (Burkina Faso)
TOLERANCE_TENSION = 0.10   # +-10 % : 198 à 242 V
FREQ_MIN, FREQ_MAX = 49.0, 51.0          # Hz (durée d'une période : 19,6 à 20,4 ms)
SAUT_TENSION_MAX = 0.03    # d'une période à la suivante, la tension ne varie pas de plus de 3 %
COURANT_A_VIDE_MAX = 0.10  # A : à vide, au-delà c'est du bruit capté (ou un appareil en veille)
TOLERANCE_COURANT = 0.15   # +-15 % autour du courant attendu (option --charge)
SEUIL_PIC_VA = 30.0        # une période qui s'écarte de plus que ça de ses voisines est un « pic »
                           # (au-dessus de ~25 W, un pic peut déjà déclencher un faux événement)
