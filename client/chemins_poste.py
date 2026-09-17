"""Emplacement des fichiers du poste, en source comme en exécutable empaqueté.

Nommé "chemins_poste" (et non "chemins") pour ne pas entrer en collision avec
server/chemins.py : les deux modules ne partagent jamais le même processus en
production, mais un nom distinct évite toute ambiguïté, y compris en test.

Une fois gelé par PyInstaller, `Path(__file__)` pointe vers un dossier
temporaire effacé à chaque sortie : la configuration du poste (adresse du
serveur, nom de l'opérateur, clé) serait à ressaisir à chaque lancement.

Elle vit donc dans le dossier utilisateur Windows (`%APPDATA%`), qui survit
aux mises à jour de l'application et reste propre à chaque session Windows.

Un `config.default.json` posé à côté de l'exécutable permet de pré-remplir
l'adresse du serveur au premier lancement, pour éviter de faire saisir une
adresse IP à des utilisateurs non techniques sur chaque poste.
"""
import os
import sys
from pathlib import Path

NOM_APPLICATION = "WarehouseClient"


def est_gele() -> bool:
    return getattr(sys, "frozen", False)


def dossier_utilisateur() -> Path:
    """Dossier des données propres à ce poste et à cet utilisateur."""
    surcharge = os.environ.get("WMS_CLIENT_DIR")
    if surcharge:
        base = Path(surcharge)
    else:
        base = Path(os.environ.get("APPDATA", Path.home())) / NOM_APPLICATION
    base.mkdir(parents=True, exist_ok=True)
    return base


def dossier_application() -> Path:
    """Dossier de l'exécutable (ou du code source en développement)."""
    if est_gele():
        return Path(sys.executable).parent
    return Path(__file__).parent


def chemin_config() -> Path:
    return dossier_utilisateur() / "config.json"


def chemin_config_ancien() -> Path:
    """Ancien emplacement, à côté du code source.

    Conservé uniquement pour reprendre la configuration des postes installés
    avant le déplacement : sans cela, la clé du poste serait perdue et les
    écritures refusées au prochain lancement.
    """
    return Path(__file__).parent / "config.json"


def reprendre_ancienne_config() -> bool:
    """Recopie l'ancienne configuration si la nouvelle n'existe pas encore."""
    nouveau, ancien = chemin_config(), chemin_config_ancien()
    if nouveau.exists() or not ancien.exists() or nouveau == ancien:
        return False
    nouveau.parent.mkdir(parents=True, exist_ok=True)
    nouveau.write_text(ancien.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def chemin_config_defaut() -> Path:
    """Réglages pré-remplis livrés avec l'application, s'ils existent."""
    return dossier_application() / "config.default.json"
