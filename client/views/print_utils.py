"""Génération du PDF d'un bon, à partir des données du bon lui-même.

Utilisé après validation d'une saisie (document_form) pour proposer
l'impression immédiate sans repasser par l'historique.
"""
import os
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox

from i18n import t
from views.pdf_utils import PdfAvecPiedDePage, setup_pdf_fonts, safe_text

TYPE_LIBELLES = {
    "RECEIVING": "Réception",
    "DELIVERY": "Expédition",
    "RETURN": "Retour",
    "SUPPLIER_RETURN": "Retour fournisseur",
    "ADJUSTMENT": "Ajustement",
    "REGIONAL_TRANSFER": "Transfert régional",
}

# Le même geste porte deux noms selon le secteur : « Ajustement » de stock
# pour Consumables, « Jaugeage » de cuve pour le carburant (voir
# client/views/inventory.py et docs/SECTEUR_FUEL.md).
TYPE_LIBELLES_FUEL = {**TYPE_LIBELLES, "ADJUSTMENT": "Jaugeage"}


def formater_date_bon(valeur) -> str:
    """Date d'un bon en JJ/MM/AAAA (avec l'heure si elle est connue).

    Le serveur renvoie « 2026-09-01 14:23:11 » et l'historique affiche du
    M/J/AAAA (convention des trackers Excel du client). Sur un bordereau papier
    lu en français, « 9/1/2026 » se lit 9 janvier : la date imprimée doit être
    non ambiguë.
    """
    texte = str(valeur or "").strip()
    if not texte:
        return "—"
    for fmt, sortie in (("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"),
                        ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"),
                        ("%Y-%m-%d", "%d/%m/%Y"),
                        ("%m/%d/%Y", "%d/%m/%Y")):
        try:
            return datetime.strptime(texte, fmt).strftime(sortie)
        except ValueError:
            continue
    return texte


def _tronquer_a_la_largeur(pdf, texte: str, largeur_max: float) -> str:
    """Tronque `texte` pour qu'il tienne dans `largeur_max` (mm), avec « … ».

    Repéré par audit du 9 septembre 2026 (génération réelle d'un bon avec
    un SKU de 25 chiffres, positions de texte mesurées avec pypdf) :
    `str(valeur)[:25]` tronque par NOMBRE DE CARACTÈRES, pas par largeur
    rendue. `fpdf2.cell()` ne fait ni retour à la ligne ni clipping — un
    texte plus large que sa cellule (SKU long, nom de produit en
    majuscules) est dessiné par-dessus la cellule voisine, rendant la
    quantité illisible/superposée sur les cas réels, pas seulement des cas
    extrêmes fabriqués.
    """
    marge = 2 * pdf.c_margin
    if pdf.get_string_width(texte) <= largeur_max - marge:
        return texte
    while texte and pdf.get_string_width(texte + "…") > largeur_max - marge:
        texte = texte[:-1]
    return (texte + "…") if texte else "…"


def est_secteur_fuel(secteur) -> bool:
    """Vrai pour un compte du secteur carburant.

    Un seul endroit décide, pour que le ticket, ses champs et son nom de
    fichier ne puissent pas diverger.
    """
    return str(secteur or "").upper() == "FUEL"


_ACCENTS = str.maketrans(
    "àâäçéèêëîïôöùûüÿÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸ",
    "aaaceeeeiioouuuyAAACEEEEIIOOUUUY")


def _slug(texte) -> str:
    """Fragment de nom de fichier sûr (Windows, Android, WhatsApp)."""
    brut = str(texte or "").translate(_ACCENTS)
    remplace = "".join(c if c.isalnum() else "-" for c in brut)
    return "-".join(part for part in remplace.split("-") if part)


def nom_fichier_bon(bon: dict, secteur=None) -> str:
    """Nom de fichier parlant : type de bon, numéro, date.

    « bon_37.pdf » ne dit rien une fois le fichier reçu sur WhatsApp ; un
    « Expedition-REC-0012-2026-09-08.pdf » se retrouve et se transmet.
    """
    libelles = TYPE_LIBELLES_FUEL if est_secteur_fuel(secteur) else TYPE_LIBELLES
    type_brut = bon.get("type", "") or ""
    type_slug = _slug(t(libelles.get(type_brut, type_brut))) or "Bon"
    numero = _slug(bon.get("reference") or "") or f"n{bon.get('id', '')}"
    jour = _slug(str(bon.get("created_at") or "")[:10]) or datetime.now().strftime("%Y-%m-%d")
    return f"{type_slug}-{numero}-{jour}.pdf"


