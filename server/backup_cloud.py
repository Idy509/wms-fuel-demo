"""Copie hors site, chiffrée, d'une sauvegarde locale.

Les sauvegardes LOCALES restent en clair : c'est un choix assumé, documenté
en tête de `backup.py` — une archive lisible par n'importe quel outil gzip
vaut mieux, sur un LAN à deux opérateurs, qu'une archive que plus personne ne
sait ouvrir le jour de la panne. Ce module ne change rien à cela.

La copie CLOUD, elle, est chiffrée, et pour une raison qui n'existait pas en
local : le fichier quitte l'entrepôt et atterrit chez un hébergeur tiers. La
clé y est dérivée d'un mot de passe que l'admin a lui-même écrit dans
`cloud_config.json` — pas d'une constante livrée avec le programme comme
dans l'ancien chiffrement local (voir `restaurer_sauvegarde.py`, qui sait
encore déchiffrer ces vieilles archives). Un mot de passe choisi et noté par
l'admin protège vraiment ; une constante embarquée ne protégeait de rien.

Configuration : `chemins.chemin_cloud_config()` (par défaut
`cloud_config.json` à la racine des données), édité à la main :

    {
      "endpoint_url": "https://s3.us-west-000.backblazeb2.com",
      "bucket": "wms-sauvegardes",
      "access_key": "...",
      "secret_key": "...",
      "region": "us-west-000",
      "mot_de_passe": "phrase secrète notée hors ligne",
      "prefixe": "entrepot-central/"
    }

`endpoint_url` rend le module indépendant du fournisseur : n'importe quel
stockage compatible S3 convient (Backblaze B2, DigitalOcean Spaces, Wasabi,
AWS S3 — pour AWS, laisser `endpoint_url` vide). Aucun fournisseur n'est
codé en dur.

ATTENTION, à conserver hors ligne : sans `mot_de_passe`, une archive cloud
est définitivement illisible. La noter ailleurs que sur le serveur (c'est
précisément le serveur qu'on suppose perdu quand on va chercher cette copie).

Fichier de configuration ABSENT = fonction désactivée : renvoie False
immédiatement, aucune tentative réseau, aucune exception. C'est l'état par
défaut de toute installation.
"""
import base64
import json
from pathlib import Path

from chemins import chemin_cloud_config
from journalisation import logger

log = logger(__name__)

# Sans ces champs, aucun envoi n'est possible. `endpoint_url` et `region` sont
# optionnels (AWS S3 les déduit), `prefixe` aussi.
CHAMPS_REQUIS = ("bucket", "access_key", "secret_key", "mot_de_passe")

# Suffixe des objets déposés : l'archive cloud n'est PAS un .gz ordinaire, la
# distinguer évite qu'on la confonde avec une sauvegarde locale restaurable
# directement par 7-Zip.
SUFFIXE_CHIFFRE = ".enc"

# Itérations PBKDF2. 200 000 : le double de l'ancien chiffrement local, la
# dérivation n'a lieu qu'une fois par sauvegarde (toutes les 6 h).
ITERATIONS_KDF = 200_000

# Sel fixe : il ne peut pas être aléatoire sans être stocké quelque part, et
# le stocker à côté de l'archive ne protégerait de rien de plus. La sécurité
# repose ici sur le mot de passe choisi par l'admin, pas sur le sel.
SEL_KDF = b"wms_cloud_backup_v1"

# Au-delà, on refuse d'envoyer : Fernet chiffre en mémoire, et une base
# d'entrepôt fait quelques Mo. Un fichier de cette taille signale une anomalie
# (mauvais chemin passé) qu'il vaut mieux journaliser qu'avaler la RAM.
TAILLE_MAX_OCTETS = 500 * 1024 * 1024


def charger_config() -> dict | None:
    """Réglages cloud, ou None si absents/illisibles/incomplets. Ne lève pas."""
    chemin = chemin_cloud_config()
    try:
        data = json.loads(chemin.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("configuration cloud illisible (%s) : copie hors site désactivée",
                    chemin)
        return None
    if not isinstance(data, dict):
        log.warning("configuration cloud invalide (%s) : copie hors site désactivée",
                    chemin)
        return None
    manquants = [c for c in CHAMPS_REQUIS if not str(data.get(c) or "").strip()]
    if manquants:
        log.warning("configuration cloud incomplète (%s manquant) : copie hors site "
                    "désactivée", ", ".join(manquants))
        return None
    return data


def cloud_actif() -> bool:
    """Vrai si une configuration cloud exploitable est présente."""
    return charger_config() is not None


def chiffrer(donnees: bytes, mot_de_passe: str) -> bytes:
    """Chiffre avec une clé Fernet dérivée du mot de passe (PBKDF2-SHA256)."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=SEL_KDF, iterations=ITERATIONS_KDF)
    cle = base64.urlsafe_b64encode(kdf.derive(mot_de_passe.encode("utf-8")))
    return Fernet(cle).encrypt(donnees)


def dechiffrer(donnees: bytes, mot_de_passe: str) -> bytes:
    """Inverse de `chiffrer`. Sert à la restauration d'une archive cloud."""
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=SEL_KDF, iterations=ITERATIONS_KDF)
    cle = base64.urlsafe_b64encode(kdf.derive(mot_de_passe.encode("utf-8")))
    return Fernet(cle).decrypt(donnees)


def televerser_sauvegarde(chemin_fichier) -> bool:
    """Chiffre puis dépose l'archive sur le stockage compatible S3 configuré.

    Renvoie True seulement si l'objet a été accepté. Ne lève JAMAIS : cette
    fonction est appelée à la fin de `backup.backup_now()`, après qu'une
    sauvegarde locale a déjà réussi. Un hébergeur injoignable ne doit pas
    transformer une sauvegarde réussie en échec.
    """
    chemin = Path(chemin_fichier)

    config = charger_config()
    if config is None:
        return False

    try:
        taille = chemin.stat().st_size
    except OSError:
        log.warning("copie hors site ignorée : fichier introuvable (%s)", chemin)
        return False
    if taille > TAILLE_MAX_OCTETS:
        log.warning("copie hors site ignorée : fichier trop volumineux (%s, %d octets)",
                    chemin.name, taille)
        return False

    try:
        import boto3
    except ImportError:
        log.warning("copie hors site ignorée : boto3 n'est pas installé")
        return False

    try:
        contenu = chemin.read_bytes()
        chiffre = chiffrer(contenu, str(config["mot_de_passe"]))
    except Exception:
        log.exception("échec du chiffrement de la sauvegarde avant envoi hors site")
        return False

    prefixe = str(config.get("prefixe") or "").strip().lstrip("/")
    if prefixe and not prefixe.endswith("/"):
        prefixe += "/"
    cle_objet = f"{prefixe}{chemin.name}{SUFFIXE_CHIFFRE}"

    try:
        # endpoint_url vide = AWS S3 par défaut ; renseigné = n'importe quel
        # autre fournisseur compatible S3.
        endpoint = str(config.get("endpoint_url") or "").strip() or None
        region = str(config.get("region") or "").strip() or None
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=str(config["access_key"]),
            aws_secret_access_key=str(config["secret_key"]),
        )
        client.put_object(Bucket=str(config["bucket"]), Key=cle_objet, Body=chiffre)
    except Exception:
        # Volontairement large : boto3 lève des types qui n'existent pas si
        # l'import a échoué, plus les erreurs réseau/SSL sous-jacentes.
        log.exception("échec de la copie hors site de %s", chemin.name)
        return False

    log.info("sauvegarde copiée hors site : %s (%d octets chiffrés)",
             cle_objet, len(chiffre))
    return True
