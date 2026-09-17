"""Planche d'étiquettes produits (A4) — SKU en gros et code-barres Code 128.

Pourquoi le code-barres est dessiné ici plutôt qu'importé d'une bibliothèque :
aucune bibliothèque de génération de codes-barres n'est installée sur les
postes (`python-barcode` est absent), et l'entrepôt n'a pas d'accès Internet
fiable pour en ajouter une au moment de l'installation. La symbologie Code 128
est publique et tient en une table de 107 motifs : on la dessine directement
avec `fpdf2`, déjà présent pour tous les autres PDF de l'application. Cela
évite une dépendance de plus dans `client.spec` et dans l'installateur.

Le sous-ensemble B est le seul implémenté : il couvre tout l'ASCII imprimable
(espace à `~`), donc l'intégralité des SKU et des codes-barres saisis dans le
catalogue. Un code contenant un caractère hors de cette plage (accent, par
exemple) n'est pas encodable : l'étiquette est alors imprimée SANS code-barres
plutôt qu'avec un symbole illisible qu'aucune douchette ne relira.
"""
import os
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox

from i18n import t
from views.pdf_utils import PdfAvecPiedDePage, setup_pdf_fonts, safe_text

# ---------------------------------------------------------------- Code 128
#
# Motifs des valeurs 0 à 106 : six chiffres = largeurs successives, en
# modules, en commençant par une BARRE (barre, espace, barre, espace, barre,
# espace). Le motif d'arrêt (106) en compte sept, la barre finale en plus.
# Chaque motif des valeurs 0-105 totalise 11 modules — c'est ce qui donne son
# nom à la symbologie et ce que vérifient les tests.
_MOTIFS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213",
    "122312", "132212", "221213", "221312", "231212", "112232", "122132",
    "122231", "113222", "123122", "123221", "223211", "221132", "221231",
    "213212", "223112", "312131", "311222", "321122", "321221", "312212",
    "322112", "322211", "212123", "212321", "232121", "111323", "131123",
    "131321", "112313", "132113", "132311", "211313", "231113", "231311",
    "112133", "112331", "132131", "113123", "113321", "133121", "313121",
    "211331", "231131", "213113", "213311", "213131", "311123", "311321",
    "331121", "312113", "312311", "332111", "314111", "221411", "431111",
    "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114",
    "413111", "241112", "134111", "111242", "121142", "121241", "114212",
    "124112", "124211", "411212", "421112", "421211", "212141", "214121",
    "412121", "111143", "111341", "131141", "114113", "114311", "411113",
    "411311", "113141", "114131", "311141", "411131", "211412", "211214",
    "211232", "2331112",
]

START_B = 104
STOP = 106

# Zone silencieuse : dix modules vides de part et d'autre du symbole. Sans
# elle, une douchette ne trouve pas le début du code et ne lit rien.
QUIET_ZONE = 10


def encodable_code128b(donnee) -> bool:
    """Vrai si la chaîne tient entièrement dans le sous-ensemble B."""
    texte = "" if donnee is None else str(donnee)
    return bool(texte) and all(32 <= ord(c) <= 126 for c in texte)


def valeurs_code128b(donnee: str) -> list[int]:
    """Valeurs du symbole : start B, données, clé de contrôle, stop.

    La clé est la somme pondérée modulo 103 : valeur de départ, puis chaque
    caractère multiplié par sa position (1 pour le premier). Sans elle, aucune
    douchette n'accepte le symbole.
    """
    if not encodable_code128b(donnee):
        raise ValueError(f"chaîne non encodable en Code 128 B : {donnee!r}")
    valeurs = [START_B] + [ord(c) - 32 for c in donnee]
    somme = START_B
    for position, valeur in enumerate(valeurs[1:], start=1):
        somme += position * valeur
    valeurs.append(somme % 103)
    valeurs.append(STOP)
    return valeurs


def motif_code128b(donnee: str) -> list[int]:
    """Largeurs en modules, en alternant barre / espace, barre en premier.

    Zones silencieuses comprises : la liste commence donc par un ESPACE de
    `QUIET_ZONE` modules — l'appelant alterne à partir de là.
    """
    largeurs = [QUIET_ZONE]
    for valeur in valeurs_code128b(donnee):
        largeurs.extend(int(c) for c in _MOTIFS[valeur])
    largeurs.append(QUIET_ZONE)
    return largeurs


def _dessiner_code_barres(pdf, donnee: str, x: float, y: float,
                          largeur: float, hauteur: float) -> bool:
    """Trace le symbole dans le rectangle donné. False si non encodable."""
    if not encodable_code128b(donnee):
        return False
    largeurs = motif_code128b(donnee)
    total = sum(largeurs)
    if total <= 0:
        return False
    module = largeur / total
    pdf.set_fill_color(0, 0, 0)
    curseur = x
    # La liste commence par la zone silencieuse (un espace) : la première
    # BARRE est donc à l'index 1, puis un élément sur deux.
    for index, nb_modules in enumerate(largeurs):
        epaisseur = nb_modules * module
        if index % 2 == 1:
            pdf.rect(curseur, y, epaisseur, hauteur, style="F")
        curseur += epaisseur
    return True


# ------------------------------------------------------------- mise en page
#
# A4 (210 x 297 mm), 3 colonnes x 8 lignes = 24 étiquettes par feuille, au
# format des planches autocollantes les plus courantes.
COLONNES = 3
LIGNES = 8
MARGE_X = 7.0
MARGE_Y = 10.0
ETIQUETTES_PAR_PAGE = COLONNES * LIGNES


