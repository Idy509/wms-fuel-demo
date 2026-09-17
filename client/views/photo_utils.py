"""Affichage et sélection des photos, côté poste.

Partagé par la fiche produit (une photo, remplaçable) et le détail d'un bon
(plusieurs pièces jointes) : les deux écrans ont exactement le même besoin —
choisir un fichier image, en fabriquer une miniature, l'afficher.

Pillow est importé de façon défensive. Il est fourni par CustomTkinter dans
tous les environnements réels, mais ce n'est pas une dépendance déclarée du
paquet : sur un poste où il manquerait, l'application doit continuer à saisir
des bons — une photo absente est un manque de confort, pas une panne de
gestion de stock. `PHOTOS_DISPONIBLES` permet aux écrans de masquer le bouton
plutôt que de faire tomber la vue à l'ouverture.
"""
import io
import logging
from tkinter import filedialog

log = logging.getLogger(__name__)

try:  # pragma: no cover - dépend de l'environnement du poste
    from PIL import Image
    import customtkinter as ctk
    PHOTOS_DISPONIBLES = True
except Exception as e:  # pragma: no cover
    Image = None
    log.warning("Pillow indisponible, photos désactivées : %s", e)
    PHOTOS_DISPONIBLES = False

# Formats acceptés par le serveur. Les proposer ici évite un aller-retour et un
# refus 415 pour un fichier que le magasinier n'aurait jamais dû pouvoir
# choisir. La vérification qui fait foi reste celle du serveur, sur le contenu.
TYPES_FICHIER = [
    ("Images", "*.jpg *.jpeg *.png *.webp"),
    ("JPEG", "*.jpg *.jpeg"),
    ("PNG", "*.png"),
    ("WebP", "*.webp"),
]


def choisir_image(parent=None, titre: str = "Choisir une photo") -> str:
    """Ouvre le sélecteur de fichier. Chaîne vide si l'utilisateur annule."""
    return filedialog.askopenfilename(
        parent=parent, title=titre, filetypes=TYPES_FICHIER) or ""


def miniature(octets: bytes, largeur: int = 160, hauteur: int = 160):
    """Fabrique une image affichable, redimensionnée pour tenir dans le cadre.

    Renvoie None sur n'importe quel problème (Pillow absent, octets tronqués,
    format exotique accepté par le serveur mais illisible ici) : l'appelant
    affiche alors un texte de repli. Une photo illisible ne doit jamais faire
    tomber l'écran qui l'entoure.

    Le rapport largeur/hauteur est préservé — un joint torique étiré ne
    ressemble plus à un joint torique.
    """
    if not PHOTOS_DISPONIBLES or not octets:
        return None
    try:
        image = Image.open(io.BytesIO(octets))
        image.load()  # force le décodage ici, où l'erreur est rattrapée
        copie = image.copy()
        copie.thumbnail((largeur, hauteur))
        # CTkImage plutôt que ImageTk.PhotoImage : c'est lui qui gère la mise
        # à l'échelle des écrans haute densité dans CustomTkinter.
        return ctk.CTkImage(light_image=copie, dark_image=copie,
                            size=copie.size)
    except Exception as e:
        log.warning("photo illisible : %s", e)
        return None
