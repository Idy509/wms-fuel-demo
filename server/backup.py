"""Sauvegardes automatiques de la base.

Une sauvegarde est une simple archive gzip de la base SQLite :
`warehouse_AAAAMMJJ_HHMMSS.db.gz`. Pas de chiffrement.

Pourquoi pas de chiffrement : la seule clé possible aurait dû être livrée
avec l'application ou saisie à chaque démarrage. Livrée, elle ne protège de
rien ; saisie, elle rend la restauration impossible le jour où personne ne
s'en souvient — c'est-à-dire le jour où on en a besoin. Sur un LAN d'entrepôt
à deux opérateurs, sans contrainte réglementaire, une archive lisible par
n'importe quel outil vaut mieux qu'une archive illisible par tout le monde.

La copie HORS SITE, elle, est chiffrée — voir `backup_cloud.py`. La nuance
tient au lieu : une archive qui reste dans l'entrepôt est déjà protégée par
l'accès physique au poste, une archive déposée chez un hébergeur tiers ne
l'est plus. Sa clé vient d'un mot de passe écrit par l'admin dans
`cloud_config.json`, pas d'une constante livrée avec le programme. Cette
copie est optionnelle et désactivée tant que ce fichier n'existe pas.

Une archive se restaure avec `restaurer_sauvegarde.py`, ou à la main avec
n'importe quel outil gzip (7-Zip, `gzip -d`) — le fichier obtenu est
directement une base SQLite.
"""
import gzip
import os
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from chemins import dossier_sauvegardes
from database import DB_PATH
from journalisation import logger

log = logger(__name__)

BACKUP_DIR = dossier_sauvegardes()

# Répertoire secondaire optionnel (disque externe, partage réseau) où copier
# chaque sauvegarde. Sans ça, les sauvegardes vivent sur le même disque que la
# base : une panne du disque emporte les deux. Défini via variable
# d'environnement pour rester optionnel (aucun réglage ne doit empêcher le
# serveur de démarrer sur un poste qui n'a pas ce miroir).
_mirror_env = os.environ.get("WMS_BACKUP_MIRROR", "").strip()
BACKUP_MIRROR_DIR = Path(_mirror_env) if _mirror_env else None

# Dernier statut de la copie miroir, exposé par /health. "disabled" tant que
# WMS_BACKUP_MIRROR n'est pas défini ou qu'aucune sauvegarde n'a encore eu
# lieu.
_dernier_statut_miroir = "disabled" if BACKUP_MIRROR_DIR is None else "en_attente"
RETENTION_DAYS = 90
# Toutes les 6 heures : 4 sauvegardes par jour suffisent largement pour un
# entrepôt à 2 opérateurs, et ça limite l'accumulation de fichiers.
INTERVAL_SECONDS = 6 * 3600
# Plafond de taille du dossier de sauvegardes. Au-delà, les plus anciennes
# sont supprimées même si elles sont encore dans la fenêtre de rétention.
MAX_TOTAL_BYTES = 500 * 1024 * 1024  # 500 Mo

# ATTENTION : ces sauvegardes vivent sur le même disque que la base. Elles
# protègent d'une erreur de saisie ou d'une corruption logique, PAS d'une
# panne du disque. Copier régulièrement le dossier de sauvegardes sur un
# disque externe ou un partage réseau reste indispensable.

# Toutes les archives : le format actuel (warehouse_*.db.gz) comme les
# archives chiffrées d'anciennes versions (warehouse_*.enc.db.gz). Le motif
# large évite qu'une ancienne archive échappe à la rétention et au plafond
# de taille.
MOTIF_ARCHIVES = "warehouse_*.gz"

# Extension de l'archive en cours d'écriture. Elle ne correspond pas à
# MOTIF_ARCHIVES : une archive incomplète ne doit jamais être proposée à la
# restauration ni comptée comme une sauvegarde valide.
SUFFIXE_PARTIEL = ".part"


