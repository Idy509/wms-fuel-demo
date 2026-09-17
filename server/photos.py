"""Stockage des images envoyées depuis les postes.

Sert deux usages qui n'ont en commun QUE le stockage : la photo d'une fiche
produit (une seule, remplaçable) et les pièces jointes d'un bon (plusieurs,
preuve de livraison, colis abîmé). Le reste — permissions, table SQL — est
propre à chacun et vit dans app.py.

Trois règles, toutes défensives :

1. **Le nom du fichier envoyé n'est jamais réutilisé.** Il est choisi par le
   poste, donc par l'utilisateur : `../../data/warehouse.db` ou
   `bon.jpg.exe` y arrivent sans effort. Chaque image reçoit un UUID et une
   extension déduite de son CONTENU. Le nom d'origine ne sert qu'au journal.
2. **Le type est reconnu aux premiers octets, pas à l'extension.** Un exécutable
   renommé `photo.jpg` est refusé — l'extension est une déclaration de
   l'expéditeur, les octets sont un fait.
3. **La taille est bornée AVANT l'écriture disque.** Un envoi de plusieurs Go
   ne doit pas remplir le disque du serveur, base comprise.

Le dossier vient de `chemins.dossier_photos()` : surchargeable par
WMS_PHOTO_DIR, comme la base et les sauvegardes, et jamais figé à l'import —
les tests le redirigent vers un dossier temporaire entre deux appels.
"""
import logging
import re
import uuid
from pathlib import Path

from chemins import dossier_photos

log = logging.getLogger("wms")

# 5 Mo : une photo de téléphone compressée pèse 1 à 3 Mo. Au-delà, c'est une
# image non redimensionnée ou autre chose qu'une photo.
MAX_PHOTO_BYTES = 5 * 1024 * 1024

# Assez d'octets pour couvrir la plus longue signature (WebP : "RIFF" + taille
# sur 4 octets + "WEBP", soit 12).
_OCTETS_SIGNATURE = 12

# Un fichier sous la taille d'une signature ne peut être aucune des trois.
_TAILLE_MIN = _OCTETS_SIGNATURE

EXTENSIONS_ACCEPTEES = ("jpg", "png", "webp")

# Nom généré par nous : 32 caractères hexadécimaux + extension. Sert aussi de
# garde-fou à la relecture — un nom lu en base qui ne colle pas à ce motif
# n'est pas un fichier que nous avons écrit, et n'est pas servi.
_MOTIF_NOM = re.compile(r"^[0-9a-f]{32}\.(?:jpg|png|webp)$")

TYPES_MIME = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


class PhotoInvalide(ValueError):
    """Contenu refusé : trop gros, trop petit, ou pas une image acceptée."""


def detecter_extension(contenu: bytes) -> str | None:
    """Extension déduite des octets de tête, ou None si ce n'est pas une image.

    Volontairement limité à trois formats : ce sont ceux qu'un téléphone ou un
    scanner produit. Élargir la liste élargit la surface d'attaque des
    visionneuses qui ouvriront ces fichiers.
    """
    if not isinstance(contenu, (bytes, bytearray)) or len(contenu) < _TAILLE_MIN:
        return None
    tete = bytes(contenu[:_OCTETS_SIGNATURE])
    if tete.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if tete.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    # WebP : conteneur RIFF, le format est écrit après la taille, pas au début.
    # Tester seulement "RIFF" laisserait passer un WAV ou un AVI.
    if tete.startswith(b"RIFF") and tete[8:12] == b"WEBP":
        return "webp"
    return None


def nom_valide(nom) -> bool:
    """Vrai si `nom` est bien un nom que ce module a généré.

    Appelé avant toute lecture ou suppression : la valeur vient de la base,
    mais une base est un fichier, et un fichier peut avoir été trafiqué.
    Un nom refusé ne mène jamais à un accès disque.
    """
    return isinstance(nom, str) and bool(_MOTIF_NOM.match(nom))


def chemin(nom) -> Path | None:
    """Chemin absolu de l'image, ou None si le nom n'est pas des nôtres.

    Ne vérifie pas l'existence : c'est à l'appelant de distinguer « nom
    invalide » (jamais servir) de « fichier disparu » (404 explicite).
    """
    if not nom_valide(nom):
        return None
    return dossier_photos() / nom


def enregistrer(contenu: bytes, *, nom_origine: str | None = None) -> str:
    """Écrit l'image et renvoie le nom de fichier à stocker en base.

    Lève `PhotoInvalide` si le contenu n'est pas une image acceptée ou dépasse
    la limite. Rien n'est écrit sur disque avant que ces deux contrôles soient
    passés.
    """
    if not contenu:
        raise PhotoInvalide("Fichier vide")
    if len(contenu) > MAX_PHOTO_BYTES:
        raise PhotoInvalide(
            f"Image trop volumineuse (limite {MAX_PHOTO_BYTES // (1024 * 1024)} Mo)")
    extension = detecter_extension(contenu)
    if extension is None:
        raise PhotoInvalide(
            "Format non reconnu : seules les images JPG, PNG et WebP sont "
            "acceptées (le contenu du fichier est vérifié, pas son nom)")

    dossier = dossier_photos()
    dossier.mkdir(parents=True, exist_ok=True)
    nom = f"{uuid.uuid4().hex}.{extension}"
    # Écriture puis remplacement atomique : une coupure en pleine écriture ne
    # laisse pas un fichier tronqué sous un nom déjà enregistré en base.
    temporaire = dossier / f"{nom}.partiel"
    temporaire.write_bytes(contenu)
    temporaire.replace(dossier / nom)
    log.info("photo enregistrée : %s (%d octets, origine %r)",
             nom, len(contenu), nom_origine or "?")
    return nom


def supprimer(nom) -> bool:
    """Efface le fichier du disque. Vrai s'il a bien disparu.

    Ne lève jamais : une photo remplacée dont l'ancien fichier manque déjà ne
    doit pas faire échouer le remplacement. L'orphelin sur disque est un
    gaspillage d'espace, l'échec du remplacement est une panne visible.
    """
    cible = chemin(nom)
    if cible is None:
        log.warning("suppression refusée : nom de photo inattendu %r", nom)
        return False
    try:
        cible.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        log.warning("photo %s non supprimée : %s", nom, e)
        return False


def type_mime(nom) -> str:
    """Type MIME déduit de l'extension du nom que nous avons généré."""
    if not nom_valide(nom):
        return "application/octet-stream"
    return TYPES_MIME.get(str(nom).rsplit(".", 1)[-1], "application/octet-stream")