def _meta_fuel(bon: dict, T) -> list[tuple[str, str]]:
    """Métadonnées d'un bon carburant.

    Ni région ni superviseur (voir docs/SECTEUR_FUEL.md : le secteur FUEL est
    hors `SECTEURS_AVEC_REGION`) — les imprimer laissait deux lignes « — » sur
    chaque ticket, à la place des seules informations qui comptent pour une
    livraison de carburant.
    """
    meta = [
        (T(t("Référence")), bon.get("reference") or "—"),
        (T(t("Receveur")), bon.get("receiver") or bon.get("party") or "—"),
    ]
    # Les champs véhicule n'existent que sur une livraison (une réception de
    # carburant vient d'un camion-citerne, pas d'une plaque) : une ligne
    # « — » de plus sur un ticket de réception n'apprend rien.
    if bon.get("vehicle_plate"):
        meta.append((T(t("Plaque du véhicule")), bon["vehicle_plate"]))
    if bon.get("vehicle_info"):
        meta.append((T(t("Véhicule")), bon["vehicle_info"]))
    if bon.get("project"):
        meta.append((T(t("Projet")), bon["project"]))
    if bon.get("mileage") is not None:
        try:
            meta.append((T(t("Kilométrage")), f"{float(bon['mileage']):g}"))
        except (TypeError, ValueError):
            meta.append((T(t("Kilométrage")), str(bon["mileage"])))
    if bon.get("fuel_card"):
        meta.append((T(t("Carte carburant")), bon["fuel_card"]))
    if bon.get("carrier"):
        meta.append((T(t("Transporteur")), bon["carrier"]))
    if bon.get("note"):
        meta.append((T(t("Note")), str(bon["note"])[:120]))
    return meta


