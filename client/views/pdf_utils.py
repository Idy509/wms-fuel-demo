"""Utilitaires PDF partagés — police Unicode pour les accents français."""
import os

from fpdf import FPDF

# Polices Windows avec support complet UTF-8
_ARIAL = r"C:\Windows\Fonts\arial.ttf"
_ARIAL_BOLD = r"C:\Windows\Fonts\arialbd.ttf"


def setup_pdf_fonts(pdf: FPDF) -> str:
    """Enregistre une police Unicode sur le PDF.

    Retourne le nom de famille à utiliser avec ``pdf.set_font()``.
    Si Arial TTF est disponible (Windows), l'utilise pour un rendu complet
    des accents.  Sinon, retombe sur Helvetica (latin-1 seulement).
    """
    if os.path.exists(_ARIAL):
        pdf.add_font("WMS", "", _ARIAL)
        # Le style gras DOIT être enregistré : tous les appelants font
        # `set_font(font, "B", …)` pour les titres et les en-têtes de tableau,
        # et fpdf2 lève « Undefined font » si la famille n'a pas de variante
        # « B ». Sur un poste où arial.ttf existe sans arialbd.ttf, l'export PDF
        # échouait donc systématiquement. À défaut du vrai gras, on réenregistre
        # le fichier normal : le titre s'affiche sans graisse plutôt que pas
        # du tout, et les accents restent corrects.
        pdf.add_font("WMS", "B",
                     _ARIAL_BOLD if os.path.exists(_ARIAL_BOLD) else _ARIAL)
        return "WMS"
    return "Helvetica"


class PdfAvecPiedDePage(FPDF):
    """FPDF dont CHAQUE page porte le pied de page et son numéro réel.

    Les bordereaux écrivaient « Page 1/1 » en dur, une seule fois, à la fin du
    document. Un bon de plus d'une vingtaine de lignes tient sur deux pages :
    la première n'avait alors aucun pied de page, et la dernière annonçait
    « Page 1/1 » — un contrôle papier ne pouvait pas voir qu'il manquait une
    feuille. `footer()` est appelée par fpdf2 à chaque saut de page.

    Renseigner `police` et `pied_texte` juste après `setup_pdf_fonts()` et
    AVANT le premier `add_page()`.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.police = "Helvetica"
        self.pied_texte = ""
        self.pied_page_libelle = "Page {n}/{total}"
        # Renseignés par l'appelant (ex. generer_pdf_bon), AVANT le premier
        # add_page() : libellé de continuation (déjà traduit et mis en
        # forme par l'appelant — ce module reste chargeable isolément, sans
        # `client/` dans sys.path, voir tests/test_export_pdf.py, donc SANS
        # dépendance à i18n) et en-têtes/largeurs du tableau de lignes, pour
        # que `header()` puisse les répéter sur les pages suivantes. Vide =
        # rien à répéter (autre usage de cette classe qui n'a pas de tableau
        # paginé).
        self.entete_document_suite = ""
        self.entete_colonnes: list[str] = []
        self.largeurs_colonnes: list[float] = []

    def header(self):
        """Répète le contexte du document et les en-têtes de colonnes.

        Repéré par audit du 9 septembre 2026 (génération réelle d'un bon de
        90 lignes, inspectée avec pypdf) : un bon assez long pour dépasser
        une page (auto_page_break fonctionne, rien ne déborde) n'avait
        AUCUN rappel du document ni des colonnes sur ses pages suivantes —
        seules des lignes de produit nues, sans savoir quelle colonne
        contient quoi ni de quel bon il s'agit, si seule cette page arrive
        par exemple sur WhatsApp.

        La PREMIÈRE page garde son en-tête complet dessiné à la main par
        l'appelant (logo, métadonnées, éventuelle mention « BON ANNULÉ ») —
        fpdf2 appelle `header()` avant même ce code, donc cette méthode se
        limite aux pages SUIVANTES pour ne rien dupliquer sur la première.
        """
        if self.page_no() <= 1 or not self.entete_colonnes:
            return
        self.set_font(self.police, "B", 11)
        self.set_text_color(90, 90, 90)
        self.cell(0, 8, safe_text(self.entete_document_suite, self.police),
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.set_font(self.police, "B", 10)
        self.set_fill_color(40, 40, 50)
        self.set_text_color(255, 255, 255)
        self.set_draw_color(80, 80, 80)
        for largeur, entete in zip(self.largeurs_colonnes, self.entete_colonnes):
            self.cell(largeur, 8, entete, border=1, fill=True, align="C")
        self.ln()
        self.set_text_color(0, 0, 0)

    def footer(self):
        if not self.pied_texte:
            return
        self.set_y(-18)
        self.set_font(self.police, "", 8)
        self.set_text_color(120, 120, 120)
        self.set_draw_color(200, 200, 200)
        self.line(10, self.get_y(), self.w - 10, self.get_y())
        self.ln(2)
        self.cell(0, 5, safe_text(self.pied_texte, self.police))
        # « {nb} » est l'alias remplacé par fpdf2 par le nombre total de pages
        # au moment de l'écriture du fichier.
        self.cell(0, 5, safe_text(
            self.pied_page_libelle.format(n=self.page_no(), total="{nb}"),
            self.police), align="R")
        self.set_text_color(0, 0, 0)


def safe_text(text, font_family: str) -> str:
    """Rend le texte compatible avec la police courante.

    Pour Helvetica (pas d'Unicode), encode en latin-1 avec remplacement
    des caractères non supportés.  Pour WMS/Arial, passe le texte tel quel.
    """
    s = str(text) if text is not None else ""
    if font_family == "Helvetica":
        return s.encode("latin-1", "replace").decode("latin-1")
    return s