def _horodatage(fichier: Path) -> datetime | None:
    """Date portée par le nom de fichier, quel que soit le format d'archive."""
    nom = fichier.name
    if not nom.startswith("warehouse_"):
        return None
    try:
        return datetime.strptime(nom[len("warehouse_"):len("warehouse_") + 15],
                                 "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _verifier_integrite(conn: sqlite3.Connection) -> None:
    """Lève si la connexion ne passe pas PRAGMA integrity_check.

    Fonction séparée pour rester testable sans avoir à fabriquer une base
    SQLite réellement corrompue (backup() copie les pages telles quelles,
    donc une corruption qui trompe `integrity_check` est difficile à
    reproduire fidèlement en test — mais le CHEMIN qui doit échouer
    proprement, lui, se vérifie directement sur cette fonction).
    """
    verif = conn.execute("PRAGMA integrity_check").fetchone()
    if verif is None or verif[0] != "ok":
        raise RuntimeError(
            f"Sauvegarde abandonnée : la copie ne passe pas "
            f"PRAGMA integrity_check ({verif[0] if verif else 'vide'}). "
            f"La base source pourrait être corrompue — investiguer "
            f"avant toute nouvelle tentative."
        )


def backup_now() -> Path:
    """Sauvegarde à chaud via l'API backup de SQLite, compressée en gzip.

    Copier le fichier .db à la main pendant que le serveur tourne produit une
    copie incohérente : les transactions récentes vivent dans le fichier -wal.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / f"warehouse_{stamp}.db"
    archive = BACKUP_DIR / f"warehouse_{stamp}.db.gz"
    # L'archive est d'abord écrite sous un nom temporaire, puis renommée.
    # Sans ça, un processus tué pendant la compression (coupure de courant,
    # ARRETER.bat, fermeture de la fenêtre serveur) laissait un
    # `warehouse_*.db.gz` tronqué, impossible à distinguer d'une archive
    # complète : la restauration ne l'aurait découvert que le jour où on en a
    # besoin. Le renommage, lui, est atomique.
    partielle = BACKUP_DIR / f"warehouse_{stamp}.db.gz{SUFFIXE_PARTIEL}"

    # `target` est un fichier intermédiaire transitoire du début à la fin de
    # cette fonction : ce try/finally garantit sa suppression qu'on sorte par
    # succès, par échec de l'intégrité, ou par échec de la compression — un
    # seul endroit responsable, plutôt que de dupliquer le nettoyage sur
    # chaque chemin de sortie.
    try:
        src = sqlite3.connect(DB_PATH, timeout=30)
        dst = sqlite3.connect(target)
        try:
            # Sans busy_timeout, une écriture concurrente au moment de la
            # sauvegarde fait échouer l'API backup sur « database is locked ».
            src.execute("PRAGMA busy_timeout = 30000")
            with dst:
                src.backup(dst)
            # `sqlite3.backup()` copie les pages telles quelles, corrompues
            # ou non : une corruption logique silencieuse de la base de
            # production (bit rot disque, coupure pendant une écriture non
            # protégée ailleurs) ne fait PAS échouer l'API backup
            # elle-même. Sans ce contrôle, une telle corruption serait
            # fidèlement archivée chaque jour pendant 90 jours
            # (RETENTION_DAYS) sans qu'aucune alerte ne soit levée —
            # découverte seulement le jour de la restauration, exactement
            # le scénario que cette sauvegarde existe pour éviter. Repéré
            # par audit de fiabilité le 9 septembre 2026.
            _verifier_integrite(dst)
        finally:
            dst.close()
            src.close()

        try:
            with open(target, "rb") as f_in, gzip.open(partielle, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.replace(partielle, archive)
        except BaseException:
            partielle.unlink(missing_ok=True)
            raise
    finally:
        target.unlink(missing_ok=True)

    _purge_temoins_obsoletes()
    _purge_archives_partielles()
    _purge_copies_orphelines()
    _purge_anciennes()
    _purge_si_trop_volumineux()
    _copier_vers_miroir(archive)
    _copier_vers_cloud(archive)
    return archive


# Dernier statut de la copie hors site, exposé par /health, même logique que
# le miroir : "disabled" tant qu'aucun cloud_config.json n'est présent.
_dernier_statut_cloud = "disabled"


def _copier_vers_cloud(archive: Path) -> None:
    """Copie chiffrée vers le stockage hors site, si configuré.

    Comme le miroir, ne doit JAMAIS faire échouer la sauvegarde locale : elle
    est déjà écrite et valide quand on arrive ici. `televerser_sauvegarde` ne
    lève pas, mais le try reste : un import raté (module absent d'un build
    PyInstaller mal déclaré) ne doit pas non plus casser la sauvegarde.
    """
    global _dernier_statut_cloud
    try:
        from backup_cloud import cloud_actif, televerser_sauvegarde

        if not cloud_actif():
            _dernier_statut_cloud = "disabled"
            return
        _dernier_statut_cloud = "ok" if televerser_sauvegarde(archive) else "failed"
    except Exception:
        log.exception("échec de la copie hors site de la sauvegarde")
        _dernier_statut_cloud = "failed"


def statut_cloud() -> str:
    """Dernier statut connu de la copie hors site : ok / failed / disabled."""
    return _dernier_statut_cloud


def _copier_vers_miroir(archive: Path) -> None:
    """Copie l'archive vers WMS_BACKUP_MIRROR si défini.

    Ne doit jamais faire échouer la sauvegarde principale : un miroir
    inaccessible (disque externe débranché, partage réseau hors ligne) est
    journalisé et remonté dans le statut, pas levé.
    """
    global _dernier_statut_miroir
    if BACKUP_MIRROR_DIR is None:
        _dernier_statut_miroir = "disabled"
        return
    try:
        BACKUP_MIRROR_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, BACKUP_MIRROR_DIR / archive.name)
        _dernier_statut_miroir = "ok"
    except OSError:
        log.exception("échec de la copie de sauvegarde vers le miroir (%s)", BACKUP_MIRROR_DIR)
        _dernier_statut_miroir = "failed"


def derniere_sauvegarde_locale() -> datetime | None:
    """Horodatage de la sauvegarde locale complète la plus récente, ou None.

    Lue depuis le NOM des archives et non depuis leur mtime : une copie du
    dossier de sauvegardes (vers un disque externe, puis retour) réécrit les
    dates de fichier et ferait passer de vieilles archives pour fraîches.
    Les archives `.part` sont ignorées — elles ne sont pas des sauvegardes.
    """
    dates = []
    try:
        fichiers = list(BACKUP_DIR.glob(MOTIF_ARCHIVES))
    except OSError:
        log.exception("dossier de sauvegardes illisible (%s)", BACKUP_DIR)
        return None
    for f in fichiers:
        horodatage = _horodatage(f)
        if horodatage is not None:
            dates.append(horodatage)
    return max(dates) if dates else None


# Au-delà de ce délai sans sauvegarde locale réussie, l'administrateur est
# averti. 48 h et non 24 : le planificateur tourne toutes les 6 h, mais un
# poste serveur éteint tout un week-end ne doit pas déclencher une alarme le
# lundi matin alors que rien n'est cassé. Passé deux jours, en revanche, la
# sauvegarde ne tourne réellement plus.
SEUIL_SAUVEGARDE_PERIMEE_H = 48


def statut_sauvegarde() -> dict:
    """État des sauvegardes, pour l'endpoint d'administration.

    `perimee` vaut True aussi quand AUCUNE sauvegarde n'existe : c'est le cas
    le plus grave, il ne doit pas passer pour « rien à signaler ».
    """
    derniere = derniere_sauvegarde_locale()
    if derniere is None:
        age_heures = None
        perimee = True
    else:
        age_heures = (datetime.now() - derniere).total_seconds() / 3600
        perimee = age_heures > SEUIL_SAUVEGARDE_PERIMEE_H
    return {
        "derniere_locale": derniere.strftime("%Y-%m-%d %H:%M:%S") if derniere else None,
        "age_heures": round(age_heures, 1) if age_heures is not None else None,
        "perimee": perimee,
        "seuil_heures": SEUIL_SAUVEGARDE_PERIMEE_H,
        "statut_miroir": statut_miroir(),
        "statut_cloud": statut_cloud(),
        "dossier": str(BACKUP_DIR),
    }


def statut_miroir() -> str:
    """Dernier statut connu de la copie vers le miroir : ok / failed / disabled / en_attente."""
    return _dernier_statut_miroir


def _purge_temoins_obsoletes() -> None:
    """Retire les fichiers .keycheck laissés par les versions chiffrées.

    Ils ne servent plus à rien depuis l'abandon du chiffrement, mais restent
    sur les installations mises à jour.
    """
    for f in BACKUP_DIR.glob("warehouse_*.keycheck"):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            continue


DELAI_ORPHELIN = timedelta(hours=24)


def _purge_archives_partielles() -> None:
    """Supprime les archives `.gz.part` d'une compression interrompue.

    Même seuil de 24 h que les copies orphelines : plus récent, le fichier
    peut appartenir à une sauvegarde en cours (déclenchée à la main pendant
    que le planificateur travaille).
    """
    limite = datetime.now() - DELAI_ORPHELIN
    for f in BACKUP_DIR.glob(f"warehouse_*{SUFFIXE_PARTIEL}"):
        horodatage = _horodatage(f)
        if horodatage is None or horodatage >= limite:
            continue
        try:
            f.unlink(missing_ok=True)
        except OSError:
            continue
        log.info("archive incomplète supprimée : %s", f.name)


def _purge_copies_orphelines() -> None:
    """Supprime les copies .db non compressées laissées par un backup interrompu.

    backup_now() écrit d'abord `warehouse_*.db` puis le compresse en
    `warehouse_*.db.gz`. Si le processus est tué entre les deux (coupure de
    courant, arrêt du service), le .db reste sur le disque. MOTIF_ARCHIVES ne
    capture que les `.gz` : ce fichier échappait donc à la rétention comme au
    plafond de taille, et pouvait faire grossir le dossier indéfiniment.

    Seuil de 24 h : une copie plus récente peut appartenir à un backup en
    cours de compression, qu'il ne faut surtout pas supprimer sous ses pieds.
    """
    limite = datetime.now() - DELAI_ORPHELIN
    for f in BACKUP_DIR.glob("warehouse_*.db"):
        horodatage = _horodatage(f)
        if horodatage is None or horodatage >= limite:
            continue
        try:
            f.unlink(missing_ok=True)
        except OSError:
            continue
        log.info("copie de sauvegarde orpheline supprimée : %s", f.name)


def _purge_anciennes() -> None:
    """Supprime les archives plus vieilles que RETENTION_DAYS.

    Un plancher protège toujours la plus récente, même si elle a elle aussi
    dépassé RETENTION_DAYS (poste éteint longtemps, horloge système
    remontée) — même garantie que `_purge_si_trop_volumineux`, qui ne
    supprime jamais non plus la dernière. Sans lui, cette fonction
    pouvait en théorie vider entièrement le dossier de sauvegardes ; en
    pratique `backup_now()` l'appelle toujours juste après avoir créé une
    archive fraîche, donc le risque était resté contenu par l'ordre
    d'appel plutôt que par une garantie de la fonction elle-même — repéré
    par audit de fiabilité le 9 septembre 2026.
    """
    limite = datetime.now() - timedelta(days=RETENTION_DAYS)
    fichiers = sorted(
        (f for f in BACKUP_DIR.glob(MOTIF_ARCHIVES) if _horodatage(f) is not None),
        key=lambda f: f.name,
    )
    for f in fichiers[:-1]:
        horodatage = _horodatage(f)
        if horodatage < limite:
            f.unlink(missing_ok=True)


def _purge_si_trop_volumineux() -> None:
    """Supprime les sauvegardes les plus anciennes tant que le dossier dépasse
    MAX_TOTAL_BYTES. La plus récente n'est jamais supprimée."""
    fichiers = []
    for f in BACKUP_DIR.glob(MOTIF_ARCHIVES):
        if _horodatage(f) is None:
            continue
        try:
            fichiers.append((f, f.stat().st_size))
        except OSError:
            continue
    # Du plus ancien au plus récent (le nom porte l'horodatage).
    fichiers.sort(key=lambda item: item[0].name)
    total = sum(taille for _, taille in fichiers)
    for f, taille in fichiers[:-1]:
        if total <= MAX_TOTAL_BYTES:
            break
        try:
            f.unlink(missing_ok=True)
        except OSError:
            continue
        total -= taille
        log.info("sauvegarde supprimée (plafond de taille atteint) : %s", f.name)


def _boucle(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            chemin = backup_now()
            log.info("sauvegarde créée : %s", chemin)
        except Exception:
            # exception() capture la trace complète : une sauvegarde qui
            # échoue silencieusement est le pire des deux mondes.
            log.exception("échec de la sauvegarde")
        stop_event.wait(INTERVAL_SECONDS)


_thread_planificateur: threading.Thread | None = None


def demarrer_planificateur() -> threading.Event:
    """Lance la sauvegarde périodique en tâche de fond. Renvoie le drapeau d'arrêt."""
    global _thread_planificateur
    stop_event = threading.Event()
    thread = threading.Thread(target=_boucle, args=(stop_event,), daemon=True)
    thread.start()
    _thread_planificateur = thread
    return stop_event


# Une sauvegarde d'une base d'entrepôt (quelques Mo) prend moins d'une seconde ;
# 20 s couvrent largement un disque lent ou un miroir réseau qui traîne.
DELAI_ARRET_PLANIFICATEUR = 20


def attendre_planificateur(timeout: float = DELAI_ARRET_PLANIFICATEUR) -> bool:
    """Attend la fin de la sauvegarde en cours à l'arrêt du serveur.

    Le thread est `daemon=True` : sans cette attente, l'interpréteur se termine
    en le coupant net, éventuellement au milieu de la compression. Renvoie True
    si le thread s'est arrêté dans le délai imparti.
    """
    global _thread_planificateur
    thread = _thread_planificateur
    if thread is None or not thread.is_alive():
        _thread_planificateur = None
        return True
    thread.join(timeout)
    if thread.is_alive():
        log.warning("le planificateur de sauvegarde ne s'est pas arrêté en %ss", timeout)
        return False
    _thread_planificateur = None
    return True


if __name__ == "__main__":
    print(backup_now())