def generer_pdf_bon(bon: dict, unites: dict[int, str] | None = None,
                    parent=None, secteur=None) -> str | None:
    """Écrit le PDF d'un bon et l'ouvre. Retourne le chemin, ou None si annulé.

    ``bon`` est la réponse du serveur (clés : id, type, party, operator,
    region, reference, note, carrier, created_at, lines — plus, pour le
    carburant : vehicle_plate, vehicle_info, project, receiver, mileage,
    fuel_card).
    ``unites`` associe un product_id à son unité d'affichage (ex. "gls").
    ``secteur`` est celui du compte connecté : pour "FUEL", le ticket porte
    les champs carburant à la place de Région/Superviseur.
    """
    unites = unites or {}
    est_fuel = est_secteur_fuel(secteur)
    libelles = TYPE_LIBELLES_FUEL if est_fuel else TYPE_LIBELLES
    type_doc = t(libelles.get(bon.get("type", ""), bon.get("type", "")))
    lignes = bon.get("lines", []) or []
    now = datetime.now()

    reference = bon.get("reference") or ""

    pdf = PdfAvecPiedDePage(format="A4")
    font = setup_pdf_fonts(pdf)
    pdf.police = font
    pdf.pied_texte = t("Imprimé le {date} à {heure}").format(
        date=now.strftime("%d/%m/%Y"), heure=now.strftime("%H:%M"))
    pdf.pied_page_libelle = t("Page {n}/{total}")

    def T(texte):
        return safe_text(texte, font)

    # Posés AVANT le premier add_page() : `header()` (appelée par fpdf2 à
    # CHAQUE page, y compris la première) en a besoin pour répéter le
    # contexte du document et les en-têtes de colonnes sur les pages
    # suivantes d'un bon assez long pour en avoir plusieurs — repéré par
    # audit du 9 septembre 2026 (un bon de 90 lignes générait bien 3 pages
    # sans déborder, mais les pages 2 et 3 ne portaient ni le nom du
    # document ni les en-têtes de colonnes, seulement des lignes nues).
    pdf.entete_document_suite = T(t("{document} (suite)").format(
        document=f"{type_doc} — {reference}"))
    pdf.entete_colonnes = [
        "SKU", T(t("Cuve") if est_fuel else t("Produit")), T(t("Quantité"))]
    pdf.largeurs_colonnes = [40, 100, 40]

    pdf.set_auto_page_break(auto=True, margin=30)
    pdf.add_page()

    # --- En-tête ---
    pdf.set_font(font, "B", 16)
    pdf.cell(0, 10, T("DIGICEL — " + t("Gestion d'entrepôt")))
    pdf.set_font(font, "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.set_xy(pdf.w - 70, pdf.get_y())
    pdf.cell(60, 10, T(now.strftime("%d/%m/%Y %H:%M")), align="R")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(12)

    pdf.set_draw_color(40, 40, 50)
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), pdf.w - 10, pdf.get_y())
    pdf.ln(4)

    pdf.set_font(font, "B", 13)
    pdf.cell(0, 8, T(f"{type_doc} — {reference}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(font, "", 10)
    pdf.cell(0, 6, T(t("Date du bon : {date}").format(
        date=formater_date_bon(bon.get("created_at")))),
             new_x="LMARGIN", new_y="NEXT")
    # Un bon annulé reste imprimable (justificatif d'annulation) : le dire en
    # tête du bordereau, sinon la feuille est indiscernable d'un bon valide.
    if bon.get("cancelled_at"):
        pdf.set_text_color(200, 60, 60)
        pdf.set_font(font, "B", 10)
        pdf.cell(0, 6, T(t("BON ANNULÉ")), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.set_font(font, "", 10)
    pdf.ln(4)

    # --- Métadonnées ---
    if est_fuel:
        meta = _meta_fuel(bon, T)
    else:
        meta = [
            (T(t("Référence")), reference or "—"),
            (T(t("Tiers")), bon.get("party") or "—"),
            (T(t("Région")), bon.get("region") or "—"),
            (T(t("Opérateur")), bon.get("operator") or "—"),
        ]
        if bon.get("carrier"):
            meta.append((T(t("Transporteur")), bon["carrier"]))
        if bon.get("note"):
            meta.append((T(t("Note")), str(bon["note"])[:120]))

    col_w = 45 if not est_fuel else 50
    for label, val in meta:
        pdf.set_font(font, "B", 10)
        pdf.cell(col_w, 6, T(f"{label} :"))
        pdf.set_font(font, "", 10)
        pdf.cell(0, 6, T(val), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)

    # --- Tableau des lignes ---
    # headers/widths posés sur pdf.entete_colonnes/pdf.largeurs_colonnes
    # avant le premier add_page() (voir plus haut) : repris tels quels ici
    # pour que la ligne d'en-tête de la page 1 et celle que `header()`
    # répète sur les pages suivantes soient identiques, par construction.
    headers = pdf.entete_colonnes
    widths = pdf.largeurs_colonnes
    pdf.set_font(font, "B", 10)
    pdf.set_fill_color(40, 40, 50)
    pdf.set_text_color(255, 255, 255)
    pdf.set_draw_color(80, 80, 80)
    for i, h in enumerate(headers):
        pdf.cell(widths[i], 8, h, border=1, fill=True, align="C")
    pdf.ln()
    pdf.set_text_color(0, 0, 0)

    pdf.set_font(font, "", 9)
    total_qty = 0.0
    for idx, ligne in enumerate(lignes):
        qty = float(ligne.get("quantity") or 0)
        total_qty += qty
        # Le carburant est toujours stocké en gallons (unité de base) : à
        # défaut d'unité connue, l'écrire explicitement plutôt que de laisser
        # un nombre nu sur un ticket qui part par WhatsApp.
        unite = unites.get(ligne.get("product_id")) or ("gls" if est_fuel else "")
        texte_qty = f"{qty:g} {unite}".strip()
        pdf.set_fill_color(*((245, 245, 245) if idx % 2 == 0 else (255, 255, 255)))
        sku = _tronquer_a_la_largeur(pdf, T(str(ligne.get("sku", ""))), widths[0])
        nom = _tronquer_a_la_largeur(pdf, T(str(ligne.get("name", ""))), widths[1])
        pdf.cell(widths[0], 7, sku, border=1, fill=True)
        pdf.cell(widths[1], 7, nom, border=1, fill=True)
        pdf.cell(widths[2], 7, T(texte_qty), border=1, align="R", fill=True)
        pdf.ln()

    pdf.set_font(font, "B", 10)
    pdf.set_fill_color(230, 230, 230)
    pdf.cell(widths[0] + widths[1], 8,
             T(t("Total ({n} ligne(s))").format(n=len(lignes))),
             border=1, fill=True)
    pdf.cell(widths[2], 8, f"{total_qty:g}", border=1, align="R", fill=True)
    pdf.ln()

    # --- Signatures ---
    pdf.ln(25)
    y_sig = pdf.get_y()
    if y_sig > pdf.h - 50:
        pdf.add_page()
        y_sig = pdf.get_y() + 10
    pdf.set_font(font, "", 10)
    pdf.set_draw_color(80, 80, 80)
    pdf.set_xy(20, y_sig)
    pdf.cell(70, 6, T(t("Opérateur : _______________")))
    pdf.set_xy(120, y_sig)
    # Pas de superviseur sur un bon carburant : c'est le receveur qui signe
    # avoir pris les gallons.
    pdf.cell(70, 6, T(t("Receveur : _______________") if est_fuel
                      else t("Superviseur : _______________")))

    # Le pied de page est posé par PdfAvecPiedDePage.footer(), sur chaque page.

    save_path = filedialog.asksaveasfilename(
        defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
        initialfile=nom_fichier_bon(bon, secteur), parent=parent)
    if not save_path:
        return None
    try:
        pdf.output(save_path)
    except OSError as e:
        messagebox.showerror(
            t("Impression"),
            t("Impossible d'enregistrer le PDF :\n{e}").format(e=e))
        return None
    webbrowser.open(f"file:///{save_path.replace(os.sep, '/')}")
    return save_path
