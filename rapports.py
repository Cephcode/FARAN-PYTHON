"""Mise en forme des rapports téléchargés depuis l'API : CSV (toujours), PDF (reportlab) ou, si reportlab
n'est pas installé, une page HTML imprimable (Fichier > Imprimer > PDF dans le navigateur).

Contenu : par appareil, nombre d'utilisations, durée totale, énergie (kWh), coût (FCFA), puissance
moyenne ; puis le total de la maison. Le bilan vient de consommation.py.
"""
from __future__ import annotations

import html
import io
from datetime import datetime

from consommation import Ligne, ecrire_csv


def duree_texte(s):
    """5025 -> '1 h 23 min' ; 95 -> '1 min 35 s'"""
    s = int(round(s))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    if h:
        return "%d h %02d min" % (h, m)
    if m:
        return "%d min %02d s" % (m, sec)
    return "%d s" % sec


def _date(t):
    return datetime.fromtimestamp(t).strftime("%d/%m/%Y %H:%M:%S")


def tableau(bilan, tarif):
    """[(appareil, utilisations, durée, kWh, FCFA, W moyens)], puis la ligne TOTAL."""
    lignes, tot = [], Ligne(-1, "TOTAL")
    for l in bilan.triees():
        tot.ajouter(l)
        lignes.append(l)
    lignes.append(tot)
    return [(l.appareil, str(l.utilisations), duree_texte(l.duree_s), "%.3f" % (l.energie_wh / 1000),
             "%.0f" % (l.energie_wh / 1000 * tarif), "%.0f" % l.puissance_moyenne_w()) for l in lignes]


ENTETES = ("Appareil", "Utilisations", "Durée", "Énergie (kWh)", "Coût (FCFA)", "Puissance moy. (W)")


def titre(bilan):
    nom = "Rapport journalier" if bilan.titre == "journalier" else "Rapport mensuel"
    return "Faran — %s" % nom, "du %s au %s" % (_date(bilan.debut), _date(bilan.fin))


def en_csv(bilan, tarif):
    f = io.StringIO()
    ecrire_csv(f, bilan, tarif)
    return f.getvalue().encode("utf-8")


def pdf_disponible():
    try:
        import reportlab  # noqa: F401
        return True
    except ImportError:
        return False


def en_pdf(bilan, tarif, note=""):
    """Un PDF d'une page (A4) : titre, période, tableau, tarif utilisé."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    f = io.BytesIO()
    doc = SimpleDocTemplate(f, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm)
    st = getSampleStyleSheet()
    t1, t2 = titre(bilan)
    corps = [Paragraph(t1, st["Title"]), Paragraph(t2, st["Normal"]), Spacer(1, 0.6 * cm)]
    donnees = [list(ENTETES)] + [list(r) for r in tableau(bilan, tarif)]
    t = Table(donnees, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.grey),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    corps += [t, Spacer(1, 0.6 * cm), Paragraph("Tarif utilisé : %.0f FCFA par kWh. %s" % (tarif, note), st["Normal"])]
    if len(donnees) == 2:
        corps.append(Paragraph("Aucun appareil n'a consommé pendant cette période.", st["Normal"]))
    doc.build(corps)
    return f.getvalue()


def en_html(bilan, tarif, note=""):
    """Page HTML imprimable (quand reportlab n'est pas installé)."""
    t1, t2 = titre(bilan)
    e = html.escape
    lignes = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % e(c) for c in r) for r in tableau(bilan, tarif))
    return ("<!doctype html><html lang='fr'><meta charset='utf-8'><title>%s</title>"
            "<style>body{font-family:sans-serif;margin:2em;color:#1C2140}table{border-collapse:collapse}"
            "td,th{padding:.3em .8em;border-bottom:1px solid #ccc;text-align:right}td:first-child,th:first-child"
            "{text-align:left}tr:last-child td{font-weight:bold;border-top:2px solid #1C2140}</style>"
            "<h1>%s</h1><p>%s</p><table><tr>%s</tr>%s</table><p>Tarif utilisé : %.0f FCFA par kWh. %s</p>"
            "<p><button onclick='print()'>Imprimer</button></p></html>"
            % (e(t1), e(t1), e(t2), "".join("<th>%s</th>" % e(h) for h in ENTETES), lignes, tarif, e(note))
            ).encode("utf-8")
