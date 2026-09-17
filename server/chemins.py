"""Emplacement des fichiers, en source comme en exécutable empaqueté.

`Path(__file__)` ne convient pas une fois l'application gelée par PyInstaller :
`__file__` pointe alors vers `sys._MEIPASS`, un dossier temporaire **effacé à
chaque sortie du programme**. La base de données y serait détruite à chaque
arrêt du serveur, et les sauvegardes avec elle.

Deux notions distinctes :

- `dossier_donnees()` — ce qui doit SURVIVRE au programme (base, sauvegardes,
  journaux, clés de postes). À côté de l'exécutable une fois gelé, à la racine
  du projet en développement.
- `dossier_ressources()` — ce qui est livré AVEC le programme et jamais
  modifié (schema.sql). Dans `sys._MEIPASS` une fois gelé.

Chaque emplacement est surchargeable par variable d'environnement, pour
héberger la base sur un autre disque que l'exécutable (utile pour la
sauvegarde) sans recompiler.
"""
import os
import sys
from pathlib import Path


def est_gele() -> bool:
    """Vrai si l'application tourne depuis un exécutable PyInstaller."""
    return getattr(sys, "frozen", False)


def dossier_donnees() -> Path:
    """Racine des fichiers persistants. Doit survivre à l'arrêt du programme."""
    surcharge = os.environ.get("WMS_DATA_DIR")
    if surcharge:
        return Path(surcharge)
    if est_gele():
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


def dossier_ressources() -> Path:
    """Racine des fichiers livrés en lecture seule avec le programme."""
    if est_gele():
        # _MEIPASS n'existe qu'en mode onefile ; sinon les ressources sont
        # à côté de l'exécutable.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).parent


def chemin_base() -> Path:
    surcharge = os.environ.get("WMS_DB_PATH")
    return Path(surcharge) if surcharge else dossier_donnees() / "data" / "warehouse.db"


# ------------------------------------------------ base sur lecteur réseau

MESSAGE_BASE_RESEAU = (
    "La base de données est placée sur un chemin réseau : {chemin}\n"
    "\n"
    "SQLite en mode WAL ne fonctionne pas de façon fiable sur un partage "
    "réseau (SMB/CIFS, NFS) : le verrouillage de fichier n'y est pas garanti, "
    "et deux écritures simultanées corrompent la base SANS message d'erreur. "
    "La corruption ne se découvre alors que le jour où on relit les données.\n"
    "\n"
    "Le serveur refuse donc de démarrer. Placez la base sur un disque LOCAL "
    "du poste serveur, par exemple en définissant la variable "
    "d'environnement WMS_DATA_DIR (ex. WMS_DATA_DIR=C:\\WMS), puis "
    "relancez.\n"
    "\n"
    "Pour garder une copie hors du poste, utilisez WMS_BACKUP_MIRROR : "
    "les sauvegardes, elles, se copient sans risque sur un partage réseau."
)


def est_chemin_reseau(chemin) -> bool:
    """Vrai pour un chemin UNC explicite (\\\\serveur\\partage\\...).

    Détection volontairement simple et documentée. Elle couvre le seul cas
    réellement courant ici : un administrateur qui pointe WMS_DATA_DIR ou
    WMS_DB_PATH sur `\\\\NAS\\entrepot`.

    NON couvert : une lettre de lecteur RÉSEAU MAPPÉE (`Z:` monté sur un
    partage). La reconnaître demanderait d'interroger l'API Windows
    (`WNetGetConnection` via win32api/ctypes) ou d'analyser la sortie de
    `net use`, deux approches lourdes et fragiles pour un cas de figure rare
    sur ce déploiement — le poste serveur est une machine fixe dont la base
    vit à côté de l'exécutable. Le compromis est assumé : mieux vaut attraper
    le cas fréquent de façon fiable que tous les cas de façon incertaine.
    """
    texte = str(chemin)
    # os.path.splitdrive renvoie ('\\\\serveur\\partage', '\\reste') pour un
    # UNC : plus robuste qu'un test de préfixe seul, il valide au passage que
    # le chemin a bien la forme serveur+partage.
    lecteur, _ = os.path.splitdrive(texte)
    return lecteur.startswith("\\\\") or lecteur.startswith("//") \
        or texte.startswith("\\\\") or texte.startswith("//")


def dossier_sauvegardes() -> Path:
    surcharge = os.environ.get("WMS_BACKUP_DIR")
    return Path(surcharge) if surcharge else dossier_donnees() / "backups"


def dossier_journaux() -> Path:
    surcharge = os.environ.get("WMS_LOG_DIR")
    return Path(surcharge) if surcharge else dossier_donnees() / "logs"


def dossier_photos() -> Path:
    """Images envoyées depuis les postes (fiches produit, pièces jointes aux bons).

    Sur disque et NON dans SQLite, comme les sauvegardes : la base ne porte que
    le nom du fichier. Y stocker les octets ferait grossir un fichier qui est
    copié en entier à chaque sauvegarde et rechargé en entier au démarrage,
    pour des données qui ne sont jamais interrogées en SQL.
    """
    surcharge = os.environ.get("WMS_PHOTO_DIR")
    return Path(surcharge) if surcharge else dossier_donnees() / "photos"


def chemin_schema() -> Path:
    return dossier_ressources() / "schema.sql"


def chemin_version() -> Path:
    """Fichier édité à la main par l'admin à chaque nouvelle version publiée
    (voir GET /version) — pas un fichier généré par le build."""
    surcharge = os.environ.get("WMS_VERSION_FILE")
    return Path(surcharge) if surcharge else dossier_donnees() / "version.json"


def chemin_email_config() -> Path:
    """Réglages SMTP, édités à la main par l'admin (même patron que version.json).

    Fichier ABSENT = envoi d'e-mails désactivé, sans erreur. C'est l'état par
    défaut : aucune installation ne doit dépendre d'un compte mail pour
    fonctionner. Voir `notifications_email.envoyer_email`.
    """
    surcharge = os.environ.get("WMS_EMAIL_CONFIG")
    return Path(surcharge) if surcharge else dossier_donnees() / "email_config.json"


def chemin_cloud_config() -> Path:
    """Réglages de la copie de sauvegarde hors site (stockage compatible S3).

    Fichier ABSENT = copie cloud désactivée, aucune tentative réseau. Voir
    `backup_cloud.televerser_sauvegarde`.
    """
    surcharge = os.environ.get("WMS_CLOUD_CONFIG")
    return Path(surcharge) if surcharge else dossier_donnees() / "cloud_config.json"
