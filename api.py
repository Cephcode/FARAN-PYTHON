"""API web du mode production : la page apercu.html (et tout autre programme) lit ici ce qui consomme.

    python api.py --port-serie sim                     # sans matériel : la fausse carte du PC
    python api.py                                      # la vraie carte (/dev/ttyUSB0, MODE 4)
    python api.py --port-serie /dev/ttyUSB1 --sans-capteur --hote 0.0.0.0 --port 8000
puis ouvrir http://localhost:8000 dans un navigateur.

Dans le même programme : un thread lit la carte et tient le registre (service.py) ; le serveur web
(uvicorn) répond aux requêtes. `python main.py production` marche toujours tout seul, sans l'API.

Routes (JSON, heures ISO 8601 locales) :
    GET /api/etat                                     ce qui est allumé maintenant, coûts, énergie du jour et du mois
    GET /api/rapports/journalier?format=csv|pdf       le rapport du jour (depuis minuit), à télécharger
    GET /api/rapports/mensuel?format=csv|pdf          le rapport du mois (depuis le 1er)
    GET /api/sante                                    {"ok": true, "version": ...}
    GET /                                             la page apercu.html
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

import rapports
from constants import (APPAREILS, HOTE_API, MODELE, MODE_CARTE_REEL, MODE_CARTE_SIMULE, PORT, PORT_API,
                       VERSION_API)

ICI = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(ICI, "apercu.html")


def creer_app(production):
    """L'application web, pour un objet Production (service.py). Les tests en donnent un sans carte."""
    app = FastAPI(title="Faran", version=VERSION_API)
    # CORS ouvert : la page peut aussi être ouverte depuis un fichier ou un autre port, en local.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

    @app.get("/api/sante")
    def sante():
        return {"ok": True, "version": VERSION_API}

    @app.get("/api/etat")
    def etat():
        return production.etat()

    @app.get("/api/rapports/{quoi}")
    def rapport(quoi: str, format: str = Query("pdf", pattern="^(csv|pdf|html)$")):
        if quoi not in ("journalier", "mensuel"):
            raise HTTPException(404, "rapport inconnu : journalier ou mensuel")
        bilan = production.rapport(quoi)
        nom = "faran_%s_%s" % (quoi, datetime.fromtimestamp(bilan.fin).strftime("%Y-%m-%d_%H-%M"))
        note = ("Mode démonstration : une « journée » dure %d s." % production.duree_demo_s) if production.demo else ""
        if format == "csv":
            return _fichier(rapports.en_csv(bilan, production.tarif), "text/csv; charset=utf-8", nom + ".csv")
        if format == "pdf" and rapports.pdf_disponible():
            return _fichier(rapports.en_pdf(bilan, production.tarif, note), "application/pdf", nom + ".pdf")
        # pas de reportlab : une page HTML à imprimer
        return _fichier(rapports.en_html(bilan, production.tarif, note), "text/html; charset=utf-8", nom + ".html")

    @app.get("/")
    def page():
        return FileResponse(PAGE, media_type="text/html; charset=utf-8")

    return app


def _fichier(contenu, type_mime, nom):
    return Response(contenu, media_type=type_mime, headers={"Content-Disposition": 'attachment; filename="%s"' % nom})


# ------------------------------------------------------------------ lancement : carte + serveur web
def main(argv=None):
    ap = argparse.ArgumentParser(description="Faran : API web du mode production")
    ap.add_argument("--port-serie", default=PORT, help="port série de la carte, ou 'sim' (défaut : %s)" % PORT)
    ap.add_argument("--sans-capteur", action="store_true", help="la carte simule elle-même les capteurs (MODE 3)")
    ap.add_argument("--hote", default=HOTE_API, help="adresse d'écoute (0.0.0.0 : visible sur le réseau local)")
    ap.add_argument("--port", type=int, default=PORT_API, help="port web (défaut : %d)" % PORT_API)
    ap.add_argument("--modele", help="classifieur entraîné (défaut : %s ; avec 'sim' : reconnaissance par puissance)"
                    % MODELE)
    args = ap.parse_args(argv)

    import uvicorn

    from board import Board
    from dataset import charger_appareils
    from service import LecteurCarte, Production

    sim = args.port_serie == "sim"
    if sim and not args.modele:
        # la fausse carte : ses appareils sont reconnus par leur puissance, sans modele.pkl
        from carte_simulee import APPAREILS_SIMULES_NOMS, ClassifieurSimule
        appareils, classifieur = dict(APPAREILS_SIMULES_NOMS), ClassifieurSimule()
    else:
        from classifier import EventClassifier
        chemin = args.modele or MODELE
        if not os.path.exists(chemin):
            raise SystemExit("%s introuvable : lancez d'abord python main.py entrainer (ou --port-serie sim)" % chemin)
        appareils, classifieur = charger_appareils(APPAREILS), EventClassifier().charger(chemin)

    def ouvrir_carte():
        board = Board(args.port_serie, afficher_infos=False)
        board.demarrer(MODE_CARTE_SIMULE if args.sans_capteur else MODE_CARTE_REEL)
        return board

    production = Production(appareils)
    lecteur = LecteurCarte(production, ouvrir_carte, classifieur)
    lecteur.start()
    print("Faran : carte %s, page web sur http://%s:%d (Ctrl+C pour arrêter)"
          % (args.port_serie, "localhost" if args.hote in ("127.0.0.1", "0.0.0.0") else args.hote, args.port))
    try:
        uvicorn.run(creer_app(production), host=args.hote, port=args.port, log_level="warning")
    finally:
        lecteur.arreter()   # la carte se remet au repos (MODE 5)


if __name__ == "__main__":
    main()