def _tronquer(pdf, texte: str, largeur_max: float) -> str:
    """Coupe le texte pour qu'il tienne dans la largeur, avec une ellipse."""
    if pdf.get_string_width(texte) <= largeur_max:
        return texte
    while texte and pdf.get_string_width(texte + "…") > largeur_max:
        texte = texte[:-1]
    return texte + "…"


def construire_pdf_etiquettes(produits: list[dict]) -> PdfAvecPiedDePage:
    """Planche d'étiquettes des produits donnés, prête à être écrite."""
    pdf = PdfAvecPiedDePage(format="A4")
    font = setup_pdf_fonts(pdf)
    pdf.police = font
    pdf.pied_texte = t("Étiquettes — imprimé le {date}").format(
        date=datetime.now().strftime("%d/%m/%Y %H:%M"))
    pdf.pied_page_libelle = t("Page {n}/{total}")
    # Pas de saut de page automatique : la grille est posée à la main, une
    # coupure ajoutée par fpdf2 décalerait toutes les étiquettes suivantes.
    pdf.set_auto_page_break(auto=False)

    largeur_case = (pdf.w - 2 * MARGE_X) / COLONNES
    hauteur_case = (pdf.h - 2 * MARGE_Y - 12) / LIGNES

    for index, produit in enumerate(produits):
        if index % ETIQUETTES_PAR_PAGE == 0:
            pdf.add_page()
        place = index % ETIQUETTES_PAR_PAGE
        x = MARGE_X + (place % COLONNES) * largeur_case
        y = MARGE_Y + (place // COLONNES) * hauteur_case
        _dessiner_etiquette(pdf, font, produit, x, y,
                            largeur_case, hauteur_case)
    if not produits:
        # Un PDF sans page est illisible par la plupart des visionneuses :
        # mieux vaut une feuille qui dit explicitement qu'il n'y a rien.
        pdf.add_page()
        pdf.set_font(font, "", 11)
        pdf.cell(0, 10, safe_text(t("Aucun produit à étiqueter."), font))
    return pdf


def _dessiner_etiquette(pdf, font, produit: dict, x: float, y: float,
                        largeur: float, hauteur: float) -> None:
    marge = 2.5
    interieur = largeur - 2 * marge
    pdf.set_draw_color(180, 180, 180)
    pdf.set_line_width(0.2)
    pdf.rect(x + 0.5, y + 0.5, largeur - 1, hauteur - 1)

    # Nom du produit, en petit : c'est le SKU qu'on cherche du regard en
    # rayon, le nom ne sert qu'à lever un doute.
    pdf.set_xy(x + marge, y + marge)
    pdf.set_font(font, "", 7)
    pdf.set_text_color(60, 60, 60)
    nom = safe_text(str(produit.get("name") or ""), font)
    pdf.cell(interieur, 3.5, _tronquer(pdf, nom, interieur))

    sku = str(produit.get("sku") or "")
    code = str(produit.get("barcode") or "").strip()

    pdf.set_text_color(0, 0, 0)
    pdf.set_font(font, "B", 13)
    pdf.set_xy(x + marge, y + marge + 4)
    pdf.cell(interieur, 6, _tronquer(pdf, safe_text(sku, font), interieur))

    hauteur_code = hauteur - (marge + 4 + 6) - 5.5
    trace = False
    if code and hauteur_code > 4:
        trace = _dessiner_code_barres(pdf, code, x + marge, y + marge + 11,
                                      interieur, hauteur_code)
        if trace:
            pdf.set_font(font, "", 6.5)
            pdf.set_text_color(60, 60, 60)
            pdf.set_xy(x + marge, y + hauteur - 5)
            pdf.cell(interieur, 3.5, safe_text(code, font), align="C")
    if not trace:
        # Aucun code-barres exploitable : on le DIT, plutôt que de laisser un
        # cadre vide qu'on prendrait pour une impression ratée.
        pdf.set_font(font, "", 6.5)
        pdf.set_text_color(120, 120, 120)
        pdf.set_xy(x + marge, y + hauteur - 6)
        pdf.cell(interieur, 3.5,
                 safe_text(t("sans code-barres — à scanner : voir la fiche produit"),
                           font))
    pdf.set_text_color(0, 0, 0)


def produits_a_etiqueter(produits: list[dict]) -> list[dict]:
    """Ne garde que les produits actifs, dans l'ordre reçu.

    Un produit archivé n'est plus en rayon : lui imprimer une étiquette
    gaspille une planche autocollante et remet en circulation une référence
    volontairement retirée.
    """
    return [p for p in produits if not p.get("archived")]


def imprimer_etiquettes(produits: list[dict], parent=None) -> str | None:
    """Demande où enregistrer la planche, l'écrit et l'ouvre.

    Renvoie le chemin, ou None si l'opérateur a annulé.
    """
    a_imprimer = produits_a_etiqueter(produits)
    if not a_imprimer:
        messagebox.showinfo(
            t("Étiquettes"),
            t("Aucun produit actif à étiqueter dans la sélection."),
            parent=parent)
        return None
    chemin = filedialog.asksaveasfilename(
        defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
        initialfile=f"etiquettes_{datetime.now().strftime('%Y%m%d')}.pdf",
        parent=parent)
    if not chemin:
        return None
    pdf = construire_pdf_etiquettes(a_imprimer)
    try:
        pdf.output(chemin)
    except OSError as e:
        messagebox.showerror(
            t("Étiquettes"),
            t("Impossible d'enregistrer le PDF :\n{e}").format(e=e),
            parent=parent)
        return None
    webbrowser.open(f"file:///{chemin.replace(os.sep, '/')}")
    return chemin
