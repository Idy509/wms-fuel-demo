import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import (BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query,
                     Request, UploadFile)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response

import backup
from backup import attendre_planificateur, demarrer_planificateur
from database import get_conn, init_db
from horodatage import FORMAT as FORMAT_DATETIME
from horodatage import maintenant, maintenant_texte, normaliser
from indicateurs import (
    activite_operateurs, anomalies_mouvements, comptages_a_temps,
    concentration_regions, consommation_regionale, delai_fournisseurs,
    dette_consigne, ecarts_reception, precision_inventaire, produits_dormants,
    resolution_alertes,
)
from journalisation import configurer as configurer_journalisation
from journalisation import logger
from locations import (
    ajuster_ouverture, attend_confirmation_reception, enregistrer_mouvements,
    enregistrer_ouverture, est_transfert_inter_regional, est_transfert_regional,
    id_entrepot_regional, solde_regional_detail, stock_entrepot_regional,
    stock_par_emplacement,
)
from excel_io import (
    build_audit_log_excel,
    build_comptable_excel,
    build_documents_excel,
    build_reappro_excel,
    build_regional_inventaire_excel,
    build_stock_excel,
    export_filename,
    parse_products_excel,
)
from notifications_email import (destinataires_alertes, digest_actif, envoyer_email,
                                  envoyer_en_arriere_plan, mode_alertes)
from models import (
    AdjustmentIn, BottleReturnIn, CancelIn, ContactIn, ContactUpdate,
    DocumentIn, DocumentOut, LoginIn, PasswordChangeIn,
    ProductIn, ProductOut, ProductUpdate, ReceptionConfirmIn,
    RegionalAlertSignalIn,
    RegionalInventoryIn, RegionalTransferIn, RegionNoteIn, RlReturnIn,
    SECTEUR_DEFAUT,
    StockDateIn,
    ThresholdUpdate,
    UserIn, UserOut,
    UserRegionsIn, UserUpdate, erreur_mot_de_passe, est_admin_multi_secteurs,
    seuil_ecart_important,
)
from utilisateurs import (
    consommer_ticket_ws, creer_session, creer_ticket_ws, exiger_admin,
    exiger_ecriture, exiger_utilisateur, fermer_session, hacher_jeton,
    hacher_mot_de_passe,
    invalider_cache_sessions_utilisateur,
    nettoyer_sessions_expirees, role_de_session, session_valide,
    utilisateur_courant, utilisateurs_existent, verifier_mot_de_passe,
)
import photos
from board_routes import router as board_router
from precision import arrondir
from qualite_referentiel import DEFAUT_LIBELLES, analyser_produits
from reference_data import (
    FUEL_PROJECTS, REGION_SUPERVISORS, REGIONS, REGIONS_AVEC_ENTREPOT,
    SECTEURS_AVEC_REGION, TECHNICIANS,
    a_entrepot_regional, normaliser_region, region_valide_pour_secteur,
    regions_avec_entrepot_for, regions_for, supervisor_est_valide,
    supervisors_for, technicians_for, technicien_valide_pour_secteur,
    warehouse_operators_for,
)
from websocket_manager import (
    broadcast_alert_triggered,
    broadcast_document_cancelled,
    broadcast_document_created,
    broadcast_regional_alert_signaled,
    broadcast_stock_update,
    broadcast_thresholds_updated,
    enregistrer_boucle,
    manager,
    planifier,
)

_MODE_DEV = bool(os.getenv("WMS_DEV"))
_DEMO_MODE = os.environ.get("WMS_DEMO_MODE") == "1"
log = logger(__name__)

# --- Anti brute-force login ---
#
# Les échecs sont comptés EN BASE (table `login_attempts`), plus en mémoire :
# un dictionnaire de processus se vidait à chaque redémarrage du serveur, et
# il suffisait donc d'attendre (ou de provoquer) un redémarrage pour repartir
# avec un compteur neuf. La table existait déjà, purgée périodiquement, mais
# rien ne l'alimentait.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300  # 5 minutes, pour la première série de 5 échecs
# Verrou plafonné : au-delà, un attaquant pourrait rendre un compte
# indisponible pour la journée entière en tapant cinq mots de passe de plus.
LOGIN_LOCKOUT_MAX_SECONDS = 3600  # 1 heure
# Fenêtre glissante de comptage. Passé ce délai sans nouvel échec, la série
# repart de zéro : un magasinier qui se trompe une fois par mois ne doit
# jamais se retrouver verrouillé.
LOGIN_FENETRE_SERIE_SECONDES = LOGIN_LOCKOUT_MAX_SECONDS
# Conservé pour le seul usage qu'il ait encore : les tests vident cette
# structure entre deux cas. Le verrouillage ne s'y appuie plus.
_login_attempts: dict[str, dict] = {}


# Empreinte d'un mot de passe qui n'appartient à personne. Jouée quand
# l'identifiant est inconnu, pour que la réponse coûte le même temps que sur
# un compte existant : sans elle, une réponse instantanée disait « cet
# identifiant n'existe pas » aussi sûrement qu'un message dédié.
_EMPREINTE_FACTICE = hacher_mot_de_passe(secrets.token_urlsafe(32))


def _login_verrou_restant(conn, username: str) -> float:
    """Secondes de verrouillage restantes pour cet identifiant, 0 si libre.

    Le verrou double à chaque nouvelle série de LOGIN_MAX_ATTEMPTS échecs
    (5 min, 10, 20, 40, puis plafonné à 1 h) : un verrou fixe de 5 minutes se
    contournait en attendant simplement 5 minutes entre chaque salve.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "       (julianday('now', 'localtime') - julianday(MAX(attempt_time))) * 86400 "
        "           AS depuis "
        "FROM login_attempts "
        "WHERE username = ? COLLATE NOCASE "
        "  AND attempt_time > datetime('now', 'localtime', ?)",
        (username, f"-{LOGIN_FENETRE_SERIE_SECONDES} seconds"),
    ).fetchone()
    nb = row["n"] or 0
    if nb < LOGIN_MAX_ATTEMPTS:
        return 0.0
    series = nb // LOGIN_MAX_ATTEMPTS
    verrou = min(LOGIN_LOCKOUT_SECONDS * (2 ** (series - 1)),
                 LOGIN_LOCKOUT_MAX_SECONDS)
    depuis = row["depuis"]
    if depuis is None:
        return 0.0
    restant = verrou - float(depuis)
    return restant if restant > 0 else 0.0


def _refuser_si_verrouille(username: str) -> None:
    """Lève 429 si l'identifiant est sous le coup d'un verrouillage."""
    with get_conn() as conn:
        restant = _login_verrou_restant(conn, username)
    if restant > 0:
        minutes = int(restant // 60) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Trop de tentatives, réessaie dans {minutes} minute(s)",
        )


def _register_login_failure(username: str, ip: Optional[str] = None) -> None:
    """Enregistre un échec d'authentification pour cet identifiant.

    Appelée sur TOUS les chemins d'échec : identifiant inconnu, mot de passe
    faux, et vérification de l'ancien mot de passe sur /me/password. Un échec
    qui n'était pas compté était un échec gratuit pour qui cherche un mot de
    passe.

    N'exceptionne jamais : ne pas pouvoir écrire la trace ne doit pas
    transformer un 401 lisible en 500.
    """
    try:
        with get_conn(write=True) as conn:
            conn.execute(
                "INSERT INTO login_attempts (username, ip) VALUES (?, ?)",
                (username, ip),
            )
            nb = conn.execute(
                "SELECT COUNT(*) AS n FROM login_attempts "
                "WHERE username = ? COLLATE NOCASE "
                "  AND attempt_time > datetime('now', 'localtime', ?)",
                (username, f"-{LOGIN_FENETRE_SERIE_SECONDES} seconds"),
            ).fetchone()["n"]
        if nb and nb % LOGIN_MAX_ATTEMPTS == 0:
            log.warning("compte %r verrouillé après %d tentatives", username, nb)
    except Exception:
        log.exception("échec d'enregistrement d'une tentative de connexion")

# --- Rate limiter ---
RATE_LIMIT_MAX = 60  # requêtes par minute
RATE_LIMIT_WINDOW = 60  # secondes
_rate_limits: dict[str, list[float]] = defaultdict(list)
# Nombre d'adresses suivies simultanément. Sur un réseau local de confiance, ce
# dictionnaire ne dépassait jamais une dizaine d'entrées et rien ne le bornait.
# Exposé à Internet (déploiement multi-sites), il grandit d'une entrée par
# adresse source qui tente une écriture : un balayage automatisé, ou n'importe
# quel client sur une plage IPv6, en crée des milliers entre deux passages du
# nettoyage (toutes les 5 minutes). Au-delà du plafond, on élague d'abord les
# entrées expirées ; si cela ne suffit pas, la plus ancienne cède la place.
RATE_LIMIT_MAX_IPS = 10_000

# Plafond séparé, beaucoup plus bas, pour les exports (chemins contenant
# « /export »). Ce sont des GET — donc hors du plafond d'écriture ci-dessus —
# mais ce sont aussi les requêtes les plus coûteuses du serveur (le classeur
# Excel est construit ENTIÈREMENT en mémoire) et les plus sensibles
# (`/documents/export` = tout l'historique, `/reports/comptable/export` = les
# coûts d'achat). Sans plafond, un jeton volé exfiltrait tout en boucle et
# saturait la machine au passage. 10/minute laisse largement de quoi
# travailler : un opérateur exporte quelques fois par jour, pas dix fois par
# minute.
RATE_LIMIT_EXPORT_MAX = 10
_rate_limits_export: dict[str, list[float]] = defaultdict(list)


def _journaliser_audit(conn, entity_type: str, entity_id: int, action: str,
                       *, new: dict, old: dict | None = None,
                       username: str = "système", sector: str | None = None) -> None:
    """Trace une écriture sensible dans audit_log.

    Centralise ce que 9 endpoints construisaient chacun à la main (import
    json local compris) : un SQL à 5 ou 6 paramètres dupliqué invite à
    l'incohérence dès qu'un seul site est corrigé sans les autres.

    `sector` cloisonne la trace : sans lui, l'écran « Journal d'audit »
    montrait à l'admin de n'importe quel secteur les écritures de tous les
    autres (migration 54). `None` reste permis pour une trace SYSTÈME qui
    n'appartient à aucun secteur (changement de station, export) — elle
    demeure alors visible de tous les admins, ce qui est le comportement
    voulu pour ce type d'entrée.
    """
    if old is not None:
        conn.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, old_values, "
            "new_values, username, sector) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entity_type, entity_id, action, json.dumps(old, ensure_ascii=False),
             json.dumps(new, ensure_ascii=False), username, sector),
        )
    else:
        conn.execute(
            "INSERT INTO audit_log (entity_type, entity_id, action, new_values, "
            "username, sector) VALUES (?, ?, ?, ?, ?, ?)",
            (entity_type, entity_id, action, json.dumps(new, ensure_ascii=False),
             username, sector),
        )


def _journaliser_export(type_export: str, user, **filtres) -> None:
    """Trace le TÉLÉCHARGEMENT d'un export dans le journal d'audit.

    Un export sort de l'application des données que rien ne rattrape ensuite :
    le classeur comptable porte les coûts d'achat ligne à ligne, l'export du
    journal d'audit nomme qui a fait quoi sur chaque compte. Jusqu'ici, aucun
    de ces téléchargements ne laissait la moindre trace — on pouvait vider
    l'historique de l'entrepôt dans un fichier sans que personne ne puisse
    dire, après coup, qui l'avait fait ni quand.

    Seule l'ACTION est journalisée, avec ses filtres (période, région, type) :
    recopier le contenu exporté dans `audit_log` doublerait la base sans rien
    apprendre de plus.

    Aucune exception ne remonte : un journal d'audit indisponible ne doit
    jamais empêcher un magasinier de sortir son fichier. L'échec est signalé
    dans les logs techniques.
    """
    retenus = {k: v for k, v in filtres.items() if v not in (None, "")}
    try:
        with get_conn(write=True) as conn:
            _journaliser_audit(
                conn, "export", 0, type_export,
                new={"export": type_export, "filtres": retenus},
                username=(user or {}).get("username") or "système",
                # L'export appartient au secteur de celui qui l'a téléchargé :
                # c'est SON catalogue ou SES bons qui sont sortis de l'app.
                sector=_secteur_du_compte(conn, user) if user else None)
    except Exception:
        log.exception("export %s : journalisation d'audit impossible", type_export)


def _cleanup_rate_limits():
    """Nettoie les entrées expirées des deux compteurs (écritures et exports)."""
    seuil = time.monotonic() - RATE_LIMIT_WINDOW
    for compteurs in (_rate_limits, _rate_limits_export):
        a_supprimer = [ip for ip, ts in compteurs.items()
                       if all(t < seuil for t in ts)]
        for ip in a_supprimer:
            del compteurs[ip]


# Rétention des journaux techniques. Aucune purge ne touche stock_movements
# ni documents : ce sont les registres comptables de l'entrepôt, ils doivent
# rester complets indéfiniment.
RETENTION_AUDIT_MOIS = 24
RETENTION_LOGIN_MOIS = 6
INTERVALLE_PURGE_SECONDES = 24 * 3600

_derniere_purge: float = 0.0


def _purger_historiques() -> dict[str, int]:
    """Supprime les entrées de journal au-delà de la rétention.

    Ne concerne QUE les journaux techniques (audit_log, login_history,
    login_attempts). Les mouvements de stock et les bons ne sont jamais purgés.
    """
    supprimes = {"audit_log": 0, "login_history": 0, "login_attempts": 0}
    try:
        with get_conn(write=True) as conn:
            cur = conn.execute(
                "DELETE FROM audit_log "
                "WHERE created_at < datetime('now', 'localtime', ?)",
                (f"-{RETENTION_AUDIT_MOIS} months",),
            )
            supprimes["audit_log"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM login_history "
                "WHERE created_at < datetime('now', 'localtime', ?)",
                (f"-{RETENTION_LOGIN_MOIS} months",),
            )
            supprimes["login_history"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM login_attempts "
                "WHERE attempt_time < datetime('now', 'localtime', ?)",
                (f"-{RETENTION_LOGIN_MOIS} months",),
            )
            supprimes["login_attempts"] = cur.rowcount
    except Exception:
        # Une purge d'entretien ne doit jamais interrompre la boucle de
        # nettoyage : le serveur continue, l'incident est journalisé.
        log.exception("purge des historiques échouée")
        return supprimes
    if any(supprimes.values()):
        log.info("purge historiques : %s", supprimes)
    return supprimes


async def _periodic_cleanup():
    """Nettoyage périodique des structures en mémoire."""
    global _derniere_purge
    while True:
        await asyncio.sleep(300)
        _cleanup_rate_limits()
        # Rien à élaguer côté brute-force : les tentatives vivent désormais
        # dans `login_attempts` (base), purgée par `_purger_historiques` avec
        # les autres journaux techniques.
        # Sessions expirées : sans ce passage, la table `sessions` ne se vidait
        # qu'au démarrage du serveur, qui tourne des semaines d'affilée.
        try:
            nettoyer_sessions_expirees()
        except Exception:
            log.exception("nettoyage des sessions expirées échoué")
        # Purge des journaux : une fois par jour au plus, le coût étant sans
        # rapport avec le cycle de 5 minutes.
        if now - _derniere_purge >= INTERVALLE_PURGE_SECONDES:
            _derniere_purge = now
            _purger_historiques()
        # Contrôle d'intégrité du grand livre, une fois par jour : la fonction
        # porte elle-même son échéance et n'a donc rien à faire les autres
        # tours. Elle n'exceptionne jamais — voir sa docstring.
        verifier_reconciliation_planifiee()
        # Résumé quotidien des alertes. Appelé APRÈS la réconciliation pour
        # que le contrôle du jour figure dans le résumé du jour et non dans
        # celui du lendemain. Sans effet en mode immédiat (le défaut) : la
        # fonction sort tout de suite. Elle n'exceptionne jamais non plus.
        envoyer_digest_quotidien_planifie()
        # Rien à nettoyer côté WebSocket : uvicorn assure lui-même le
        # heartbeat protocolaire (ws_ping_interval / ws_ping_timeout). Une
        # socket dont le pong n'arrive pas est fermée par la couche transport,
        # ce qui déclenche la WebSocketDisconnect de l'endpoint /ws et donc
        # manager.disconnect(). Un nettoyage applicatif par horodatage ferait
        # doublon et retirerait à tort les sessions ouvertes mais silencieuses.


# Très au-dessus d'un catalogue réel (quelques centaines de Ko) : sans cette
# borne, un fichier de plusieurs gigaoctets suffirait à épuiser la mémoire du
# serveur avant même d'être validé.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


async def _lire_fichier_borne(file: UploadFile, limite: int = MAX_UPLOAD_BYTES) -> bytes:
    """Lit un fichier envoyé par morceaux, sans jamais dépasser `limite` en mémoire."""
    morceaux: list[bytes] = []
    taille = 0
    while True:
        morceau = await file.read(1024 * 1024)
        if not morceau:
            break
        taille += len(morceau)
        if taille > limite:
            log.warning("upload refusé : fichier %r dépasse %d Mo",
                        file.filename, limite // (1024 * 1024))
            raise HTTPException(
                status_code=413,
                detail=f"Fichier trop volumineux (limite {limite // (1024 * 1024)} Mo)",
            )
        morceaux.append(morceau)
    return b"".join(morceaux)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configurer_journalisation()
    log.info("démarrage du serveur")
    # Les endpoints d'écriture sont synchrones : ils diffuseront leurs
    # événements en réinjectant des coroutines dans cette boucle.
    enregistrer_boucle(asyncio.get_running_loop())
    init_db()
    nettoyer_sessions_expirees()

    # Base sans le moindre compte : soit c'est le tout premier démarrage (et
    # l'écran d'installation va créer l'administrateur), soit une sauvegarde a
    # été restaurée de travers — et là, personne ne peut plus se connecter.
    # Signalé en CRITICAL pour que ce soit la première ligne qu'un admin voie
    # dans le journal, au lieu de le découvrir par un poste qui refuse le mot
    # de passe.
    with get_conn() as conn:
        if not utilisateurs_existent(conn):
            log.critical(
                "AUCUN COMPTE UTILISATEUR EN BASE : l'application n'est pas "
                "initialisée. Créez le premier compte administrateur "
                "(ou vérifiez la sauvegarde restaurée) — en attendant, tous "
                "les écrans répondent 503."
            )

    stop_backup = demarrer_planificateur()
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    yield
    cleanup_task.cancel()
    stop_backup.set()
    # Attendre la sauvegarde en cours : le thread est daemon, l'interpréteur
    # le couperait sinon au milieu de l'écriture de l'archive.
    await asyncio.to_thread(attendre_planificateur)
    # Ne pas laisser une boucle morte derrière soi : une diffusion planifiée
    # dessus lèverait au prochain démarrage (cas des tests successifs).
    enregistrer_boucle(None)

    log.info("arrêt du serveur")


# /docs expose une interface permettant d'ecrire dans la base sans code :
# on ne l'ouvre qu'en developpement.
app = FastAPI(
    title="Warehouse API",
    lifespan=lifespan,
    docs_url="/docs" if (_MODE_DEV or _DEMO_MODE) else None,
    redoc_url=None,
    openapi_url="/openapi.json" if (_MODE_DEV or _DEMO_MODE) else None,
)

app.include_router(board_router)

import pathlib as _pathlib
_board_dir = _pathlib.Path(__file__).resolve().parent.parent / "board"
if _board_dir.is_dir():
    from fastapi.staticfiles import StaticFiles

    class _BoardStaticFiles(StaticFiles):
        """Sans cache : le portail board est encore en test local et change
        souvent (CSS/JS édités directement sur disque, sans build). Un
        navigateur qui garde une vieille version de board.js en cache pendant
        qu'il charge un board.html frais casse silencieusement la page
        (fonctions manquantes) — on préfère revalider à chaque requête."""

        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            return response

    app.mount("/board", _BoardStaticFiles(directory=str(_board_dir), html=True), name="board")


_DASHBOARD_HTML = (_pathlib.Path(__file__).resolve().parent / "dashboard.html").read_text(encoding="utf-8")

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard_page():
    return _DASHBOARD_HTML


@app.middleware("http")
async def journaliser_requetes(request: Request, call_next):
    """Trace chaque requête : point de départ de tout post-mortem.

    N'accède pas au contenu du corps (mots de passe, données métier) : seuls
    la méthode, le chemin, le code renvoyé et la durée sont consignés.
    """
    debut = time.monotonic()
    try:
        reponse = await call_next(request)
    except Exception:
        log.exception("exception non gérée sur %s %s", request.method, request.url.path)
        raise
    duree_ms = (time.monotonic() - debut) * 1000
    niveau = logging.WARNING if reponse.status_code >= 400 else logging.INFO
    log.log(niveau, "%s %s -> %d (%.1f ms) depuis %s",
            request.method, request.url.path, reponse.status_code, duree_ms,
            request.client.host if request.client else "?")
    return reponse


def _debit_depasse(compteurs: dict[str, list[float]], ip: str, plafond: int) -> bool:
    """Compte une requête pour cette adresse et dit si le plafond est atteint.

    Le corps du rate limiter, extrait pour être partagé par ses deux
    compteurs (écritures et exports) : même fenêtre glissante, même bornage
    du nombre d'adresses suivies, une seule implémentation à maintenir.
    """
    now = time.monotonic()
    seuil = now - RATE_LIMIT_WINDOW
    if ip not in compteurs and len(compteurs) >= RATE_LIMIT_MAX_IPS:
        _cleanup_rate_limits()
        while len(compteurs) >= RATE_LIMIT_MAX_IPS:
            # `next(iter(...))` : les dict Python conservent l'ordre
            # d'insertion, c'est donc l'adresse suivie depuis le plus
            # longtemps qui part.
            compteurs.pop(next(iter(compteurs)), None)
    # Élaguer les entrées expirées
    compteurs[ip] = [t for t in compteurs[ip] if t > seuil]
    if len(compteurs[ip]) >= plafond:
        return True
    compteurs[ip].append(now)
    return False


@app.middleware("http")
async def limiter_debit(request: Request, call_next):
    """Rate limiter : les écritures (POST/PUT/PATCH/DELETE) et les exports.

    Les autres GET restent exemptés : ils ne modifient pas l'état et doivent
    rester fluides pour l'affichage temps réel du dashboard.

    Les exports sont des GET, donc historiquement illimités, alors qu'ils
    construisent un classeur Excel entier en mémoire et renvoient l'historique
    complet (coûts d'achat compris pour le rapport comptable). Ils ont leur
    propre plafond, bien plus bas — un compteur distinct, pour qu'un export ne
    consomme pas le quota d'écriture d'un poste en pleine saisie, et
    réciproquement.
    """
    ip = request.client.host if request.client else "unknown"
    est_export = "/export" in request.url.path
    if est_export and _debit_depasse(_rate_limits_export, ip, RATE_LIMIT_EXPORT_MAX):
        log.warning("rate limit export atteint pour %s (%d req/%ds) sur %s",
                    ip, RATE_LIMIT_EXPORT_MAX, RATE_LIMIT_WINDOW, request.url.path)
        return JSONResponse(
            status_code=429,
            content={"detail": "Trop d'exports demandés coup sur coup. "
                               "Patientez une minute avant le prochain."},
        )
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if _debit_depasse(_rate_limits, ip, RATE_LIMIT_MAX):
            log.warning("rate limit atteint pour %s (%d req/%ds)",
                        ip, RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)
            return JSONResponse(
                status_code=429,
                content={"detail": "Trop de requêtes, réessayez dans quelques secondes"},
            )
    return await call_next(request)


# Chemins qu'un compte régional peut atteindre. Tout le reste lui est fermé :
# il ne voit ni le catalogue produits global, ni le stock central, ni
# l'historique des autres régions. Un filtrage centralisé plutôt qu'un contrôle
# répété dans chaque endpoint : un oubli ouvrirait tout un pan de l'application.
CHEMINS_OUVERTS_AU_REGIONAL = frozenset({
    "/health", "/login", "/logout", "/me", "/me/password",
    "/users/status", "/ws-ticket", "/ws", "/reference/regions",
    # Référentiel en lecture seule, sans donnée centrale sensible — au même
    # titre que /reference/regions. Chargé par tous les postes au démarrage
    # (reference_data.charger_depuis_serveur) ; un compte régional le
    # déclenchait donc à chaque connexion pour se voir opposer un 403 inutile.
    "/reference/technicians",
})


def _role_de_session(token: str | None) -> str | None:
    """Rôle porté par un jeton de session encore valide, sans effet de bord.

    Délègue à `utilisateurs.role_de_session`, qui partage le cache mémoire de
    `session_valide` : ce middleware s'exécutant sur CHAQUE requête HTTP, sa
    lecture SQLite doublait celle de la dépendance d'authentification.
    """
    return role_de_session(token)


@app.middleware("http")
async def restreindre_comptes_regionaux(request: Request, call_next):
    """Un compte régional n'accède qu'à ses trois écrans, rien d'autre."""
    if _role_de_session(request.headers.get("x-user-token")) == "regional":
        chemin = request.url.path
        autorise = (
            chemin in CHEMINS_OUVERTS_AU_REGIONAL
            or (chemin.startswith("/regional/")
                # Résoudre un signalement est une décision du central : une
                # région qui clôt sa propre demande la ferait disparaître sans
                # qu'un seul article ait bougé.
                and not chemin.endswith("/resoudre"))
            or chemin.endswith("/confirmer-reception")
        )
        if not autorise:
            log.warning("accès refusé à un compte régional : %s %s",
                        request.method, chemin)
            return JSONResponse(
                status_code=403,
                content={"detail": "Un compte régional n'a pas accès à cette fonction. "
                                   "Contactez l'entrepôt central."},
            )
    return await call_next(request)


def _valeur_json_sure(valeur):
    """Rend une valeur sérialisable en JSON strict, récursivement.

    FastAPI renvoie dans le corps d'une erreur 422 la valeur fautive (`input`).
    Quand cette valeur est `Infinity` ou `NaN` — précisément ce qu'on vient de
    refuser — `json.dumps` lève « Out of range float values are not JSON
    compliant » : le 422 explicite se transformait en 500 muet, et l'opérateur
    n'apprenait jamais quelle cellule corriger.
    """
    if isinstance(valeur, float) and (valeur != valeur or valeur in (
            float("inf"), float("-inf"))):
        return repr(valeur)
    if isinstance(valeur, dict):
        return {k: _valeur_json_sure(v) for k, v in valeur.items()}
    if isinstance(valeur, (list, tuple)):
        return [_valeur_json_sure(v) for v in valeur]
    return valeur


@app.exception_handler(RequestValidationError)
async def erreur_validation(request: Request, exc: RequestValidationError):
    """422 de validation, garanti sérialisable."""
    return JSONResponse(
        status_code=422,
        content={"detail": _valeur_json_sure(jsonable_encoder(exc.errors()))},
    )


@app.exception_handler(Exception)
async def erreur_non_geree(request: Request, exc: Exception):
    """Filet de sécurité : toute exception non prévue est journalisée avec sa
    trace complète avant de répondre — jamais silencieuse, jamais de détail
    interne renvoyé au client."""
    log.exception("erreur interne sur %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Erreur interne du serveur"})


@app.get("/version")
def version_disponible():
    """Dernière version du client publiée par l'admin, et où la télécharger.

    Lit un fichier édité à la main (voir `chemins.chemin_version`) — il n'y a
    pas de build qui le génère automatiquement, c'est l'admin qui déclare
    « voici la nouvelle version » après avoir mis le nouvel installateur en
    ligne. Absent ou illisible : pas de mise à jour à annoncer, jamais d'erreur
    (les postes ne doivent jamais planter sur ce contrôle, purement informatif).
    """
    from chemins import chemin_version
    try:
        contenu = chemin_version().read_text(encoding="utf-8")
        data = json.loads(contenu)
        return {"version": data.get("version"), "url": data.get("url")}
    except (OSError, ValueError):
        return {"version": None, "url": None}


@app.get("/health")
def health():
    """Sonde de disponibilité, volontairement muette sur l'état interne.

    Le seul endpoint non authentifié de l'application : le client s'en sert
    pour savoir si le serveur tourne, avant même d'avoir un jeton. Il
    renvoyait aussi le nombre de produits en base et l'état du miroir de
    sauvegarde — deux renseignements internes offerts à quiconque atteint le
    port, sans se connecter.

    Le détail reste disponible pour l'administrateur sur `/systeme/etat`
    (nombre de produits, taille de la base, version du schéma, et l'état du
    miroir via `sauvegarde.statut_miroir`).
    """
    return {"status": "ok"}


def _next_reference(conn, doc_type: str) -> str:
    """Auto-generate a sequential reference number using doc_counters.

    RECEIVING → REC-2026-0001, DELIVERY → DEL-2026-0001, etc.
    """
    prefix = {"RECEIVING": "REC", "DELIVERY": "DEL",
              "RETURN": "RET", "SUPPLIER_RETURN": "SRET"}.get(doc_type, "DOC")
    year = datetime.now().year
    row = conn.execute(
        "SELECT seq FROM doc_counters WHERE type_prefix=? AND year=?",
        (prefix, year),
    ).fetchone()
    if row:
        new_seq = row[0] + 1
        conn.execute(
            "UPDATE doc_counters SET seq=? WHERE type_prefix=? AND year=?",
            (new_seq, prefix, year),
        )
    else:
        new_seq = 1
        conn.execute(
            "INSERT INTO doc_counters (type_prefix, year, seq) VALUES (?, ?, ?)",
            (prefix, year, new_seq),
        )
    return f"{prefix}-{year}-{new_seq:04d}"


@app.get("/next-reference")
def next_reference(type: str = "RECEIVING", user=Depends(exiger_ecriture)):
    """Preview the next auto-generated reference number."""
    prefix = {"RECEIVING": "REC", "DELIVERY": "DEL",
              "RETURN": "RET", "SUPPLIER_RETURN": "SRET"}.get(type, "DOC")
    year = datetime.now().year
    with get_conn() as conn:
        row = conn.execute(
            "SELECT seq FROM doc_counters WHERE type_prefix=? AND year=?",
            (prefix, year),
        ).fetchone()
        next_seq = (row[0] + 1) if row else 1
    return {"reference": f"{prefix}-{year}-{next_seq:04d}"}


@app.get("/demo/summary")
def demo_summary():
    """Public endpoint for the web dashboard — no auth required."""
    if os.environ.get("WMS_DEMO_MODE") != "1":
        raise HTTPException(status_code=404, detail="Not Found")
    with get_conn() as conn:
        products = [dict(r) for r in conn.execute(
            "SELECT id, sku, name, tank_capacity, current_stock, site "
            "FROM products"
        ).fetchall()]
        doc_count = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        rec_count = conn.execute(
            "SELECT count(*) FROM documents WHERE type='RECEIVING'"
        ).fetchone()[0]
        del_count = conn.execute(
            "SELECT count(*) FROM documents WHERE type='DELIVERY'"
        ).fetchone()[0]
        docs = [dict(r) for r in conn.execute(
            "SELECT d.reference, d.type, d.party, d.created_at, "
            "d.vehicle_plate, d.project, p.name AS product_name, dl.quantity "
            "FROM documents d "
            "LEFT JOIN document_lines dl ON dl.document_id = d.id "
            "LEFT JOIN products p ON p.id = dl.product_id "
            "ORDER BY d.created_at DESC LIMIT 12"
        ).fetchall()]
        by_receiver = [dict(r) for r in conn.execute(
            "SELECT d.party AS name, count(*) AS ops, "
            "round(sum(dl.quantity),1) AS total "
            "FROM documents d JOIN document_lines dl ON dl.document_id=d.id "
            "WHERE d.type='DELIVERY' AND d.party IS NOT NULL "
            "GROUP BY d.party ORDER BY total DESC"
        ).fetchall()]
        by_project = [dict(r) for r in conn.execute(
            "SELECT d.project AS name, count(*) AS ops, "
            "round(sum(dl.quantity),1) AS total "
            "FROM documents d JOIN document_lines dl ON dl.document_id=d.id "
            "WHERE d.type='DELIVERY' AND d.project IS NOT NULL "
            "GROUP BY d.project ORDER BY total DESC"
        ).fetchall()]
        by_vehicle = [dict(r) for r in conn.execute(
            "SELECT d.vehicle_plate AS name, count(*) AS ops, "
            "round(sum(dl.quantity),1) AS total "
            "FROM documents d JOIN document_lines dl ON dl.document_id=d.id "
            "WHERE d.type='DELIVERY' AND d.vehicle_plate IS NOT NULL "
            "GROUP BY d.vehicle_plate ORDER BY total DESC"
        ).fetchall()]
        by_site = [dict(r) for r in conn.execute(
            "SELECT p.site AS name, round(sum(dl.quantity),1) AS total "
            "FROM documents d JOIN document_lines dl ON dl.document_id=d.id "
            "JOIN products p ON p.id=dl.product_id "
            "WHERE d.type='DELIVERY' "
            "GROUP BY p.site ORDER BY total DESC"
        ).fetchall()]
    return {
        "products": products,
        "total_documents": doc_count,
        "receptions": rec_count,
        "deliveries": del_count,
        "recent_documents": docs,
        "by_receiver": by_receiver,
        "by_project": by_project,
        "by_vehicle": by_vehicle,
        "by_site": by_site,
    }


@app.get("/backup/statut")
def backup_statut(admin=Depends(exiger_admin)):
    """État des sauvegardes : date de la dernière, et si elle est périmée.

    Réservé à l'administrateur — c'est lui qui doit agir, et la réponse
    contient le chemin du dossier de sauvegardes.

    Appelé via le module et non par une fonction importée à la ligne d'import :
    un test peut ainsi repointer `backup.BACKUP_DIR` ou remplacer
    `backup.statut_sauvegarde` sans toucher à `app`.
    """
    return backup.statut_sauvegarde()


@app.get("/systeme/etat")
def systeme_etat(admin=Depends(exiger_admin)):
    """Tableau de bord technique en UN appel, pour l'écran « État du système ».

    Agrège ce qui existe déjà et ne coûte rien : l'état des sauvegardes
    (`backup.statut_sauvegarde`, exactement la même logique que
    `/backup/statut`), la version publiée (même fichier que `/version`), la
    version du schéma (PRAGMA), le nombre de postes actuellement connectés en
    WebSocket (dictionnaire en mémoire) et deux mesures de base bon marché
    (nombre de produits, taille du fichier).

    Volontairement SANS réconciliation : ce calcul balaye `document_lines` en
    entier (voir `calculer_reconciliation`), il tourne déjà une fois par jour
    en tâche de fond et une fois à la connexion de l'admin. Le rejouer à
    chaque ouverture de l'écran d'état coûterait cher pour une information
    déjà disponible ailleurs.

    Chaque bloc est protégé : un écran de diagnostic ne doit JAMAIS être le
    seul à tomber en panne. Un signal illisible vaut `None`, il s'affichera
    « inconnu » côté client au lieu de renvoyer une 500.
    """
    import database

    try:
        sauvegarde = backup.statut_sauvegarde()
    except Exception:
        log.exception("état système : statut de sauvegarde illisible")
        sauvegarde = None

    try:
        from chemins import chemin_version
        data = json.loads(chemin_version().read_text(encoding="utf-8"))
        version_publiee = data.get("version")
        url_version = data.get("url")
    except (OSError, ValueError):
        version_publiee = None
        url_version = None

    # `manager.active_connections` est un simple dict en mémoire : le lire ne
    # touche ni le réseau ni la base.
    try:
        postes_connectes = len(manager.active_connections)
        comptes_connectes = sorted({
            (info or {}).get("username")
            for info in manager.active_connections.values()
            if (info or {}).get("username")
        })
    except Exception:
        postes_connectes = None
        comptes_connectes = []

    produits = None
    version_schema = None
    try:
        with get_conn() as conn:
            produits = conn.execute(
                "SELECT COUNT(*) AS n FROM products WHERE COALESCE(archived, 0) = 0"
            ).fetchone()["n"]
            version_schema = conn.execute("PRAGMA user_version").fetchone()[0]
    except Exception:
        log.exception("état système : base illisible")

    try:
        taille_base_octets = database.DB_PATH.stat().st_size
    except OSError:
        taille_base_octets = None

    return {
        "sauvegarde": sauvegarde,
        "version_publiee": version_publiee,
        "url_version": url_version,
        "version_schema": version_schema,
        "postes_connectes": postes_connectes,
        "comptes_connectes": comptes_connectes,
        "produits": produits,
        "taille_base_octets": taille_base_octets,
        "horodatage": maintenant_texte(),
    }


def _categorie_depuis_nom(nom: str) -> str:
    """Extrait la catégorie du libellé : 'Air Filter (Perkins/...)' -> 'Air Filter'."""
    return nom.split(" (")[0].strip() if " (" in nom else nom.strip()


def _lister_produits(include_archived: bool = False, sector: str = SECTEUR_DEFAUT,
                     site: Optional[str] = None) -> list[dict]:
    """Lecture du catalogue d'UN secteur, sans contrôle d'accès (usage interne).

    `site` restreint en plus aux produits de CE site (ou sans site défini).
    None = aucune restriction — l'appelant décide : `list_products` passe
    `_site_du_compte(...)` par défaut (scope même un admin à son site,
    ex. Rijkaard/Canapé-Vert dans Stock/Réception/Jaugeage), ou `None`
    explicitement via `all_sites=true` (réservé admin — écran Produits).
    """
    filtre_site = " AND (site IS NULL OR site = ?)" if site else ""
    params = [sector] + ([site] if site else [])
    with get_conn() as conn:
        if include_archived:
            rows = conn.execute(
                f"SELECT * FROM products WHERE sector = ?{filtre_site} ORDER BY name",
                params,
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM products WHERE sector = ? AND COALESCE(archived, 0) = 0"
                f"{filtre_site} ORDER BY name",
                params,
            ).fetchall()
        return [dict(r) for r in rows]


@app.get("/products", response_model=list[ProductOut])
def list_products(include_archived: bool = False, all_sites: bool = False,
                  user=Depends(exiger_utilisateur)):
    """Catalogue des produits du secteur de l'appelant (Consumables, FON...).

    Un produit archivé est refusé à la saisie d'un bon : le proposer encore
    dans les listes déroulantes ne pouvait que produire une erreur. Il reste
    accessible avec `include_archived=true` (écrans d'administration).

    Lecture réservée aux comptes connectés : le catalogue et les quantités
    sont des données d'exploitation, pas des informations publiques du réseau.

    Scopée par site en plus du secteur (Fuel) : un compte (admin compris)
    rattaché à un site (ex. Orelus WH Central, Rijkaard Canapé-Vert) ne voit
    par défaut que les produits de ce site (ou sans site défini) — c'est le
    comportement voulu pour Stock/Réception/Expédition/Jaugeage, où mélanger
    les cuves d'un autre site n'aide personne, admin compris.

    `all_sites=true` lève ce filtre — réservé à l'admin (silencieusement
    ignoré sinon) : c'est ce que demande l'écran Produits, où l'admin doit
    pouvoir gérer le catalogue de TOUS les sites de son secteur.
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        if all_sites and user and user.get("role") == "admin":
            site = None
        else:
            site = _site_du_compte(conn, user)
    return _lister_produits(include_archived, sector=secteur, site=site)


# Déclarée AVANT `/products/{product_id}` : sans cela, FastAPI ferait
# correspondre « qualite » au paramètre de chemin et répondrait 422.
@app.get("/products/qualite")
def qualite_referentiel_produits(admin=Depends(exiger_admin)):
    """Fiches produit présentant un défaut de saisie évident.

    Écran d'administration, pas de contrôle bloquant : rien n'est corrigé ni
    empêché ici. C'est une liste de travail — l'admin ouvre la fiche signalée
    et décide. Réservée à l'admin car elle expose le coût unitaire et parce
    que lui seul peut modifier une fiche produit.
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, admin)
        # SELECT * et non une projection : le client ouvre la fiche produit
        # directement depuis cette liste, et cette fiche renvoie TOUS ses
        # champs au serveur (un champ absent du formulaire y est envoyé vide,
        # donc effacé). Une projection partielle effacerait la consigne
        # bouteille du produit qu'on vient de corriger.
        rows = conn.execute(
            "SELECT * FROM products WHERE sector = ? AND COALESCE(archived, 0) = 0 ORDER BY name",
            (secteur,),
        ).fetchall()
    produits = analyser_produits([dict(r) for r in rows])
    return {
        "produits": produits,
        "nombre": len(produits),
        "total_actifs": len(rows),
        "libelles": DEFAUT_LIBELLES,
    }


def _produit_par_code_barres(conn, code: str, sector: str, sauf_id: int | None = None):
    """Produit ACTIF portant ce code-barres DANS CE SECTEUR, ou None.

    Seuls les produits actifs sont consultés : un code libéré par l'archivage
    d'une fiche doit pouvoir être réattribué (l'index unique partiel de la
    migration 42 pose exactement la même règle). Scopé par secteur : deux
    secteurs différents peuvent réutiliser le même code-barres sans conflit.
    """
    sql = ("SELECT * FROM products "
           "WHERE barcode = ? AND sector = ? AND COALESCE(archived, 0) = 0")
    params: list = [code, sector]
    if sauf_id is not None:
        sql += " AND id != ?"
        params.append(sauf_id)
    return conn.execute(sql, params).fetchone()


# Déclarée AVANT `/products/{product_id}` pour la même raison que
# `/products/qualite` : l'ordre de déclaration décide de l'appariement.
@app.get("/products/scan/{code}", response_model=ProductOut)
def scan_product(code: str, user=Depends(exiger_utilisateur)):
    """Produit correspondant à un code-barres scanné, 404 sinon.

    Point d'entrée unique de toute douchette : celle-ci se comporte comme un
    clavier (elle tape le code puis Entrée), l'écran appelle cet endpoint, et
    rien de matériel n'a besoin d'être connu du serveur.

    Ouvert à tout compte connecté, comme la lecture du catalogue : scanner un
    article pour le retrouver est un geste d'exploitation, pas d'administration.
    """
    nettoye = (code or "").strip()
    if not nettoye:
        raise HTTPException(status_code=404, detail="Code-barres vide")
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        row = _produit_par_code_barres(conn, nettoye, secteur)
        # Scopé par SITE en plus du secteur (admin compris) : la douchette sert
        # en Réception/Expédition, écrans où chacun ne travaille que sur ses
        # propres cuves/articles. Un code scanné qui désigne le produit d'un
        # autre site répond « aucun produit », comme s'il n'existait pas.
        if row and _produit_accessible(conn, row["id"], user) is None:
            row = None
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Aucun produit ne porte le code-barres « {nettoye} »")
    return dict(row)


@app.post("/products", response_model=ProductOut)
def create_product(product: ProductIn, user=Depends(exiger_admin)):
    # Créer une référence relève du catalogue, pas de l'exploitation : la
    # modifier (PATCH), l'archiver (DELETE) et l'importer en masse étaient déjà
    # réservés à l'administrateur, et le client ne montre le formulaire
    # « Ajouter un produit » qu'à lui. Seule la création restait ouverte à un
    # magasinier — qui pouvait ainsi ouvrir un SKU avec un stock initial et un
    # coût unitaire de son choix, sans qu'aucun écran ne le propose ni ne le
    # contrôle. Le stock initial saisi ici crée un mouvement d'ouverture.
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)
        # Contrôle AVANT l'INSERT : l'index unique partiel lèverait bien une
        # IntegrityError, mais elle est indiscernable de celle du SKU juste
        # en dessous — le magasinier lirait « SKU existe déjà » pour un
        # conflit de code-barres.
        if product.barcode:
            deja = _produit_par_code_barres(conn, product.barcode, secteur)
            if deja:
                raise HTTPException(
                    status_code=409,
                    detail=f"Le code-barres « {product.barcode} » est déjà utilisé "
                           f"par le produit {deja['sku']} — {deja['name']}")
        try:
            cur = conn.execute(
                "INSERT INTO products (sku, name, category, unit, unit_type, bidon_capacity, "
                "consigne_bouteille, initial_stock, current_stock, min_stock, unit_cost, "
                "barcode, sector, part_number, group_name, sub_category, "
                "supported_genset_models, vendor, rl_apply, discontinued, tank_capacity, site) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (product.sku, product.name, product.category or _categorie_depuis_nom(product.name),
                 product.unit, product.unit_type, product.bidon_capacity,
                 product.consigne_bouteille,
                 product.current_stock, product.current_stock, product.min_stock,
                 product.unit_cost, product.barcode, secteur,
                 product.part_number, product.group_name, product.sub_category,
                 product.supported_genset_models, product.vendor,
                 int(product.rl_apply), int(product.discontinued),
                 product.tank_capacity, product.site),
            )
        except sqlite3.IntegrityError:
            # NOTE : products.sku est UNIQUE globalement (tous secteurs
            # confondus), pas seulement au sein d'un secteur. Un vrai
            # doublon entre deux secteurs est rejeté ici comme un doublon
            # normal — limite connue, acceptable tant que chaque secteur
            # utilise ses propres préfixes de code (ex. « FON-xxx »).
            raise HTTPException(status_code=409, detail=f"SKU '{product.sku}' existe déjà")
        enregistrer_ouverture(conn, cur.lastrowid, product.current_stock)
        row = conn.execute("SELECT * FROM products WHERE id = ?", (cur.lastrowid,)).fetchone()

        auteur = user["username"] if user else "système"
        _journaliser_audit(conn, "product", cur.lastrowid, "create",
                           new={"sku": product.sku, "name": product.name}, username=auteur,
                           sector=secteur)

        resultat = dict(row)

    # Hors transaction, une fois le COMMIT fait : une nouvelle référence change
    # references_total, unites_totales et — si elle naît à zéro — en_rupture.
    # Sans cette invalidation, le tableau de bord ignorait le produit créé
    # pendant les 20 s du TTL, sur TOUS les postes.
    _invalider_cache_dashboard()
    return resultat


@app.post("/products/import")
async def import_products(file: UploadFile, admin=Depends(exiger_admin)):
    # Limite passée explicitement : le paramètre par défaut de
    # _lire_fichier_borne est figé à la définition de la fonction, il ne
    # verrait pas un MAX_UPLOAD_BYTES modifié après coup (tests, config).
    content = await _lire_fichier_borne(file, limite=MAX_UPLOAD_BYTES)
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, admin)
    try:
        # openpyxl est purement bloquant : lancé tel quel dans la boucle
        # d'événements, un catalogue de plusieurs centaines de lignes gelait
        # tout le serveur (aucun autre poste ne pouvait saisir un bon).
        #
        # `sector` passé au parsing : `products.sku` est UNIQUE au niveau de
        # TOUTE la base (pas composite avec `sector`), donc une référence
        # temporaire générée pour un article "sans code officiel" doit
        # dépendre du secteur en plus du libellé — sinon deux secteurs au
        # catalogue proche (RAN et FON partagent des patchcords fibre au nom
        # identique) génèrent la même référence TBA-xxxxxxxx et l'import
        # échoue (UNIQUE constraint failed), vécu en réel le 12 septembre
        # 2026 à l'import du Masterlist RAN.
        products, rapport = await asyncio.to_thread(parse_products_excel, content, secteur)
    except Exception as e:
        log.warning("import refusé : fichier illisible — %s", e)
        raise HTTPException(status_code=400, detail=f"Fichier Excel illisible : {e}")

    if not products:
        motif = "; ".join(rapport.get("erreurs") or ["aucune ligne exploitable"])
        log.warning("import refusé : %s", motif)
        raise HTTPException(status_code=400, detail=f"Aucun produit importé — {motif}")

    auteur = admin["username"] if admin else "système"
    created, updated, ecarts = await asyncio.to_thread(
        _ecrire_import_produits, products, auteur, secteur)

    log.info("import (par %s, secteur %s) : %d créé(s), %d mis à jour, %d écart(s)",
             auteur, secteur, created, updated, len(ecarts))
    _invalider_cache_dashboard(secteur)
    return {"created": created, "updated": updated, "ecarts": ecarts, **rapport}


def _ecrire_import_produits(products: list[dict], auteur: str, sector: str = SECTEUR_DEFAUT
                            ) -> tuple[int, int, list[dict]]:
    """Écrit le catalogue importé en base, pour UN secteur. Bloquant : à appeler hors boucle."""
    created, updated = 0, 0
    ecarts: list[dict] = []
    with get_conn(write=True) as conn:
        # Un SELECT groupé plutôt qu'un par produit importé (repéré par audit
        # de performance le 9 septembre 2026) : un catalogue Fuel ou
        # Consumables importé d'un coup peut porter sur des centaines de SKUs,
        # et ce bloc reste sous le verrou d'écriture unique de la base — chaque
        # aller-retour SQL en moins raccourcit d'autant l'attente des autres
        # postes qui saisissent une réception/livraison en même temps.
        skus = [p["sku"] for p in products]
        existants_par_sku: dict[str, sqlite3.Row] = {}
        if skus:
            marqueurs = ",".join("?" * len(skus))
            for row in conn.execute(
                f"SELECT id, sku, current_stock, name, category, capacity_group, unit, "
                f"unit_type, bidon_capacity, min_stock FROM products "
                f"WHERE sector = ? AND sku IN ({marqueurs})",
                [sector, *skus],
            ).fetchall():
                existants_par_sku[row["sku"]] = row
        for p in products:
            # Scopé par secteur : sans ce filtre, importer un SKU qui existe
            # déjà dans UN AUTRE secteur mettrait à jour la fiche de cet autre
            # secteur au lieu d'en créer une nouvelle ici — une vraie
            # corruption croisée entre catalogues.
            existing = existants_par_sku.get(p["sku"])
            if existing:
                # Un import de catalogue ne modifie JAMAIS les quantités : celles-ci
                # ne changent que par un mouvement daté et tracé. Sinon l'import
                # efface silencieusement l'historique des réceptions/expéditions.
                nouvelle_categorie = p.get("category") or _categorie_depuis_nom(p["name"])
                nouveau_capacity_group = p.get("capacity_group") or existing["capacity_group"]
                nouveau_unit_type = p.get("unit_type") or existing["unit_type"]
                nouveau_bidon = p.get("bidon_capacity") or existing["bidon_capacity"]
                conn.execute(
                    "UPDATE products SET name = ?, category = ?, capacity_group = COALESCE(?, capacity_group), "
                    "unit = ?, unit_type = COALESCE(?, unit_type), "
                    "bidon_capacity = COALESCE(?, bidon_capacity), min_stock = ?, "
                    "part_number = COALESCE(?, part_number), group_name = COALESCE(?, group_name), "
                    "sub_category = COALESCE(?, sub_category), "
                    "supported_genset_models = COALESCE(?, supported_genset_models), "
                    "vendor = COALESCE(?, vendor), rl_apply = ?, discontinued = ? "
                    "WHERE id = ?",
                    (p["name"], nouvelle_categorie,
                     p.get("capacity_group"), p["unit"], p.get("unit_type"),
                     p.get("bidon_capacity"), p["min_stock"],
                     p.get("part_number"), p.get("group_name"), p.get("sub_category"),
                     p.get("supported_genset_models"), p.get("vendor"),
                     int(p.get("rl_apply", False)), int(p.get("discontinued", False)),
                     existing["id"]),
                )
                updated += 1
                # Piste d'audit par produit : sans elle, un import en masse ne
                # laissait qu'un compte agrégé — impossible de savoir ensuite
                # QUEL produit a changé et QUOI, en particulier son seuil.
                avant = {"name": existing["name"], "category": existing["category"],
                         "capacity_group": existing["capacity_group"], "unit": existing["unit"],
                         "unit_type": existing["unit_type"], "bidon_capacity": existing["bidon_capacity"],
                         "min_stock": existing["min_stock"]}
                apres = {"name": p["name"], "category": nouvelle_categorie,
                         "capacity_group": nouveau_capacity_group, "unit": p["unit"],
                         "unit_type": nouveau_unit_type, "bidon_capacity": nouveau_bidon,
                         "min_stock": p["min_stock"]}
                champs_modifies = {k for k in avant if avant[k] != apres[k]}
                if champs_modifies:
                    _journaliser_audit(
                        conn, "product", existing["id"], "import_update",
                        sector=sector,
                        old={k: avant[k] for k in champs_modifies},
                        new={k: apres[k] for k in champs_modifies},
                        username=auteur,
                    )
                if abs(existing["current_stock"] - p["current_stock"]) > 1e-9:
                    ecarts.append({
                        "sku": p["sku"],
                        "stock_systeme": existing["current_stock"],
                        "stock_fichier": p["current_stock"],
                    })
            else:
                cur_produit = conn.execute(
                    "INSERT INTO products (sku, name, category, capacity_group, unit, unit_type, "
                    "bidon_capacity, initial_stock, current_stock, min_stock, sector, "
                    "part_number, group_name, sub_category, supported_genset_models, "
                    "vendor, rl_apply, discontinued) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (p["sku"], p["name"], p.get("category") or _categorie_depuis_nom(p["name"]),
                     p.get("capacity_group"), p["unit"], p.get("unit_type", "piece"),
                     p.get("bidon_capacity"), p["current_stock"],
                     p["current_stock"], p["min_stock"], sector,
                     p.get("part_number"), p.get("group_name"), p.get("sub_category"),
                     p.get("supported_genset_models"), p.get("vendor"),
                     int(p.get("rl_apply", False)), int(p.get("discontinued", False))),
                )
                enregistrer_ouverture(conn, cur_produit.lastrowid, p["current_stock"])
                created += 1

        # Un import touche tout le catalogue : il doit laisser une trace
        # nominative, au même titre qu'une création de produit.
        _journaliser_audit(conn, "product", 0, "import", sector=sector,
                           new={"created": created, "updated": updated,
                                "ecarts": len(ecarts)},
                           username=auteur)

    return created, updated, ecarts


@app.patch("/products/thresholds")
def update_thresholds(thresholds: list[ThresholdUpdate], admin=Depends(exiger_admin)):
    """Met a jour le seuil de reapprovisionnement (min_stock) de plusieurs produits.

    Chaque element doit contenir product_id (int) et min_stock (float >= 0).
    Reserve aux administrateurs.
    """
    if not thresholds:
        raise HTTPException(status_code=400, detail="Liste de seuils vide")

    updated = 0
    auteur = admin["username"] if admin else "système"
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, admin)
        for entry in thresholds:
            # Scopé par secteur : un id de produit d'un autre secteur est
            # traité comme introuvable, jamais modifié.
            avant = conn.execute(
                "SELECT min_stock FROM products WHERE id = ? AND sector = ?",
                (entry.product_id, secteur),
            ).fetchone()
            if avant is None:
                raise HTTPException(status_code=404, detail=f"Produit {entry.product_id} introuvable")
            cur = conn.execute(
                "UPDATE products SET min_stock = ROUND(?, 3) WHERE id = ? AND sector = ?",
                (entry.min_stock, entry.product_id, secteur),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"Produit {entry.product_id} introuvable")
            # Piste d'audit : un seuil mal saisi (ou baissé pour masquer une
            # rupture) doit rester traçable au même titre qu'un ajustement.
            _journaliser_audit(conn, "product", entry.product_id, "threshold",
                               old={"min_stock": avant["min_stock"]},
                               new={"min_stock": entry.min_stock}, username=auteur,
                               sector=secteur)
            updated += 1

    log.info("seuils mis a jour par %s : %d produit(s)", auteur, updated)
    # Les autres postes rechargent leur vue Seuils sans avoir a rafraichir
    # a la main. Sans cette diffusion, le handler client n'etait jamais
    # declenche.
    planifier(broadcast_thresholds_updated({
        "produits": [
            {"product_id": e.product_id, "min_stock": e.min_stock} for e in thresholds
        ],
    }, sector=secteur))
    _invalider_cache_dashboard(secteur)
    return {"updated": updated}


@app.patch("/products/stock-date")
def set_global_stock_date(body: StockDateIn, admin=Depends(exiger_admin)):
    date_str = (body.date or "").strip()
    reset_initial = body.reset_initial
    if not date_str:
        raise HTTPException(status_code=400, detail="Date requise")

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        raise HTTPException(status_code=422, detail="Format de date invalide (AAAA-MM-JJ)")

    try:
        chosen = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="Date invalide")
    if chosen > date.today():
        raise HTTPException(status_code=422, detail="La date ne peut pas dépasser aujourd'hui")

    # ---- Réinitialisation du stock de début : l'écriture la plus destructrice
    # du système. Elle rebase le cycle de stock de TOUS les produits d'un coup
    # (initial_stock = current_stock) et réécrit les mouvements d'ouverture :
    # les anciens stocks de début ne sont récupérables NULLE PART après coup.
    # Deux garde-fous sont posés ici, avant la moindre écriture :
    #   1. une sauvegarde horodatée immédiate de la base ;
    #   2. un résumé chiffré de l'état d'avant, dans le journal d'audit.
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, admin)

    etat_avant: dict = {}
    archive_sauvegarde = ""
    if reset_initial:
        with get_conn() as conn:
            ligne = conn.execute("""
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(current_stock), 0) AS q_actuelle,
                       COALESCE(SUM(initial_stock), 0) AS q_initiale,
                       COALESCE(SUM(current_stock
                                    * COALESCE(unit_cost, 0)), 0) AS v_actuelle,
                       COALESCE(SUM(initial_stock
                                    * COALESCE(unit_cost, 0)), 0) AS v_initiale
                FROM products WHERE COALESCE(archived, 0) = 0 AND sector = ?
            """, (secteur,)).fetchone()
        etat_avant = {
            "produits_actifs": ligne["n"],
            "quantite_totale": arrondir(ligne["q_actuelle"]),
            "quantite_initiale_totale": arrondir(ligne["q_initiale"]),
            "valeur_totale": round(ligne["v_actuelle"], 2),
            "valeur_initiale_totale": round(ligne["v_initiale"], 2),
        }
        try:
            archive_sauvegarde = backup.backup_now().name
        except Exception as e:
            # Refus volontaire : sans point de retour, cette opération est
            # irréversible pour de bon. Mieux vaut un administrateur bloqué
            # cinq minutes qu'un cycle de stock perdu sans recours.
            log.error("sauvegarde prealable au reset de stock impossible : %s", e)
            raise HTTPException(
                status_code=503,
                detail="Sauvegarde préalable impossible : la réinitialisation "
                       "du stock de début a été annulée par sécurité. "
                       "Vérifiez l'espace disque du serveur, puis réessayez.",
            )
        log.warning(
            "RESET stock debut demande par %s : %d produit(s), valeur %.2f, "
            "sauvegarde %s",
            (admin["username"] if admin else "système"),
            etat_avant["produits_actifs"], etat_avant["valeur_totale"],
            archive_sauvegarde or "—")

    with get_conn(write=True) as conn:
        # Photo de l'état d'avant : cette opération réécrit la date de début de
        # stock de TOUS les produits, et remet en plus initial_stock à
        # current_stock quand reset_initial est demandé. Sans trace, plus
        # personne ne peut expliquer pourquoi la réconciliation d'un produit a
        # changé de base du jour au lendemain.
        dates_avant = [
            r["initial_stock_date"] for r in conn.execute(
                "SELECT DISTINCT initial_stock_date FROM products WHERE sector = ? "
                "ORDER BY initial_stock_date LIMIT 10",
                (secteur,),
            ).fetchall()
        ]
        if reset_initial:
            # Le dernier bon existant borne la réconciliation : tout ce qui
            # précède est déjà contenu dans la nouvelle base (initial_stock =
            # current_stock) et ne doit plus être recompté. Bornée au secteur :
            # un bon FON ne doit pas borner la réconciliation de Consumables.
            dernier_bon = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM documents WHERE sector = ?",
                (secteur,),
            ).fetchone()[0]
            cur = conn.execute(
                "UPDATE products SET initial_stock_date = ?, "
                "initial_stock = current_stock, initial_stock_reset_doc_id = ? "
                "WHERE sector = ?",
                (date_str, dernier_bon, secteur),
            )
            # Réaligner les mouvements d'ouverture sur la nouvelle base. Un
            # SELECT groupé plutôt qu'un par produit du secteur (repéré par
            # audit de performance le 9 septembre 2026) : cette boucle reste
            # sous le verrou d'écriture unique de la base pendant tout le
            # parcours du catalogue.
            products = conn.execute(
                "SELECT id, current_stock FROM products WHERE sector = ?", (secteur,)
            ).fetchall()
            mvts_ouverture_par_produit = {
                r["product_id"]: r["id"] for r in conn.execute(
                    "SELECT id, product_id FROM stock_movements "
                    "WHERE document_id IS NULL AND product_id IN "
                    f"({','.join('?' * len(products))})",
                    [p["id"] for p in products],
                ).fetchall()
            } if products else {}
            for p in products:
                mvt_id = mvts_ouverture_par_produit.get(p["id"])
                if mvt_id:
                    conn.execute(
                        "UPDATE stock_movements SET quantity = ? WHERE id = ?",
                        (p["current_stock"], mvt_id),
                    )
                elif p["current_stock"] > 0:
                    enregistrer_ouverture(conn, p["id"], p["current_stock"])
        else:
            cur = conn.execute(
                "UPDATE products SET initial_stock_date = ? WHERE sector = ?",
                (date_str, secteur),
            )
        updated = cur.rowcount

        ancien = {"dates": dates_avant}
        if etat_avant:
            ancien.update(etat_avant)
        nouveau = {"date": date_str, "reset_initial": bool(reset_initial),
                   "produits": updated}
        if archive_sauvegarde:
            # Le nom de l'archive est dans le journal : c'est ce qu'on cherche
            # le jour où il faut revenir en arrière.
            nouveau["sauvegarde"] = archive_sauvegarde
        _journaliser_audit(
            conn, "product", 0, "stock_date",
            old=ancien, new=nouveau,
            username=(admin["username"] if admin else "système"),
            sector=secteur,
        )

    log.info("date stock debut global %s par %s (secteur %s), reset=%s, %d produit(s)",
             date_str, (admin["username"] if admin else "système"), secteur, reset_initial, updated)
    _invalider_cache_dashboard(secteur)
    reponse = {"updated": updated, "date": date_str, "reset": reset_initial}
    if archive_sauvegarde:
        reponse["sauvegarde"] = archive_sauvegarde
    return reponse


@app.patch("/products/{product_id}", response_model=ProductOut)
def update_product(product_id: int, body: ProductUpdate, admin=Depends(exiger_admin)):
    # exclude_unset : distingue "pas envoyé" de "envoyé à null"
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Aucun champ à modifier")

    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, admin)
        # Scopé par secteur : un produit d'un autre secteur est traité comme
        # introuvable, jamais modifiable depuis ici. `tous_sites=True` :
        # gestion du catalogue, l'admin doit pouvoir corriger la fiche d'un
        # autre site (même périmètre que `GET /products?all_sites=true`, qui
        # alimente précisément cet écran).
        row = _produit_accessible(conn, product_id, admin, tous_sites=True)
        if not row:
            raise HTTPException(status_code=404, detail="Produit introuvable")

        # Garde-fou : ne pas changer le type d'unité s'il y a des mouvements
        if "unit_type" in updates and updates["unit_type"] != row["unit_type"]:
            nb_mvts = conn.execute(
                "SELECT COUNT(*) AS n FROM document_lines dl "
                "JOIN documents d ON d.id = dl.document_id "
                "WHERE dl.product_id = ? AND d.cancelled_at IS NULL",
                (product_id,),
            ).fetchone()["n"]
            if nb_mvts > 0:
                raise HTTPException(
                    status_code=422,
                    detail="Impossible de changer le type d'unité : des mouvements "
                           "existent pour ce produit",
                )

        # Garde-fou : archivage. La colonne est NOT NULL, et archiver par ici
        # ne doit pas contourner le contrôle de DELETE /products/{id} (un
        # produit encore en stock disparaîtrait des totaux sans mouvement).
        if "archived" in updates:
            if updates["archived"] is None:
                raise HTTPException(
                    status_code=422,
                    detail="Le champ « archivé » ne peut pas être vide (0 ou 1)",
                )
            updates["archived"] = 1 if updates["archived"] else 0
            if updates["archived"] == 1 and not row["archived"] and row["current_stock"] > 1e-9:
                raise HTTPException(
                    status_code=422,
                    detail=f"Ce produit a encore {row['current_stock']:g} {row['unit']} en stock. "
                           f"Mettez le stock à zéro par un ajustement avant d'archiver ce produit.",
                )
            # Réactivation : le code-barres de la fiche archivée a pu être
            # réattribué entre-temps (l'index unique ne couvre que les actifs).
            # Sans ce contrôle, la réactivation échouait en IntegrityError
            # brute — écran d'administration bloqué sans explication.
            code_repris = updates.get("barcode", row["barcode"] if "barcode" in row.keys() else None)
            if updates["archived"] == 0 and row["archived"] and code_repris:
                autre = _produit_par_code_barres(conn, code_repris, secteur, sauf_id=product_id)
                if autre:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Le code-barres « {code_repris} » de ce produit est "
                               f"maintenant utilisé par {autre['sku']} — {autre['name']}. "
                               f"Retire-le de l'une des deux fiches avant de réactiver.")

        # Garde-fou : SKU unique. NOTE : products.sku est UNIQUE en base pour
        # TOUS les secteurs confondus (limite connue, voir create_product) —
        # ce contrôle applicatif reste scopé au secteur pour son message,
        # mais l'UPDATE plus bas peut malgré tout heurter un SKU d'un autre
        # secteur ; il est protégé par un try/except IntegrityError.
        if "sku" in updates and updates["sku"] != row["sku"]:
            doublon = conn.execute(
                "SELECT id FROM products WHERE sku = ? AND id != ? AND sector = ?",
                (updates["sku"], product_id, secteur),
            ).fetchone()
            if doublon:
                raise HTTPException(
                    status_code=422,
                    detail=f"Le SKU existe déjà sur un autre produit",
                )

        # Garde-fou : code-barres unique parmi les produits ACTIFS DU SECTEUR.
        # 409 (et non 422 comme le SKU) : c'est bien un conflit avec une
        # ressource existante, et l'écran de rattachement rapide s'en sert
        # pour dire QUEL produit porte déjà le code plutôt que d'échouer sans
        # raison.
        if updates.get("barcode"):
            autre = _produit_par_code_barres(conn, updates["barcode"], secteur, sauf_id=product_id)
            if autre:
                raise HTTPException(
                    status_code=409,
                    detail=f"Le code-barres « {updates['barcode']} » est déjà utilisé "
                           f"par le produit {autre['sku']} — {autre['name']}")

        # Traiter initial_stock séparément pour vérifier le stock négatif
        if "initial_stock" in updates:
            new_initial = updates["initial_stock"]
            old_initial = row["initial_stock"]
            diff = new_initial - old_initial
            new_current = row["current_stock"] + diff
            if new_current < -1e-9:
                raise HTTPException(
                    status_code=422,
                    detail=f"Ce changement rendrait le stock négatif "
                           f"({row['current_stock']} + {diff:g} = {new_current:g})",
                )
            # Garde-fou : un produit déjà mouvementé ne doit plus voir son
            # stock de début modifié directement — ce PATCH contourne
            # entièrement /adjustments (motif obligatoire, plafond physique)
            # ET reste invisible de /stock/reconciliation, puisque
            # ajuster_ouverture (plus bas) réaligne le mouvement d'ouverture
            # sur la nouvelle valeur : aucun écart n'apparaît jamais. Seul un
            # produit sans mouvement actif reste corrigible ici — même
            # périmètre que le garde-fou unit_type ci-dessus, dont la requête
            # est reprise à l'identique.
            #
            # Bornée au cycle courant, comme calculer_reconciliation : un
            # produit rebasé par reset_initial n'a plus d'historique PRÉ-reset
            # à protéger (déjà absorbé dans le nouvel initial_stock) ; seuls
            # les mouvements POSTÉRIEURS au reset comptent. Sans cette clause,
            # un produit d'un entrepôt déjà rebasé resterait bloqué à vie,
            # y compris pour corriger une faute de frappe commise APRÈS
            # le reset.
            #
            # Ignore aussi bien sûr la valeur inchangée (diff nul : un client
            # qui renvoie son formulaire entier sans y toucher ne doit pas
            # être bloqué) que les mouvements annulés (d.cancelled_at IS
            # NULL) : un produit dont l'unique mouvement a été annulé est
            # revenu exactement à son état d'ouverture, rien à protéger.
            if abs(diff) > 1e-9:
                reset_doc_id = row["initial_stock_reset_doc_id"]
                nb_mvts = conn.execute(
                    "SELECT COUNT(*) AS n FROM document_lines dl "
                    "JOIN documents d ON d.id = dl.document_id "
                    "WHERE dl.product_id = ? AND d.cancelled_at IS NULL "
                    "AND (? IS NULL OR d.id > ?)",
                    (product_id, reset_doc_id, reset_doc_id),
                ).fetchone()["n"]
                if nb_mvts > 0:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Impossible de modifier le stock de début : "
                               f"{nb_mvts} mouvement(s) existent déjà pour ce "
                               f"produit. Utilisez un ajustement d'inventaire "
                               f"(POST /adjustments) pour corriger le stock "
                               f"d'un produit déjà mouvementé.",
                    )

        # Appliquer les mises à jour
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [product_id]
        try:
            conn.execute(f"UPDATE products SET {set_clause} WHERE id = ?", values)
        except sqlite3.IntegrityError:
            # Le contrôle applicatif ci-dessus est scopé au secteur ; l'index
            # UNIQUE(sku) en base, lui, ne l'est pas (limite connue). Un SKU
            # qui existe déjà dans UN AUTRE secteur atterrit ici plutôt qu'en
            # 500 brut.
            raise HTTPException(status_code=409, detail="Ce SKU est déjà utilisé")

        if "initial_stock" in updates:
            new_initial = updates["initial_stock"]
            old_initial = row["initial_stock"]
            diff = new_initial - old_initial
            conn.execute(
                "UPDATE products SET current_stock = ROUND(current_stock + ?, 3) WHERE id = ?",
                (diff, product_id),
            )
            # Réaligner le mouvement d'ouverture sur le nouveau stock initial.
            # `ajuster_ouverture` CRÉE le mouvement s'il manque : un produit
            # ouvert à 0 n'en a aucun (enregistrer_ouverture sort si <= 0), et
            # un simple UPDATE ne touchait alors aucune ligne — le grand livre
            # restait à 0 pendant que current_stock montait, soit un écart
            # permanent en réconciliation.
            ajuster_ouverture(conn, product_id, new_initial)

        updated_row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()

        # Piste d'audit : modifier un produit peut changer le SKU, le nom ou le
        # stock. Sans trace, une correction de stock passée par ici serait
        # indiscernable d'une saisie d'origine.
        champs = set(updates) | ({"current_stock"} if "initial_stock" in updates else set())
        old_values = {k: row[k] for k in champs if k in row.keys()}
        new_values = {k: updated_row[k] for k in champs if k in updated_row.keys()}

        _journaliser_audit(conn, "product", product_id, "update",
                           old=old_values, new=new_values,
                           username=(admin["username"] if admin else "système"),
                           sector=secteur)

        log.info("produit %d modifie par %s : %s", product_id, (admin["username"] if admin else "système"), updates)
        resultat = dict(updated_row)

    # APRÈS le COMMIT, jamais dedans : vidé à l'intérieur de la transaction, un
    # GET /dashboard concurrent (les endpoints sync tournent dans un pool de
    # threads) repeuplait le cache avec les valeurs d'AVANT l'écriture, et ce
    # cache périmé était alors servi pendant les 20 s suivantes.
    _invalider_cache_dashboard(secteur)
    return resultat


# Fiche « vie d'un produit ». Plafond par source, pas global : sans lui, un
# produit très mouvementé noierait ses changements de fiche sous mille lignes
# de bons, alors que c'est justement le croisement des deux qui est utile.
HISTORIQUE_PRODUIT_MAX = 200
HISTORIQUE_PRODUIT_DEFAUT = 50

# Libellés des types de bon, côté serveur : le client affiche la valeur telle
# quelle et n'a pas à connaître la liste. Un type inconnu reste affiché sous
# son code brut plutôt que masqué.
LIBELLES_TYPE_DOCUMENT = {
    "RECEIVING": "Réception",
    "DELIVERY": "Expédition",
    "RETURN": "Retour terrain",
    "SUPPLIER_RETURN": "Retour fournisseur",
    "ADJUSTMENT": "Ajustement d'inventaire",
    "REGIONAL_TRANSFER": "Transfert inter-régional",
}

# Idem pour les actions du journal d'audit qui concernent un produit.
LIBELLES_ACTION_AUDIT = {
    "create": "Création de la fiche",
    "update": "Modification de la fiche",
    "threshold": "Changement de seuil",
    "archive": "Archivage",
    "photo": "Photo ajoutée",
    "photo_suppression": "Photo retirée",
    "import": "Import de catalogue",
}


def _date_triable(valeur) -> str:
    """Date comparable, quel que soit le format écrit en base.

    `normaliser` lève sur une valeur non reconnue — comportement voulu à
    l'écriture, inacceptable à la lecture d'un historique : une seule ligne
    mal datée priverait l'utilisateur de toute la fiche. Ici, l'irrécupérable
    devient une chaîne vide, donc la plus ancienne du tri.
    """
    try:
        return normaliser(valeur) or ""
    except (ValueError, TypeError, AttributeError):
        return ""


@app.get("/products/{product_id}/historique")
def historique_produit(product_id: int,
                       limite: int = Query(HISTORIQUE_PRODUIT_DEFAUT, ge=1,
                                           le=HISTORIQUE_PRODUIT_MAX),
                       user=Depends(exiger_utilisateur)):
    """Toute la vie d'un produit en une seule réponse, du plus récent au plus ancien.

    Il fallait jusqu'ici croiser trois écrans pour répondre à « qu'est-il
    arrivé à cet article ? » : Historique pour les bons, Journal d'audit pour
    les changements de fiche, Rapport d'ajustements pour les comptages. Aucun
    des trois ne montrait les autres, et c'est précisément l'enchaînement qui
    explique un écart (« le seuil a été changé la veille du comptage »).

    Deux sources, fusionnées et datées :
      - `mouvement` : les lignes de bons où le produit apparaît, avec le type
        du bon, la quantité, la région et l'auteur. Les bons ANNULÉS sont
        conservés et marqués : un bon annulé fait partie de l'histoire, le
        masquer laisserait un trou inexplicable dans la chronologie.
      - `fiche` : les entrées d'`audit_log` portant sur CE produit
        (entity_type='product', entity_id=id). L'import de catalogue, qui
        s'enregistre sous entity_id=0, n'y figure donc pas : il ne dit rien de
        ce produit en particulier.

    Ouvert à tout compte authentifié, comme l'écran Historique : rien ici n'est
    plus sensible que ce qu'un magasinier voit déjà bon par bon.
    """
    with get_conn() as conn:
        # Scopé par secteur : un id d'un autre secteur est traité comme
        # introuvable. Les requêtes suivantes filtrent par `product_id`, déjà
        # garanti appartenir à ce secteur une fois cette vérification passée
        # (les id de `products` sont uniques tous secteurs confondus).
        # Scopé aussi par SITE (`_produit_accessible`). `tous_sites=True` :
        # l'historique s'ouvre depuis la fiche produit, et l'admin peut
        # légitimement ouvrir la fiche d'un autre site de son secteur
        # (all_sites) — refuser ici casserait ce bouton pour lui.
        produit = _produit_accessible(conn, product_id, user, tous_sites=True)
        if produit is None:
            raise HTTPException(status_code=404, detail="Produit introuvable")

        mouvements = conn.execute(
            """
            SELECT d.id AS document_id, d.type, d.created_at, d.region,
                   d.created_by, d.operator, d.reference, d.cancelled_at,
                   d.technician, d.source_region,
                   dl.quantity, dl.received_quantity
            FROM document_lines dl
            JOIN documents d ON d.id = dl.document_id
            WHERE dl.product_id = ?
            ORDER BY d.created_at DESC, d.id DESC
            LIMIT ?
            """,
            (product_id, limite),
        ).fetchall()

        fiche = conn.execute(
            """
            SELECT id, action, old_values, new_values, username, created_at
            FROM audit_log
            WHERE entity_type = 'product' AND entity_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (product_id, limite),
        ).fetchall()

    entrees: list[dict] = []
    for m in mouvements:
        entrees.append({
            "categorie": "mouvement",
            "date": m["created_at"],
            "libelle": LIBELLES_TYPE_DOCUMENT.get(m["type"], m["type"]),
            "type": m["type"],
            "document_id": m["document_id"],
            "quantite": m["quantity"],
            "quantite_recue": m["received_quantity"],
            "region": m["region"],
            "source_region": m["source_region"],
            "technician": m["technician"],
            "operator": m["operator"],
            "reference": m["reference"],
            "auteur": m["created_by"],
            "annule": bool(m["cancelled_at"]),
        })
    for f in fiche:
        entrees.append({
            "categorie": "fiche",
            "date": f["created_at"],
            "libelle": LIBELLES_ACTION_AUDIT.get(f["action"], f["action"]),
            "action": f["action"],
            "audit_id": f["id"],
            "old_values": f["old_values"],
            "new_values": f["new_values"],
            "auteur": f["username"],
            "annule": False,
        })

    # Tri unique sur la date normalisée : `created_at` peut porter un « T »
    # (bons remontés par le client) ou un espace selon la source, et deux
    # formats mélangés se trient faux en comparaison de chaînes brutes.
    # `_date_triable` ne lève pas : une date illisible en base doit repousser
    # sa ligne en fin de liste, jamais faire échouer la fiche entière.
    entrees.sort(key=lambda e: (_date_triable(e["date"]), e["categorie"]),
                 reverse=True)
    entrees = entrees[:limite]

    return {
        "product_id": produit["id"],
        "sku": produit["sku"],
        "name": produit["name"],
        "unit": produit["unit"],
        "current_stock": produit["current_stock"],
        "limite": limite,
        "nb_mouvements": sum(1 for e in entrees if e["categorie"] == "mouvement"),
        "nb_fiche": sum(1 for e in entrees if e["categorie"] == "fiche"),
        "entrees": entrees,
    }


@app.get("/stock", response_model=list[ProductOut])
def get_stock(user=Depends(exiger_utilisateur)):
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        site = _site_du_compte(conn, user)
    return _lister_produits(sector=secteur, site=site)


@app.get("/stock/export")
def export_stock(user=Depends(exiger_utilisateur)):
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        site = _site_du_compte(conn, user)
        sql = ("SELECT * FROM products WHERE COALESCE(archived, 0) = 0 AND sector = ? ")
        params: list = [secteur]
        if site:
            sql += "AND (site IS NULL OR site = ?) "
            params.append(site)
        sql += "ORDER BY name"
        products = [dict(r) for r in conn.execute(sql, params).fetchall()]
    content = build_stock_excel(products)
    filename = export_filename("stock")
    _journaliser_export("stock", user, nb_produits=len(products))
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Fenêtre d'observation de la consommation servant à la couverture. 90 jours
# (trois mois) : assez long pour absorber le creux d'une semaine sans
# expédition, assez court pour suivre une variation réelle d'activité.
JOURS_CONSOMMATION_REAPPRO = 90


def _consommation_mensuelle(conn, jours: int = JOURS_CONSOMMATION_REAPPRO) -> dict[int, float]:
    """Sortie moyenne PAR MOIS de chaque produit, sur les `jours` derniers jours.

    « Sortie » a ici le même sens que dans `indicateurs.consommation_regionale` :
    les bons `DELIVERY` et `REGIONAL_TRANSFER` non annulés. Un ajustement n'est
    pas une consommation, et un retour est un flux inverse dont l'inclusion
    ferait sous-estimer le besoin réel.

    Renvoie `{product_id: quantité par mois}`. Un produit sans aucune sortie
    sur la fenêtre est absent du dictionnaire (et non à zéro) : l'appelant
    doit pouvoir distinguer « pas de consommation connue » de « zéro mesuré ».
    """
    depuis = (maintenant() - timedelta(days=jours)).strftime(FORMAT_DATETIME)
    rows = conn.execute(
        """
        SELECT dl.product_id AS product_id, SUM(dl.quantity) AS total
        FROM document_lines dl
        JOIN documents d ON d.id = dl.document_id
        WHERE d.cancelled_at IS NULL
          AND d.type IN ('DELIVERY', 'REGIONAL_TRANSFER')
          AND d.created_at >= ?
        GROUP BY dl.product_id
        HAVING SUM(dl.quantity) > 0
        """,
        (depuis,),
    ).fetchall()
    mois = jours / 30.0
    return {r["product_id"]: float(r["total"]) / mois for r in rows}


def _lignes_reappro(conn, mois_couverture: float | None = None,
                    sector: str = SECTEUR_DEFAUT,
                    site: str | None = None) -> list[dict]:
    """Produits sous leur seuil mini, avec la quantité à commander.

    Ne concerne que les produits dont un seuil a réellement été configuré
    (min_stock > 0) : un produit jamais paramétré (min_stock = 0 par défaut)
    n'a rien à « commander » au sens de ce rapport, même à stock nul — c'est
    le rôle des alertes du tableau de bord, pas de celui-ci.

    `mois_couverture` (facultatif) change la quantité suggérée : au lieu du
    strict minimum pour repasser le seuil — qui garantit de recommander la
    semaine suivante —, on commande de quoi tenir ce nombre de mois au rythme
    de consommation récent. Le minimum reste un PLANCHER : la couverture ne
    peut jamais faire commander moins que ce qu'il faut pour sortir de
    l'alerte. Sans ce paramètre, le calcul est inchangé.
    """
    site_clause = "AND (site IS NULL OR site = ?)" if site else ""
    rows = conn.execute(
        f"""
        SELECT id, sku, name, category, unit, current_stock, min_stock
        FROM products
        WHERE sector = ? AND COALESCE(archived, 0) = 0
          AND min_stock > 0 AND current_stock <= min_stock
          {site_clause}
        ORDER BY (current_stock - min_stock), name
        """,
        (sector,) + ((site,) if site else ()),
    ).fetchall()
    consommation = (_consommation_mensuelle(conn)
                    if mois_couverture and mois_couverture > 0 else {})
    lignes = []
    for r in rows:
        d = dict(r)
        product_id = d.pop("id")
        # Commande de quoi remonter au seuil : le strict minimum pour sortir
        # de la zone d'alerte, pas une reconstitution de stock arbitraire.
        minimum = max(d["min_stock"] - d["current_stock"], 0)
        quantite = minimum
        if mois_couverture and mois_couverture > 0:
            par_mois = consommation.get(product_id)
            if par_mois:
                # Ce qui manque pour tenir `mois_couverture` mois, stock actuel
                # déduit : commander la consommation entière ignorerait ce qui
                # reste en rayon et gonflerait la commande d'autant.
                besoin = par_mois * mois_couverture - d["current_stock"]
                quantite = max(minimum, besoin)
            d["consommation_mensuelle"] = (
                arrondir(par_mois) if par_mois else None)
            d["mois_couverture"] = mois_couverture
        d["quantite_a_commander"] = arrondir(quantite)
        lignes.append(d)
    return lignes


# Bornes de `mois_couverture` : en dessous d'un demi-mois la couverture ne dit
# rien de plus que le seuil lui-même, et au-delà de deux ans on immobilise du
# stock (et de la trésorerie) pour un besoin qui n'est plus prévisible.
MOIS_COUVERTURE_MIN = 0.5
MOIS_COUVERTURE_MAX = 24.0


@app.get("/reports/reappro")
def reports_reappro(
    mois_couverture: Optional[float] = Query(
        None, ge=MOIS_COUVERTURE_MIN, le=MOIS_COUVERTURE_MAX),
    user=Depends(exiger_utilisateur),
):
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        site = _site_du_compte(conn, user)
        return _lignes_reappro(conn, mois_couverture, sector=secteur, site=site)


@app.get("/reports/reappro/export")
def reports_reappro_export(
    mois_couverture: Optional[float] = Query(
        None, ge=MOIS_COUVERTURE_MIN, le=MOIS_COUVERTURE_MAX),
    user=Depends(exiger_utilisateur),
):
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        site = _site_du_compte(conn, user)
        lignes = _lignes_reappro(conn, mois_couverture, sector=secteur, site=site)
    content = build_reappro_excel(lignes)
    filename = export_filename("reapprovisionnement")
    _journaliser_export("reapprovisionnement", user, nb_lignes=len(lignes))
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/reports/ajustements")
def reports_ajustements(jours: int = Query(7, ge=1, le=365),
                        admin=Depends(exiger_admin)):
    """Synthèse des ajustements de stock par opérateur, sur une fenêtre glissante.

    Un magasinier seul peut corriger le stock affiché (garde-fous chiffrés
    seulement — plafond physique, confirmation d'écart important). Sans ce
    rapport, rien ne permettait à l'administrateur de repérer un opérateur
    dont les ajustements sortent de l'ordinaire, autrement qu'en dépouillant
    le journal d'audit bon par bon.
    """
    depuis = (maintenant() - timedelta(days=jours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, admin)
        site = _site_du_compte(conn, admin)
        site_clause = "AND (p.site IS NULL OR p.site = ?)" if site else ""
        lignes = conn.execute(
            f"""
            SELECT d.id AS document_id, d.created_by AS operateur,
                   d.reference AS motif, d.created_at,
                   dl.product_id, p.sku, p.name, dl.quantity AS ecart,
                   dl.counted_quantity
            FROM documents d
            JOIN document_lines dl ON dl.document_id = d.id
            JOIN products p ON p.id = dl.product_id
            WHERE d.sector = ? AND d.type = 'ADJUSTMENT' AND d.cancelled_at IS NULL
              AND d.created_at >= ? {site_clause}
            ORDER BY d.created_at DESC
            """,
            (secteur, depuis) + ((site,) if site else ()),
        ).fetchall()

    par_operateur: dict[str, dict] = {}
    for l in lignes:
        operateur = l["operateur"] or "inconnu"
        stats = par_operateur.setdefault(operateur, {
            "operateur": operateur, "nb_ajustements": set(),
            "nb_lignes": 0, "ecart_absolu_total": 0.0,
        })
        stats["nb_ajustements"].add(l["document_id"])
        stats["nb_lignes"] += 1
        stats["ecart_absolu_total"] += abs(l["ecart"])

    par_operateur_liste = sorted(
        (
            {**stats, "nb_ajustements": len(stats["nb_ajustements"]),
             "ecart_absolu_total": arrondir(stats["ecart_absolu_total"])}
            for stats in par_operateur.values()
        ),
        key=lambda s: s["ecart_absolu_total"], reverse=True,
    )

    return {
        "depuis": depuis,
        "jours": jours,
        "par_operateur": par_operateur_liste,
        "lignes": [dict(l) for l in lignes],
    }


# ------------------------------------------------- indicateurs de pilotage
#
# Quatre mesures réservées à l'administrateur, regroupées sous /reports comme
# les rapports existants. Chacune délègue son calcul à server/indicateurs.py :
# app.py ne fait qu'ouvrir la connexion et exposer le résultat, pour que les
# formules restent testables sans passer par HTTP.


@app.get("/reports/precision-inventaire")
def reports_precision_inventaire(jours_cycle: int = Query(7, ge=1, le=90),
                                 admin=Depends(exiger_admin)):
    """Fiabilité du stock informatique mesurée sur le dernier cycle de comptage."""
    with get_conn() as conn:
        return precision_inventaire(conn, jours_cycle,
                                    sector=_secteur_du_compte(conn, admin))


@app.get("/reports/concentration-regions")
def reports_concentration_regions(jours: int = Query(90, ge=1, le=730),
                                  admin=Depends(exiger_admin)):
    """Part de chaque région dans le volume livré, avec cumul (logique ABC)."""
    with get_conn() as conn:
        return concentration_regions(conn, jours,
                                     sector=_secteur_du_compte(conn, admin))


@app.get("/reports/comptages-a-temps")
def reports_comptages_a_temps(admin=Depends(exiger_admin)):
    """Régions à entrepôt ayant rendu un comptage sur le mois calendaire en cours."""
    with get_conn() as conn:
        return comptages_a_temps(conn, sector=_secteur_du_compte(conn, admin))


@app.get("/reports/anomalies")
def reports_anomalies(facteur: float = Query(3.0, ge=1.5, le=50.0),
                      limite: int = Query(10, ge=1, le=100),
                      admin=Depends(exiger_admin)):
    """Mouvements du mois anormalement gros face à l'habitude de leur produit."""
    with get_conn() as conn:
        return anomalies_mouvements(conn, facteur=facteur, limite=limite,
                                    sector=_secteur_du_compte(conn, admin))


@app.get("/reports/consommation-regionale")
def reports_consommation_regionale(mois: int = Query(12, ge=1, le=36),
                                   admin=Depends(exiger_admin)):
    """Sorties mensuelles par région et par produit sur les derniers mois."""
    with get_conn() as conn:
        return consommation_regionale(conn, mois=mois,
                                      sector=_secteur_du_compte(conn, admin))


@app.get("/reports/produits-dormants")
def reports_produits_dormants(jours: int = Query(180, ge=30, le=1095),
                              admin=Depends(exiger_admin)):
    """Produits actifs sans aucune sortie depuis `jours` jours, et leur valeur."""
    with get_conn() as conn:
        return produits_dormants(conn, jours=jours,
                                 sector=_secteur_du_compte(conn, admin))


@app.get("/reports/resolution-alertes")
def reports_resolution_alertes(jours: int = Query(90, ge=7, le=730),
                               admin=Depends(exiger_admin)):
    """Signalements régionaux résolus vs ouverts, et délai moyen de résolution."""
    with get_conn() as conn:
        return resolution_alertes(conn, jours=jours,
                                  sector=_secteur_du_compte(conn, admin))


@app.get("/reports/dette-consigne")
def reports_dette_consigne(admin=Depends(exiger_admin)):
    """Bouteilles consignées non rendues, par région et par technicien."""
    with get_conn() as conn:
        return dette_consigne(conn, sector=_secteur_du_compte(conn, admin))


@app.get("/reports/activite-operateurs")
def reports_activite_operateurs(jours: int = Query(30, ge=1, le=365),
                                admin=Depends(exiger_admin)):
    """Bons saisis par compte sur la période, et répartition par heure."""
    with get_conn() as conn:
        return activite_operateurs(conn, jours=jours,
                                   sector=_secteur_du_compte(conn, admin))


@app.get("/reports/ecarts-reception")
def reports_ecarts_reception(jours: int = Query(90, ge=1, le=730),
                             admin=Depends(exiger_admin)):
    """Écarts expédié / reçu cumulés, par région destinataire et transporteur.

    Réservé aux administrateurs comme les autres rapports de pilotage : il
    nomme des transporteurs et chiffre ce qui se perd en route.
    """
    with get_conn() as conn:
        return ecarts_reception(conn, jours=jours,
                                sector=_secteur_du_compte(conn, admin))


@app.get("/reports/delai-fournisseurs")
def reports_delai_fournisseurs(jours: int = Query(365, ge=30, le=1825),
                               min_receptions: int = Query(2, ge=2, le=50),
                               admin=Depends(exiger_admin)):
    """Régularité d'approvisionnement par fournisseur (admin).

    ATTENTION AU NOM : ce n'est PAS un délai commande -> livraison. Le schéma
    ne porte aucune date de commande (vérifié : `created_at` est la date de
    saisie de la réception, `server_received_at` celle de son arrivée sur le
    serveur, `received_at` ne concerne que les transferts régionaux). La
    mesure renvoyée est l'intervalle entre deux réceptions successives d'un
    même fournisseur — un proxy de régularité. Voir l'en-tête de la section 11
    de `indicateurs.py` pour le détail du raisonnement.

    Réservé aux administrateurs comme les autres rapports de pilotage : il
    nomme des fournisseurs et juge leur tenue.
    """
    with get_conn() as conn:
        return delai_fournisseurs(conn, jours=jours, min_receptions=min_receptions,
                                   sector=_secteur_du_compte(conn, admin))


FORMAT_DATE = "%Y-%m-%d"


def _borne_date(valeur: Optional[str], defaut: Optional[date],
                champ: str) -> Optional[date]:
    """Convertit une date `AAAA-MM-JJ` venue de la requête, ou renvoie le défaut.

    `defaut` peut valoir None : c'est le cas des filtres facultatifs (une
    borne absente = pas de filtre de ce côté), par opposition aux exports qui
    imposent toujours une période.
    """
    if not valeur:
        return defaut
    try:
        return datetime.strptime(valeur.strip(), FORMAT_DATE).date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Date invalide pour « {champ} » : attendu AAAA-MM-JJ.",
        )


@app.get("/reports/comptable/export")
def reports_comptable_export(depuis: Optional[str] = None,
                             jusqu_a: Optional[str] = Query(None, alias="jusqu_a"),
                             admin=Depends(exiger_admin)):
    """Classeur comptable : valeur du stock + mouvements valorisés de la période.

    Réservé aux administrateurs : c'est le seul export qui expose les coûts
    d'achat ligne à ligne, ce qu'un magasinier n'a pas à voir.

    Défaut : les 30 derniers jours. Les bons annulés sont exclus — ils n'ont
    jamais eu d'effet comptable.

    LIMITE ASSUMÉE : la valeur d'un mouvement est calculée avec le coût
    unitaire ACTUEL du produit, faute d'historique des prix dans le schéma.
    C'est écrit noir sur blanc dans une note en haut de la feuille Excel, pour
    que personne ne prenne le fichier pour une valorisation au coût d'époque.
    """
    aujourdhui = maintenant().date()
    fin = _borne_date(jusqu_a, aujourdhui, "jusqu_a")
    debut = _borne_date(depuis, fin - timedelta(days=30), "depuis")
    if debut > fin:
        raise HTTPException(status_code=400,
                            detail="La date de début est postérieure à la date de fin.")

    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, admin)
        produits = [dict(r) for r in conn.execute(
            """
            SELECT sku, name, unit, current_stock, unit_cost
            FROM products
            WHERE sector = ? AND COALESCE(archived, 0) = 0
            ORDER BY name
            """,
            (secteur,),
        ).fetchall()]
        # `date(d.created_at)` plutôt qu'une comparaison de chaînes : created_at
        # porte l'heure, un simple `<= '2026-09-01'` exclurait toute la journée
        # du 1er septembre.
        mouvements = [dict(r) for r in conn.execute(
            """
            SELECT d.created_at, d.type, d.region, d.reference, d.operator,
                   p.sku, p.name, p.unit_cost, dl.quantity
            FROM document_lines dl
            JOIN documents d ON d.id = dl.document_id
            JOIN products p ON p.id = dl.product_id
            WHERE d.sector = ? AND d.cancelled_at IS NULL
              AND date(d.created_at) BETWEEN ? AND ?
            ORDER BY d.created_at, d.id, dl.id
            """,
            (secteur, debut.strftime(FORMAT_DATE), fin.strftime(FORMAT_DATE)),
        ).fetchall()]

    content = build_comptable_excel(produits, mouvements,
                                    debut.strftime(FORMAT_DATE),
                                    fin.strftime(FORMAT_DATE))
    filename = export_filename("comptable")
    _journaliser_export("comptable", admin,
                        depuis=debut.strftime(FORMAT_DATE),
                        jusqu_a=fin.strftime(FORMAT_DATE),
                        nb_mouvements=len(mouvements))
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _corps_rapport_mensuel(donnees: dict) -> str:
    """Rapport lisible en texte brut, à partir des chiffres de /dashboard.

    Texte brut et non HTML : ce message est lu sur un téléphone, souvent hors
    de l'entrepôt. Rien à cliquer, rien à charger.
    """
    resume = donnees.get("resume") or {}
    mouvements = donnees.get("mouvements") or {}
    alertes = donnees.get("alertes") or []

    lignes = [
        f"Rapport de stock — {maintenant_texte()[:10]}",
        "",
        f"Références actives : {resume.get('references_total', 0)}",
        f"Unités en stock : {resume.get('unites_totales', 0):g}",
        f"Valeur totale du stock : {resume.get('valeur_totale', 0):,.2f}",
        f"Références non valorisées : {resume.get('non_valorises', 0)}",
        f"Ruptures : {resume.get('en_rupture', 0)}",
        f"Sous le seuil : {resume.get('sous_seuil', 0)}",
        "",
        "Mouvements des 30 derniers jours :",
        f"  Reçu : {mouvements.get('recu_30j', 0):g}",
        f"  Livré : {mouvements.get('livre_30j', 0):g}",
        "",
    ]
    if alertes:
        lignes.append(f"Produits à réapprovisionner ({len(alertes)}) :")
        # 20 lignes maximum : au-delà, un e-mail devient illisible et le
        # rapport de réapprovisionnement complet s'exporte depuis l'application.
        for a in alertes[:20]:
            lignes.append(f"  - {a.get('sku')} {a.get('name')} : "
                          f"{a.get('current_stock', 0):g} (seuil {a.get('min_stock', 0):g})")
        if len(alertes) > 20:
            lignes.append(f"  ... et {len(alertes) - 20} autre(s).")
    else:
        lignes.append("Aucun produit sous son seuil.")
    lignes += ["", "Message automatique du serveur WMS."]
    return "\n".join(lignes)


@app.post("/reports/mensuel/envoyer-maintenant")
def envoyer_rapport_mensuel(background: BackgroundTasks, admin=Depends(exiger_admin)):
    """Envoie le rapport de stock par e-mail, à la demande d'un administrateur.

    Déclenchement MANUEL uniquement : il n'y a volontairement pas de
    planificateur mensuel dans cette passe (voir RISQUES.md). L'envoi part en
    tâche de fond — la réponse HTTP ne doit pas attendre un relais SMTP lent.
    """
    destinataires = destinataires_alertes()
    if not destinataires:
        raise HTTPException(
            status_code=400,
            detail="Aucun destinataire configuré. L'administrateur doit créer "
                   "email_config.json sur le serveur (clé « destinataires_alertes »).",
        )

    # `@app.get` renvoie la fonction inchangée : on réutilise directement la
    # génération de /dashboard plutôt que de dupliquer ses requêtes.
    corps = _corps_rapport_mensuel(dashboard(admin))
    sujet = f"[WMS] Rapport de stock — {maintenant_texte()[:10]}"
    background.add_task(envoyer_email, destinataires, sujet, corps)
    return {"envoi": "programme", "destinataires": destinataires}


@app.get("/reference/regions")
def reference_regions(sector: str | None = None, user=Depends(exiger_utilisateur)):
    """Référentiel régions/superviseurs, scopé par secteur. Le client s'aligne
    dessus au démarrage.

    Les régions, superviseurs et opérateurs d'entrepôt sont scopés par
    secteur : un compte FON ne doit jamais se voir proposer un nom Consumables
    (et réciproquement) dans son sélecteur de réception/inventaire/livraison.

    Le paramètre `sector` n'est respecté que pour un admin : c'est le seul cas
    où quelqu'un doit voir les régions d'un secteur qui n'est pas le sien —
    un admin Consumables créant un compte régional FON doit voir les régions
    FON dans le formulaire, pas les siennes.
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        if sector and user and user.get("role") == "admin":
            secteur = sector
    regions = regions_for(secteur)
    return {
        "regions": regions,
        "supervisors_by_region": {r: supervisors_for(r, secteur) for r in regions},
        "warehouse_operators": warehouse_operators_for(secteur),
    }


@app.get("/reference/technicians")
def reference_technicians(user=Depends(exiger_utilisateur)):
    """Référentiel des techniciens livrés individuellement, scopé par secteur."""
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
    return {"technicians": technicians_for(secteur)}


@app.get("/reference/fuel")
def reference_fuel(user=Depends(exiger_utilisateur)):
    """Liste des « projets » Fuel connus — une étiquette budgétaire souple,
    pas une autorisation : sert de suggestions pour l'autocomplétion, la
    saisie n'est jamais bloquée sur un nom encore inconnu."""
    if os.environ.get("WMS_DEMO_MODE") == "1":
        return {"projects": ["Operations", "Logistics", "Maintenance",
                             "Transport", "Emergency", "Construction"]}
    return {"projects": FUEL_PROJECTS}


@app.get("/fuel/vehicles")
def fuel_vehicles(user=Depends(exiger_utilisateur)):
    """Plaques déjà utilisées par ce secteur, les plus récentes d'abord.

    Pas de table dédiée pour ce lot (décision utilisateur) : l'autocomplétion
    se base directement sur l'historique des bons.
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        rows = conn.execute(
            "SELECT vehicle_plate, MAX(created_at) AS derniere "
            "FROM documents WHERE sector = ? AND vehicle_plate IS NOT NULL "
            "GROUP BY vehicle_plate ORDER BY derniere DESC LIMIT 100",
            (secteur,),
        ).fetchall()
    return {"plates": [r["vehicle_plate"] for r in rows]}


@app.get("/fuel/niveau-cuve/{product_id}")
def fuel_niveau_cuve(product_id: int, jours: int = Query(30, ge=1, le=365),
                     user=Depends(exiger_utilisateur)):
    """Niveau de la cuve dans le temps, pour tracer une courbe sur le
    tableau de bord — la jauge ne montre qu'un instantané, pas la vitesse à
    laquelle une cuve se vide ni si le rythme s'accélère.

    Aucune table d'historique de stock n'existe : le niveau à un instant
    donné est reconstruit à partir de `current_stock` (l'état présent, sûr)
    et des mouvements non annulés dans la fenêtre, rejoués À L'ENVERS pour
    retrouver le niveau au début de la fenêtre, puis À L'ENDROIT pour
    produire un point après chaque mouvement (courbe en escalier : plate
    entre deux mouvements, verticale à chaque réception/livraison).

    Réservé au secteur FUEL — c'est le seul qui affiche une jauge de cuve —
    et scopé comme le reste de la fiche produit (`tous_sites=True` : c'est
    une LECTURE, l'admin garde sa vue nationale, comme le tableau de bord).
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        if secteur != "FUEL":
            raise HTTPException(status_code=404, detail="Produit introuvable")
        produit = _produit_accessible(conn, product_id, user, tous_sites=True)
        if produit is None:
            raise HTTPException(status_code=404, detail="Produit introuvable")

        depuis = (maintenant() - timedelta(days=jours)).strftime(FORMAT_DATETIME)
        lignes = conn.execute(
            """
            SELECT d.id AS document_id, d.type, d.created_at,
                   dl.quantity AS quantite
            FROM document_lines dl
            JOIN documents d ON d.id = dl.document_id
            WHERE dl.product_id = ? AND d.cancelled_at IS NULL
              AND d.created_at >= ?
              AND d.type IN ('RECEIVING', 'DELIVERY', 'RETURN',
                             'SUPPLIER_RETURN', 'ADJUSTMENT')
            ORDER BY d.created_at ASC, d.id ASC
            """,
            (product_id, depuis),
        ).fetchall()

    # Même convention de signe que le reste de l'application (voir le
    # commentaire de `create_document` sur `delta_sign`) : un ADJUSTMENT
    # porte déjà son écart signé, les autres types ont un signe fixe.
    SIGNE = {"RECEIVING": 1, "RETURN": 1, "DELIVERY": -1, "SUPPLIER_RETURN": -1}
    mouvements = []
    total_periode = 0.0
    for l in lignes:
        delta = l["quantite"] if l["type"] == "ADJUSTMENT" else SIGNE[l["type"]] * l["quantite"]
        total_periode += delta
        mouvements.append({"created_at": l["created_at"], "type": l["type"],
                           "quantite": abs(l["quantite"]), "delta": delta})

    # Le niveau ACTUEL est la seule vérité sûre : on en repart pour calculer
    # celui du début de fenêtre, plutôt que de partir d'`initial_stock` (dont
    # la date peut être bien antérieure, avec des mouvements hors fenêtre non
    # comptés ici).
    niveau = arrondir(produit["current_stock"] - total_periode)
    points = [{"created_at": depuis, "niveau": niveau, "type": None,
              "quantite": None}]
    for m in mouvements:
        niveau = arrondir(niveau + m["delta"])
        points.append({"created_at": m["created_at"], "type": m["type"],
                       "quantite": m["quantite"], "niveau": niveau})

    return {
        "product_id": product_id, "name": produit["name"],
        "tank_capacity": produit["tank_capacity"], "jours": jours,
        "depuis": depuis, "niveau_actuel": arrondir(produit["current_stock"]),
        "points": points,
    }


# Le tableau de bord recalcule 8 agrégats (dont plusieurs balayages complets
# de document_lines) à chaque appel. Sans conséquence sur le volume actuel de
# l'entrepôt, mais un cache de courte durée coûte peu et évite que le
# polling de secours (30s, plusieurs postes) ne recalcule inutilement la même
# chose. 20s : assez court pour qu'un opérateur ne remarque jamais le
# décalage, assez long pour absorber les rafales de rafraîchissement.
_CACHE_DASHBOARD_TTL_S = 20
# Une entrée par secteur : un FON qui rafraîchit son tableau de bord ne doit
# ni voir les chiffres de Consumables, ni invalider leur cache à eux (et
# réciproquement).
# Clé : (secteur, site) — et non le secteur seul. Le tableau de bord est
# désormais scopé au site pour un compte rattaché à un site (voir
# `dashboard`) : gardé sous une clé purement sectorielle, le résumé de
# Canapé-Vert aurait été servi tel quel à WH Central, ce qui est exactement
# la fuite qu'on vient de fermer.
_cache_dashboard: dict[tuple[str, str | None], dict] = {}
_cache_dashboard_horodatage: dict[tuple[str, str | None], float] = {}


def _invalider_cache_dashboard(sector: str | None = None) -> None:
    """Appelé par toute écriture qui change le stock, le catalogue ou les
    bons : le TTL seul (20s) suffirait, mais laisserait un opérateur voir un
    résumé visiblement obsolète pendant que le sien vient de créer un bon.

    `sector=None` (appelants pas encore migrés à un secteur précis) invalide
    TOUS les secteurs — plus prudent qu'une invalidation partielle oubliée.

    Invalide TOUS les sites du secteur : une écriture peut concerner
    n'importe lequel, et les appelants ne connaissent que le secteur.
    """
    if sector is None:
        _cache_dashboard.clear()
        _cache_dashboard_horodatage.clear()
    else:
        for cle in [c for c in _cache_dashboard if c[0] == sector]:
            _cache_dashboard.pop(cle, None)
            _cache_dashboard_horodatage.pop(cle, None)


def _notifier_email_alertes_seuil(alertes: list[dict]) -> None:
    """Relaie par e-mail les ruptures de seuil déjà diffusées par WebSocket.

    Même contenu que le toast admin (broadcast_alert_triggered), pour qui n'a
    pas l'application ouverte. Court-circuite déjà si aucune config SMTP :
    voir `notifications_email.envoyer_en_arriere_plan`.

    En mode « digest_quotidien », l'alerte est mise de côté au lieu de partir
    tout de suite (voir la section « digest quotidien »). Le mode par défaut,
    et celui de toute installation qui ne demande rien, reste l'envoi immédiat.
    """
    if not alertes:
        return
    if digest_actif():
        _digest_ajouter_seuils(alertes)
        return
    destinataires = destinataires_alertes()
    if not destinataires:
        return
    lignes = [f"- {a['name']} ({a['sku']}) : stock {a['stock']}, seuil {a['seuil']}"
              for a in alertes]
    corps = "Seuil minimum atteint pour :\n\n" + "\n".join(lignes)
    envoyer_en_arriere_plan(destinataires, "WMS — alerte de rupture de seuil", corps)


def _notifier_email_alerte_regionale(result: dict) -> None:
    """Relaie par e-mail un signalement régional (rupture signalée par une
    région dépourvue d'entrepôt local), même patron que les alertes de seuil —
    mise de côté pour le résumé du jour si le mode digest est actif."""
    if digest_actif():
        _digest_ajouter_signalement(result)
        return
    destinataires = destinataires_alertes()
    if not destinataires:
        return
    corps = (f"Rupture signalée par la région {result['region']} :\n\n"
             f"- {result['name']} ({result['sku']})"
             + (f"\n  Note : {result['note']}" if result.get("note") else ""))
    envoyer_en_arriere_plan(destinataires, "WMS — signalement régional de rupture", corps)


@app.get("/dashboard")
def dashboard(user=Depends(exiger_utilisateur)):
    """Indicateurs temps réel du stock du secteur — et du site — de l'appelant.

    Scopé par SITE pour un compte rattaché à un site, admin excepté. Sans ce
    filtre, Orelus (magasinier de WH Central, une cuve Diesel) voyait sur son
    tableau de bord la carte « Gasoline — 2 cuve(s) à sec » de Canapé-Vert,
    les alertes de stock et la couverture qui vont avec : des chiffres qu'il
    ne peut ni expliquer ni corriger, pour des cuves qu'il n'opère pas. Les
    jauges de cuve, elles, étaient déjà correctement filtrées — d'où
    l'incohérence à l'écran.

    L'admin garde la vue GLOBALE du secteur, tous sites confondus : c'est la
    vue nationale voulue pour Rijkaard (responsable Fuel), documentée dans
    `docs/SECTEUR_FUEL.md`, et le seul écran où elle existe encore avec le
    catalogue Produits.
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        site = (None if (user or {}).get("role") == "admin"
                else _site_du_compte(conn, user))

    cle_cache = (secteur, site)
    horodatage = _cache_dashboard_horodatage.get(cle_cache, 0.0)
    if cle_cache in _cache_dashboard and time.monotonic() - horodatage < _CACHE_DASHBOARD_TTL_S:
        return _cache_dashboard[cle_cache]

    # Fragments de filtrage par site, insérés dans les requêtes ci-dessous.
    # Vides quand le compte n'a pas de site (ou est admin) : les requêtes
    # retrouvent alors mot pour mot leur forme d'origine.
    #   - `_prod` / `_prod_p` : produits, selon que la table est aliasée `p`.
    #   - `_lignes` : bons, via le produit de leurs lignes (documents n'a pas
    #     de colonne site — c'est le produit qui porte le rattachement).
    #   - `_bons` : bons sans jointure de lignes (activité du jour).
    f_prod = "" if site is None else " AND (site IS NULL OR site = ?)"
    f_prod_p = "" if site is None else " AND (p.site IS NULL OR p.site = ?)"
    f_lignes = "" if site is None else (
        " AND dl.product_id IN (SELECT id FROM products "
        "WHERE sector = ? AND (site IS NULL OR site = ?))")
    f_bons = "" if site is None else (
        " AND d.id IN (SELECT dl2.document_id FROM document_lines dl2 "
        "JOIN products p2 ON p2.id = dl2.product_id "
        "WHERE p2.sector = ? AND (p2.site IS NULL OR p2.site = ?))")
    p_site = [] if site is None else [site]
    p_lignes = [] if site is None else [secteur, site]

    # Fenêtre calculée en heure locale de l'entrepôt, comme les dates stockées.
    depuis_30j = (maintenant() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        resume = dict(conn.execute(
            f"""
            SELECT COUNT(*) AS references_total,
                   COALESCE(SUM(current_stock), 0) AS unites_totales,
                   COALESCE(SUM(CASE WHEN current_stock <= 0 THEN 1 ELSE 0 END), 0) AS en_rupture,
                   -- Même seuil effectif que la liste `alertes` plus bas (15 %
                   -- de la capacité pour une cuve sans seuil saisi) : les deux
                   -- chiffres sont lus côte à côte sur le tableau de bord, ils
                   -- ne peuvent pas répondre différemment à « manque-t-il du
                   -- stock ? ».
                   COALESCE(SUM(CASE WHEN current_stock > 0
                                      AND current_stock <= CASE
                                            WHEN tank_capacity IS NOT NULL
                                                 AND COALESCE(min_stock, 0) <= 0
                                            THEN tank_capacity * 0.15
                                            ELSE min_stock END
                                     THEN 1 ELSE 0 END), 0) AS sous_seuil,
                   -- unit_cost NULL = produit non valorisé : compté pour 0,
                   -- pas exclu (sinon la valeur totale sous-estime sans le dire).
                   COALESCE(SUM(current_stock * unit_cost), 0) AS valeur_totale,
                   COALESCE(SUM(CASE WHEN unit_cost IS NULL THEN 1 ELSE 0 END), 0) AS non_valorises
            FROM products
            WHERE COALESCE(archived, 0) = 0 AND sector = ?{f_prod}
            """,
            (secteur, *p_site),
        ).fetchone())
        # SUM(current_stock * unit_cost) porte le bruit flottant IEEE 754 tel
        # quel (ex. 139.96500000000001) — masqué à l'affichage du dashboard
        # par le format `.2f` (ligne ~2102), mais visible tel quel dans tout
        # export qui écrit resume["valeur_totale"] sans reformater (Rapport
        # d'activité, client/views/overview.py). Repéré par audit
        # d'exactitude métier le 9 septembre 2026.
        resume["valeur_totale"] = arrondir(resume["valeur_totale"])

        categories = [dict(r) for r in conn.execute(
            f"""
            SELECT COALESCE(category, 'Sans catégorie') AS categorie,
                   COUNT(*) AS references_total,
                   COALESCE(SUM(current_stock), 0) AS unites,
                   COALESCE(SUM(CASE WHEN current_stock <= 0 THEN 1 ELSE 0 END), 0) AS en_rupture,
                   -- Même seuil effectif que `resume.sous_seuil` juste au-dessus
                   -- (15 % de la capacité pour une cuve sans seuil saisi) : la
                   -- carte par carburant du tableau de bord Fuel affichait
                   -- « Approvisionné » sur une cuve à 170/1300 gal (13 %, ROUGE
                   -- sur sa jauge juste en dessous) parce que ce comptage ne
                   -- connaissait que la rupture totale (current_stock <= 0),
                   -- pas le seuil de réapprovisionnement — constaté par Idy509
                   -- le 2026-09-09, cette requête-ci avait été oubliée lors de
                   -- la correction du même défaut sur `resume`.
                   COALESCE(SUM(CASE WHEN current_stock > 0
                                      AND current_stock <= CASE
                                            WHEN tank_capacity IS NOT NULL
                                                 AND COALESCE(min_stock, 0) <= 0
                                            THEN tank_capacity * 0.15
                                            ELSE min_stock END
                                     THEN 1 ELSE 0 END), 0) AS sous_seuil
            FROM products
            WHERE COALESCE(archived, 0) = 0 AND sector = ?{f_prod}
            GROUP BY COALESCE(category, 'Sans catégorie')
            ORDER BY unites DESC
            """,
            (secteur, *p_site),
        ).fetchall()]

        # `seuil_effectif` : pour une CUVE (tank_capacity défini) sans seuil
        # explicite, le seuil est 15 % de la capacité — exactement le rouge de
        # la jauge du tableau de bord (`client/views/overview.py::JaugeCuve`).
        # Sans cela, les deux moitiés de l'écran se contredisaient : la cuve
        # Diesel d'Orelus s'affichait ROUGE à 170/1300 gal (13 %) pendant que
        # la carte « À réapprovisionner » annonçait « Tout OK », parce que
        # min_stock valait 0 et que 170 <= 0 est faux (constaté par Idy509 le
        # 2026-09-08). Un seuil saisi à la main reste prioritaire : c'est le
        # choix de l'exploitant, pas une valeur par défaut à écraser.
        alertes = [dict(r) for r in conn.execute(
            f"""
            SELECT sku, name, category, unit, current_stock,
                   CASE WHEN tank_capacity IS NOT NULL
                             AND COALESCE(min_stock, 0) <= 0
                        THEN ROUND(tank_capacity * 0.15, 3)
                        ELSE min_stock
                   END AS min_stock
            FROM products
            WHERE COALESCE(archived, 0) = 0 AND sector = ?{f_prod}
              AND current_stock <= CASE WHEN tank_capacity IS NOT NULL
                                             AND COALESCE(min_stock, 0) <= 0
                                        THEN tank_capacity * 0.15
                                        ELSE min_stock
                                   END
            ORDER BY (current_stock - min_stock), name
            LIMIT 50
            """,
            (secteur, *p_site),
        ).fetchall()]

        mouvements = dict(conn.execute(
            f"""
            SELECT COALESCE(SUM(CASE WHEN d.type = 'RECEIVING' THEN dl.quantity ELSE 0 END), 0) AS recu_30j,
                   COALESCE(SUM(CASE WHEN d.type = 'DELIVERY'  THEN dl.quantity ELSE 0 END), 0) AS livre_30j,
                   COALESCE(SUM(CASE WHEN d.type = 'RETURN'    THEN dl.quantity ELSE 0 END), 0) AS retours_30j,
                   COUNT(DISTINCT d.id) AS bons_30j
            FROM documents d JOIN document_lines dl ON dl.document_id = d.id
            WHERE d.created_at >= ? AND d.cancelled_at IS NULL AND d.sector = ?{f_lignes}
            """,
            (depuis_30j, secteur, *p_lignes),
        ).fetchone())

        depuis_60j = (maintenant() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
        mouvements_prev = dict(conn.execute(
            f"""
            SELECT COALESCE(SUM(CASE WHEN d.type = 'RECEIVING' THEN dl.quantity ELSE 0 END), 0) AS recu_prev,
                   COALESCE(SUM(CASE WHEN d.type = 'DELIVERY'  THEN dl.quantity ELSE 0 END), 0) AS livre_prev,
                   COALESCE(SUM(CASE WHEN d.type = 'RETURN'    THEN dl.quantity ELSE 0 END), 0) AS retours_prev,
                   COUNT(DISTINCT d.id) AS bons_prev
            FROM documents d JOIN document_lines dl ON dl.document_id = d.id
            WHERE d.created_at >= ? AND d.created_at < ? AND d.cancelled_at IS NULL
              AND d.sector = ?{f_lignes}
            """,
            (depuis_60j, depuis_30j, secteur, *p_lignes),
        ).fetchone())

        aujourd_hui = maintenant().strftime("%Y-%m-%d")
        activite_jour = dict(conn.execute(
            f"""
            SELECT COUNT(DISTINCT d.id) AS bons_jour,
                   COALESCE(SUM(CASE WHEN d.type = 'RECEIVING' THEN 1 ELSE 0 END), 0) AS receptions_jour,
                   COALESCE(SUM(CASE WHEN d.type = 'DELIVERY'  THEN 1 ELSE 0 END), 0) AS livraisons_jour
            FROM documents d
            WHERE d.created_at >= ? AND d.cancelled_at IS NULL AND d.sector = ?{f_bons}
            """,
            (aujourd_hui, secteur, *p_lignes),
        ).fetchone())

        top_sorties = [dict(r) for r in conn.execute(
            f"""
            SELECT p.sku, p.name, p.category, SUM(dl.quantity) AS quantite
            FROM document_lines dl
            JOIN documents d ON d.id = dl.document_id
            JOIN products p ON p.id = dl.product_id
            WHERE d.type = 'DELIVERY' AND d.created_at >= ?
              AND d.cancelled_at IS NULL AND d.sector = ?{f_prod_p}
            GROUP BY p.id
            ORDER BY quantite DESC
            LIMIT 5
            """,
            (depuis_30j, secteur, *p_site),
        ).fetchall()]

        par_region = [dict(r) for r in conn.execute(
            f"""
            -- Les régions sont désormais normalisées à l'écriture (référentiel
            -- serveur) : on regroupe sur la valeur telle quelle, sans retoucher
            -- la casse. L'ancien reformatage transformait 'Port-au-Prince'
            -- en 'Port-au-prince'.
            SELECT COALESCE(NULLIF(TRIM(d.region), ''), 'Non renseignée') AS region,
                   SUM(dl.quantity) AS quantite
            FROM documents d JOIN document_lines dl ON dl.document_id = d.id
            WHERE d.type = 'DELIVERY' AND d.created_at >= ?
              AND d.cancelled_at IS NULL AND d.sector = ?{f_lignes}
            GROUP BY region
            ORDER BY quantite DESC
            LIMIT 8
            """,
            (depuis_30j, secteur, *p_lignes),
        ).fetchall()]

        # Fuel n'a pas de "top produits" qui vaille (une seule cuve) ni de
        # région : les dimensions qui comptent sont le véhicule et le projet
        # qui consomment le carburant. Calculé uniquement pour ce secteur —
        # coût nul pour les autres.
        top_vehicules = []
        par_projet = []
        if secteur == "FUEL":
            # `p.site` en plus de la dimension propre à chaque graphique :
            # sur la vue nationale de l'admin, deux entrées portant le même
            # nom (un même receveur, un même projet) peuvent exister à des
            # sites différents sans que rien ne les distingue — constaté par
            # Idy509 le 2026-09-09, un ajout du graphique `par_site` seul
            # n'a pas suffi, il fallait aussi lever l'ambiguïté ligne par
            # ligne. Le client n'affiche ce site en suffixe QUE si plusieurs
            # sites distincts apparaissent dans la même réponse (un compte
            # scopé à un site n'en verra jamais qu'un, donc aucun suffixe).
            top_vehicules = [dict(r) for r in conn.execute(
                f"""
                SELECT d.vehicle_plate AS plaque,
                       MAX(NULLIF(TRIM(d.vehicle_info), '')) AS modele,
                       SUM(dl.quantity) AS quantite,
                       p.site AS site
                FROM documents d
                JOIN document_lines dl ON dl.document_id = d.id
                JOIN products p ON p.id = dl.product_id
                WHERE d.type = 'DELIVERY' AND d.created_at >= ?
                  AND d.cancelled_at IS NULL AND d.sector = ?{f_lignes}
                  AND d.vehicle_plate IS NOT NULL AND TRIM(d.vehicle_plate) <> ''
                GROUP BY d.vehicle_plate, p.site
                ORDER BY quantite DESC
                LIMIT 8
                """,
                (depuis_30j, secteur, *p_lignes),
            ).fetchall()]

            par_projet = [dict(r) for r in conn.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM(d.project), ''), 'Non renseigné') AS projet,
                       SUM(dl.quantity) AS quantite,
                       p.site AS site
                FROM documents d
                JOIN document_lines dl ON dl.document_id = d.id
                JOIN products p ON p.id = dl.product_id
                WHERE d.type = 'DELIVERY' AND d.created_at >= ?
                  AND d.cancelled_at IS NULL AND d.sector = ?{f_lignes}
                GROUP BY projet, p.site
                ORDER BY quantite DESC
                LIMIT 8
                """,
                (depuis_30j, secteur, *p_lignes),
            ).fetchall()]

            par_receveur = [dict(r) for r in conn.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM(d.receiver), ''), NULLIF(TRIM(d.party), ''),
                                'Non renseigné') AS receveur,
                       SUM(dl.quantity) AS quantite,
                       p.site AS site
                FROM documents d
                JOIN document_lines dl ON dl.document_id = d.id
                JOIN products p ON p.id = dl.product_id
                WHERE d.type = 'DELIVERY' AND d.created_at >= ?
                  AND d.cancelled_at IS NULL AND d.sector = ?{f_lignes}
                GROUP BY receveur, p.site
                ORDER BY quantite DESC
                LIMIT 8
                """,
                (depuis_30j, secteur, *p_lignes),
            ).fetchall()]

            # Constaté par Idy509 le 2026-09-09 : sur la vue nationale de
            # l'admin (Rijkaard), les trois graphiques ci-dessus mélangent
            # les sites sans le dire — rien ne distingue « generatrice » de
            # WH Central (Orelus) de « generatrice » de Canapé-Vert (lui-
            # même). Un quatrième graphique, le total PAR SITE, répond à la
            # question que les trois autres laissent sans réponse. Calculé
            # pour tout compte FUEL (pas seulement l'admin) : un compte
            # rattaché à un site n'y verra qu'une seule barre, la sienne —
            # cohérent, jamais trompeur.
            par_site = [dict(r) for r in conn.execute(
                f"""
                SELECT COALESCE(NULLIF(TRIM(p.site), ''), 'Non renseigné') AS site,
                       SUM(dl.quantity) AS quantite
                FROM documents d
                JOIN document_lines dl ON dl.document_id = d.id
                JOIN products p ON p.id = dl.product_id
                WHERE d.type = 'DELIVERY' AND d.created_at >= ?
                  AND d.cancelled_at IS NULL AND d.sector = ?{f_lignes}
                GROUP BY site
                ORDER BY quantite DESC
                LIMIT 8
                """,
                (depuis_30j, secteur, *p_lignes),
            ).fetchall()]
            # Constaté par Idy509 le 2026-09-09 : la partie basse du tableau
            # de bord Fuel (« Couverture de stock ») réserve une hauteur pour
            # plusieurs lignes mais n'en affiche qu'UNE pour un site à une
            # seule cuve — un grand espace vide, en plus d'une info déjà
            # portée par l'autonomie affichée sur chaque jauge. Remplacé pour
            # ce secteur par un fil d'activité : les derniers mouvements
            # réels (réception/livraison), scopés au SITE comme le reste des
            # écrans d'exploitation Fuel (`f_lignes`) — jamais la vue
            # nationale de l'admin sur cet écran-là, contrairement au
            # Tableau de bord dans son ensemble : « Dernières transactions »
            # affiche ce qui s'est passé sur SA cuve, pas sur tout le pays.
            dernieres_transactions = [dict(r) for r in conn.execute(
                f"""
                SELECT d.id, d.type, d.reference, d.created_at,
                       d.vehicle_plate,
                       COALESCE(NULLIF(TRIM(d.receiver), ''), NULLIF(TRIM(d.party), '')) AS receveur,
                       d.project,
                       (SELECT SUM(dl2.quantity) FROM document_lines dl2
                         WHERE dl2.document_id = d.id) AS quantite
                FROM documents d
                WHERE d.sector = ? AND d.cancelled_at IS NULL{f_bons}
                ORDER BY d.created_at DESC
                LIMIT 8
                """,
                (secteur, *p_lignes),
            ).fetchall()]
        else:
            par_receveur = []
            par_site = []
            dernieres_transactions = []

        couverture = [dict(r) for r in conn.execute(
            f"""
            SELECT p.id AS product_id, p.sku, p.name, p.current_stock,
                   COALESCE(SUM(dl.quantity), 0) / 30.0 AS conso_jour,
                   CASE WHEN COALESCE(SUM(dl.quantity), 0) > 0
                        THEN p.current_stock / (COALESCE(SUM(dl.quantity), 0) / 30.0)
                        ELSE 999
                   END AS jours_restants
            FROM products p
            LEFT JOIN document_lines dl ON dl.product_id = p.id
                AND dl.document_id IN (
                    SELECT id FROM documents
                    WHERE type = 'DELIVERY' AND created_at >= ?
                      AND cancelled_at IS NULL AND sector = ?
                )
            WHERE p.current_stock > 0 AND COALESCE(p.archived, 0) = 0
              AND p.sector = ?{f_prod_p}
            GROUP BY p.id
            HAVING conso_jour > 0
            ORDER BY jours_restants ASC
            LIMIT 20
            """,
            (depuis_30j, secteur, secteur, *p_site),
        ).fetchall()]

        nb_couverture_critique = sum(1 for c in couverture if c["jours_restants"] < 7)

    # Rotation de stock (30j) : quantité livrée / stock actuel. Une
    # approximation assumée — la rotation « en bonne et due forme » veut un
    # stock MOYEN sur la période, que ce schéma ne conserve pas (aucun
    # instantané historique du stock). Sur un entrepôt à mouvement
    # relativement stable, le stock actuel reste une base raisonnable.
    unites_totales = resume["unites_totales"]
    taux_rotation_30j = (
        arrondir(mouvements["livre_30j"] / unites_totales) if unites_totales > 1e-9 else 0
    )

    resultat = {
        "resume": resume,
        "categories": categories,
        "alertes": alertes,
        "mouvements": mouvements,
        "mouvements_prev": mouvements_prev,
        "activite_jour": activite_jour,
        "top_sorties": top_sorties,
        "par_region": par_region,
        "top_vehicules": top_vehicules,
        "par_projet": par_projet,
        "par_receveur": par_receveur,
        "par_site": par_site,
        "dernieres_transactions": dernieres_transactions,
        "couverture": couverture,
        "nb_couverture_critique": nb_couverture_critique,
        "taux_rotation_30j": taux_rotation_30j,
    }
    _cache_dashboard[cle_cache] = resultat
    _cache_dashboard_horodatage[cle_cache] = time.monotonic()
    return resultat


# `{categorie:path}` et non `{categorie}` : uvicorn décode les %XX du chemin
# AVANT le routage, si bien qu'une catégorie contenant une barre oblique —
# 'Huile/Lubrifiant', 'Filtre/Joint' — devenait deux segments et ne
# correspondait à aucune route. La catégorie était pourtant bien listée par
# /dashboard : cliquer dessus renvoyait un 404 incompréhensible. Le paramètre
# étant le dernier segment de la route, `:path` capture le reste tel quel.
@app.get("/dashboard/categorie/{categorie:path}")
def dashboard_categorie(categorie: str, user=Depends(exiger_utilisateur)):
    """Détail d'une catégorie : sous-totaux par groupe de capacité (P11, P26, HPU...)."""
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        site = _site_du_compte(conn, user)
        site_clause = "AND (site IS NULL OR site = ?) " if site else ""
        base_params: list = [categorie, secteur] + ([site] if site else [])

        total = dict(conn.execute(
            f"""
            SELECT COUNT(*) AS references_total,
                   COALESCE(SUM(current_stock), 0) AS unites
            FROM products
            WHERE COALESCE(category, 'Sans catégorie') = ? AND COALESCE(archived, 0) = 0
              AND sector = ? {site_clause}
            """,
            base_params,
        ).fetchone())

        groupes = [dict(r) for r in conn.execute(
            f"""
            SELECT COALESCE(NULLIF(TRIM(capacity_group), ''), 'Non classé') AS groupe,
                   COUNT(*) AS references_total,
                   COALESCE(SUM(current_stock), 0) AS unites,
                   COALESCE(SUM(CASE WHEN current_stock <= 0 THEN 1 ELSE 0 END), 0) AS en_rupture
            FROM products
            WHERE COALESCE(category, 'Sans catégorie') = ? AND COALESCE(archived, 0) = 0
              AND sector = ? {site_clause}
            GROUP BY groupe
            ORDER BY unites DESC
            """,
            base_params,
        ).fetchall()]

        articles = [dict(r) for r in conn.execute(
            f"""
            SELECT sku, name, capacity_group, unit, current_stock, min_stock
            FROM products
            WHERE COALESCE(category, 'Sans catégorie') = ? AND COALESCE(archived, 0) = 0
              AND sector = ? {site_clause}
            ORDER BY COALESCE(capacity_group, 'zzz'), current_stock DESC
            """,
            base_params,
        ).fetchall()]

    return {"categorie": categorie, "total": total, "groupes": groupes, "articles": articles}


@app.get("/dashboard/cuve/{product_id}")
def dashboard_cuve(product_id: int, user=Depends(exiger_utilisateur)):
    """Détail d'UNE cuve : les mêmes trois graphiques que le tableau de bord
    Fuel (receveur/projet/véhicule), mais filtrés sur cette seule cuve.

    Demandé par Idy509 le 2026-09-09 : cliquer une jauge de cuve doit filtrer
    les graphiques en dessous sur CETTE cuve, au lieu du mélange national
    habituel. `tous_sites=True` : lecture depuis le Tableau de bord, le seul
    écran où l'admin garde sa vue nationale (voir docs/SECTEUR_FUEL.md) — un
    compte non-admin reste de toute façon scopé à son site par
    `_produit_accessible`, donc à ses seules cuves.
    """
    with get_conn() as conn:
        produit = _produit_accessible(conn, product_id, user, tous_sites=True)
        if produit is None:
            raise HTTPException(status_code=404, detail="Produit introuvable")

        depuis_30j = (maintenant() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

        top_vehicules = [dict(r) for r in conn.execute(
            """
            SELECT d.vehicle_plate AS plaque,
                   MAX(NULLIF(TRIM(d.vehicle_info), '')) AS modele,
                   SUM(dl.quantity) AS quantite
            FROM documents d JOIN document_lines dl ON dl.document_id = d.id
            WHERE d.type = 'DELIVERY' AND d.created_at >= ?
              AND d.cancelled_at IS NULL AND dl.product_id = ?
              AND d.vehicle_plate IS NOT NULL AND TRIM(d.vehicle_plate) <> ''
            GROUP BY d.vehicle_plate
            ORDER BY quantite DESC
            LIMIT 8
            """,
            (depuis_30j, product_id),
        ).fetchall()]

        par_projet = [dict(r) for r in conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(d.project), ''), 'Non renseigné') AS projet,
                   SUM(dl.quantity) AS quantite
            FROM documents d JOIN document_lines dl ON dl.document_id = d.id
            WHERE d.type = 'DELIVERY' AND d.created_at >= ?
              AND d.cancelled_at IS NULL AND dl.product_id = ?
            GROUP BY projet
            ORDER BY quantite DESC
            LIMIT 8
            """,
            (depuis_30j, product_id),
        ).fetchall()]

        par_receveur = [dict(r) for r in conn.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(d.receiver), ''), NULLIF(TRIM(d.party), ''),
                            'Non renseigné') AS receveur,
                   SUM(dl.quantity) AS quantite
            FROM documents d JOIN document_lines dl ON dl.document_id = d.id
            WHERE d.type = 'DELIVERY' AND d.created_at >= ?
              AND d.cancelled_at IS NULL AND dl.product_id = ?
            GROUP BY receveur
            ORDER BY quantite DESC
            LIMIT 8
            """,
            (depuis_30j, product_id),
        ).fetchall()]

    return {
        "product_id": product_id,
        "name": produit["name"],
        "top_vehicules": top_vehicules,
        "par_projet": par_projet,
        "par_receveur": par_receveur,
    }


@app.get("/stock/emplacements")
def stock_emplacements(product_id: Optional[int] = None,
                       user=Depends(exiger_utilisateur)):
    """Stock détenu par l'entrepôt et par chaque superviseur.

    C'est la vue que le client tient aujourd'hui dans son onglet Stock Levels.
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        site = _site_du_compte(conn, user)
        lignes = stock_par_emplacement(conn, product_id, sector=secteur, site=site)

    entrepot = [l for l in lignes if l["type"] == "WAREHOUSE"]
    terrain = [l for l in lignes if l["type"] == "FIELD"]
    regionaux = [l for l in lignes if l["type"] == "REGIONAL_WAREHOUSE"]

    par_region: dict[str, dict] = {}
    for l in regionaux:
        entree = par_region.setdefault(l["region"], {
            "region": l["region"], "references": 0, "unites": 0.0, "articles": [],
        })
        entree["references"] += 1
        # Chaque addition est arrondie, comme partout ailleurs (precision.py) :
        # sinon le total d'une région dérive au fil des produits cumulés.
        entree["unites"] = arrondir(entree["unites"] + l["quantite"])
        entree["articles"].append({
            "sku": l["sku"], "name": l["product_name"],
            "category": l["category"], "unit": l["unit"], "quantite": l["quantite"],
        })

    par_superviseur: dict[tuple, dict] = {}
    for l in terrain:
        cle = (l["region"], l["supervisor"])
        entree = par_superviseur.setdefault(cle, {
            "region": l["region"], "supervisor": l["supervisor"],
            "references": 0, "unites": 0.0, "articles": [],
        })
        entree["references"] += 1
        entree["unites"] = arrondir(entree["unites"] + l["quantite"])
        entree["articles"].append({
            "sku": l["sku"], "name": l["product_name"],
            "category": l["category"], "unit": l["unit"], "quantite": l["quantite"],
        })

    return {
        "entrepot": {
            "references": len(entrepot),
            "unites": arrondir(sum(l["quantite"] for l in entrepot)),
            "articles": [
                {"sku": l["sku"], "name": l["product_name"], "category": l["category"],
                 "unit": l["unit"], "quantite": l["quantite"]}
                for l in entrepot
            ],
        },
        "terrain": sorted(par_superviseur.values(),
                          key=lambda e: (-e["unites"], e["region"], e["supervisor"])),
        "total_terrain": arrondir(sum(l["quantite"] for l in terrain)),
        # Entrepôts régionaux : stock CONFIRMÉ reçu. Ce qui est encore en
        # transit n'est crédité nulle part — c'est voulu, et visible ici comme
        # la différence entre le total sorti et le total détenu.
        "entrepots_regionaux": sorted(par_region.values(),
                                      key=lambda e: (-e["unites"], e["region"])),
        "total_regional": arrondir(sum(l["quantite"] for l in regionaux)),
    }


def calculer_reconciliation(conn, sector: str = SECTEUR_DEFAUT,
                            site: str | None = None) -> dict:
    """Compare le stock enregistré au stock recalculé depuis les mouvements,
    pour UN secteur (et optionnellement un seul site FUEL).

    Toute ligne renvoyée est une anomalie : le grand livre ne se réconcilie plus.

    Périmètre : le cycle en cours. Un « Réinitialiser stock début » repose
    initial_stock sur le stock du moment — l'historique antérieur y est déjà
    contenu. Les deux requêtes ne comptent donc que les bons postérieurs à
    products.initial_stock_reset_doc_id ; sinon l'historique d'avant le reset
    serait additionné deux fois et créerait un écart impossible à résorber.

    Fonction pure (ne dépend que de `conn`) : l'endpoint l'expose aux
    utilisateurs, et la vérification quotidienne de fond l'appelle sans passer
    par HTTP — les deux voient donc EXACTEMENT le même calcul. C'était la
    condition pour que l'alerte automatique ne dérive jamais de l'écran.
    """
    site_clause_p = "AND (p.site IS NULL OR p.site = ?)" if site else ""
    site_clause_bare = "AND (site IS NULL OR site = ?)" if site else ""
    site_params = (site,) if site else ()

    rows = conn.execute(
        f"""
        SELECT p.id, p.sku, p.name, p.initial_stock, p.current_stock,
               p.initial_stock + COALESCE(SUM(
                   -- d.id NULL = bon annulé (écarté par le JOIN) : mouvement extourné,
                   -- il ne compte plus. Sans ce cas explicite, le NULL tomberait
                   -- dans le ELSE et soustrairait la quantité.
                   CASE WHEN d.id IS NULL THEN 0
                        -- RETURN : le superviseur rapporte du stock à l'entrepôt,
                        -- au même titre qu'une réception — un retour traité comme
                        -- une sortie faussait l'écart de 2x sa quantité.
                        WHEN d.type IN ('RECEIVING', 'RETURN') THEN dl.quantity
                        -- ADJUSTMENT : la quantité EST l'écart signé
                        WHEN d.type = 'ADJUSTMENT' THEN dl.quantity
                        -- REGIONAL_TRANSFER : mouvement d'entrepôt régional
                        -- à entrepôt régional. Le stock du CENTRAL n'y est
                        -- pour rien : le compter en sortie creuserait un
                        -- écart permanent que rien ne pourrait résorber.
                        WHEN d.type = 'REGIONAL_TRANSFER' THEN 0
                        ELSE -dl.quantity END), 0)
               AS stock_calcule
        FROM products p
        LEFT JOIN document_lines dl ON dl.product_id = p.id
        LEFT JOIN documents d ON d.id = dl.document_id
             AND d.cancelled_at IS NULL
             -- Bon antérieur au dernier reset : déjà pris en compte dans
             -- initial_stock, on l'écarte (d.id devient NULL -> CASE = 0).
             AND (p.initial_stock_reset_doc_id IS NULL
                  OR d.id > p.initial_stock_reset_doc_id)
        WHERE p.sector = ? {site_clause_p}
        GROUP BY p.id
        HAVING ABS(stock_calcule - p.current_stock) > 1e-9
        ORDER BY p.name
        """,
        (sector,) + site_params,
    ).fetchall()

    # Second invariant : le stock de l'entrepôt recalculé depuis les
    # mouvements par emplacement doit égaler products.current_stock.
    # Sans ce contrôle, les deux comptabilités pourraient diverger sans
    # que rien ne le signale.
    ecarts_emplacement = [dict(r) for r in conn.execute(
        f"""
        SELECT p.id, p.sku, p.name, p.current_stock,
               COALESCE(SUM(mvt.quantite), 0) AS stock_entrepot
        FROM products p
        LEFT JOIN (
            SELECT m.product_id, m.quantity AS quantite,
                   m.document_id AS doc_id
            FROM stock_movements m
            LEFT JOIN documents d ON d.id = m.document_id
            JOIN locations l ON l.id = m.to_location_id
            WHERE l.type = 'WAREHOUSE'
              AND (m.document_id IS NULL OR d.cancelled_at IS NULL)
            UNION ALL
            SELECT m.product_id, -m.quantity, m.document_id
            FROM stock_movements m
            LEFT JOIN documents d ON d.id = m.document_id
            JOIN locations l ON l.id = m.from_location_id
            WHERE l.type = 'WAREHOUSE'
              AND (m.document_id IS NULL OR d.cancelled_at IS NULL)
        ) AS mvt ON mvt.product_id = p.id
             -- doc_id NULL = mouvement d'ouverture : il PORTE la base du
             -- cycle (réalignée au reset), il compte toujours.
             AND (mvt.doc_id IS NULL
                  OR p.initial_stock_reset_doc_id IS NULL
                  OR mvt.doc_id > p.initial_stock_reset_doc_id)
        WHERE p.sector = ? {site_clause_p}
        GROUP BY p.id
        HAVING ABS(COALESCE(SUM(mvt.quantite), 0) - p.current_stock) > 1e-9
        ORDER BY p.name
        """,
        (sector,) + site_params,
    )]

    soldes_negatifs = [
        s for s in stock_par_emplacement(conn, sector=sector, site=site)
        if s["quantite"] < -1e-9
    ]

    produits_archives_avec_stock = [dict(r) for r in conn.execute(
        f"""
        SELECT id, sku, name, unit, current_stock
        FROM products
        WHERE COALESCE(archived, 0) = 1 AND ABS(current_stock) > 1e-9 AND sector = ?
            {site_clause_bare}
        ORDER BY name
        """,
        (sector,) + site_params,
    )]

    return {
        "ecarts_emplacement": ecarts_emplacement,
        "nombre_ecarts_emplacement": len(ecarts_emplacement),
        "ecarts": [
            {**dict(r), "difference": r["current_stock"] - r["stock_calcule"]} for r in rows
        ],
        "nombre_ecarts": len(rows),
        "soldes_negatifs": soldes_negatifs,
        "nombre_soldes_negatifs": len(soldes_negatifs),
        "produits_archives_avec_stock": produits_archives_avec_stock,
        "nombre_produits_archives_avec_stock": len(produits_archives_avec_stock),
    }


@app.get("/stock/reconciliation")
def stock_reconciliation(user=Depends(exiger_utilisateur)):
    """Écarts entre le stock enregistré et le stock recalculé, pour le
    secteur de l'appelant (voir `calculer_reconciliation`)."""
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        site = _site_du_compte(conn, user)
        return calculer_reconciliation(conn, sector=secteur, site=site)


# --------------------------------------- réconciliation planifiée (alerte)

# Une fois par jour. La réconciliation balaye document_lines en entier : la
# passer dans le cycle de 5 minutes du nettoyage coûterait cher pour rien,
# un écart de grand livre ne naît pas d'une minute à l'autre. Même patron que
# `INTERVALLE_PURGE_SECONDES` juste au-dessus.
INTERVALLE_RECONCILIATION_SECONDES = 24 * 3600

_derniere_reconciliation: float = 0.0

# Nombre d'écarts détaillés dans le corps de l'e-mail. Au-delà, seul le
# compte est donné : une alerte de 400 lignes n'est plus lue, et l'écran
# /stock/reconciliation porte déjà la liste complète.
ALERTE_RECONCILIATION_MAX_LIGNES = 15


def _corps_alerte_reconciliation(resultat: dict) -> str:
    """Message de l'alerte : ce qui ne se réconcilie plus, et où regarder."""
    lignes = [
        "Le grand livre de l'entrepôt ne se réconcilie plus.",
        "",
        f"- Écarts stock enregistré / mouvements : {resultat['nombre_ecarts']}",
        f"- Écarts stock enregistré / emplacements : "
        f"{resultat['nombre_ecarts_emplacement']}",
    ]
    detail = resultat.get("ecarts") or []
    if detail:
        lignes += ["", "Produits concernés (stock enregistré vs recalculé) :"]
        for e in detail[:ALERTE_RECONCILIATION_MAX_LIGNES]:
            lignes.append(
                f"  - {e.get('name')} ({e.get('sku')}) : "
                f"enregistré {arrondir(e.get('current_stock') or 0)}, "
                f"recalculé {arrondir(e.get('stock_calcule') or 0)} "
                f"(écart {arrondir(e.get('difference') or 0):+g})")
        reste = len(detail) - ALERTE_RECONCILIATION_MAX_LIGNES
        if reste > 0:
            lignes.append(f"  … et {reste} autre(s).")
    lignes += [
        "",
        "À vérifier dans l'application : Vue d'ensemble > Réconciliation.",
        "Un écart ne se corrige pas en base : il se solde par un bon "
        "d'inventaire, après avoir compris d'où il vient.",
    ]
    return "\n".join(lignes)


def verifier_reconciliation_planifiee(force: bool = False) -> dict | None:
    """Contrôle quotidien du grand livre, avec alerte e-mail en cas d'écart.

    Jusqu'ici, `/stock/reconciliation` ne disait la vérité qu'à l'admin qui
    pensait à ouvrir l'écran. Un écart pouvait donc vivre des semaines sans
    que personne ne le sache — or plus il est ancien, moins il est explicable.

    Appelée par la boucle de nettoyage (`_periodic_cleanup`), mais utilisable
    directement : c'est ce qui la rend testable sans attendre 24 h. `force`
    court-circuite le compteur de temps, rien d'autre.

    Renvoie le résultat de la réconciliation, ou None si l'échéance n'est pas
    atteinte. Ne lève jamais : un contrôle d'entretien ne doit pas interrompre
    la boucle qui le porte.
    """
    global _derniere_reconciliation
    maintenant_monotone = time.monotonic()
    if not force and _derniere_reconciliation and (
            maintenant_monotone - _derniere_reconciliation
            < INTERVALLE_RECONCILIATION_SECONDES):
        return None
    _derniere_reconciliation = maintenant_monotone

    try:
        with get_conn() as conn:
            # Reste borné au secteur Consumables par défaut : étendre cette
            # alerte automatique aux autres secteurs (FON...) est laissé à un
            # prochain lot, une fois l'écran GET /stock/reconciliation (lui,
            # déjà scopé par secteur) validé en usage.
            resultat = calculer_reconciliation(conn)
    except Exception:
        log.exception("réconciliation planifiée : calcul impossible")
        return None

    total = resultat["nombre_ecarts"] + resultat["nombre_ecarts_emplacement"]

    # En mode digest, le résultat rejoint le résumé du jour — y compris quand
    # il est bon. « Aucun écart » est justement ce qu'un responsable veut lire
    # une fois par jour ; en mode immédiat, cette information ne donnait lieu
    # à aucun e-mail (personne ne veut d'une notification quotidienne
    # « tout va bien » en plus des alertes réelles).
    try:
        if digest_actif():
            _digest_ajouter_reconciliation(resultat)
            if total:
                log.warning("réconciliation planifiée : %d écart(s) de grand "
                            "livre (mis au résumé quotidien)", total)
            else:
                log.info("réconciliation planifiée : aucun écart")
            return resultat
    except Exception:
        # Mode illisible : on retombe sur l'envoi immédiat ci-dessous, jamais
        # sur un silence.
        log.exception("réconciliation planifiée : mode d'envoi illisible")

    if not total:
        log.info("réconciliation planifiée : aucun écart")
        return resultat

    log.warning("réconciliation planifiée : %d écart(s) de grand livre", total)
    try:
        destinataires = destinataires_alertes()
        if destinataires:
            # Même patron que les alertes de seuil : envoi détaché, court-circuité
            # tout seul si aucune configuration SMTP n'est présente.
            envoyer_en_arriere_plan(
                destinataires,
                f"WMS — écart de réconciliation ({total})",
                _corps_alerte_reconciliation(resultat))
    except Exception:
        # Un e-mail qui ne part pas ne doit pas effacer la ligne de journal
        # ci-dessus, qui reste la trace du contrôle.
        log.exception("réconciliation planifiée : alerte e-mail impossible")
    return resultat


# ------------------------------------------ digest quotidien (mode alternatif)
#
# MODE ALTERNATIF, JAMAIS ACTIF PAR DÉFAUT. Le comportement historique — un
# e-mail par événement, tout de suite — reste celui de toute installation qui
# ne demande rien : `notifications_email.mode_alertes()` ne renvoie
# "digest_quotidien" que si `email_config.json` porte explicitement
# `"mode": "digest_quotidien"`. Clé absente, vide ou mal orthographiée = mode
# immédiat (voir la docstring de `mode_alertes`).
#
# Ce que le digest change : les trois alertes (rupture de seuil, signalement
# régional, réconciliation) ne partent plus une par une. Elles sont accumulées
# en mémoire et résumées dans UN e-mail par jour. Pour une boîte qui reçoit
# vingt alertes de seuil dans la même après-midi et finit par ne plus en lire
# aucune.
#
# EN MÉMOIRE, DONC PERDU AU REDÉMARRAGE, ET C'EST ASSUMÉ : le digest est un
# CONFORT DE LECTURE, pas une source de vérité. Tout ce qu'il résume est déjà
# durablement en base (les seuils dans `products`, les signalements dans
# `regional_alert_signals`, la réconciliation recalculable à tout instant) et
# consultable dans l'application. Persister ces accumulations créerait une
# table à purger, à migrer et à réconcilier pour une information qui existe
# déjà ailleurs. Un serveur redémarré perd le résumé du jour, pas les faits.
#
# Le verrou est indispensable : les alertes de seuil sont produites depuis les
# endpoints d'écriture (threads de travail d'uvicorn) tandis que l'envoi part
# de la boucle de nettoyage. Sans lui, deux ajouts simultanés pourraient se
# marcher dessus.

INTERVALLE_DIGEST_SECONDES = 24 * 3600

# Bornes par catégorie : un digest doit tenir dans un e-mail lisible. Au-delà,
# seul le nombre total est donné — comme ALERTE_RECONCILIATION_MAX_LIGNES.
DIGEST_MAX_LIGNES = 40

_digest_lock = threading.Lock()
_digest_seuils: list[dict] = []
_digest_signalements: list[dict] = []
_digest_reconciliation: dict | None = None
_dernier_digest: float = 0.0


def _digest_reinitialiser() -> None:
    """Vide l'accumulation. Appelée après un envoi, et par les tests."""
    global _digest_reconciliation, _dernier_digest
    with _digest_lock:
        _digest_seuils.clear()
        _digest_signalements.clear()
        _digest_reconciliation = None
        _dernier_digest = 0.0


def _digest_ajouter_seuils(alertes: list[dict]) -> None:
    """Range des ruptures de seuil dans le résumé du jour.

    Un même produit peut repasser sous son seuil plusieurs fois dans la
    journée (sortie, réapprovisionnement partiel, nouvelle sortie) : seule la
    DERNIÈRE observation est conservée, sur son SKU. Empiler les répétitions
    ferait un digest de trente lignes décrivant cinq produits.
    """
    if not alertes:
        return
    with _digest_lock:
        for alerte in alertes:
            sku = alerte.get("sku")
            for i, existante in enumerate(_digest_seuils):
                if existante.get("sku") == sku:
                    _digest_seuils[i] = dict(alerte)
                    break
            else:
                _digest_seuils.append(dict(alerte))


def _digest_ajouter_signalement(signalement: dict) -> None:
    """Range un signalement régional dans le résumé du jour.

    Aucune déduplication ici, contrairement aux seuils : deux signalements du
    même produit par deux régions différentes sont deux demandes distinctes,
    et deux signalements de la même région le même jour disent une insistance
    qu'il vaut mieux montrer.
    """
    with _digest_lock:
        _digest_signalements.append(dict(signalement))


def _digest_ajouter_reconciliation(resultat: dict) -> None:
    """Retient le DERNIER résultat de réconciliation connu du jour.

    Écrasement volontaire : la réconciliation est un état, pas un événement.
    Deux passages dans la même journée (serveur redémarré, appel forcé)
    doivent laisser le constat le plus récent, jamais deux constats
    contradictoires côte à côte.
    """
    global _digest_reconciliation
    with _digest_lock:
        _digest_reconciliation = dict(resultat)


def _corps_digest_quotidien(seuils: list[dict], signalements: list[dict],
                            reconciliation: dict | None) -> str:
    """Résumé du jour, en trois parties, dans l'ordre de l'urgence."""
    lignes = [f"Résumé WMS du {maintenant_texte()[:10]}.", ""]

    lignes.append(f"1) Ruptures de seuil ({len(seuils)})")
    if seuils:
        for a in seuils[:DIGEST_MAX_LIGNES]:
            lignes.append(f"  - {a.get('name')} ({a.get('sku')}) : "
                          f"stock {a.get('stock')}, seuil {a.get('seuil')}")
        reste = len(seuils) - DIGEST_MAX_LIGNES
        if reste > 0:
            lignes.append(f"  … et {reste} autre(s).")
    else:
        lignes.append("  Aucune.")

    lignes += ["", f"2) Signalements régionaux ({len(signalements)})"]
    if signalements:
        for s in signalements[:DIGEST_MAX_LIGNES]:
            note = f" — {s.get('note')}" if s.get("note") else ""
            lignes.append(f"  - {s.get('region')} : "
                          f"{s.get('name')} ({s.get('sku')}){note}")
        reste = len(signalements) - DIGEST_MAX_LIGNES
        if reste > 0:
            lignes.append(f"  … et {reste} autre(s).")
    else:
        lignes.append("  Aucun.")

    lignes += ["", "3) Réconciliation du grand livre"]
    if reconciliation is None:
        # Cas normal d'un serveur démarré depuis moins de 24 h : le contrôle
        # quotidien n'a pas encore tourné. Le dire vaut mieux que laisser
        # croire à un grand livre vérifié et sain.
        lignes.append("  Pas de contrôle effectué depuis le dernier démarrage.")
    else:
        total = (reconciliation.get("nombre_ecarts", 0)
                 + reconciliation.get("nombre_ecarts_emplacement", 0))
        if total:
            lignes.append(f"  {total} écart(s) détecté(s) :")
            lignes.append(f"    - stock enregistré / mouvements : "
                          f"{reconciliation.get('nombre_ecarts', 0)}")
            lignes.append(f"    - stock enregistré / emplacements : "
                          f"{reconciliation.get('nombre_ecarts_emplacement', 0)}")
            lignes.append("  Détail : Vue d'ensemble > Réconciliation.")
        else:
            lignes.append("  Aucun écart.")

    lignes += ["", "Message automatique du serveur WMS "
                   "(mode « digest_quotidien »)."]
    return "\n".join(lignes)


def envoyer_digest_quotidien_planifie(force: bool = False) -> dict | None:
    """Expédie le résumé quotidien des alertes, si le mode digest est actif.

    Même patron que `verifier_reconciliation_planifiee` : appelée à chaque
    tour de `_periodic_cleanup`, elle porte elle-même son échéance et ne fait
    rien les autres tours. `force` court-circuite le compteur de temps, ce qui
    la rend testable sans attendre 24 h.

    Renvoie un compte-rendu de ce qui a été envoyé, ou None si rien n'était à
    faire (mode immédiat, échéance non atteinte, ou aucun événement du jour).
    NE LÈVE JAMAIS : elle tourne dans la boucle d'entretien du serveur.

    Le compteur de temps est armé DÈS LE PREMIER PASSAGE, même quand il n'y a
    rien à envoyer : sans cela, la toute première alerte de la journée partirait
    seule à la seconde suivante, ce qui est exactement le comportement immédiat
    que le mode digest cherche à éviter.
    """
    global _dernier_digest, _digest_reconciliation
    try:
        if not digest_actif():
            return None
    except Exception:
        log.exception("digest quotidien : mode illisible")
        return None

    maintenant_monotone = time.monotonic()
    if not _dernier_digest:
        _dernier_digest = maintenant_monotone
        if not force:
            return None
    elif not force and (maintenant_monotone - _dernier_digest
                        < INTERVALLE_DIGEST_SECONDES):
        return None
    _dernier_digest = maintenant_monotone

    with _digest_lock:
        seuils = list(_digest_seuils)
        signalements = list(_digest_signalements)
        reconciliation = _digest_reconciliation
        _digest_seuils.clear()
        _digest_signalements.clear()
        _digest_reconciliation = None

    if not seuils and not signalements and reconciliation is None:
        log.info("digest quotidien : aucun événement à résumer")
        return None

    total = len(seuils) + len(signalements)
    try:
        destinataires = destinataires_alertes()
        if destinataires:
            envoyer_en_arriere_plan(
                destinataires,
                f"WMS — résumé quotidien ({total} alerte(s))",
                _corps_digest_quotidien(seuils, signalements, reconciliation))
        else:
            log.warning("digest quotidien : aucun destinataire configuré")
    except Exception:
        # L'accumulation a déjà été vidée : un échec d'envoi ne doit pas la
        # rejouer indéfiniment le lendemain. La ligne de journal ci-dessous
        # reste la trace de ce qui s'est passé.
        log.exception("digest quotidien : envoi impossible")

    log.info("digest quotidien : %d rupture(s) de seuil, %d signalement(s)",
             len(seuils), len(signalements))
    return {
        "seuils": len(seuils),
        "signalements": len(signalements),
        "reconciliation": reconciliation is not None,
    }


MAX_PAGE = 500         # borne haute d'une page, garde aussi sous la limite
                        # SQLite de 999 paramètres pour le IN (...) des lignes


def _filtre_documents(type_doc, region, product_id, date_from, date_to,
                       include_cancelled, reference_prefix=None,
                       regions_autorisees=None,
                       region_expediteur_ou_destinataire=False,
                       sector: str = SECTEUR_DEFAUT,
                       search: Optional[str] = None,
                       operator: Optional[str] = None,
                       site: Optional[str] = None) -> tuple[str, tuple]:
    """Construit la clause WHERE et ses paramètres. Toujours paramétré.

    `regions_autorisees` : None = aucune restriction. Une liste restreint la
    lecture à ces seules régions (lecteur rattaché à une zone) — les bons sans
    région, c'est-à-dire l'activité de l'entrepôt central, en sont exclus.

    `sector` : TOUJOURS appliqué (jamais optionnel dans les faits) — un bon
    d'un autre secteur ne doit apparaître dans AUCUNE liste ni export.

    `search` : texte libre, cherché sur la référence, l'opérateur, le
    tiers (`party`) et le SKU/nom des produits des lignes — l'écran
    Historique ne filtrait jusqu'ici que les lignes déjà chargées en
    mémoire (200 max), un document correspondant sur une autre page restait
    invisible.

    `operator` : un nom peut être le superviseur du bon (`operator`) OU son
    saisisseur (`created_by`) — même règle que le filtre local qu'il
    remplace côté client.

    `site` : pour un compte FUEL rattaché à un site (magasinier), restreint
    aux bons dont au moins une ligne porte un produit de CE site (ou sans
    site défini). None = aucune restriction (admin = lecture nationale, ou
    secteurs sans site). Même rattachement que le dashboard (les documents
    n'ont pas de colonne `site` — c'est le produit des lignes qui porte
    l'information).
    """
    conditions, params = ["sector = ?"], [sector]

    if regions_autorisees is not None:
        if not regions_autorisees:
            # Défense en profondeur : une liste vide ne doit jamais devenir
            # « IN () », qui est une erreur de syntaxe SQLite. L'appelant ne
            # passe jamais de liste vide (None dans ce cas), mais un filtre de
            # sécurité qui échoue en erreur 500 vaut mieux qu'un filtre absent.
            conditions.append("0=1")
        else:
            marqueurs = ",".join("?" * len(regions_autorisees))
            conditions.append(f"region IN ({marqueurs})")
            params.extend(regions_autorisees)

    if type_doc:
        if type_doc not in ("RECEIVING", "DELIVERY", "ADJUSTMENT", "RETURN",
                            "SUPPLIER_RETURN", "REGIONAL_TRANSFER"):
            raise HTTPException(status_code=422, detail=f"Type inconnu : {type_doc}")
        conditions.append("type = ?")
        params.append(type_doc)

    if region:
        region_normalisee = normaliser_region(region)
        if region_expediteur_ou_destinataire:
            # Historique personnel d'une région : un REGIONAL_TRANSFER qu'elle
            # a ENVOYÉ porte sa région dans `source_region`, pas `region`
            # (la colonne `region` y est la destination). Sans ce OR, l'écran
            # "Mon historique" d'une région ne montrait jamais ce qu'elle a
            # elle-même expédié vers une autre région.
            conditions.append("(region = ? OR source_region = ?)")
            params.append(region_normalisee)
            params.append(region_normalisee)
        else:
            conditions.append("region = ?")
            params.append(region_normalisee)

    if date_from:
        conditions.append("created_at >= ?")
        params.append(f"{date_from} 00:00:00")

    if date_to:
        conditions.append("created_at <= ?")
        params.append(f"{date_to} 23:59:59")

    if not include_cancelled:
        conditions.append("cancelled_at IS NULL")

    if product_id:
        conditions.append(
            "EXISTS (SELECT 1 FROM document_lines dl WHERE dl.document_id = documents.id "
            "AND dl.product_id = ?)"
        )
        params.append(product_id)

    if reference_prefix:
        conditions.append("reference LIKE ? ESCAPE '\\'")
        escaped = reference_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"{escaped}%")

    if operator:
        conditions.append("(operator = ? OR created_by = ?)")
        params.append(operator)
        params.append(operator)

    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        motif = f"%{escaped}%"
        conditions.append(
            "(reference LIKE ? ESCAPE '\\' OR operator LIKE ? ESCAPE '\\' "
            "OR party LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM document_lines dl "
            "JOIN products p ON p.id = dl.product_id "
            "WHERE dl.document_id = documents.id "
            "AND (p.sku LIKE ? ESCAPE '\\' OR p.name LIKE ? ESCAPE '\\')))"
        )
        params.extend([motif] * 5)

    if site:
        conditions.append(
            "EXISTS (SELECT 1 FROM document_lines dl "
            "JOIN products p ON p.id = dl.product_id "
            "WHERE dl.document_id = documents.id "
            "AND (p.site IS NULL OR p.site = ?))"
        )
        params.append(site)

    return " AND ".join(conditions), tuple(params)


def _charger_page(conn, filtre: str, params: tuple, limit: int, offset: int) -> list[dict]:
    """Charge une page de bons en 2 requêtes, quel que soit leur nombre.

    L'ancienne version exécutait 2 requêtes PAR document : 401 requêtes pour
    l'historique, plus de 20 000 pour un export.
    """
    documents = conn.execute(
        f"SELECT * FROM documents WHERE {filtre} ORDER BY created_at DESC, id DESC "
        f"LIMIT ? OFFSET ?",
        params + (limit, offset),
    ).fetchall()
    if not documents:
        return []

    par_id = {d["id"]: {**dict(d), "lines": []} for d in documents}
    marqueurs = ",".join("?" * len(par_id))
    lignes = conn.execute(
        f"SELECT dl.document_id, dl.id, dl.product_id, dl.quantity, dl.counted_quantity, "
        f"dl.received_quantity, "
        f"p.sku, p.name FROM document_lines dl JOIN products p ON p.id = dl.product_id "
        f"WHERE dl.document_id IN ({marqueurs}) ORDER BY dl.id",
        tuple(par_id),
    )
    for ligne in lignes:
        ligne = dict(ligne)
        par_id[ligne.pop("document_id")]["lines"].append(ligne)

    # Conserve l'ordre du tri SQL
    return [par_id[d["id"]] for d in documents]


def _rejeu_idempotent(conn, cle: Optional[str], sector: str) -> Optional[dict]:
    """Renvoie le bon déjà enregistré sous cette clé DANS CE SECTEUR, s'il existe.

    La vérification est faite DANS la transaction en écriture (BEGIN IMMEDIATE) :
    deux tentatives simultanées ne peuvent pas passer toutes les deux.

    Scopé par secteur : `idempotency_key` est fourni par l'appelant (UUID côté
    client normalement, mais rien ne l'impose côté serveur), et l'index unique
    qui la porte (migration 41) est global, tous secteurs confondus. Sans ce
    filtre, une clé qui coïncide par hasard avec celle d'un bon déjà créé dans
    UN AUTRE secteur renvoyait ce bon tel quel — lignes, produits, contact,
    `created_by` compris — à un appelant qui n'a pourtant rien créé. Même
    patron que `_rejeu_idempotent_inventaire`, qui l'a toujours eu.
    """
    if not cle:
        return None
    existant = conn.execute(
        "SELECT id FROM documents WHERE idempotency_key = ? AND sector = ?",
        (cle, sector),
    ).fetchone()
    return _fetch_document(conn, existant["id"]) if existant else None


def _fetch_document(conn, document_id: int) -> dict:
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Bon {document_id} introuvable")
    lines = conn.execute(
        "SELECT dl.id, dl.product_id, dl.quantity, dl.counted_quantity, "
        "dl.received_quantity, p.sku, p.name "
        "FROM document_lines dl JOIN products p ON p.id = dl.product_id "
        "WHERE dl.document_id = ?",
        (document_id,),
    ).fetchall()
    result = dict(doc)
    result["lines"] = [dict(l) for l in lines]
    return result


# Fenêtre de détection d'une référence de bordereau déjà saisie. Voir
# l'avertissement construit dans `create_document`.
REFERENCE_DOUBLON_JOURS = 90

# Plafond de quantité sur une ligne de bon, tous types confondus. Remonté au
# niveau du module : le transfert région -> région applique exactement le même
# garde-fou anti-faute de frappe, et deux plafonds différents auraient fini
# par diverger.
QUANTITE_MAX_PAR_LIGNE = 10_000


@app.post("/documents", response_model=DocumentOut)
def create_document(document: DocumentIn, response: Response,
                     user=Depends(exiger_ecriture)):
    if not document.lines:
        raise HTTPException(status_code=400, detail="Le bon doit contenir au moins une ligne")

    quantites = defaultdict(float)
    for line in document.lines:
        quantites[line.product_id] += line.quantity

    for product_id, quantite in quantites.items():
        if quantite > QUANTITE_MAX_PAR_LIGNE:
            raise HTTPException(
                status_code=422,
                detail=f"Quantité {quantite:g} dépasse le plafond autorisé "
                       f"({QUANTITE_MAX_PAR_LIGNE:g}) pour le produit {product_id}. "
                       f"Contactez l'administrateur si cette quantité est légitime.",
            )

    # write=True : BEGIN IMMEDIATE, la vérification et la mise à jour sont atomiques.
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)

        # Le modèle Pydantic canonicalise la région SANS connaître le secteur
        # (contrôle orthographique large — voir `normaliser_region`). Un même
        # libellé peut avoir un sens différent selon le secteur (ex.
        # « Petit-Goâve » : alias vers « Aquin » pour Consumables, mais une
        # vraie région à part pour RAN) : re-canonicaliser ICI, maintenant que
        # le secteur est connu, redonne le bon résultat quel que soit celui
        # que la passe sans secteur avait produit — `normaliser_region` avec
        # secteur utilise l'index propre à CE secteur, alias compris.
        if document.region:
            try:
                document.region = normaliser_region(document.region, secteur)
            except ValueError:
                # Région inconnue DANS CE secteur (ex. une région Consumables
                # soumise par un compte RAN) : on laisse la valeur telle
                # quelle, le contrôle `region_valide_pour_secteur` juste
                # après produit le 422 propre attendu — pas la peine de
                # dupliquer ce message d'erreur ici.
                pass

        # Le modèle Pydantic ne connaît que la validité GLOBALE (toutes
        # secteurs confondus) de la région/du superviseur — il n'a pas accès
        # au secteur de l'auteur. La vérification stricte se fait ici,
        # maintenant que le secteur est connu : sans elle, un bon FON pourrait
        # utiliser une région Consumables (ou l'inverse).
        if document.region and not region_valide_pour_secteur(document.region, secteur):
            raise HTTPException(
                status_code=422,
                detail=f"Région inconnue pour le secteur {secteur} : "
                       f"{document.region!r}",
            )
        if document.operator and not supervisor_est_valide(
                document.region, document.operator, secteur):
            raise HTTPException(
                status_code=422,
                detail=f"Le superviseur {document.operator!r} ne correspond pas "
                       f"à la région {document.region!r} pour le secteur {secteur}",
            )
        # Même défaut que région/superviseur ci-dessus, jamais corrigé pour le
        # technicien : `normaliser_technicien` (Pydantic, sans secteur) ne
        # fait qu'un contrôle orthographique large — rien n'empêchait un bon
        # FON de porter un technicien Consumables (constaté le 2026-09-09).
        if document.technician and not technicien_valide_pour_secteur(
                document.technician, secteur):
            raise HTTPException(
                status_code=422,
                detail=f"Technicien inconnu pour le secteur {secteur} : "
                       f"{document.technician!r}",
            )
        # La région n'est obligatoire pour une expédition/un retour QUE pour
        # les secteurs qui utilisent le système région/superviseur —
        # aujourd'hui Consumables et FON. Fuel n'a ni région ni entrepôt
        # régional (une seule cuve centrale) : c'est un véhicule qui remplace
        # ce rôle (voir le contrôle vehicle_plate ci-dessous).
        if (document.type in ("DELIVERY", "RETURN")
                and secteur in SECTEURS_AVEC_REGION and not document.region):
            libelle = {"DELIVERY": "une expédition", "RETURN": "un retour"}
            raise HTTPException(
                status_code=422,
                detail=f"La région est obligatoire pour {libelle[document.type]}",
            )
        if secteur == "FUEL" and document.type == "DELIVERY" and not document.vehicle_plate:
            raise HTTPException(
                status_code=422,
                detail="La plaque du véhicule est obligatoire pour une "
                       "livraison de carburant",
            )

        deja = _rejeu_idempotent(conn, document.idempotency_key, secteur)
        if deja is not None:
            response.headers["X-Idempotent-Replay"] = "true"
            return deja

        # Un seul aller-retour pour tous les produits du bon, plutôt qu'un
        # SELECT par ligne et par contrôle (jusqu'à 6 selon les cas). Le
        # dictionnaire est réutilisé pour toute la suite de la fonction :
        # rien ne peut changer entre ce SELECT et la fin de la transaction,
        # write=True verrouille en écriture dès BEGIN IMMEDIATE.
        #
        # Scopé par secteur : un product_id d'un AUTRE secteur n'apparaît pas
        # dans `produits`, donc tombe dans le même 404 « introuvable » qu'un
        # id inexistant juste en dessous — un bon ne peut jamais référencer
        # un produit hors du secteur de son créateur.
        site = _site_du_compte(conn, user)
        placeholders = ",".join("?" for _ in quantites)
        produits = {
            row["id"]: dict(row)
            for row in conn.execute(
                f"SELECT id, name, sku, archived, current_stock, min_stock, "
                f"consigne_bouteille, rl_apply, tank_capacity, site FROM products "
                f"WHERE id IN ({placeholders}) AND sector = ?",
                list(quantites) + [secteur],
            ).fetchall()
        }
        for product_id, quantite in quantites.items():
            product = produits.get(product_id)
            if product is None:
                raise HTTPException(status_code=404, detail=f"Produit {product_id} introuvable")
            # Même traitement qu'un secteur différent (404, pas 403) : un
            # compte rattaché à un site ne doit même pas savoir qu'un produit
            # d'un autre site existe. `site IS NULL` reste accessible à tous
            # (produit pas encore rattaché à un site précis).
            if site and product["site"] and product["site"] != site:
                raise HTTPException(status_code=404, detail=f"Produit {product_id} introuvable")
            # Contrôlé ICI, avant l'INSERT du document : l'ancienne version le
            # faisait après, ce qui laissait un document orphelin (rollback
            # mis à part, l'ordre restait trompeur à la lecture).
            if product["archived"]:
                raise HTTPException(
                    status_code=422,
                    detail=f"Le produit {product['sku']} est archivé et ne peut plus "
                           f"figurer sur un bon. Réactivez-le d'abord.",
                )
            if document.type in ("DELIVERY", "SUPPLIER_RETURN") and product["current_stock"] < quantite:
                raise HTTPException(
                    status_code=400,
                    detail=f"Stock insuffisant pour '{product['name']}' "
                           f"(disponible: {product['current_stock']}, demandé sur ce bon: {quantite})",
                )
            # Capacité de cuve (secteur FUEL, ex. Diesel plafonné à 1300 gal) :
            # une réception qui la ferait déborder est refusée, pas juste
            # signalée — décision utilisateur, contrairement au reste des
            # avertissements non bloquants de cette fonction.
            if document.type == "RECEIVING" and product["tank_capacity"]:
                nouveau_stock = product["current_stock"] + quantite
                if nouveau_stock > product["tank_capacity"] + 1e-9:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Capacité de cuve dépassée pour {product['name']} : "
                               f"{arrondir(product['tank_capacity'])} gal max, stock "
                               f"actuel {arrondir(product['current_stock'])} gal, "
                               f"cette réception de {arrondir(quantite)} gal la "
                               f"ferait déborder de "
                               f"{arrondir(nouveau_stock - product['tank_capacity'])} gal.",
                    )

        if document.party_contact_id is not None:
            # Scopé par secteur, comme les produits ci-dessus : un contact
            # d'un autre secteur ne doit jamais pouvoir être rattaché à ce bon.
            contact = conn.execute(
                "SELECT id, name FROM contacts WHERE id = ? AND sector = ?",
                (document.party_contact_id, secteur),
            ).fetchone()
            if contact is None:
                raise HTTPException(status_code=404, detail="Contact introuvable")
            # party reste rempli même quand un contact est lié : les vues qui
            # n'affichent que le texte (historique, export) ne doivent pas
            # montrer une case vide.
            if not document.party:
                document.party = contact["name"]

        recu_le = maintenant_texte()
        saisisseur = user["display_name"] if user else document.created_by
        ref = document.reference or _next_reference(conn, document.type)
        cur = conn.execute(
            "INSERT INTO documents (type, party, party_contact_id, operator, region, reference, note, "
            "carrier, technician, idempotency_key, created_by, station, server_received_at, created_at, "
            "sector, vehicle_plate, vehicle_info, mileage, project, receiver, fuel_card) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (document.type, document.party, document.party_contact_id, document.operator, document.region,
             ref, document.note, document.carrier, document.technician,
             document.idempotency_key,
             saisisseur, "", recu_le, document.created_at or recu_le, secteur,
             document.vehicle_plate, document.vehicle_info, document.mileage,
             document.project, document.receiver, document.fuel_card),
        )
        document_id = cur.lastrowid

        delta_sign = 1 if document.type in ("RECEIVING", "RETURN") else -1
        for product_id, quantite in quantites.items():
            conn.execute(
                "INSERT INTO document_lines (document_id, product_id, quantity) VALUES (?, ?, ?)",
                (document_id, product_id, quantite),
            )
            delta = delta_sign * quantite
            # UPDATE gardé : dernier rempart si le contrôle applicatif est contourné.
            # ROUND() : sans lui, des milliers d'additions flottantes dérivent
            # (un stock revenu à zéro finirait à 1.4e-14 au lieu de 0).
            cur = conn.execute(
                "UPDATE products SET current_stock = ROUND(current_stock + ?, 3) "
                "WHERE id = ? AND ROUND(current_stock + ?, 3) >= 0",
                (delta, product_id, delta),
            )
            if cur.rowcount == 0:
                raise HTTPException(
                    status_code=409,
                    detail=f"Stock insuffisant pour le produit {product_id} "
                           f"(modifié par un autre poste pendant la saisie)",
                )

        # Relu en un seul aller-retour après les UPDATE : sert à la fois aux
        # alertes de seuil ci-dessous et à la diffusion WebSocket en fin de
        # fonction (stocks_maj), qui faisaient chacune leurs propres SELECT.
        stocks_apres = {
            row["id"]: row["current_stock"]
            for row in conn.execute(
                f"SELECT id, current_stock FROM products WHERE id IN ({placeholders})",
                list(quantites),
            ).fetchall()
        }

        enregistrer_mouvements(
            conn,
            {"id": document_id, "type": document.type, "region": document.region,
             "operator": document.operator, "technician": document.technician},
            [{"product_id": pid, "quantity": q} for pid, q in quantites.items()],
        )
        # Consigne bouteilles : une expédition de produit consigné crée une dette
        if document.type == "DELIVERY":
            for product_id, quantite in quantites.items():
                consigne = produits[product_id]["consigne_bouteille"]
                if consigne:
                    # PAS d'arrondir() ici, volontairement (un audit du
                    # 9 septembre 2026 a proposé ce correctif à tort, revenu
                    # en arrière après avoir cassé
                    # test_retour_exactement_egal_a_la_dette_accepte) : la
                    # dette de consigne reste un flottant périodique tel
                    # quel, et c'est la comparaison à 1e-9 près des soldes
                    # (_solde/_solde_rl, /bottles/return) qui absorbe
                    # l'imprécision — pas un arrondi à l'écriture. Arrondir
                    # ici désynchronise le solde stocké (3.333) de la valeur
                    # que le retour recalcule pour comparaison (10/3 =
                    # 3.3333333333335), et un retour EXACT de toute la dette
                    # se met à échouer.
                    bouteilles = quantite / consigne
                    conn.execute(
                        "INSERT INTO bottle_ledger (product_id, region, technician, document_id, "
                        "bottles, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                        (product_id, document.region, document.technician, document_id,
                         bouteilles, saisisseur),
                    )

            # Reverse logistics (RL) : une livraison d'équipement prêté (ex.
            # routeur GPON FON, `products.rl_apply`) crée une attente de
            # retour. Contrairement à la consigne bouteille, pas de
            # conversion : 1 unité livrée = 1 unité en attente de retour.
            for product_id, quantite in quantites.items():
                if produits[product_id]["rl_apply"]:
                    conn.execute(
                        "INSERT INTO rl_ledger (product_id, region, technician, document_id, "
                        "quantity, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                        (product_id, document.region, document.technician, document_id,
                         quantite, saisisseur),
                    )

        alertes_seuil = []
        if document.type in ("DELIVERY", "SUPPLIER_RETURN"):
            for product_id in quantites:
                min_stock = produits[product_id]["min_stock"]
                stock = stocks_apres[product_id]
                if min_stock and stock <= min_stock:
                    alertes_seuil.append({
                        "sku": produits[product_id]["sku"], "name": produits[product_id]["name"],
                        "stock": stock, "seuil": min_stock,
                    })

        # Avertissements non bloquants pour les retours
        avertissements = []

        # Référence fournisseur déjà saisie récemment sur un bon du même type.
        #
        # NON BLOQUANT, délibérément : une collision légitime existe (un
        # fournisseur qui réutilise par erreur son propre numéro de bordereau,
        # une référence interne volontairement répétée). Refuser la saisie
        # laisserait l'opérateur bloqué devant un camion déchargé. On se
        # contente de le dire — c'est lui qui sait s'il vient de saisir deux
        # fois le même bon.
        #
        # 90 jours : au-delà, une même référence est presque sûrement un
        # nouveau bordereau et l'avertissement deviendrait du bruit.
        if document.reference and document.reference.strip():
            reference = document.reference.strip()
            limite = (maintenant() - timedelta(days=REFERENCE_DOUBLON_JOURS)).strftime(
                "%Y-%m-%d %H:%M:%S")
            doublons = conn.execute(
                """
                SELECT id, created_at, created_by
                FROM documents
                WHERE id != ?
                  AND type = ?
                  AND sector = ?
                  AND cancelled_at IS NULL
                  AND reference IS NOT NULL
                  AND TRIM(reference) = ?
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 3
                """,
                (document_id, document.type, secteur, reference, limite),
            ).fetchall()
            if doublons:
                details = ", ".join(
                    f"#{d['id']} du {(d['created_at'] or '')[:10]}"
                    f"{' par ' + d['created_by'] if d['created_by'] else ''}"
                    for d in doublons
                )
                avertissements.append(
                    f"La référence « {reference} » figure déjà sur "
                    f"{len(doublons)} bon(s) du même type : {details}. "
                    f"Vérifiez qu'il ne s'agit pas d'une double saisie."
                )

        if (document.type == "RETURN" and document.region
                and a_entrepot_regional(document.region, secteur)):
            # STOCK-4 : règle H/X/S validée. H = quantité confirmée reçue par
            # l'entrepôt régional (grand livre, jamais l'expédié) ; X = déjà
            # sortie (retours antérieurs + transferts inter-régionaux émis) ;
            # S = H - X = solde confirmé avant CE retour.
            #   H = 0                -> refus définitif (un retour ne peut
            #                           pas venir de nulle part) ;
            #   H > 0 et Q <= S      -> accepté ;
            #   H > 0 et Q > S       -> refus 422, motif explicite.
            # Le mouvement de CE retour est déjà écrit (enregistrer_mouvements,
            # plus haut, dans la même transaction) : X inclut donc déjà cette
            # quantité, d'où S_avant = solde_actuel + quantite. Un refus ici
            # lève une HTTPException à l'intérieur du `with get_conn(write=True)`
            # englobant : get_conn fait un ROLLBACK complet sur exception (voir
            # database.py), donc le mouvement et l'écriture sur current_stock
            # déjà faits plus haut sont annulés avec le reste — aucun
            # réordonnancement n'était nécessaire pour garantir l'atomicité.
            for product_id, quantite in quantites.items():
                detail = solde_regional_detail(conn, document.region, product_id)
                h = detail["recu"]
                s_avant = detail["solde"] + quantite
                nom = produits[product_id]["name"]
                if h < 1e-9:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Retour refusé pour « {nom} » : l'entrepôt "
                               f"régional de {document.region} n'a jamais reçu "
                               f"ce produit (aucune réception confirmée). Un "
                               f"retour ne peut pas venir de nulle part — si ce "
                               f"stock est réellement présent, régularisez par "
                               f"un ajustement d'inventaire (POST /adjustments).",
                    )
                if quantite > s_avant + 1e-9:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Retour de {quantite:g} pour « {nom} » dépasse "
                               f"le stock confirmé de l'entrepôt régional de "
                               f"{document.region} (disponible avant ce "
                               f"retour : {s_avant:g}, reçu confirmé : {h:g}).",
                    )
        elif document.type == "RETURN" and document.region:
            for product_id, quantite in quantites.items():
                # Combien a été expédié vers cette région (non annulé) ?
                expedie = conn.execute(
                    """
                    SELECT COALESCE(SUM(dl.quantity), 0) AS total
                    FROM document_lines dl
                    JOIN documents d ON d.id = dl.document_id
                    WHERE dl.product_id = ? AND d.region = ?
                      AND d.type = 'DELIVERY' AND d.cancelled_at IS NULL
                    """,
                    (product_id, document.region),
                ).fetchone()["total"]
                # Combien déjà retourné de cette région ?
                deja_retourne = conn.execute(
                    """
                    SELECT COALESCE(SUM(dl.quantity), 0) AS total
                    FROM document_lines dl
                    JOIN documents d ON d.id = dl.document_id
                    WHERE dl.product_id = ? AND d.region = ?
                      AND d.type = 'RETURN' AND d.cancelled_at IS NULL
                      AND d.id != ?
                    """,
                    (product_id, document.region, document_id),
                ).fetchone()["total"]
                if quantite + deja_retourne > expedie:
                    nom = produits[product_id]["name"]
                    avertissements.append(
                        f"Retour de {quantite:g} pour « {nom} » dépasse le total "
                        f"expédié vers {document.region} ({expedie:g} expédié, "
                        f"{deja_retourne:g} déjà retourné)"
                    )

        if document.type == "RETURN":
            for product_id, quantite in quantites.items():
                row_plafond = conn.execute("""
                    SELECT p.initial_stock,
                           p.current_stock,
                           p.initial_stock + COALESCE(SUM(
                               CASE WHEN d.type = 'RECEIVING' THEN dl.quantity
                                    ELSE 0 END
                           ), 0) AS plafond_physique
                    FROM products p
                    LEFT JOIN document_lines dl ON dl.product_id = p.id
                    LEFT JOIN documents d ON d.id = dl.document_id
                        AND d.cancelled_at IS NULL
                        AND d.type = 'RECEIVING'
                        -- STOCK-4 (bug corollaire) : sans cette clause, une
                        -- réception antérieure à un reset_initial est comptée
                        -- DEUX FOIS -- une fois déjà absorbée dans le nouvel
                        -- initial_stock, une fois de plus ici -- et gonfle le
                        -- plafond artificiellement. Même filtre que
                        -- calculer_reconciliation (app.py, plus haut).
                        AND (p.initial_stock_reset_doc_id IS NULL
                             OR d.id > p.initial_stock_reset_doc_id)
                    WHERE p.id = ?
                    GROUP BY p.id
                """, (product_id,)).fetchone()
                # Le plafond n'a de sens que si le produit a un historique
                # de reception : un stock initial a zero sans aucune
                # reception est un cas de demarrage, pas une anomalie.
                if (row_plafond
                        and row_plafond["plafond_physique"] > 1e-9
                        and row_plafond["current_stock"] > row_plafond["plafond_physique"] + 1e-9):
                    nom = produits[product_id]["name"]
                    avertissements.append(
                        f"Le stock résultant de « {nom} » ({row_plafond['current_stock']:g}) "
                        f"dépasse le total théorique ({row_plafond['plafond_physique']:g} = "
                        f"stock initial {row_plafond['initial_stock']:g} + total reçu)"
                    )

        log.info("bon #%d créé : %s saisi_par=%s région=%s lignes=%d",
                 document_id, document.type, document.created_by,
                 document.region, len(quantites))
        # Stocks résultants pour la diffusion WebSocket : déjà relus juste
        # après les UPDATE (stocks_apres), pas de nouvel aller-retour ici.
        stocks_maj = stocks_apres
        result = _fetch_document(conn, document_id)
        result["alertes_seuil"] = alertes_seuil
        result["avertissements"] = avertissements

    _invalider_cache_dashboard(secteur)
    # Diffusion hors transaction : les autres postes ne doivent être prévenus
    # qu'une fois l'écriture réellement validée.
    planifier(broadcast_document_created({
        "id": document_id,
        "doc_type": document.type,
        "type": document.type,
        "region": document.region,
        "party": document.party,
        "reference": document.reference,
    }, sector=secteur))
    for pid, stock in stocks_maj.items():
        planifier(broadcast_stock_update(pid, stock, timestamp=recu_le,
                                         sector=secteur))
    # Ciblée sur les admins : un magasinier qui vient de causer l'alerte n'a
    # pas besoin d'être notifié de son propre geste, l'écran de saisie le
    # montre déjà. role_filter existait dans websocket_manager sans jamais
    # être utilisé par un endpoint.
    for alerte in alertes_seuil:
        planifier(broadcast_alert_triggered(alerte, role_filter="admin",
                                            sector=secteur))
    _notifier_email_alertes_seuil(alertes_seuil)
    return result


@app.post("/returns", response_model=DocumentOut)
def create_return(document: DocumentIn, response: Response,
                  user=Depends(exiger_ecriture)):
    """Alias pour un bon de type RETURN. Le client peut poster ici directement.

    `exiger_ecriture` doit être posé ICI aussi : la délégation à
    `create_document` est un appel Python direct, pas une requête HTTP — les
    dépendances de l'autre endpoint n'y sont donc jamais évaluées.
    """
    if document.type != "RETURN":
        document.type = "RETURN"
    return create_document(document, response, user)


@app.post("/supplier-returns", response_model=DocumentOut)
def create_supplier_return(document: DocumentIn, response: Response,
                           user=Depends(exiger_ecriture)):
    """Marchandise non conforme renvoyée à un fournisseur : le stock quitte
    l'entrepôt, sans région ni superviseur (contrairement à RETURN).

    Même remarque que `create_return` sur `exiger_ecriture` : la délégation
    est un appel Python, les dépendances de `create_document` ne s'y
    appliquent pas.
    """
    if document.type != "SUPPLIER_RETURN":
        document.type = "SUPPLIER_RETURN"
    return create_document(document, response, user)


@app.post("/adjustments", response_model=DocumentOut)
def create_adjustment(adjustment: AdjustmentIn, response: Response,
                       user=Depends(exiger_ecriture)):
    """Ajustement d'inventaire après comptage physique.

    L'opérateur déclare ce qu'il a compté ; le serveur calcule l'écart et
    corrige le stock. Contrairement à un bon de réception ou d'expédition,
    cela n'affirme pas qu'une marchandise est entrée ou sortie : cela constate
    un écart entre le stock théorique et le stock réel.
    """
    if not adjustment.lines:
        raise HTTPException(status_code=400, detail="L'ajustement doit contenir au moins une ligne")

    comptages: dict[int, float] = {}
    for ligne in adjustment.lines:
        if ligne.product_id in comptages and comptages[ligne.product_id] != ligne.counted_quantity:
            raise HTTPException(
                status_code=400,
                detail=f"Deux comptages différents pour le produit {ligne.product_id} "
                       f"({comptages[ligne.product_id]} puis {ligne.counted_quantity})",
            )
        comptages[ligne.product_id] = ligne.counted_quantity

    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)
        deja = _rejeu_idempotent(conn, adjustment.idempotency_key, secteur)
        if deja is not None:
            response.headers["X-Idempotent-Replay"] = "true"
            return deja

        ecarts = []
        for product_id, compte in comptages.items():
            # Scopé par secteur ET par site, admin compris (pas de
            # `tous_sites`) : le jaugeage/comptage est un geste d'exploitation
            # sur SES propres cuves — Rijkaard compte Canapé-Vert, Orelus
            # compte WH Central. Un id d'un autre site (ou d'un autre secteur)
            # est introuvable ici.
            produit = _produit_accessible(conn, product_id, user)
            if produit is None:
                raise HTTPException(status_code=404, detail=f"Produit {product_id} introuvable")
            # arrondir() : une soustraction flottante entre deux valeurs déjà
            # arrondies peut laisser un résidu (ex. 92.3 - 100 = -7.699999999999996).
            delta = arrondir(compte - produit["current_stock"])
            if abs(delta) > 1e-9:
                ecarts.append((product_id, delta, compte))

        # Un comptage CONFORME reste une information d'exploitation : qui a
        # compté, quand, et combien — pas seulement un écart à corriger.
        # Initialement réservé à Fuel (où le relevé EST le geste métier —
        # refuser le faisait disparaître sans trace, constaté par Idy509 le
        # 2026-09-08), étendu à TOUS les secteurs le 13 septembre 2026 :
        # même besoin constaté pour un inventaire RAN sans écart, qui ne
        # laissait alors AUCUNE trace dans l'Historique — alors que
        # l'opérateur avait bien compté 54 produits. Un comptage conforme
        # est une preuve d'exploitation (qui, quand, combien), pas du bruit.
        releve_conforme = not ecarts
        # Lignes à écrire : les écarts, ou — relevé conforme — le comptage
        # lui-même à écart nul, pour que le bon porte le niveau relevé.
        lignes_bon = ecarts or [(pid, 0.0, compte) for pid, compte in comptages.items()]

        est_releve_seul = (secteur == "FUEL"
                           and user and user.get("role") != "admin")

        for product_id, delta, compte in ecarts:
            if delta > 0 and not est_releve_seul:
                # Plafond physique : tout ce qui est ENTRÉ dans l'entrepôt,
                # moins tout ce qui en est SORTI. Les signes suivent ceux que
                # `create_document` applique à current_stock (delta_sign) :
                #   RECEIVING, RETURN  -> +  (entrée)
                #   DELIVERY, SUPPLIER_RETURN -> −  (sortie)
                # SUPPLIER_RETURN manquait des deux côtés (CASE et filtre du
                # JOIN) : un produit reçu puis intégralement retourné au
                # fournisseur gardait un plafond égal au total reçu, alors que
                # plus rien n'est physiquement présent.
                # REGIONAL_TRANSFER reste volontairement EXCLU : il déplace du
                # stock déjà sorti du central, d'un entrepôt régional à un
                # autre, et ne touche jamais products.current_stock.
                # ADJUSTMENT aussi : sa quantité EST l'écart déjà appliqué au
                # stock, la compter ici entérinerait un ajustement précédent.
                # Tout nouveau type de bon qui touche current_stock doit être
                # ajouté ICI, dans le CASE ET dans le filtre du JOIN.
                row = conn.execute("""
                    SELECT p.initial_stock + COALESCE(SUM(
                        CASE WHEN d.type IN ('RECEIVING', 'RETURN') THEN dl.quantity
                             WHEN d.type IN ('DELIVERY', 'SUPPLIER_RETURN')
                                 THEN -dl.quantity
                             ELSE 0 END
                    ), 0) AS plafond
                    FROM products p
                    LEFT JOIN document_lines dl ON dl.product_id = p.id
                    LEFT JOIN documents d ON d.id = dl.document_id
                        AND d.cancelled_at IS NULL
                        AND d.type IN ('RECEIVING', 'DELIVERY', 'RETURN',
                                       'SUPPLIER_RETURN')
                    WHERE p.id = ?
                    GROUP BY p.id
                """, (product_id,)).fetchone()
                plafond = arrondir(row["plafond"]) if row else 0
                if compte > plafond:
                    nom = conn.execute(
                        "SELECT name FROM products WHERE id = ?",
                        (product_id,)).fetchone()["name"]
                    raise HTTPException(
                        status_code=422,
                        detail=f"Quantité physiquement impossible pour « {nom} » : "
                               f"compté {compte:g} dépasse le maximum possible "
                               f"{plafond:g} (stock initial + total reçu − total "
                               f"expédié − total retourné au fournisseur). "
                               f"Un ajustement ne peut pas créer du stock "
                               f"qui n'est jamais entré dans l'entrepôt.",
                    )

        if not est_releve_seul:
            for product_id, delta, compte in ecarts:
                theorique = arrondir(compte - delta)
                plafond_dur = max(abs(theorique) * 5, 1000)
                if abs(delta) > plafond_dur:
                    nom = conn.execute("SELECT name FROM products WHERE id = ?",
                                       (product_id,)).fetchone()["name"]
                    raise HTTPException(
                        status_code=422,
                        detail=f"Écart bloqué pour « {nom} » : {delta:+g} dépasse "
                               f"le plafond autorisé de ±{plafond_dur:g}. "
                               f"Contactez l'administrateur.",
                    )

        if not adjustment.confirmation_ecart_important and not est_releve_seul:
            alertes = []
            for product_id, delta, compte in ecarts:
                theorique = arrondir(compte - delta)
                # Règle partagée avec l'écran de comptage (models.py) : c'est
                # elle qui déclenche la double saisie à l'aveugle côté client.
                seuil = seuil_ecart_important(theorique)
                if abs(delta) > seuil:
                    nom = conn.execute("SELECT name FROM products WHERE id = ?",
                                       (product_id,)).fetchone()["name"]
                    alertes.append(
                        f"{nom} : théorique {theorique:g} → compté {compte:g} "
                        f"(écart {delta:+g}, seuil ±{seuil:g})")
            if alertes:
                raise HTTPException(
                    status_code=422,
                    detail="Écart(s) anormalement élevé(s) — confirmation requise :\n"
                           + "\n".join(alertes),
                )

        recu_le = maintenant_texte()
        saisisseur = user["display_name"] if user else adjustment.created_by
        cur = conn.execute(
            "INSERT INTO documents (type, operator, region, note, idempotency_key, "
            "created_by, station, server_received_at, created_at, sector) "
            "VALUES ('ADJUSTMENT', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (adjustment.operator, adjustment.region, adjustment.note,
             adjustment.idempotency_key, saisisseur, "",
             recu_le, adjustment.created_at or recu_le, secteur),
        )
        document_id = cur.lastrowid
        # Le motif est obligatoire : un ajustement sans justification n'est pas auditable.
        conn.execute("UPDATE documents SET reference = ? WHERE id = ?",
                     (adjustment.reason.strip(), document_id))

        for product_id, delta, compte in lignes_bon:
            conn.execute(
                "INSERT INTO document_lines (document_id, product_id, quantity, counted_quantity) "
                "VALUES (?, ?, ?, ?)",
                (document_id, product_id, delta, compte),
            )
            if not est_releve_seul:
                conn.execute("UPDATE products SET current_stock = ROUND(?, 3) WHERE id = ?",
                             (compte, product_id))

        if not est_releve_seul:
            enregistrer_mouvements(
                conn,
                {"id": document_id, "type": "ADJUSTMENT", "region": adjustment.region,
                 "operator": adjustment.operator},
                [{"product_id": pid, "quantity": delta} for pid, delta, _ in ecarts],
            )
        # Alertes seuil : un ajustement peut aussi faire passer sous le mini.
        # Un SELECT groupé plutôt qu'un par écart (repéré par audit de
        # performance le 9 septembre 2026) : un inventaire physique complet
        # porte facilement sur des dizaines à centaines de SKUs en une seule
        # saisie, et ce bloc reste sous le verrou d'écriture unique de la base.
        alertes_seuil = []
        if ecarts and not est_releve_seul:
            ids_ecarts = [product_id for product_id, _, _ in ecarts]
            marqueurs = ",".join("?" * len(ids_ecarts))
            produits_par_id = {
                p["id"]: p for p in conn.execute(
                    f"SELECT id, name, sku, current_stock, min_stock FROM products "
                    f"WHERE id IN ({marqueurs})", ids_ecarts,
                ).fetchall()
            }
            for product_id, delta, compte in ecarts:
                p = produits_par_id.get(product_id)
                if p and p["min_stock"] and p["current_stock"] <= p["min_stock"]:
                    alertes_seuil.append({
                        "sku": p["sku"], "name": p["name"],
                        "stock": p["current_stock"], "seuil": p["min_stock"],
                    })

        # Piste d'audit : c'est l'écriture la plus sensible du système (un
        # opérateur seul peut corriger le stock affiché), et pourtant la seule
        # absente du journal d'audit jusqu'ici — ce qui donnait une fausse
        # impression d'exhaustivité à l'écran « Journal d'audit ».
        _journaliser_audit(
            conn, "document", document_id, "adjust",
            new={
                "reason": adjustment.reason.strip(),
                "ecarts": [
                    {"product_id": pid, "delta": delta, "counted_quantity": compte}
                    for pid, delta, compte in ecarts
                ],
            },
            username=saisisseur,
            sector=secteur,
        )

        log.info("ajustement #%d créé : saisi_par=%s motif=%r écarts=%d%s%s",
                 document_id, adjustment.created_by, adjustment.reason, len(ecarts),
                 " (relevé conforme, sans écart)" if releve_conforme else "",
                 " (relevé seul, stock non corrigé)" if est_releve_seul else "")
        stocks_maj = {} if est_releve_seul else {
            product_id: compte for product_id, _, compte in ecarts}
        result = _fetch_document(conn, document_id)
        result["alertes_seuil"] = alertes_seuil

    _invalider_cache_dashboard(secteur)
    planifier(broadcast_document_created({
        "id": document_id,
        "doc_type": "ADJUSTMENT",
        "type": "ADJUSTMENT",
        "region": adjustment.region,
    }, sector=secteur))
    for pid, stock in stocks_maj.items():
        planifier(broadcast_stock_update(pid, stock, sector=secteur))
    for alerte in alertes_seuil:
        planifier(broadcast_alert_triggered(alerte, role_filter="admin",
                                            sector=secteur))
    _notifier_email_alertes_seuil(alertes_seuil)
    return result


@app.post("/documents/{document_id}/cancel", response_model=DocumentOut)
def cancel_document(document_id: int, payload: CancelIn,
                     user=Depends(exiger_ecriture)):
    """Annule un bon par extourne : le mouvement est inversé, le bon est conservé.

    On ne supprime jamais un document — la trace de l'erreur et de sa correction
    fait partie de la piste d'audit.
    """
    DELAI_ANNULATION_HEURES = 72

    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)
        # Scopé par secteur : un bon d'un autre secteur est introuvable, même
        # pour un admin — chaque secteur n'agit que sur ses propres bons.
        doc = conn.execute("SELECT * FROM documents WHERE id = ? AND sector = ?",
                           (document_id, secteur)).fetchone()
        if doc is None:
            raise HTTPException(status_code=404, detail=f"Bon {document_id} introuvable")
        if doc["cancelled_at"]:
            raise HTTPException(
                status_code=409,
                detail=f"Bon déjà annulé le {doc['cancelled_at']} par {doc['cancelled_by'] or 'inconnu'}",
            )

        # Seul l'auteur du bon (ou un admin) peut l'annuler. L'ancien système de
        # clé de poste imposait déjà cette règle (risque S3) ; sa suppression
        # cette session l'a fait disparaître sans remplacement — n'importe quel
        # magasinier pouvait alors annuler le bon d'un collègue.
        if (user and user.get("role") != "admin"
                and doc["created_by"] and doc["created_by"] != user["display_name"]):
            raise HTTPException(
                status_code=403,
                detail=f"Seul {doc['created_by']} (ou un admin) peut annuler ce bon.",
            )

        try:
            date_creation = datetime.strptime(
                doc["server_received_at"] or doc["created_at"], "%Y-%m-%d %H:%M:%S")
            # Même horloge que celle qui a écrit server_received_at : sinon
            # un serveur hébergé hors du fuseau de l'entrepôt (WMS_UTC_OFFSET)
            # comparait deux heures décalées.
            if maintenant() - date_creation > timedelta(hours=DELAI_ANNULATION_HEURES):
                raise HTTPException(
                    status_code=422,
                    detail=f"Ce bon date de plus de {DELAI_ANNULATION_HEURES}h "
                           f"(créé le {doc['created_at']}). "
                           f"Utilisez un ajustement d'inventaire pour corriger le stock.",
                )
        except ValueError:
            pass

        lignes = conn.execute(
            "SELECT dl.product_id, dl.quantity, p.name, p.sku, p.archived "
            "FROM document_lines dl JOIN products p ON p.id = dl.product_id "
            "WHERE dl.document_id = ?",
            (document_id,),
        ).fetchall()

        # Produit archivé : `create_document` refuse déjà de le porter sur un
        # bon. L'annulation, elle, ne le vérifiait pas — annuler une
        # expédition dont le produit a été archivé depuis remettait du stock
        # sur une fiche invisible partout (/stock, /dashboard et
        # /stock/export filtrent tous archived = 0). Contrôlé AVANT toute
        # écriture, comme les deux garde-fous qui suivent.
        archives = [l for l in lignes if l["archived"]]
        if archives:
            noms = ", ".join(f"{l['sku']} ({l['name']})" for l in archives)
            raise HTTPException(
                status_code=422,
                detail=f"Annulation impossible : le(s) produit(s) {noms} sont "
                       f"archivés. Le stock remonterait sur une fiche invisible "
                       f"dans tous les écrans. Réactivez-les d'abord, puis "
                       f"annulez ce bon.",
            )

        # Consigne bouteilles : annuler un bon efface la dette qu'il a créée
        # (le solde ne compte plus les lignes d'un bon annulé). Si la région a
        # déjà rendu ces bouteilles, le solde passerait sous zéro — un état
        # sans aucun sens physique, que `return_bottles` refuse déjà à
        # l'écriture. On applique ici le même garde-fou, avant toute écriture.
        dettes = conn.execute(
            "SELECT product_id, region, technician, bottles FROM bottle_ledger "
            "WHERE document_id = ?",
            (document_id,),
        ).fetchall()
        for dette in dettes:
            nom_produit = None

            def _solde(colonne, valeur):
                return conn.execute(
                    f"""
                    SELECT COALESCE(SUM(
                        CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN bl.bottles ELSE 0 END
                    ), 0) AS balance
                    FROM bottle_ledger bl
                    LEFT JOIN documents d ON d.id = bl.document_id
                    WHERE bl.product_id = ? AND bl.{colonne} IS ?
                    """,
                    (dette["product_id"], valeur),
                ).fetchone()["balance"]

            # Le solde du TECHNICIEN doit être vérifié en plus de celui de la
            # région : quand la région a reçu d'autres bons, son solde absorbe
            # l'extourne alors que la dette personnelle, elle, passe sous zéro
            # (le technicien avait déjà rendu ses bouteilles). L'écran « qui
            # doit rendre ses bouteilles » affichait alors un solde négatif
            # impossible, et la région restait débitrice sans porteur.
            controles = [("region", dette["region"], f"la région {dette['region']}")]
            if dette["technician"]:
                controles.append(
                    ("technician", dette["technician"],
                     f"le technicien {dette['technician']}")
                )

            for colonne, valeur, porteur in controles:
                solde_apres = _solde(colonne, valeur) - dette["bottles"]
                if solde_apres < -1e-9:
                    if nom_produit is None:
                        row = conn.execute(
                            "SELECT name FROM products WHERE id = ?",
                            (dette["product_id"],),
                        ).fetchone()
                        nom_produit = row["name"] if row else str(dette["product_id"])
                    raise HTTPException(
                        status_code=409,
                        detail=f"Annulation impossible : {porteur} a déjà rendu "
                               f"les bouteilles de « {nom_produit} ». "
                               f"Annuler ce bon laisserait un solde de "
                               f"{arrondir(solde_apres)} bouteille(s). "
                               f"Annulez d'abord le retour de bouteilles correspondant.",
                    )

        # Reverse logistics (RL) : même garde-fou que la consigne bouteille,
        # pour l'équipement prêté (routeurs GPON FON). Annuler une livraison
        # dont le routeur a déjà été marqué retourné laisserait un solde
        # négatif — un état sans aucun sens physique.
        dettes_rl = conn.execute(
            "SELECT product_id, region, technician, quantity FROM rl_ledger "
            "WHERE document_id = ?",
            (document_id,),
        ).fetchall()
        for dette in dettes_rl:
            nom_produit = None

            def _solde_rl(colonne, valeur):
                return conn.execute(
                    f"""
                    SELECT COALESCE(SUM(
                        CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN rl.quantity ELSE 0 END
                    ), 0) AS balance
                    FROM rl_ledger rl
                    LEFT JOIN documents d ON d.id = rl.document_id
                    WHERE rl.product_id = ? AND rl.{colonne} IS ?
                    """,
                    (dette["product_id"], valeur),
                ).fetchone()["balance"]

            controles_rl = [("region", dette["region"], f"la région {dette['region']}")]
            if dette["technician"]:
                controles_rl.append(
                    ("technician", dette["technician"],
                     f"le technicien {dette['technician']}")
                )

            for colonne, valeur, porteur in controles_rl:
                solde_apres = _solde_rl(colonne, valeur) - dette["quantity"]
                if solde_apres < -1e-9:
                    if nom_produit is None:
                        row = conn.execute(
                            "SELECT name FROM products WHERE id = ?",
                            (dette["product_id"],),
                        ).fetchone()
                        nom_produit = row["name"] if row else str(dette["product_id"])
                    raise HTTPException(
                        status_code=409,
                        detail=f"Annulation impossible : {porteur} a déjà rendu "
                               f"l'équipement « {nom_produit} ». "
                               f"Annuler ce bon laisserait un solde de "
                               f"{arrondir(solde_apres)}. "
                               f"Annulez d'abord le retour correspondant.",
                    )

        # Transfert régional DÉJÀ confirmé reçu : son annulation retire le
        # crédit posé sur l'entrepôt régional. Si la région a depuis retourné
        # ou consommé cette marchandise, ce retrait laisserait son stock
        # négatif ET rendrait au central plus que le plafond physique. Même
        # garde-fou que pour les bouteilles ci-dessus : on refuse avant toute
        # écriture, en indiquant la marche à suivre.
        credites = conn.execute(
            "SELECT m.product_id, SUM(m.quantity) AS credite "
            "FROM stock_movements m JOIN locations l ON l.id = m.to_location_id "
            "WHERE m.document_id = ? AND l.type = 'REGIONAL_WAREHOUSE' "
            "GROUP BY m.product_id",
            (document_id,),
        ).fetchall()
        if credites:
            detenu = {s["product_id"]: s["quantite"]
                      for s in stock_entrepot_regional(conn, doc["region"], sector=secteur)}
            for credit in credites:
                reste = detenu.get(credit["product_id"], 0) - credit["credite"]
                if reste < -1e-9:
                    nom = conn.execute(
                        "SELECT name FROM products WHERE id = ?",
                        (credit["product_id"],),
                    ).fetchone()
                    raise HTTPException(
                        status_code=409,
                        detail=f"Annulation impossible : la région {doc['region']} a déjà "
                               f"ressorti de son entrepôt la marchandise « "
                               f"{nom['name'] if nom else credit['product_id']} » reçue sur "
                               f"ce bon. Annuler laisserait son stock régional à "
                               f"{arrondir(reste):g}. Annulez d'abord le bon de retour "
                               f"correspondant, ou saisissez un ajustement.",
                    )

        # Inverse du mouvement d'origine.
        #  RECEIVING       : quantité ajoutée      -> on retire      (-1)
        #  DELIVERY        : quantité retirée      -> on remet       (+1)
        #  RETURN          : quantité ajoutée      -> on retire      (-1)
        #  SUPPLIER_RETURN : quantité retirée      -> on remet       (+1)
        #  ADJUSTMENT      : écart signé appliqué  -> on applique son opposé (-1)
        #
        #  REGIONAL_TRANSFER : n'a JAMAIS touché products.current_stock (le
        #  stock du central) — il déplace du stock déjà sorti, d'un entrepôt
        #  régional à un autre. Son extourne est portée entièrement par le
        #  grand livre des mouvements, que `cancelled_at` neutralise : le
        #  stock revient de lui-même à la région d'origine. Y appliquer un
        #  sens inventerait du stock central à partir de rien.
        sens = (0 if doc["type"] == "REGIONAL_TRANSFER"
                else 1 if doc["type"] in ("DELIVERY", "SUPPLIER_RETURN") else -1)
        for ligne in lignes:
            delta = sens * ligne["quantity"]
            cur = conn.execute(
                "UPDATE products SET current_stock = ROUND(current_stock + ?, 3) "
                "WHERE id = ? AND ROUND(current_stock + ?, 3) >= 0",
                (delta, ligne["product_id"], delta),
            )
            if cur.rowcount == 0:
                raise HTTPException(
                    status_code=409,
                    detail=f"Annulation impossible : le stock de '{ligne['name']}' "
                           f"deviendrait négatif (marchandise déjà ressortie de l'entrepôt). "
                           f"Saisir un bon de correction à la place.",
                )

        # Utiliser le nom de la session pour l'audit, pas le champ déclaratif.
        auteur = user["display_name"] if user else payload.operator

        annule_le = maintenant_texte()
        conn.execute(
            "UPDATE documents SET cancelled_at = ?, cancelled_by = ?, "
            "cancel_reason = ? WHERE id = ?",
            (annule_le, auteur, payload.reason.strip(), document_id),
        )

        _journaliser_audit(conn, "document", document_id, "cancel",
                           new={"cancelled_at": annule_le, "cancelled_by": auteur,
                                "cancel_reason": payload.reason.strip()},
                           username=user["username"] if user else auteur,
                           sector=secteur)

        log.info("bon #%d annulé : par=%s motif=%r",
                 document_id, auteur, payload.reason)
        stocks_maj = {
            ligne["product_id"]: conn.execute(
                "SELECT current_stock FROM products WHERE id = ?", (ligne["product_id"],)
            ).fetchone()["current_stock"]
            for ligne in lignes
        }
        result = _fetch_document(conn, document_id)

    _invalider_cache_dashboard(secteur)
    planifier(broadcast_document_cancelled(
        document_id, reason=payload.reason.strip(), doc_type=doc["type"],
        sector=secteur))
    for pid, stock in stocks_maj.items():
        planifier(broadcast_stock_update(pid, stock, timestamp=annule_le,
                                         sector=secteur))
    return result


@app.get("/documents", response_model=list[DocumentOut])
def list_documents(
    response: Response,
    type: Optional[str] = Query(None, description="RECEIVING, DELIVERY ou ADJUSTMENT"),
    region: Optional[str] = None,
    product_id: Optional[int] = None,
    date_from: Optional[str] = Query(None, description="AAAA-MM-JJ inclus"),
    date_to: Optional[str] = Query(None, description="AAAA-MM-JJ inclus"),
    include_cancelled: bool = True,
    reference_prefix: Optional[str] = Query(None, description="Filtre par début de référence"),
    region_expediteur_ou_destinataire: bool = Query(
        False, description="region= filtre sur source_region OU region (historique d'une région)"),
    search: Optional[str] = Query(
        None, description="Texte libre : référence, opérateur, tiers, SKU/nom produit"),
    operator: Optional[str] = Query(
        None, description="Filtre sur operator OU created_by"),
    limit: int = Query(200, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    user=Depends(exiger_utilisateur),
):
    """Historique paginé et filtrable.

    Le total est renvoyé en en-tête X-Total-Count : l'opérateur doit savoir
    qu'il ne voit qu'une partie des mouvements, sinon il croit à des données
    manquantes.
    """
    with get_conn() as conn:
        # Ouvert avant la construction du filtre : le périmètre d'un lecteur
        # restreint se lit en base, il fait partie de la clause WHERE.
        secteur = _secteur_du_compte(conn, user)
        site_filtre = None if (user and user.get("role") == "admin") \
            else _site_du_compte(conn, user)
        filtre, params = _filtre_documents(
            type, region, product_id, date_from, date_to, include_cancelled,
            reference_prefix, regions_autorisees=_regions_autorisees(conn, user),
            region_expediteur_ou_destinataire=region_expediteur_ou_destinataire,
            sector=secteur, search=search, operator=operator,
            site=site_filtre)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM documents WHERE {filtre}", params
        ).fetchone()["n"]
        documents = _charger_page(conn, filtre, params, limit, offset)

    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Returned-Count"] = str(len(documents))
    return documents


@app.get("/documents/{document_id}", response_model=DocumentOut)
def get_document(document_id: int, user=Depends(exiger_utilisateur)):
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        doc = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND sector = ?",
            (document_id, secteur),
        ).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Document introuvable")
        doc = dict(doc)
        lines = [dict(r) for r in conn.execute(
            "SELECT dl.product_id, dl.quantity, p.sku AS product_sku, p.name AS product_name "
            "FROM document_lines dl LEFT JOIN products p ON p.id = dl.product_id "
            "WHERE dl.document_id = ?", (document_id,)
        ).fetchall()]
        doc["lines"] = lines
    return doc


@app.get("/documents/export")
def export_documents(
    type: Optional[str] = None,
    region: Optional[str] = None,
    product_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    reference_prefix: Optional[str] = None,
    user=Depends(exiger_utilisateur),
):
    EXPORT_MAX_DOCUMENTS = 10_000
    # Les bons annulés sont extournés : ils ne doivent pas figurer dans les
    # trackers comme s'ils avaient eu lieu (filtré en SQL, pas après coup).
    # Ils restent visibles dans l'historique.
    with get_conn() as conn:
        # Même filtrage que la liste : un export ne doit pas être la porte
        # dérobée par laquelle un lecteur restreint récupère toutes les régions.
        secteur = _secteur_du_compte(conn, user)
        site_filtre = None if (user and user.get("role") == "admin") \
            else _site_du_compte(conn, user)
        filtre, params = _filtre_documents(
            type, region, product_id, date_from, date_to,
            include_cancelled=False, reference_prefix=reference_prefix,
            regions_autorisees=_regions_autorisees(conn, user),
            sector=secteur, site=site_filtre)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM documents WHERE {filtre}", params
        ).fetchone()["n"]
        documents = _charger_page(conn, filtre, params, EXPORT_MAX_DOCUMENTS, 0)

    tronque = len(documents) < total
    if tronque:
        log.warning("export tronqué : %d bons sur %d (limite %d). "
                    "Affiner les filtres pour un export complet.",
                    len(documents), total, EXPORT_MAX_DOCUMENTS)

    content = build_documents_excel(documents, type_filter=type)
    filename = export_filename("historique")
    _journaliser_export("historique_bons", user, type=type, region=region,
                        product_id=product_id, date_from=date_from,
                        date_to=date_to, reference_prefix=reference_prefix,
                        nb_bons=len(documents), tronque=tronque)
    # La troncature n'était signalée que dans le journal du serveur : l'opérateur
    # repartait avec un fichier amputé sans le savoir — inexploitable pour un
    # rapport mensuel, où un total faux passe pour un total vrai.
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Total-Count": str(total),
            "X-Returned-Count": str(len(documents)),
            "X-Export-Truncated": "1" if tronque else "0",
        },
    )


# ---------------------------------------------------------------- utilisateurs

@app.get("/users/status")
def users_status():
    """Indique si des comptes existent (le client décide d'afficher ou non le login)."""
    with get_conn() as conn:
        return {"has_users": utilisateurs_existent(conn)}


@app.post("/login")
def login(creds: LoginIn, request: Request):
    key = creds.username.lower()
    ip = request.client.host if request.client else None
    _refuser_si_verrouille(key)

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (creds.username,)
        ).fetchone()

    def _echec():
        """Réponse UNIQUE de tous les échecs d'identification."""
        log.warning("échec de connexion pour %r", creds.username)
        _register_login_failure(key, ip)
        try:
            with get_conn(write=True) as conn:
                conn.execute(
                    "INSERT INTO login_history (username, success) VALUES (?, 0)",
                    (creds.username,))
        except Exception:
            log.exception("échec d'écriture dans login_history")
        return HTTPException(status_code=401,
                             detail="Identifiant ou mot de passe incorrect")

    # Le mot de passe est vérifié EN PREMIER, avant même de regarder si le
    # compte est actif — et même sur un identifiant inconnu, contre une
    # empreinte factice. Sans cela, l'ordre des contrôles renseignait un
    # attaquant qui ne connaît qu'un identifiant : « ce compte est désactivé »
    # confirmait son existence, et une réponse immédiate (aucun PBKDF2 joué)
    # trahissait un identifiant inconnu. Ici, les trois cas d'échec coûtent le
    # même temps, rendent le même 401 et sont comptés de la même façon.
    if not row:
        verifier_mot_de_passe(creds.password, _EMPREINTE_FACTICE)
        raise _echec()
    if not verifier_mot_de_passe(creds.password, row["password_hash"]):
        raise _echec()
    if not row["active"]:
        # Mot de passe JUSTE : le titulaire légitime a le droit de savoir
        # pourquoi il n'entre pas, sinon il appelle en disant « mon mot de
        # passe ne marche plus » et personne ne cherche au bon endroit. Rien
        # n'est compté ici : ce n'est pas une tentative de devinette.
        raise HTTPException(status_code=403, detail="Ce compte est désactivé")
    # Connexion réussie : la série d'échecs de cet identifiant est close.
    # Sans cet effacement, quatre erreurs de frappe suivies d'une connexion
    # réussie laissaient le compte à un seul échec du verrouillage.
    try:
        with get_conn(write=True) as conn:
            conn.execute("DELETE FROM login_attempts WHERE username = ? COLLATE NOCASE",
                         (key,))
    except Exception:
        log.exception("échec de remise à zéro des tentatives de connexion")
    with get_conn(write=True) as conn:
        conn.execute("INSERT INTO login_history (username, success) VALUES (?, 1)",
                     (creds.username,))
    token = creer_session(row["id"], row["username"], row["display_name"], row["role"])
    log.info("connexion : %s (rôle %s, secteur %s)", row["username"], row["role"],
             row["sector"])
    return {
        "token": token,
        "user": {
            "id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "role": row["role"],
            # Le client en a besoin dès la connexion pour intituler les écrans
            # régionaux ; NULL pour tous les autres rôles.
            "region": row["region"],
            "sector": row["sector"],
            # Vrai seulement quand ce serveur tourne en WMS_DEMO_MODE=1 (voir
            # scripts/seed_demo_fuel.py) : permet au client de remplacer des
            # noms réels (fournisseur, entrepôt) par des libellés génériques
            # dans les captures publiques, sans jamais toucher à l'usage réel.
            "demo_mode": os.environ.get("WMS_DEMO_MODE") == "1",
        },
    }


@app.post("/logout")
def logout(x_user_token: str | None = Header(default=None)):
    if x_user_token:
        fermer_session(x_user_token)
    return {"ok": True}


@app.get("/users", response_model=list[UserOut])
def list_users(sector: str | None = None, user=Depends(exiger_admin)):
    # La liste des comptes est une donnée d'administration : identifiants,
    # rôles et comptes désactivés. Un magasinier ou un lecteur n'a aucun écran
    # qui l'affiche (l'onglet « Utilisateurs » est réservé à l'admin) ; la
    # laisser lisible par tout compte connecté ne servait qu'à énumérer les
    # identifiants administrateurs depuis n'importe quel poste du réseau.
    if user is None:
        raise HTTPException(status_code=401, detail="Connexion requise")
    with get_conn() as conn:
        # Par défaut, un admin ne voit que les comptes de SON secteur : un
        # admin FON ne doit pas voir les comptes Consumables, et
        # réciproquement.
        #
        # `sector="ALL"` (ou un secteur autre que le sien) lève ce filtre —
        # réservé aux comptes d'amorçage `ADMINS_MULTI_SECTEURS` (aujourd'hui
        # Idy509, qui crée les premiers comptes FON/RAN/Fuel/Spare avant
        # qu'ils n'aient leur propre admin). Le paramètre était auparavant
        # ouvert à TOUT admin : un admin FON énumérait ainsi les identifiants
        # administrateurs de tous les autres secteurs.
        #
        # Un admin non autorisé qui le passe quand même est silencieusement
        # ramené à son propre secteur, sans erreur — même traitement que
        # `all_sites` sur /products : un paramètre de confort ignoré ne doit
        # pas casser un écran.
        secteur_appelant = _secteur_du_compte(conn, user)
        multi = est_admin_multi_secteurs(user.get("username"))
        if not multi and sector and sector != secteur_appelant:
            sector = None
        if sector == "ALL":
            rows = conn.execute(
                "SELECT * FROM users ORDER BY sector, display_name",
            ).fetchall()
        else:
            secteur = sector if sector else secteur_appelant
            rows = conn.execute(
                "SELECT * FROM users WHERE sector = ? ORDER BY display_name",
                (secteur,),
            ).fetchall()
    return [dict(r) for r in rows]


@app.post("/users", response_model=UserOut)
def create_user(payload: UserIn, user=Depends(utilisateur_courant)):
    with get_conn(write=True) as conn:
        if utilisateurs_existent(conn):
            if user is None or user["role"] != "admin":
                raise HTTPException(status_code=403, detail="Seul un administrateur peut créer des comptes")
            # Le secteur demandé est IMPOSÉ à celui de l'appelant. Sans ce
            # contrôle, un admin FON créait un compte admin CONSUMABLES en
            # posant simplement `sector` dans le corps de la requête : le
            # cloisonnement par secteur, appliqué partout ailleurs en lecture
            # comme en écriture, se contournait en un appel.
            #
            # Seuls les comptes d'amorçage (ADMINS_MULTI_SECTEURS) gardent le
            # droit d'ouvrir un compte dans un autre secteur : c'est ainsi que
            # les premiers comptes FON/RAN/Fuel/Spare existent.
            secteur_appelant = _secteur_du_compte(conn, user)
            if (payload.sector != secteur_appelant
                    and not est_admin_multi_secteurs(user.get("username"))):
                raise HTTPException(
                    status_code=403,
                    detail=f"Vous ne pouvez créer des comptes que dans votre secteur "
                           f"({secteur_appelant}).",
                )
        # Aucun compte en base : tout premier démarrage. Le secteur reste
        # libre — c'est l'installateur qui choisit celui de son entrepôt.
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, display_name, role, region, "
                "sector, site) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (payload.username, hacher_mot_de_passe(payload.password),
                 payload.display_name, payload.role, payload.region, payload.sector,
                 payload.site),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"L'identifiant '{payload.username}' existe déjà")
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
        auteur = user["username"] if user else "premier lancement"

        _journaliser_audit(conn, "user", cur.lastrowid, "create",
                           sector=payload.sector,
                           new={"username": payload.username, "role": payload.role,
                                "display_name": payload.display_name,
                                "region": payload.region, "sector": payload.sector},
                           username=auteur)

        log.info("utilisateur créé : %s (rôle %s, secteur %s) par %s",
                 payload.username, payload.role, payload.sector, auteur)
        return dict(row)


@app.put("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, admin=Depends(exiger_admin)):
    with get_conn(write=True) as conn:
        existing = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Utilisateur introuvable")
        # Un admin ne gère que les comptes de SON secteur : un compte d'un
        # autre secteur (ex. FON vu par un admin Consumables) est traité
        # comme introuvable, pas comme un 403 — il ne doit même pas savoir
        # que cet identifiant existe ailleurs.
        if existing["sector"] != _secteur_du_compte(conn, admin):
            raise HTTPException(status_code=404, detail="Utilisateur introuvable")

        # `UserUpdate` ne connaît pas le secteur du compte (volontairement,
        # voir le modèle) et n'a donc pu faire qu'un contrôle large. La
        # vérification stricte — cette région appartient-elle au secteur DE
        # CE COMPTE — se fait ici, maintenant que `existing["sector"]` est
        # connu.
        region_visee = payload.region if payload.region is not None else (
            existing["region"] if (payload.role or existing["role"]) == "regional" else None)
        if region_visee and not a_entrepot_regional(region_visee, existing["sector"]):
            raise HTTPException(
                status_code=422,
                detail=f"{region_visee} n'a pas d'entrepôt régional pour le "
                       f"secteur {existing['sector']} (régions possibles : "
                       f"{', '.join(regions_avec_entrepot_for(existing['sector']))})",
            )

        # Anti-verrouillage : empêcher de désactiver ou rétrograder le dernier
        # admin ACTIF DE SON SECTEUR (un admin FON et un admin Consumables ne
        # se comptent pas l'un l'autre : chaque secteur doit garder le sien).
        est_dernier_admin = False
        if existing["role"] == "admin" and existing["active"]:
            nb_admins_actifs = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1 "
                "AND sector = ?",
                (existing["sector"],),
            ).fetchone()["n"]
            est_dernier_admin = (nb_admins_actifs <= 1)

        if est_dernier_admin:
            if payload.active is not None and not payload.active:
                raise HTTPException(
                    status_code=422,
                    detail="Impossible de désactiver le seul compte administrateur actif",
                )
            if payload.role is not None and payload.role != "admin":
                raise HTTPException(
                    status_code=422,
                    detail="Impossible de rétrograder le seul compte administrateur actif",
                )

        # Politique de mot de passe : c'est le rôle RÉSULTANT qui compte, pas
        # l'ancien. Un PUT qui promeut un magasinier en admin tout en lui
        # posant « 123456 » créerait sinon un compte administrateur avec un
        # mot de passe de magasinier.
        if payload.password is not None:
            role_resultant = payload.role or existing["role"]
            erreur = erreur_mot_de_passe(payload.password, role_resultant)
            if erreur:
                raise HTTPException(status_code=422, detail=erreur)

        old_values = {
            "display_name": existing["display_name"],
            "role": existing["role"],
            "region": existing["region"],
            "active": existing["active"],
        }

        revoque_session = False
        if payload.display_name is not None:
            conn.execute("UPDATE users SET display_name = ? WHERE id = ?",
                         (payload.display_name, user_id))
        if payload.role is not None:
            # region suit le rôle : 'regional' l'exige (contrôlé par UserUpdate),
            # tout autre rôle la remet à NULL — un ancien rattachement laissé en
            # place rouvrirait la région au compte s'il redevenait régional.
            conn.execute("UPDATE users SET role = ?, region = ? WHERE id = ?",
                         (payload.role, payload.region, user_id))
            if payload.role != existing["role"]:
                revoque_session = True
        elif payload.region is not None and existing["role"] == "regional":
            conn.execute("UPDATE users SET region = ? WHERE id = ?",
                         (payload.region, user_id))
            revoque_session = True
        if payload.active is not None:
            conn.execute("UPDATE users SET active = ? WHERE id = ?",
                         (1 if payload.active else 0, user_id))
            if not payload.active and existing["active"]:
                revoque_session = True
        if payload.password is not None:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (hacher_mot_de_passe(payload.password), user_id))
            revoque_session = True
        if "site" in payload.model_fields_set:
            # `is not None` ne suffit pas ici : le validateur transforme déjà
            # une chaîne vide en None (site effacé), donc "envoyé vide" et
            # "pas envoyé" sont tous deux `None` — seul `model_fields_set`
            # distingue "le champ était dans la requête" du champ absent,
            # ce qui permet de vraiment EFFACER un site (retour à "aucune
            # restriction"). Pas une frontière de sécurité comme le secteur :
            # changer de site ne révoque pas la session, ça prend effet au
            # prochain appel (`_site_du_compte` relit en base à chaque appel).
            conn.execute("UPDATE users SET site = ? WHERE id = ?",
                         (payload.site, user_id))

        # Révoquer les sessions de l'utilisateur modifié si nécessaire
        if revoque_session:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            # Le cache mémoire des sessions est indexé par jeton, pas par
            # compte : sans ce balayage, un jeton révoqué resterait accepté
            # jusqu'à CACHE_SESSION_DUREE_S.
            invalider_cache_sessions_utilisateur(user_id)

        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

        new_values = {
            "display_name": row["display_name"],
            "role": row["role"],
            "region": row["region"],
            "active": row["active"],
        }
        if payload.password is not None:
            old_values["password"] = "(réinitialisé)"
            new_values["password"] = "(réinitialisé)"

        _journaliser_audit(conn, "user", user_id, "update",
                           old=old_values, new=new_values,
                           username=(admin["username"] if admin else "système"),
                           sector=existing["sector"])

        log.info("utilisateur modifié : %s par %s", existing["username"], (admin["username"] if admin else "système"))
        return dict(row)


@app.get("/login-history")
def login_history(limit: int = Query(50, ge=1, le=500), admin=Depends(exiger_admin)):
    """Connexions récentes des comptes DU SECTEUR de l'admin.

    `login_history` ne porte qu'un identifiant : le secteur vient donc du
    compte correspondant. Les tentatives sur un identifiant INCONNU (aucun
    compte de ce nom) n'appartiennent à aucun secteur et restent visibles de
    tous les admins — c'est précisément la trace qu'il ne faut cacher à
    personne : quelqu'un essaie des identifiants au hasard.
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, admin)
        rows = conn.execute(
            "SELECT h.* FROM login_history h "
            "LEFT JOIN users u ON u.username = h.username COLLATE NOCASE "
            "WHERE u.sector = ? OR u.id IS NULL "
            "ORDER BY h.created_at DESC LIMIT ?",
            (secteur, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/users/revue")
def revue_des_comptes(admin=Depends(exiger_admin)):
    """Revue des comptes : qui existe, et qui ne s'est plus connecté depuis quand.

    Un compte oublié reste un compte valide : le magasinier parti l'an dernier
    peut encore se connecter avec son mot de passe. Cet écran met les comptes
    dormants sous les yeux de l'admin.

    AUCUNE désactivation automatique. Un compte peut dormir six mois pour de
    bonnes raisons (superviseur de région saisonnière) : c'est l'admin qui
    tranche, jamais le système.
    """
    aujourdhui = maintenant().date()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.username, u.display_name, u.role, u.region,
                   u.active, u.created_at,
                   (SELECT MAX(h.created_at) FROM login_history h
                     WHERE h.username = u.username COLLATE NOCASE
                       AND h.success = 1) AS derniere_connexion
            FROM users u
            WHERE u.sector = ?
            ORDER BY u.display_name
            """,
            (_secteur_du_compte(conn, admin),),
        ).fetchall()
        # Périmètre des lecteurs restreints : l'admin doit voir, dans le même
        # écran, quels comptes sont limités à une zone.
        perimetres: dict[int, list[str]] = defaultdict(list)
        for lien in conn.execute(
                "SELECT user_id, region FROM user_regions ORDER BY region"):
            perimetres[lien["user_id"]].append(lien["region"])

    comptes = []
    for row in rows:
        compte = dict(row)
        compte["active"] = bool(compte["active"])
        compte["regions_autorisees"] = perimetres.get(compte["id"], [])
        # Jamais connecté : l'ancienneté du compte fait office de repère, sinon
        # un compte créé hier apparaîtrait aussi « inactif » qu'un compte de 2023.
        reference = compte["derniere_connexion"] or compte["created_at"]
        compte["jamais_connecte"] = compte["derniere_connexion"] is None
        compte["jours_inactif"] = _jours_depuis(reference, aujourdhui)
        comptes.append(compte)

    # Les plus dormants en tête : c'est là que se trouvent les comptes à fermer.
    comptes.sort(key=lambda c: (-(c["jours_inactif"] if c["jours_inactif"] is not None else -1),
                                c["display_name"].lower()))
    return {"comptes": comptes, "nombre": len(comptes)}


def _jours_depuis(horodatage: str | None, aujourdhui) -> Optional[int]:
    """Nombre de jours écoulés depuis un horodatage SQLite, ou None.

    Ne lève jamais : un horodatage illisible (base restaurée à la main,
    import) doit laisser la ligne s'afficher sans son ancienneté, pas faire
    tomber tout l'écran de revue des comptes.
    """
    if not horodatage:
        return None
    try:
        jour = date.fromisoformat(str(horodatage)[:10])
    except (ValueError, TypeError):
        return None
    return max(0, (aujourdhui - jour).days)


@app.get("/users/{user_id}/regions")
def lister_regions_utilisateur(user_id: int, admin=Depends(exiger_admin)):
    """Périmètre régional d'un compte lecteur. Liste vide = voit tout."""
    with get_conn() as conn:
        # Scopé au secteur de l'admin (comme `update_user`) : sans ce filtre,
        # un admin lisait — et par le PUT ci-dessous MODIFIAIT — le périmètre
        # régional d'un compte d'un autre secteur, en connaissant son id.
        # 404 et non 403 : l'existence du compte ne se révèle pas.
        compte = conn.execute(
            "SELECT id, role FROM users WHERE id = ? AND sector = ?",
            (user_id, _secteur_du_compte(conn, admin))).fetchone()
        if not compte:
            raise HTTPException(status_code=404, detail="Utilisateur introuvable")
        rows = conn.execute(
            "SELECT region FROM user_regions WHERE user_id = ? ORDER BY region",
            (user_id,)).fetchall()
    return {"user_id": user_id, "role": compte["role"],
            "regions": [r["region"] for r in rows]}


@app.put("/users/{user_id}/regions")
def definir_regions_utilisateur(user_id: int, payload: UserRegionsIn,
                                admin=Depends(exiger_admin)):
    """Restreint un compte LECTEUR à une ou plusieurs régions.

    Réservé au rôle lecteur : un magasinier saisit pour l'entrepôt central,
    un compte régional a déjà sa région unique, un admin doit tout voir.
    Restreindre l'un des trois donnerait un compte à moitié muselé, avec des
    écrans à demi vides et aucune explication.

    Liste vide = restriction levée, le lecteur revoit toutes les régions.
    """
    with get_conn(write=True) as conn:
        compte = conn.execute(
            "SELECT * FROM users WHERE id = ? AND sector = ?",
            (user_id, _secteur_du_compte(conn, admin))).fetchone()
        if not compte:
            raise HTTPException(status_code=404, detail="Utilisateur introuvable")
        if compte["role"] != "lecteur":
            raise HTTPException(
                status_code=422,
                detail="Seul un compte lecteur peut être restreint à des régions "
                       f"(ce compte est « {compte['role']} »).",
            )

        # `UserRegionsIn` ne connaît que le contrôle large (voir le modèle) :
        # la vérification stricte par secteur se fait ici, avec le secteur
        # RÉEL de ce compte.
        hors_secteur = [r for r in payload.regions
                        if not region_valide_pour_secteur(r, compte["sector"])]
        if hors_secteur:
            raise HTTPException(
                status_code=422,
                detail=f"Région(s) hors du secteur {compte['sector']} de ce "
                       f"compte : {', '.join(hors_secteur)}",
            )

        anciennes = [r["region"] for r in conn.execute(
            "SELECT region FROM user_regions WHERE user_id = ? ORDER BY region",
            (user_id,))]

        conn.execute("DELETE FROM user_regions WHERE user_id = ?", (user_id,))
        for region in payload.regions:
            conn.execute("INSERT INTO user_regions (user_id, region) VALUES (?, ?)",
                         (user_id, region))

        if anciennes != payload.regions:
            # Le périmètre est lu en base à chaque requête, la session n'a pas
            # besoin d'être révoquée pour que le changement s'applique. On la
            # révoque quand même : l'écran déjà ouvert du lecteur affiche
            # encore des régions qu'il n'a plus le droit de voir.
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            invalider_cache_sessions_utilisateur(user_id)
            _journaliser_audit(conn, "user", user_id, "regions",
                               old={"regions": anciennes},
                               new={"regions": payload.regions},
                               username=(admin["username"] if admin else "système"),
                               sector=compte["sector"])
            log.info("périmètre régional de %s : %s -> %s",
                     compte["username"], anciennes or "toutes", payload.regions or "toutes")

    return {"user_id": user_id, "regions": payload.regions}


# ------------------------------------------------- annotations par région

@app.get("/regions/notes")
def lister_notes_regions(user=Depends(exiger_utilisateur)):
    """Annotations libres par région (saisonnalité, accès dégradé).

    Lisible par tout compte connecté : « route difficile juin-septembre » est
    une information de terrain utile à qui prépare une expédition, pas un
    secret d'administration.
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        rows = conn.execute(
            "SELECT region, note, updated_by, updated_at FROM region_notes "
            "WHERE TRIM(note) <> '' AND sector = ? ORDER BY region",
            (secteur,),
        ).fetchall()
    notes = [dict(r) for r in rows]
    return {"notes": notes, "par_region": {n["region"]: n["note"] for n in notes}}


@app.put("/regions/note")
def definir_note_region(payload: RegionNoteIn, admin=Depends(exiger_admin)):
    """Pose ou efface l'annotation d'une région. Une note vide efface."""
    auteur = admin["username"] if admin else "système"
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, admin)
        if not region_valide_pour_secteur(payload.region, secteur):
            raise HTTPException(
                status_code=422,
                detail=f"Région inconnue pour le secteur {secteur} : {payload.region!r}",
            )
        ancienne = conn.execute(
            "SELECT note FROM region_notes WHERE region = ? AND sector = ?",
            (payload.region, secteur)).fetchone()
        ancien_texte = ancienne["note"] if ancienne else ""
        if payload.note:
            conn.execute(
                "INSERT INTO region_notes (region, sector, note, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(region, sector) DO UPDATE SET note = excluded.note, "
                "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (payload.region, secteur, payload.note, auteur, maintenant_texte()),
            )
        else:
            conn.execute("DELETE FROM region_notes WHERE region = ? AND sector = ?",
                         (payload.region, secteur))

        if ancien_texte != payload.note:
            _journaliser_audit(conn, "region_note", 0, "update",
                               old={"region": payload.region, "note": ancien_texte},
                               new={"region": payload.region, "note": payload.note},
                               username=auteur,
                               sector=secteur)

        row = conn.execute(
            "SELECT region, note, updated_by, updated_at FROM region_notes "
            "WHERE region = ? AND sector = ?",
            (payload.region, secteur)).fetchone()

    return dict(row) if row else {"region": payload.region, "note": "",
                                  "updated_by": None, "updated_at": None}


@app.patch("/me/password")
def change_my_password(payload: PasswordChangeIn, request: Request,
                       user=Depends(exiger_utilisateur),
                       x_user_token: Optional[str] = Header(default=None)):
    # Aucun compte en base (tout premier lancement) : exiger_utilisateur laisse
    # passer avec user=None et l'accès à user["user_id"] plus bas partait en 500.
    if user is None:
        raise HTTPException(status_code=401, detail="Connexion requise")
    old_pw = (payload.old_password or "").strip()
    new_pw = (payload.new_password or "").strip()
    cle_echecs = (user.get("username") or "").lower()
    if not old_pw:
        raise HTTPException(status_code=400,
                            detail="Ancien mot de passe requis, nouveau 6+ caractères")
    # Même verrouillage qu'à la connexion : cet endpoint vérifie lui aussi un
    # mot de passe, et n'était soumis à AUCUN comptage. Une session ouverte
    # (poste laissé déverrouillé) permettait d'y essayer autant de mots de
    # passe que voulu, à la vitesse du réseau.
    _refuser_si_verrouille(cle_echecs)
    # Même politique qu'à la création : renforcée pour un compte admin.
    erreur = erreur_mot_de_passe(new_pw, user.get("role", ""),
                                 username=user.get("username"))
    if erreur:
        raise HTTPException(status_code=400, detail=erreur)
    with get_conn() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?",
                           (user["user_id"],)).fetchone()
    if not row or not verifier_mot_de_passe(old_pw, row["password_hash"]):
        _register_login_failure(cle_echecs,
                                request.client.host if request.client else None)
        raise HTTPException(status_code=403, detail="Ancien mot de passe incorrect")
    with get_conn(write=True) as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hacher_mot_de_passe(new_pw), user["user_id"]))
        # Cohérence avec PUT /users/{id} : changer un mot de passe invalide les
        # sessions ouvertes ailleurs (poste laissé connecté, mot de passe
        # partagé). La session courante est conservée, sinon l'utilisateur se
        # ferait déconnecter par son propre changement.
        if x_user_token:
            # `sessions.token` porte l'EMPREINTE du jeton, pas le jeton :
            # comparer `x_user_token` tel quel ne préservait plus aucune
            # ligne, et l'utilisateur se déconnectait lui-même en changeant
            # son mot de passe. Voir `utilisateurs.hacher_jeton`.
            conn.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?",
                         (user["user_id"], hacher_jeton(x_user_token)))
        else:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["user_id"],))
        # Les autres jetons de ce compte viennent d'être révoqués : le cache
        # mémoire doit les oublier tout de suite. La session courante est
        # remise en cache dès la requête suivante.
        invalider_cache_sessions_utilisateur(user["user_id"])
        # Un changement de mot de passe fait par l'admin (PUT /users/{id}) est
        # tracé ; celui fait par le compte lui-même ne l'était pas. C'est
        # pourtant la même chose du point de vue d'un audit : « qui a changé
        # l'accès de ce compte, et quand ». Aucun secret n'est journalisé.
        _journaliser_audit(conn, "user", user["user_id"], "password_change",
                           new={"password": "(changé par l'utilisateur)"},
                           username=user["username"],
                           sector=_secteur_du_compte(conn, user))
    log.info("mot de passe changé par l'utilisateur %s", user["username"])
    return {"ok": True}


@app.get("/me")
def me(user=Depends(exiger_utilisateur)):
    if user is None:
        raise HTTPException(status_code=401, detail="Connexion requise")
    with get_conn() as conn:
        region = _region_du_compte(conn, user)
    return {
        "id": user["user_id"], "username": user["username"],
        "display_name": user["display_name"], "role": user["role"],
        "region": region,
    }


# ---------------------------------------------------------------- audit log

@app.get("/audit-log")
def audit_log(
    entity_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    admin=Depends(exiger_admin),
):
    # Scopé au secteur de l'admin : le journal montrait à l'admin de n'importe
    # quel secteur toutes les écritures des autres (qui a modifié quel
    # produit, annulé quel bon, créé quel compte). `sector IS NULL` reste
    # visible de tous : ce sont les traces système (export, station) et les
    # lignes antérieures à la migration 54 dont l'entité a disparu depuis.
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, admin)
        conditions = ["(sector = ? OR sector IS NULL)"]
        params: list = [secteur]
        if entity_type:
            conditions.append("entity_type = ?")
            params.append(entity_type)
        rows = conn.execute(
            f"SELECT * FROM audit_log WHERE {' AND '.join(conditions)} "
            f"ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


# Plafond de l'export du journal. Le journal grossit sans limite entre deux
# purges ; sans borne, un export sur « toute la période » construirait un
# classeur de plusieurs centaines de milliers de lignes en mémoire.
AUDIT_EXPORT_MAX = 20_000
# Fenêtre par défaut, alignée sur l'export comptable.
AUDIT_EXPORT_JOURS_DEFAUT = 30


@app.get("/audit-log/export")
def audit_log_export(depuis: Optional[str] = None,
                     jusqu_a: Optional[str] = Query(None, alias="jusqu_a"),
                     entity_type: Optional[str] = None,
                     admin=Depends(exiger_admin)):
    """Journal d'audit d'une période, en classeur Excel.

    Réservé aux administrateurs, comme l'écran qui l'affiche : le journal
    nomme qui a fait quoi sur chaque compte et chaque bon.

    Défaut : les 30 derniers jours. `date(created_at)` et non une comparaison
    de chaînes : `created_at` porte l'heure, un `<= '2026-09-01'` exclurait
    toute la journée du 1er septembre (même piège que l'export comptable).
    """
    aujourdhui = maintenant().date()
    fin = _borne_date(jusqu_a, aujourdhui, "jusqu_a")
    debut = _borne_date(depuis, fin - timedelta(days=AUDIT_EXPORT_JOURS_DEFAUT), "depuis")
    if debut > fin:
        raise HTTPException(status_code=400,
                            detail="La date de début est postérieure à la date de fin.")

    conditions = ["date(created_at) BETWEEN ? AND ?"]
    params: list = [debut.strftime(FORMAT_DATE), fin.strftime(FORMAT_DATE)]
    if entity_type:
        conditions.append("entity_type = ?")
        params.append(entity_type)

    with get_conn() as conn:
        # Même cloisonnement que l'écran : un export est une COPIE du journal
        # qui sort de l'application. Le filtrer à l'écran mais pas à l'export
        # n'aurait fermé la porte qu'à moitié.
        secteur_admin = _secteur_du_compte(conn, admin)
    conditions.append("(sector = ? OR sector IS NULL)")
    params.append(secteur_admin)

    filtre = " AND ".join(conditions)
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM audit_log WHERE {filtre}", params
        ).fetchone()["n"]
        entrees = [dict(r) for r in conn.execute(
            f"SELECT created_at, username, action, entity_type, entity_id, "
            f"old_values, new_values FROM audit_log WHERE {filtre} "
            f"ORDER BY created_at DESC, id DESC LIMIT ?",
            params + [AUDIT_EXPORT_MAX],
        ).fetchall()]

    tronque = len(entrees) < total
    if tronque:
        log.warning("export du journal d'audit tronqué : %d entrées sur %d "
                    "(limite %d). Réduire la période.",
                    len(entrees), total, AUDIT_EXPORT_MAX)

    content = build_audit_log_excel(entrees)
    filename = export_filename("journal_audit")
    # L'export du journal d'audit s'inscrit LUI AUSSI dans le journal d'audit :
    # c'est l'export le plus sensible du système, celui qui nomme qui a fait
    # quoi sur chaque compte. La ligne écrite ici n'apparaît pas dans le
    # fichier qui vient d'être produit (il est déjà en mémoire) : elle sera
    # visible au prochain export, ce qui est exactement ce qu'on veut.
    _journaliser_export("journal_audit", admin,
                        depuis=debut.strftime(FORMAT_DATE),
                        jusqu_a=fin.strftime(FORMAT_DATE),
                        entity_type=entity_type, nb_entrees=len(entrees),
                        tronque=tronque)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Total-Count": str(total),
            "X-Returned-Count": str(len(entrees)),
            "X-Export-Truncated": "1" if tronque else "0",
        },
    )


# ---------------------------------------------------------------- contacts

TYPES_CONTACT_VALIDES = {"supplier", "customer", "other"}

# Champs libres de la fiche contact, modifiables par POST comme par PUT.
# `name`, `type` et `active` en sont exclus : ils ont chacun leur validation
# propre (nom non vide, type dans le référentiel, booléen -> 0/1).
CHAMPS_CONTACT_LIBRES = ("phone", "email", "address", "notes",
                          "devise", "conditions_paiement", "whatsapp")

# Un délai de livraison annoncé se compte en jours, pas en années : 365 est
# déjà absurde pour un consommable, mais laisse passer un import maritime
# exceptionnel. La borne existe pour attraper une faute de frappe (« 300 »
# tapé pour « 30 »), pas pour discuter le délai du fournisseur.
DELAI_FOURNISSEUR_MAX_JOURS = 365


def _delai_jours_valide(valeur):
    """Normalise `delai_jours` reçu du client, ou lève un 422 lisible.

    Chaîne vide et None valent tous deux « non renseigné » (NULL) : le
    formulaire client envoie une chaîne vide quand l'utilisateur efface le
    champ, et cet effacement doit vider la colonne, pas échouer.
    """
    if valeur is None or (isinstance(valeur, str) and not valeur.strip()):
        return None
    try:
        jours = int(str(valeur).strip())
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="Le délai de livraison doit être un nombre entier de jours.")
    if jours < 0 or jours > DELAI_FOURNISSEUR_MAX_JOURS:
        raise HTTPException(
            status_code=422,
            detail=f"Le délai de livraison doit être compris entre 0 et "
                   f"{DELAI_FOURNISSEUR_MAX_JOURS} jours.")
    return jours


@app.get("/contacts")
def list_contacts(type: Optional[str] = None, include_inactive: bool = False,
                  user=Depends(exiger_utilisateur)):
    """Liste les contacts, filtrables par type (fournisseur, client, autre)."""
    if type is not None and type not in TYPES_CONTACT_VALIDES:
        raise HTTPException(
            status_code=422,
            detail=f"Type de contact invalide : '{type}'. "
                   f"Valeurs acceptées : {', '.join(sorted(TYPES_CONTACT_VALIDES))}",
        )
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        conditions, params = ["sector = ?"], [secteur]
        if not include_inactive:
            conditions.append("active = 1")
        if type is not None:
            conditions.append("type = ?")
            params.append(type)
        where = f"WHERE {' AND '.join(conditions)}"
        rows = conn.execute(
            f"SELECT * FROM contacts {where} ORDER BY name", params
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/contacts/{contact_id}/documents")
def contact_documents(contact_id: int, limit: int = Query(50, ge=1, le=500),
                      user=Depends(exiger_utilisateur)):
    """Historique des bons liés à ce contact (le plus récent d'abord)."""
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        contact = conn.execute(
            "SELECT id FROM contacts WHERE id = ? AND sector = ?",
            (contact_id, secteur),
        ).fetchone()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact introuvable")
        rows = conn.execute(
            "SELECT * FROM documents WHERE party_contact_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (contact_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/contacts")
def create_contact(body: ContactIn, user=Depends(exiger_ecriture)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Le nom est obligatoire")
    contact_type = body.type
    if contact_type not in TYPES_CONTACT_VALIDES:
        raise HTTPException(
            status_code=422,
            detail=f"Type de contact invalide : '{contact_type}'. "
                   f"Valeurs acceptées : {', '.join(sorted(TYPES_CONTACT_VALIDES))}",
        )
    delai_jours = _delai_jours_valide(body.delai_jours)
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)
        colonnes = ("name", "type", *CHAMPS_CONTACT_LIBRES, "delai_jours", "sector")
        valeurs = ([name, contact_type]
                   + [getattr(body, c) for c in CHAMPS_CONTACT_LIBRES]
                   + [delai_jours, secteur])
        try:
            cur = conn.execute(
                f"INSERT INTO contacts ({', '.join(colonnes)}) "
                f"VALUES ({', '.join('?' * len(colonnes))})",
                valeurs,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail=f"Le contact '{name}' existe déjà")
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cur.lastrowid,)).fetchone()

        auteur = user["username"] if user else "inconnu"
        _journaliser_audit(conn, "contact", cur.lastrowid, "create",
                           new={"name": name, "type": contact_type}, username=auteur,
                           sector=secteur)

        log.info("contact créé : %s par %s", name, auteur)
        return dict(row)


@app.put("/contacts/{contact_id}")
def update_contact(contact_id: int, payload: ContactUpdate,
                   user=Depends(exiger_ecriture)):
    # `exclude_unset` : « champ absent » et « champ envoyé vide » restent
    # distincts, exactement comme le `in body` du dict d'avant.
    body = payload.model_dump(exclude_unset=True)
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)
        existing = conn.execute(
            "SELECT * FROM contacts WHERE id = ? AND sector = ?", (contact_id, secteur)
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Contact introuvable")

        if "type" in body and body["type"] not in TYPES_CONTACT_VALIDES:
            raise HTTPException(
                status_code=422,
                detail=f"Type de contact invalide : '{body['type']}'. "
                       f"Valeurs acceptées : {', '.join(sorted(TYPES_CONTACT_VALIDES))}",
            )

        old_values = {"name": existing["name"], "type": existing["type"],
                      "active": existing["active"]}
        updates = {}
        for field in ("name", "type", *CHAMPS_CONTACT_LIBRES):
            if field in body:
                updates[field] = body[field]
        # Validé AVANT l'UPDATE : un délai aberrant doit produire un 422
        # explicite, pas un entier silencieusement rangé en base.
        if "delai_jours" in body:
            updates["delai_jours"] = _delai_jours_valide(body["delai_jours"])
        # Même validation que POST /contacts : nom nettoyé et non vide.
        if "name" in updates:
            nom = (updates["name"] or "").strip()
            if not nom:
                raise HTTPException(status_code=400, detail="Le nom est obligatoire")
            updates["name"] = nom
        # "active" est stocké en INTEGER (0/1) : on convertit le booléen JSON.
        if "active" in body:
            updates["active"] = 1 if body["active"] else 0
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            try:
                conn.execute(f"UPDATE contacts SET {set_clause} WHERE id = ?",
                             list(updates.values()) + [contact_id])
            except sqlite3.IntegrityError:
                raise HTTPException(
                    status_code=409,
                    detail=f"Le contact '{updates.get('name', existing['name'])}' existe déjà",
                )

        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        new_values = {"name": row["name"], "type": row["type"], "active": row["active"]}

        auteur = user["username"] if user else "inconnu"
        _journaliser_audit(conn, "contact", contact_id, "update",
                           old=old_values, new=new_values, username=auteur,
                           sector=secteur)

        return dict(row)


# ---------------------------------------------------------------- archivage produits

@app.delete("/products/{product_id}")
def archive_product(product_id: int, admin=Depends(exiger_admin)):
    """Archive un produit : il ne sera plus proposé sur les bons."""
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, admin)
        # `tous_sites=True` : archiver relève de la gestion du catalogue, au
        # même titre que PATCH — l'admin doit pouvoir retirer la fiche d'un
        # autre site de son secteur.
        row = _produit_accessible(conn, product_id, admin, tous_sites=True)
        if not row:
            raise HTTPException(status_code=404, detail="Produit introuvable")

        # Un produit archivé disparaît du tableau de bord et de l'export : son
        # stock restant s'évaporerait des totaux sans aucun mouvement en face.
        if row["current_stock"] > 1e-9:
            raise HTTPException(
                status_code=422,
                detail=f"Ce produit a encore {row['current_stock']:g} {row['unit']} en stock. "
                       f"Mettez le stock à zéro par un ajustement avant d'archiver ce produit.",
            )

        conn.execute("UPDATE products SET archived = 1 WHERE id = ?", (product_id,))

        _journaliser_audit(conn, "product", product_id, "archive",
                           old={"sku": row["sku"], "name": row["name"], "archived": row["archived"]},
                           new={"sku": row["sku"], "name": row["name"], "archived": 1},
                           username=(admin["username"] if admin else "système"),
                           sector=secteur)

        log.info("produit %d archivé (sku=%s) par %s", product_id, row["sku"],
                 (admin["username"] if admin else "système"))

    # Hors transaction, comme dans update_product : un archivage retire le
    # produit de TOUS les agrégats du tableau de bord.
    _invalider_cache_dashboard(secteur)
    return {"ok": True}


# ---------------------------------------------------------------- photos
#
# Deux usages distincts, un seul magasin de fichiers (server/photos.py) :
#   - la fiche produit porte UNE photo, remplaçable (colonne products.photo_path) ;
#   - un bon porte PLUSIEURS pièces jointes (table document_photos), parce
#     qu'un colis abîmé se photographie sous plusieurs angles.
#
# Les octets ne transitent jamais par SQLite : la base ne retient que le nom du
# fichier, généré par nous (UUID), jamais celui envoyé par le poste.


async def _photo_recue(file: UploadFile) -> bytes:
    """Lit et valide une image envoyée. Traduit un refus en 4xx explicite.

    Deux contrôles indépendants :
      - la taille, bornée pendant la lecture (rien n'est accumulé au-delà) ;
      - le format, décidé par les premiers octets du fichier. Un `.exe`
        renommé `.jpg` échoue ici, l'extension n'étant qu'une déclaration.
    """
    contenu = await _lire_fichier_borne(file, limite=photos.MAX_PHOTO_BYTES)
    if photos.detecter_extension(contenu) is None:
        log.warning("photo refusée : %r n'est pas une image acceptée",
                    file.filename)
        raise HTTPException(
            status_code=415,
            detail="Ce fichier n'est pas une image JPG, PNG ou WebP. "
                   "Le contenu du fichier est vérifié, pas son nom : "
                   "renommer un fichier en .jpg ne suffit pas.",
        )
    return contenu


def _servir_photo(nom, *, contexte: str) -> Response:
    """Renvoie les octets d'une image déjà enregistrée.

    Lue en mémoire plutôt que servie en flux : 5 Mo au maximum, et cela évite
    de laisser un descripteur de fichier ouvert dans la boucle d'événements.
    """
    cible = photos.chemin(nom)
    if cible is None or not cible.exists():
        # Nom invalide et fichier disparu se répondent pareil : ne pas
        # renseigner l'appelant sur ce qui existe sur le disque du serveur.
        raise HTTPException(status_code=404, detail=f"Aucune photo pour {contexte}")
    return Response(content=cible.read_bytes(),
                    media_type=photos.type_mime(nom),
                    headers={"Cache-Control": "private, max-age=86400"})


@app.post("/products/{product_id}/photo")
async def upload_product_photo(product_id: int, file: UploadFile,
                               admin=Depends(exiger_admin)):
    """Pose ou remplace la photo d'une fiche produit. Administrateur seul.

    Même exigence que la fiche elle-même : le catalogue est la référence
    partagée par tous les postes, il ne se modifie pas depuis n'importe quel
    compte.
    """
    contenu = await _photo_recue(file)

    # Écriture disque AVANT la transaction : une transaction SQLite en écriture
    # tient un verrou exclusif sur toute la base, et rien ne doit attendre une
    # écriture de 5 Mo pendant ce temps.
    nom = photos.enregistrer(contenu, nom_origine=file.filename)

    try:
        with get_conn(write=True) as conn:
            # Fiche produit = catalogue : `tous_sites=True`, comme PATCH.
            row = _produit_accessible(conn, product_id, admin, tous_sites=True)
            if not row:
                raise HTTPException(status_code=404, detail="Produit introuvable")
            ancienne = row["photo_path"]
            conn.execute("UPDATE products SET photo_path = ? WHERE id = ?",
                         (nom, product_id))
            _journaliser_audit(conn, "product", product_id, "photo",
                               old={"photo_path": ancienne},
                               new={"photo_path": nom},
                               username=(admin["username"] if admin else "système"),
                               sector=row["sector"])
    except Exception:
        # Le produit n'existe pas, ou l'écriture a échoué : le fichier déjà
        # posé sur disque n'est référencé nulle part, on le retire tout de
        # suite plutôt que de laisser un orphelin permanent.
        photos.supprimer(nom)
        raise

    # L'ancienne image est effacée APRÈS la validation de la transaction : si
    # celle-ci avait échoué, la fiche pointerait encore dessus.
    if ancienne and ancienne != nom:
        photos.supprimer(ancienne)

    log.info("photo produit %d posée par %s", product_id,
             (admin["username"] if admin else "système"))
    return {"ok": True, "photo_path": nom}


@app.get("/products/{product_id}/photo")
def get_product_photo(product_id: int, user=Depends(exiger_utilisateur)):
    """Sert la photo d'un produit. Ouvert à tout compte connecté, lecteur inclus."""
    with get_conn() as conn:
        # `tous_sites=True` : la photo suit la fiche produit — un admin qui
        # ouvre le catalogue de tous les sites (`all_sites=true`) doit voir
        # les images qui vont avec. Un compte non-admin reste scopé à son site.
        row = _produit_accessible(conn, product_id, user, tous_sites=True)
    if not row:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    return _servir_photo(row["photo_path"], contexte=f"le produit {product_id}")


@app.delete("/products/{product_id}/photo")
def delete_product_photo(product_id: int, admin=Depends(exiger_admin)):
    with get_conn(write=True) as conn:
        # Même périmètre que la pose de la photo (catalogue, admin).
        row = _produit_accessible(conn, product_id, admin, tous_sites=True)
        if not row:
            raise HTTPException(status_code=404, detail="Produit introuvable")
        ancienne = row["photo_path"]
        if not ancienne:
            raise HTTPException(status_code=404, detail="Ce produit n'a pas de photo")
        conn.execute("UPDATE products SET photo_path = NULL WHERE id = ?",
                     (product_id,))
        _journaliser_audit(conn, "product", product_id, "photo_suppression",
                           old={"photo_path": ancienne}, new={"photo_path": None},
                           username=(admin["username"] if admin else "système"),
                           sector=row["sector"])
    photos.supprimer(ancienne)
    return {"ok": True}


@app.post("/documents/{document_id}/photo")
async def upload_document_photo(document_id: int, file: UploadFile,
                                caption: Optional[str] = Query(default=None,
                                                               max_length=200),
                                user=Depends(exiger_ecriture)):
    """Joint une photo à un bon existant (colis abîmé, preuve de livraison).

    Ouvert à tout compte qui peut créer un bon — c'est-à-dire tout le monde
    sauf le lecteur, dont le rôle est de consulter, pas d'ajouter des pièces
    au dossier.

    Plusieurs photos par bon, délibérément : un colis abîmé se photographie
    sous plusieurs angles, et la photo de l'étiquette ne remplace pas celle du
    dégât.
    """
    contenu = await _photo_recue(file)
    nom = photos.enregistrer(contenu, nom_origine=file.filename)
    auteur = user["username"] if user else "système"

    try:
        with get_conn(write=True) as conn:
            secteur = _secteur_du_compte(conn, user)
            doc = conn.execute("SELECT id FROM documents WHERE id = ? AND sector = ?",
                               (document_id, secteur)).fetchone()
            if not doc:
                raise HTTPException(status_code=404,
                                    detail=f"Bon {document_id} introuvable")
            cur = conn.execute(
                "INSERT INTO document_photos (document_id, filename, caption, "
                "uploaded_by) VALUES (?, ?, ?, ?)",
                (document_id, nom, (caption or "").strip() or None, auteur),
            )
            photo_id = cur.lastrowid
            _journaliser_audit(conn, "document", document_id, "photo",
                               new={"photo_id": photo_id, "filename": nom},
                               username=auteur,
                               sector=secteur)
    except Exception:
        photos.supprimer(nom)
        raise

    log.info("photo jointe au bon %d par %s", document_id, auteur)
    return {"ok": True, "id": photo_id, "filename": nom}


@app.get("/documents/{document_id}/photos")
def list_document_photos(document_id: int, user=Depends(exiger_utilisateur)):
    """Liste les pièces jointes d'un bon — métadonnées seules, pas les octets."""
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        doc = conn.execute("SELECT id FROM documents WHERE id = ? AND sector = ?",
                           (document_id, secteur)).fetchone()
        if not doc:
            raise HTTPException(status_code=404,
                                detail=f"Bon {document_id} introuvable")
        rows = conn.execute(
            "SELECT id, caption, uploaded_by, created_at FROM document_photos "
            "WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()
    return {"photos": [dict(r) for r in rows]}


@app.get("/documents/{document_id}/photos/{photo_id}")
def get_document_photo(document_id: int, photo_id: int,
                       user=Depends(exiger_utilisateur)):
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        doc = conn.execute("SELECT id FROM documents WHERE id = ? AND sector = ?",
                           (document_id, secteur)).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail=f"Bon {document_id} introuvable")
        row = conn.execute(
            "SELECT filename FROM document_photos WHERE id = ? AND document_id = ?",
            (photo_id, document_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Photo introuvable")
    return _servir_photo(row["filename"], contexte=f"le bon {document_id}")


@app.delete("/documents/{document_id}/photos/{photo_id}")
def delete_document_photo(document_id: int, photo_id: int,
                          admin=Depends(exiger_admin)):
    """Retire une pièce jointe. Administrateur seul.

    Une photo est une pièce au dossier d'un bon : celui qui l'a posée ne doit
    pas pouvoir la retirer seul après coup, sinon la preuve ne prouve rien.
    """
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, admin)
        doc = conn.execute("SELECT id FROM documents WHERE id = ? AND sector = ?",
                           (document_id, secteur)).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail=f"Bon {document_id} introuvable")
        row = conn.execute(
            "SELECT filename FROM document_photos WHERE id = ? AND document_id = ?",
            (photo_id, document_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Photo introuvable")
        conn.execute("DELETE FROM document_photos WHERE id = ?", (photo_id,))
        _journaliser_audit(conn, "document", document_id, "photo_suppression",
                           old={"photo_id": photo_id, "filename": row["filename"]},
                           new={"photo_id": photo_id, "filename": None},
                           username=(admin["username"] if admin else "système"),
                           sector=secteur)
    photos.supprimer(row["filename"])
    return {"ok": True}


# ---------------------------------------------------------------- websocket

# ---------------------------------------------------------------- bouteilles consignées

@app.get("/bottles/balance")
def bottles_balance(
    product_id: Optional[int] = None,
    region: Optional[str] = None,
    technician: Optional[str] = None,
    par_technicien: bool = False,
    user=Depends(exiger_utilisateur),
):
    """Solde des bouteilles dues par produit et région.

    `par_technicien=true` bascule le regroupement sur le technicien nommé au
    lieu de la région (seules les lignes portant un technicien sont alors
    comptées) : c'est la vue « qui doit encore rendre ses bouteilles ».
    Le regroupement par défaut reste strictement celui d'avant — une ligne
    attribuée à un technicien porte aussi sa région, donc les totaux
    régionaux continuent de tout compter.
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        conditions = ["p.sector = ?"]
        params = [secteur]
        if product_id:
            conditions.append("bl.product_id = ?")
            params.append(product_id)
        if region:
            conditions.append("bl.region = ?")
            params.append(region)
        if technician:
            conditions.append("bl.technician = ?")
            params.append(technician)
        if par_technicien:
            conditions.append("bl.technician IS NOT NULL")
        filtre = " AND ".join(conditions)
        colonne = "bl.technician" if par_technicien else "bl.region"
        # La colonne hors regroupement est renvoyée à NULL plutôt qu'à une
        # valeur arbitraire choisie par SQLite parmi les lignes agrégées.
        colonnes = ("NULL AS region, bl.technician" if par_technicien
                    else "bl.region, NULL AS technician")

        rows = conn.execute(
            f"""
            SELECT bl.product_id, {colonnes}, p.sku, p.name,
                   SUM(CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN bl.bottles ELSE 0 END) AS balance
            FROM bottle_ledger bl
            JOIN products p ON p.id = bl.product_id
            LEFT JOIN documents d ON d.id = bl.document_id
            WHERE {filtre}
            GROUP BY bl.product_id, {colonne}
            HAVING ABS(balance) > 1e-9
            ORDER BY p.name, {colonne}
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/bottles/return")
def return_bottles(body: BottleReturnIn, user=Depends(exiger_ecriture)):
    """Enregistre le retour de bouteilles vides par une région.

    La validation (quantité > 0, région canonique) est portée par le modèle
    Pydantic : elle était contournée tant que le corps arrivait en dict brut.
    """
    product_id = body.product_id
    region_name = body.region
    bottles = body.bottles
    note = body.note
    technician = body.technician

    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)
        produit = conn.execute(
            "SELECT consigne_bouteille FROM products WHERE id = ? AND sector = ?",
            (product_id, secteur),
        ).fetchone()
        if not produit:
            raise HTTPException(status_code=404, detail="Produit introuvable")
        if not produit["consigne_bouteille"]:
            raise HTTPException(
                status_code=422,
                detail="Ce produit n'a pas de consigne bouteille définie",
            )
        # Même contrôle que sur POST /documents : `normaliser_technicien`
        # (Pydantic) ne fait qu'un contrôle orthographique large, sans le
        # secteur de l'auteur.
        if technician and not technicien_valide_pour_secteur(technician, secteur):
            raise HTTPException(
                status_code=422,
                detail=f"Technicien inconnu pour le secteur {secteur} : {technician!r}",
            )

        # Une fiche papier ne peut être saisie qu'une fois par produit : deux
        # saisies du même numéro créditaient deux fois le même retour de
        # bouteilles (double clic, ou reprise d'une fiche déjà enregistrée par
        # l'autre poste). Le contrôle ne porte que sur les retours ENCORE
        # valides : une fiche annulée peut être ressaisie.
        if body.reference:
            doublon = conn.execute(
                """
                SELECT bl.id, bl.bottles, bl.created_at, bl.created_by
                FROM bottle_ledger bl
                WHERE bl.product_id = ? AND bl.bottles < 0
                  AND bl.reference = ? COLLATE NOCASE
                  AND NOT EXISTS (
                      SELECT 1 FROM bottle_ledger a WHERE a.cancels_entry_id = bl.id
                  )
                """,
                (product_id, body.reference),
            ).fetchone()
            if doublon:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"La fiche n° {body.reference} a déjà été enregistrée pour ce "
                        f"produit le {doublon['created_at']} "
                        f"({arrondir(abs(doublon['bottles']))} bouteille(s), "
                        f"retour #{doublon['id']}). "
                        f"Vérifie le numéro de fiche, ou annule d'abord ce retour."
                    ),
                )

        # Contrôle du solde AVANT écriture : une région ne peut pas rendre
        # plus de bouteilles qu'elle n'en doit, sinon l'entrepôt se retrouve
        # débiteur d'un solde négatif qui n'a aucun sens physique.
        # Quand un technicien est nommé, c'est SA dette qui borne le retour :
        # sinon un technicien pourrait solder les bouteilles d'un collègue.
        if technician:
            porteur, porteur_min = f"Le technicien {technician}", f"le technicien {technician}"
            solde_du = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN bl.bottles ELSE 0 END
                ), 0) AS balance
                FROM bottle_ledger bl
                LEFT JOIN documents d ON d.id = bl.document_id
                WHERE bl.product_id = ? AND bl.technician = ?
                """,
                (product_id, technician),
            ).fetchone()["balance"]
        else:
            porteur, porteur_min = f"La région {region_name}", f"la région {region_name}"
            solde_du = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN bl.bottles ELSE 0 END
                ), 0) AS balance
                FROM bottle_ledger bl
                LEFT JOIN documents d ON d.id = bl.document_id
                WHERE bl.product_id = ? AND bl.region = ?
                """,
                (product_id, region_name),
            ).fetchone()["balance"]
        if bottles > solde_du + 1e-9:
            if solde_du <= 1e-9:
                detail = (f"{porteur} ne doit aucune bouteille "
                          f"pour ce produit : retour impossible")
            else:
                detail = (f"Retour de {arrondir(bottles)} bouteille(s) refusé : "
                          f"{porteur_min} ne doit que "
                          f"{arrondir(solde_du)} bouteille(s) pour ce produit")
            raise HTTPException(status_code=422, detail=detail)

        # Quand un technicien est nommé, sa dette borne le retour — mais la
        # région portée par le retour doit AUSSI pouvoir l'absorber. Sans ce
        # second contrôle, un retour saisi avec le bon technicien et la
        # MAUVAISE région (mauvais choix dans la liste) était accepté : la
        # ligne partait au crédit d'une région qui n'a jamais rien reçu, dont
        # le solde passait sous zéro, pendant que la région réellement
        # débitrice restait affichée comme devant tout.
        if technician:
            solde_region = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN bl.bottles ELSE 0 END
                ), 0) AS balance
                FROM bottle_ledger bl
                LEFT JOIN documents d ON d.id = bl.document_id
                WHERE bl.product_id = ? AND bl.region = ?
                """,
                (product_id, region_name),
            ).fetchone()["balance"]
            if bottles > solde_region + 1e-9:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Retour de {arrondir(bottles)} bouteille(s) refusé : "
                        f"la région {region_name} ne doit que "
                        f"{arrondir(solde_region)} bouteille(s) pour ce produit. "
                        f"Vérifie la région du technicien {technician}."
                    ),
                )

        # Enregistrer le retour (négatif dans le ledger)
        created_by = user["display_name"] if user else body.created_by
        conn.execute(
            "INSERT INTO bottle_ledger (product_id, region, technician, bottles, note, "
            "reference, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (product_id, region_name, technician, -bottles, note, body.reference, created_by),
        )

        # Calculer le nouveau solde
        solde = conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN bl.bottles ELSE 0 END
            ), 0) AS balance
            FROM bottle_ledger bl
            LEFT JOIN documents d ON d.id = bl.document_id
            WHERE bl.product_id = ? AND bl.region = ?
            """,
            (product_id, region_name),
        ).fetchone()["balance"]

        # Solde personnel restant, quand le retour est attribué à quelqu'un.
        solde_technicien = None
        if technician:
            solde_technicien = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN bl.bottles ELSE 0 END
                ), 0) AS balance
                FROM bottle_ledger bl
                LEFT JOIN documents d ON d.id = bl.document_id
                WHERE bl.product_id = ? AND bl.technician = ?
                """,
                (product_id, technician),
            ).fetchone()["balance"]

    return {"product_id": product_id, "region": region_name, "balance": solde,
            "technician": technician, "technician_balance": solde_technicien,
            "reference": body.reference}


@app.get("/bottles/ledger")
def bottles_ledger(
    product_id: Optional[int] = None,
    region: Optional[str] = None,
    technician: Optional[str] = None,
    user=Depends(exiger_utilisateur),
):
    """Écritures de retour de bouteilles (bottles < 0)."""
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        conditions = ["p.sector = ?", "bl.bottles < 0"]
        params = [secteur]
        if product_id:
            conditions.append("bl.product_id = ?")
            params.append(product_id)
        if region:
            conditions.append("bl.region = ?")
            params.append(region)
        if technician:
            conditions.append("bl.technician = ?")
            params.append(technician)
        filtre = " AND ".join(conditions)

        # `cancelled` : la contre-écriture d'annulation est positive, donc
        # absente de cette liste (filtrée sur bottles < 0). Sans ce drapeau,
        # un retour annulé restait affiché comme un retour valide — l'écran
        # des bouteilles montrait une reprise qui ne compte plus dans aucun
        # solde, et le bouton « Annuler » n'y répondait que par un 409.
        rows = conn.execute(
            f"""
            SELECT bl.*, p.sku, p.name,
                   EXISTS (SELECT 1 FROM bottle_ledger a
                           WHERE a.cancels_entry_id = bl.id) AS cancelled
            FROM bottle_ledger bl
            JOIN products p ON p.id = bl.product_id
            WHERE {filtre}
            ORDER BY bl.created_at DESC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/bottles/return/{entry_id}/cancel")
def cancel_bottle_return(entry_id: int, admin=Depends(exiger_admin)):
    """Annule un retour de bouteilles."""
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, admin)
        entry = conn.execute(
            "SELECT bl.* FROM bottle_ledger bl "
            "JOIN products p ON p.id = bl.product_id "
            "WHERE bl.id = ? AND p.sector = ?",
            (entry_id, secteur),
        ).fetchone()
        if not entry:
            raise HTTPException(status_code=404, detail="Écriture introuvable")
        if entry["bottles"] >= 0:
            raise HTTPException(
                status_code=422, detail="Seuls les retours (bottles < 0) peuvent être annulés"
            )
        # Détection par clé étrangère, pas par comparaison de texte : une note
        # saisie par un opérateur ne peut plus casser ce contrôle, et l'index
        # UNIQUE partiel empêche une double annulation même simultanée.
        deja = conn.execute(
            "SELECT id FROM bottle_ledger WHERE cancels_entry_id = ?", (entry_id,)
        ).fetchone()
        if deja:
            raise HTTPException(status_code=409, detail="Retour déjà annulé")

        bottles_restored = abs(entry["bottles"])
        auteur = admin["username"] if admin else "système"
        created_by = admin["display_name"] if admin else "système"
        conn.execute(
            "INSERT INTO bottle_ledger (product_id, region, technician, bottles, note, "
            "reference, created_by, cancels_entry_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            # Le technicien est repris de l'écriture annulée : sans lui, la
            # dette reviendrait sur la région sans revenir sur la personne.
            # Le n° de fiche l'est aussi : les deux écritures renvoient au
            # même document papier.
            (entry["product_id"], entry["region"], entry["technician"], bottles_restored,
             f"annulation #{entry_id}", entry["reference"], created_by, entry_id),
        )
        _journaliser_audit(
            conn, "bottle_ledger", entry_id, "cancel",
            old={"product_id": entry["product_id"], "region": entry["region"],
                 "bottles": entry["bottles"]},
            new={"bottles_restored": bottles_restored},
            username=auteur,
            sector=secteur,
        )

        solde = conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN bl.bottles ELSE 0 END
            ), 0) AS balance
            FROM bottle_ledger bl
            LEFT JOIN documents d ON d.id = bl.document_id
            WHERE bl.product_id = ? AND bl.region = ?
            """,
            (entry["product_id"], entry["region"]),
        ).fetchone()["balance"]

    return {
        "cancelled_entry_id": entry_id,
        "bottles_restored": bottles_restored,
        "balance": solde,
    }


# ------------------------------------------------------ reverse logistics (RL)
#
# Équipement prêté (routeurs GPON FON, `products.rl_apply`) qui doit être
# physiquement retourné après livraison. Même mécanique que la consigne
# bouteille ci-dessus : dette créée à la livraison (voir `create_document`),
# retour = ligne négative, annulation = contre-écriture, jamais de
# suppression. Pas de conversion (1 unité livrée = 1 unité due), donc pas de
# `capacité` équivalente à `consigne_bouteille`.

@app.get("/rl/balance")
def rl_balance(
    product_id: Optional[int] = None,
    region: Optional[str] = None,
    technician: Optional[str] = None,
    par_technicien: bool = False,
    user=Depends(exiger_utilisateur),
):
    """Solde d'équipement RL dû (livré, pas encore retourné) par produit et région."""
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        conditions = ["p.sector = ?"]
        params = [secteur]
        if product_id:
            conditions.append("rl.product_id = ?")
            params.append(product_id)
        if region:
            conditions.append("rl.region = ?")
            params.append(region)
        if technician:
            conditions.append("rl.technician = ?")
            params.append(technician)
        if par_technicien:
            conditions.append("rl.technician IS NOT NULL")
        filtre = " AND ".join(conditions)
        colonne = "rl.technician" if par_technicien else "rl.region"
        colonnes = ("NULL AS region, rl.technician" if par_technicien
                    else "rl.region, NULL AS technician")

        rows = conn.execute(
            f"""
            SELECT rl.product_id, {colonnes}, p.sku, p.name,
                   SUM(CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN rl.quantity ELSE 0 END) AS balance
            FROM rl_ledger rl
            JOIN products p ON p.id = rl.product_id
            LEFT JOIN documents d ON d.id = rl.document_id
            WHERE {filtre}
            GROUP BY rl.product_id, {colonne}
            HAVING ABS(balance) > 1e-9
            ORDER BY p.name, {colonne}
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/rl/return")
def rl_return(body: RlReturnIn, user=Depends(exiger_ecriture)):
    """Enregistre le retour physique d'un équipement RL."""
    product_id = body.product_id
    region_name = body.region
    quantity = body.quantity
    note = body.note
    technician = body.technician

    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)
        produit = conn.execute(
            "SELECT rl_apply FROM products WHERE id = ? AND sector = ?",
            (product_id, secteur),
        ).fetchone()
        if not produit:
            raise HTTPException(status_code=404, detail="Produit introuvable")
        if not produit["rl_apply"]:
            raise HTTPException(
                status_code=422,
                detail="Ce produit n'est pas soumis à la reverse logistics",
            )

        if body.reference:
            doublon = conn.execute(
                """
                SELECT rl.id, rl.quantity, rl.created_at, rl.created_by
                FROM rl_ledger rl
                WHERE rl.product_id = ? AND rl.quantity < 0
                  AND rl.reference = ? COLLATE NOCASE
                  AND NOT EXISTS (
                      SELECT 1 FROM rl_ledger a WHERE a.cancels_entry_id = rl.id
                  )
                """,
                (product_id, body.reference),
            ).fetchone()
            if doublon:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"La référence {body.reference} a déjà été enregistrée pour ce "
                        f"produit le {doublon['created_at']} "
                        f"({arrondir(abs(doublon['quantity']))} unité(s), "
                        f"retour #{doublon['id']}). "
                        f"Vérifie la référence, ou annule d'abord ce retour."
                    ),
                )

        if technician:
            porteur, porteur_min = f"Le technicien {technician}", f"le technicien {technician}"
            solde_du = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN rl.quantity ELSE 0 END
                ), 0) AS balance
                FROM rl_ledger rl
                LEFT JOIN documents d ON d.id = rl.document_id
                WHERE rl.product_id = ? AND rl.technician = ?
                """,
                (product_id, technician),
            ).fetchone()["balance"]
        else:
            porteur, porteur_min = f"La région {region_name}", f"la région {region_name}"
            solde_du = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN rl.quantity ELSE 0 END
                ), 0) AS balance
                FROM rl_ledger rl
                LEFT JOIN documents d ON d.id = rl.document_id
                WHERE rl.product_id = ? AND rl.region = ?
                """,
                (product_id, region_name),
            ).fetchone()["balance"]
        if quantity > solde_du + 1e-9:
            if solde_du <= 1e-9:
                detail = (f"{porteur} ne doit aucune unité "
                          f"pour ce produit : retour impossible")
            else:
                detail = (f"Retour de {arrondir(quantity)} unité(s) refusé : "
                          f"{porteur_min} ne doit que "
                          f"{arrondir(solde_du)} unité(s) pour ce produit")
            raise HTTPException(status_code=422, detail=detail)

        if technician:
            solde_region = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN rl.quantity ELSE 0 END
                ), 0) AS balance
                FROM rl_ledger rl
                LEFT JOIN documents d ON d.id = rl.document_id
                WHERE rl.product_id = ? AND rl.region = ?
                """,
                (product_id, region_name),
            ).fetchone()["balance"]
            if quantity > solde_region + 1e-9:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Retour de {arrondir(quantity)} unité(s) refusé : "
                        f"la région {region_name} ne doit que "
                        f"{arrondir(solde_region)} unité(s) pour ce produit. "
                        f"Vérifie la région du technicien {technician}."
                    ),
                )

        created_by = user["display_name"] if user else body.created_by
        conn.execute(
            "INSERT INTO rl_ledger (product_id, region, technician, quantity, note, "
            "reference, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (product_id, region_name, technician, -quantity, note, body.reference, created_by),
        )

        solde = conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN rl.quantity ELSE 0 END
            ), 0) AS balance
            FROM rl_ledger rl
            LEFT JOIN documents d ON d.id = rl.document_id
            WHERE rl.product_id = ? AND rl.region = ?
            """,
            (product_id, region_name),
        ).fetchone()["balance"]

        solde_technicien = None
        if technician:
            solde_technicien = conn.execute(
                """
                SELECT COALESCE(SUM(
                    CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN rl.quantity ELSE 0 END
                ), 0) AS balance
                FROM rl_ledger rl
                LEFT JOIN documents d ON d.id = rl.document_id
                WHERE rl.product_id = ? AND rl.technician = ?
                """,
                (product_id, technician),
            ).fetchone()["balance"]

    return {"product_id": product_id, "region": region_name, "balance": solde,
            "technician": technician, "technician_balance": solde_technicien,
            "reference": body.reference}


@app.get("/rl/ledger")
def rl_ledger_list(
    product_id: Optional[int] = None,
    region: Optional[str] = None,
    technician: Optional[str] = None,
    user=Depends(exiger_utilisateur),
):
    """Écritures de retour RL (quantity < 0)."""
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        conditions = ["p.sector = ?", "rl.quantity < 0"]
        params = [secteur]
        if product_id:
            conditions.append("rl.product_id = ?")
            params.append(product_id)
        if region:
            conditions.append("rl.region = ?")
            params.append(region)
        if technician:
            conditions.append("rl.technician = ?")
            params.append(technician)
        filtre = " AND ".join(conditions)

        rows = conn.execute(
            f"""
            SELECT rl.*, p.sku, p.name,
                   EXISTS (SELECT 1 FROM rl_ledger a
                           WHERE a.cancels_entry_id = rl.id) AS cancelled
            FROM rl_ledger rl
            JOIN products p ON p.id = rl.product_id
            WHERE {filtre}
            ORDER BY rl.created_at DESC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/rl/return/{entry_id}/cancel")
def cancel_rl_return(entry_id: int, admin=Depends(exiger_admin)):
    """Annule un retour d'équipement RL."""
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, admin)
        entry = conn.execute(
            "SELECT rl.* FROM rl_ledger rl "
            "JOIN products p ON p.id = rl.product_id "
            "WHERE rl.id = ? AND p.sector = ?",
            (entry_id, secteur),
        ).fetchone()
        if not entry:
            raise HTTPException(status_code=404, detail="Écriture introuvable")
        if entry["quantity"] >= 0:
            raise HTTPException(
                status_code=422, detail="Seuls les retours (quantity < 0) peuvent être annulés"
            )
        deja = conn.execute(
            "SELECT id FROM rl_ledger WHERE cancels_entry_id = ?", (entry_id,)
        ).fetchone()
        if deja:
            raise HTTPException(status_code=409, detail="Retour déjà annulé")

        quantity_restored = abs(entry["quantity"])
        auteur = admin["username"] if admin else "système"
        created_by = admin["display_name"] if admin else "système"
        conn.execute(
            "INSERT INTO rl_ledger (product_id, region, technician, quantity, note, "
            "reference, created_by, cancels_entry_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (entry["product_id"], entry["region"], entry["technician"], quantity_restored,
             f"annulation #{entry_id}", entry["reference"], created_by, entry_id),
        )
        _journaliser_audit(
            conn, "rl_ledger", entry_id, "cancel",
            old={"product_id": entry["product_id"], "region": entry["region"],
                 "quantity": entry["quantity"]},
            new={"quantity_restored": quantity_restored},
            username=auteur,
            sector=secteur,
        )

        solde = conn.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL THEN rl.quantity ELSE 0 END
            ), 0) AS balance
            FROM rl_ledger rl
            LEFT JOIN documents d ON d.id = rl.document_id
            WHERE rl.product_id = ? AND rl.region = ?
            """,
            (entry["product_id"], entry["region"]),
        ).fetchone()["balance"]

    return {
        "cancelled_entry_id": entry_id,
        "quantity_restored": quantity_restored,
        "balance": solde,
    }


# ------------------------------------------- entrepôts régionaux (Consommables)

# Une région sans la moindre trace d'activité depuis ce délai est signalée.
#
# CHOIX ARBITRAIRE ET AJUSTABLE : 7 jours. Aucune règle métier ne l'impose,
# c'est un compromis observé — une région active saisit au moins un comptage
# ou une réception par semaine, tandis qu'un poste éteint, un opérateur
# absent ou une liaison coupée dépassent presque toujours la semaine. Trop
# court, l'écran serait orange en permanence et plus personne ne le
# regarderait ; trop long, la panne se découvre trop tard.
SEUIL_SILENCE_REGIONAL_JOURS = 7


@app.get("/regions/silence")
def regions_silence(admin=Depends(exiger_admin)):
    """Dernière activité connue de chaque région dotée d'un entrepôt régional.

    « Activité » = toute trace laissée par la région dans le système :
    un comptage d'inventaire régional, un bon la concernant (non annulé), ou
    un signalement de rupture. La plus récente des trois fait foi.

    Le silence n'est PAS une anomalie de stock : c'est une anomalie
    d'exploitation (poste éteint, opérateur absent, réseau coupé), qui ne se
    voyait jusqu'ici sur aucun écran — une région qui ne dit rien ressemble
    exactement à une région qui va bien.
    """
    aujourdhui = maintenant().date()
    lignes = []
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, admin)
        for region in regions_avec_entrepot_for(secteur):
            # Une seule requête par région, trois sous-requêtes : le nombre de
            # régions est fixe et petit (référentiel), et MAX() sur des
            # colonnes indexées reste immédiat.
            row = conn.execute(
                """
                SELECT MAX(quand) AS derniere FROM (
                    SELECT MAX(created_at) AS quand
                      FROM regional_inventory_reports WHERE region = ? AND sector = ?
                    UNION ALL
                    SELECT MAX(created_at) FROM documents
                     WHERE region = ? AND cancelled_at IS NULL AND sector = ?
                    UNION ALL
                    SELECT MAX(created_at) FROM regional_alert_signals
                     WHERE region = ? AND sector = ?
                )
                """,
                (region, secteur, region, secteur, region, secteur),
            ).fetchone()
            derniere = row["derniere"] if row else None
            jours = _jours_depuis(derniere, aujourdhui)
            lignes.append({
                "region": region,
                "derniere_activite": derniere,
                "jours_silence": jours,
                # Aucune activité DU TOUT (jours is None) compte comme un
                # silence : une région qui n'a jamais rien saisi est
                # exactement le cas qu'on cherche à voir.
                "silencieuse": jours is None or jours >= SEUIL_SILENCE_REGIONAL_JOURS,
            })
    return {
        "seuil_jours": SEUIL_SILENCE_REGIONAL_JOURS,
        "regions": lignes,
        "nombre_silencieuses": sum(1 for l in lignes if l["silencieuse"]),
    }


# Au-delà, le dernier comptage d'une région est jugé trop ancien pour qu'on
# se fie au stock qu'il déclare. Un mois : les comptages régionaux sont
# mensuels (voir `comptages_a_temps`), donc rien ne se déclenche tant que la
# région tient son rythme.
COMPTAGE_PERIME_JOURS = 35


@app.get("/regions/tableau-de-bord")
def tableau_de_bord_regions(admin=Depends(exiger_admin)):
    """État complet de chaque région à entrepôt, en un seul appel.

    Quatre informations qui vivaient jusqu'ici sur quatre écrans séparés :
    le stock déclaré au dernier comptage, les ruptures signalées et non
    résolues, la date de ce dernier comptage, et l'annotation saisonnière.
    Les croiser à la main obligeait l'administrateur à ouvrir chaque écran
    pour chaque région — treize fois quatre — avant de décider quoi
    réapprovisionner.

    « Stock déclaré » = somme des lignes du DERNIER comptage seulement, pas
    de tout l'historique : additionner deux comptages successifs du même
    produit doublerait un stock qui n'a jamais bougé.

    Une requête par information, agrégée par région — pas une requête par
    région : le nombre de régions est fixe, mais la boucle par région
    multipliait les allers-retours SQLite pour rien.
    """
    aujourdhui = maintenant().date()
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, admin)
        derniers = {
            r["region"]: r["dernier"]
            for r in conn.execute(
                "SELECT region, MAX(created_at) AS dernier "
                "FROM regional_inventory_reports WHERE sector = ? GROUP BY region",
                (secteur,),
            )
        }
        # Le total du dernier comptage : la jointure sur (region, created_at)
        # ne retient que les lignes saisies au même instant que le maximum,
        # c'est-à-dire le lot envoyé ensemble par l'écran d'inventaire.
        totaux = {
            r["region"]: {"total": r["total"], "articles": r["articles"]}
            for r in conn.execute(
                """
                SELECT r.region,
                       ROUND(SUM(r.quantity), 3) AS total,
                       COUNT(*) AS articles
                FROM regional_inventory_reports r
                JOIN (SELECT region, MAX(created_at) AS dernier
                        FROM regional_inventory_reports WHERE sector = ?
                        GROUP BY region) m
                  ON m.region = r.region AND m.dernier = r.created_at
                WHERE r.sector = ?
                GROUP BY r.region
                """,
                (secteur, secteur),
            )
        }
        alertes = {
            r["region"]: r["n"]
            for r in conn.execute(
                "SELECT region, COUNT(*) AS n FROM regional_alert_signals "
                "WHERE resolved_at IS NULL AND sector = ? GROUP BY region",
                (secteur,),
            )
        }
        notes = {
            r["region"]: r["note"]
            for r in conn.execute(
                "SELECT region, note FROM region_notes WHERE TRIM(note) <> '' AND sector = ?",
                (secteur,),
            )
        }
        # Transferts encore en route vers la région : un stock déclaré bas
        # n'appelle pas le même arbitrage selon qu'un camion est parti ou non.
        en_transit = {
            r["region"]: r["n"]
            for r in conn.execute(
                "SELECT region, COUNT(*) AS n FROM documents "
                "WHERE received_at IS NULL AND cancelled_at IS NULL AND sector = ? "
                "  AND (type = 'REGIONAL_TRANSFER' "
                "       OR (type = 'DELIVERY' "
                "           AND (technician IS NULL OR TRIM(technician) = ''))) "
                "GROUP BY region",
                (secteur,),
            )
        }

    lignes = []
    for region in regions_avec_entrepot_for(secteur):
        dernier = derniers.get(region)
        jours = _jours_depuis(dernier, aujourdhui)
        total = totaux.get(region) or {}
        lignes.append({
            "region": region,
            "stock_declare": total.get("total"),
            "articles_comptes": total.get("articles", 0),
            "alertes_ouvertes": alertes.get(region, 0),
            "dernier_comptage": dernier,
            "jours_depuis_comptage": jours,
            # None (jamais compté) compte comme périmé : une région qui n'a
            # jamais rien déclaré est le cas le plus préoccupant, pas le plus
            # rassurant.
            "comptage_perime": jours is None or jours >= COMPTAGE_PERIME_JOURS,
            "note": notes.get(region, ""),
            "transferts_en_transit": en_transit.get(region, 0),
        })

    return {
        "seuil_comptage_jours": COMPTAGE_PERIME_JOURS,
        "regions": lignes,
        "nombre_regions": len(lignes),
        "nombre_comptages_perimes": sum(1 for l in lignes if l["comptage_perime"]),
        "total_alertes_ouvertes": sum(l["alertes_ouvertes"] for l in lignes),
    }


def _region_du_compte(conn, user) -> Optional[str]:
    """Région à laquelle le compte connecté est rattaché, ou None.

    Lue en base et non dans la session : rattacher un compte à une autre région
    doit prendre effet sans attendre l'expiration de son jeton.
    """
    if not user:
        return None
    row = conn.execute("SELECT region FROM users WHERE id = ?",
                       (user["user_id"],)).fetchone()
    return row["region"] if row else None


def _secteur_du_compte(conn, user) -> str:
    """Secteur (Consumables, FON...) du compte connecté.

    Même principe que `_region_du_compte` : relu en base à chaque appel, pour
    qu'un changement de secteur prenne effet sans attendre l'expiration du
    jeton de session (8h). Un appel sans compte (premier lancement, avant
    toute création d'utilisateur) retombe sur Consumables, comme le reste de
    l'application aujourd'hui.
    """
    if not user:
        return SECTEUR_DEFAUT
    row = conn.execute("SELECT sector FROM users WHERE id = ?",
                       (user["user_id"],)).fetchone()
    return row["sector"] if row and row["sector"] else SECTEUR_DEFAUT


def _site_du_compte(conn, user) -> Optional[str]:
    """Site physique (secteur FUEL) du compte connecté, ou None.

    Contrairement à une première version de cette fonction, un admin n'est
    PLUS automatiquement exempté : Rijkaard (admin national, mais aussi
    responsable opérationnel de Canapé-Vert) doit lui aussi voir SA cuve au
    quotidien dans Stock/Réception/Expédition/Jaugeage, pas tout le secteur
    mélangé — exactement le même besoin qu'Orelus à WH Central. La vue
    globale reste disponible ailleurs, où elle a du sens : le Tableau de
    bord (agrégat direct, jamais filtré par site) et l'écran Produits
    (paramètre `all_sites`, réservé aux admins) pour gérer le catalogue de
    tout le monde. None = aucune restriction (compte sans site défini —
    Consumables/FON, ou un compte Fuel pas encore rattaché). Relu en base à
    chaque appel, comme `_secteur_du_compte`.
    """
    if not user:
        return None
    row = conn.execute("SELECT site FROM users WHERE id = ?",
                       (user["user_id"],)).fetchone()
    return (row["site"] or None) if row else None


def _produit_accessible(conn, product_id: int, user,
                        tous_sites: bool = False) -> Optional[dict]:
    """Produit que CE compte a le droit de manipuler, ou None.

    Un seul endroit pour la règle d'accès à UN produit désigné par son id :
    même secteur que l'appelant, et même site (ou produit sans site défini).
    Avant ce helper, seul le secteur était vérifié sur les endpoints qui
    prennent un id en paramètre — Rijkaard (Canapé-Vert) ne VOYAIT pas la cuve
    Diesel de WH Central dans les listes, mais pouvait l'ajuster, la scanner
    ou lire son historique en devinant son id.

    Renvoie None si le produit n'existe pas OU s'il est hors périmètre :
    l'appelant répond 404 dans les deux cas, jamais 403 — même convention que
    `POST /documents`, pour ne pas révéler l'existence d'un produit à qui n'a
    pas à le connaître.

    `tous_sites=True` lève la restriction de site pour un ADMIN uniquement
    (silencieusement ignoré sinon) : c'est le pendant de `all_sites` sur
    `GET /products`, réservé aux écrans de gestion du catalogue où
    l'administrateur doit pouvoir corriger la fiche d'un autre site
    (voir docs/SECTEUR_FUEL.md). Les écrans d'exploitation (scan, jaugeage/
    ajustement) restent scopés au site, admin compris.
    """
    secteur = _secteur_du_compte(conn, user)
    site = None if (tous_sites and user and user.get("role") == "admin") \
        else _site_du_compte(conn, user)
    sql = "SELECT * FROM products WHERE id = ? AND sector = ?"
    params: list = [product_id, secteur]
    if site:
        sql += " AND (site IS NULL OR site = ?)"
        params.append(site)
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _regions_autorisees(conn, user) -> Optional[list[str]]:
    """Régions auxquelles un compte LECTEUR est restreint, ou None.

    None (et non une liste vide) signifie « aucune restriction » : c'est
    l'état de tous les comptes existants, et le comportement historique du
    rôle lecteur, qui voit tout. Seul un lecteur explicitement rattaché à des
    régions est filtré — un admin ou un magasinier ne l'est jamais.

    Le rôle 'regional' n'est pas concerné : il a sa propre restriction, plus
    stricte, portée par `users.region` et appliquée par `_region_demandee`.

    Lu en base à chaque appel, comme `_region_du_compte` : modifier le
    périmètre d'un lecteur doit prendre effet sans attendre l'expiration de
    son jeton de session (8 h).
    """
    if not user or user.get("role") != "lecteur":
        return None
    rows = conn.execute(
        "SELECT region FROM user_regions WHERE user_id = ? ORDER BY region",
        (user["user_id"],),
    ).fetchall()
    regions = [r["region"] for r in rows]
    return regions or None


def _region_demandee(conn, user, region: Optional[str]) -> str:
    """Résout la région d'un écran régional, selon le rôle du demandeur.

    Un compte régional est TOUJOURS ramené à sa propre région : le paramètre
    qu'il enverrait est ignoré, jamais utilisé pour regarder ailleurs.

    Un lecteur restreint (voir `_regions_autorisees`) ne peut demander qu'une
    de ses régions ; les autres lui sont refusées en 403.
    """
    if user and user.get("role") == "regional":
        sienne = _region_du_compte(conn, user)
        if not sienne:
            raise HTTPException(
                status_code=403,
                detail="Ce compte régional n'est rattaché à aucune région. "
                       "Contactez l'administrateur.",
            )
        return sienne

    autorisees = _regions_autorisees(conn, user)
    if autorisees and not region:
        # Un lecteur restreint n'a pas à préciser sa région quand il n'en
        # surveille qu'une : sans ce repli, ses écrans régionaux s'ouvraient
        # sur « Précisez la région » alors qu'il n'a aucun choix à faire.
        region = autorisees[0]

    secteur = _secteur_du_compte(conn, user)
    try:
        region = normaliser_region(region, secteur)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not region:
        raise HTTPException(status_code=422,
                            detail="Précisez la région (paramètre `region`)")
    if not a_entrepot_regional(region, secteur):
        raise HTTPException(
            status_code=422,
            detail=f"{region} n'a pas d'entrepôt régional pour le secteur "
                   f"{secteur} (régions concernées : "
                   f"{', '.join(regions_avec_entrepot_for(secteur))})",
        )
    if autorisees is not None and region not in autorisees:
        raise HTTPException(
            status_code=403,
            detail=f"Ce compte n'est pas autorisé sur la région {region} "
                   f"(régions autorisées : {', '.join(autorisees)})",
        )
    return region


# Au-delà de ce nombre de jours, un transfert non confirmé n'est plus un
# transfert « en route » : c'est une marchandise sortie d'un entrepôt et
# entrée dans aucun autre. Trois jours couvrent largement le trajet le plus
# long du réseau ; au-delà, quelqu'un doit décrocher son téléphone.
SEUIL_TRANSFERT_EN_ATTENTE_JOURS = 3


def _jours_en_attente(created_at) -> Optional[float]:
    """Ancienneté d'un bon en jours (une décimale), ou None si illisible.

    Jamais d'exception : un horodatage exotique doit dégrader l'affichage,
    pas faire tomber en 500 l'écran de réception d'une région.
    """
    try:
        depuis = datetime.strptime(normaliser(created_at), FORMAT_DATETIME)
    except (ValueError, TypeError):
        return None
    return round(max(0.0, (maintenant() - depuis).total_seconds() / 86400.0), 1)


def _annoter_attente(documents: list[dict]) -> list[dict]:
    """Ajoute `jours_en_attente` et `en_retard` à chaque transfert en transit."""
    for doc in documents:
        jours = _jours_en_attente(doc.get("created_at"))
        doc["jours_en_attente"] = jours
        doc["en_retard"] = jours is not None and jours >= SEUIL_TRANSFERT_EN_ATTENTE_JOURS
    return documents


@app.get("/regional/transferts-en-transit")
def transferts_en_transit(region: Optional[str] = None,
                          user=Depends(exiger_utilisateur)):
    """Transferts en route vers cette région et pas encore confirmés reçus.

    Deux provenances, une seule file d'attente : le magasinier de la région
    confirme de la même façon un colis venu du central et un colis venu d'une
    région voisine. Les séparer en deux écrans aurait doublé le nombre
    d'endroits où regarder pour savoir « qu'est-ce qui m'arrive ? ».
    """
    with get_conn() as conn:
        region = _region_demandee(conn, user, region)
        secteur = _secteur_du_compte(conn, user)
        filtre = ("sector = ? AND region = ? AND cancelled_at IS NULL AND received_at IS NULL "
                  "AND (type = 'REGIONAL_TRANSFER' "
                  "     OR (type = 'DELIVERY' "
                  "         AND (technician IS NULL OR TRIM(technician) = '')))")
        documents = _charger_page(conn, filtre, (secteur, region,), MAX_PAGE, 0)
    for doc in documents:
        doc["provenance"] = doc.get("source_region") or "Entrepôt central"
    _annoter_attente(documents)
    return {"region": region, "transferts": documents,
            "seuil_relance_jours": SEUIL_TRANSFERT_EN_ATTENTE_JOURS}


@app.get("/regional/transferts-sortants")
def transferts_sortants(region: Optional[str] = None,
                        user=Depends(exiger_utilisateur)):
    """Transferts PARTIS de cette région et pas encore confirmés à l'arrivée.

    Le pendant de `/regional/transferts-en-transit` côté expéditeur : sans
    cette liste, la région qui envoie perd la marchandise de vue dès qu'elle
    quitte son entrepôt et n'a aucun moyen de savoir si elle est arrivée.
    """
    with get_conn() as conn:
        region = _region_demandee(conn, user, region)
        secteur = _secteur_du_compte(conn, user)
        filtre = ("sector = ? AND type = 'REGIONAL_TRANSFER' AND source_region = ? "
                  "AND cancelled_at IS NULL AND received_at IS NULL")
        documents = _charger_page(conn, filtre, (secteur, region,), MAX_PAGE, 0)
    _annoter_attente(documents)
    return {"region": region, "transferts": documents,
            "seuil_relance_jours": SEUIL_TRANSFERT_EN_ATTENTE_JOURS}


@app.post("/regional/transferts", response_model=DocumentOut)
def creer_transfert_regional(payload: RegionalTransferIn,
                             user=Depends(exiger_ecriture)):
    """Envoi de marchandise d'un entrepôt régional vers un autre.

    Même transit en deux temps que le transfert central -> région : la
    marchandise quitte l'entrepôt d'origine immédiatement, et n'est créditée
    à la région destinataire qu'à la confirmation de réception, pour la
    quantité RÉELLEMENT reçue.

    Le stock du central (`products.current_stock`) n'est pas touché : cette
    marchandise en est sortie le jour où elle a été expédiée en région.
    """
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)
        deja = _rejeu_idempotent(conn, payload.idempotency_key, secteur)
        if deja is not None:
            return deja

        # Un compte régional n'expédie QUE depuis sa propre région : le champ
        # qu'il enverrait est ignoré, comme pour l'inventaire régional.
        if user and user.get("role") == "regional":
            source = _region_du_compte(conn, user)
            if not source:
                raise HTTPException(
                    status_code=403,
                    detail="Ce compte régional n'est rattaché à aucune région. "
                           "Contactez l'administrateur.",
                )
        else:
            source = payload.region_source
            if not source:
                raise HTTPException(
                    status_code=400,
                    detail="Précisez la région d'origine du transfert "
                           "(`region_source`).",
                )

        if not a_entrepot_regional(source, secteur):
            raise HTTPException(
                status_code=400,
                detail=f"{source} n'a pas d'entrepôt régional pour le secteur "
                       f"{secteur} : elle est servie directement par l'entrepôt "
                       f"central et ne peut pas expédier vers une autre région. "
                       f"Régions concernées : "
                       f"{', '.join(regions_avec_entrepot_for(secteur))}.",
            )
        destination = payload.region_destination
        if not a_entrepot_regional(destination, secteur):
            raise HTTPException(
                status_code=400,
                detail=f"{destination} n'a pas d'entrepôt régional pour le "
                       f"secteur {secteur}. Régions concernées : "
                       f"{', '.join(regions_avec_entrepot_for(secteur))}.",
            )
        if source == destination:
            raise HTTPException(
                status_code=400,
                detail="La région d'origine et la région destinataire sont "
                       "identiques : ce transfert n'aurait aucun effet.",
            )

        quantites = defaultdict(float)
        for ligne in payload.lines:
            quantites[ligne.product_id] += ligne.quantity
        for product_id, quantite in quantites.items():
            if quantite > QUANTITE_MAX_PAR_LIGNE:
                raise HTTPException(
                    status_code=422,
                    detail=f"Quantité {quantite:g} dépasse le plafond autorisé "
                           f"({QUANTITE_MAX_PAR_LIGNE:g}) pour le produit "
                           f"{product_id}.",
                )

        marqueurs = ",".join("?" for _ in quantites)
        produits = {
            row["id"]: dict(row)
            for row in conn.execute(
                f"SELECT id, name, sku, unit, archived FROM products "
                f"WHERE sector = ? AND id IN ({marqueurs})",
                [secteur, *quantites],
            )
        }

        # Le stock RÉELLEMENT détenu par la région d'origine, calculé depuis
        # le grand livre — donc uniquement ce qu'elle a confirmé recevoir.
        # Sans ce plafond, une région pourrait expédier une marchandise
        # qu'elle n'a jamais reçue et son entrepôt partirait en négatif.
        detenu = {a["product_id"]: a["quantite"]
                  for a in stock_entrepot_regional(conn, source, sector=secteur)}

        for product_id, quantite in quantites.items():
            produit = produits.get(product_id)
            if produit is None:
                raise HTTPException(status_code=404,
                                    detail=f"Produit {product_id} introuvable")
            if produit["archived"]:
                raise HTTPException(
                    status_code=422,
                    detail=f"Le produit {produit['sku']} est archivé et ne peut "
                           f"plus figurer sur un transfert.",
                )
            dispo = detenu.get(product_id, 0.0)
            if quantite > dispo + 1e-9:
                raise HTTPException(
                    status_code=400,
                    detail=f"Stock insuffisant pour « {produit['name']} » dans "
                           f"l'entrepôt de {source} : {arrondir(dispo):g} "
                           f"disponible(s), {quantite:g} demandé(s). Seule la "
                           f"marchandise confirmée reçue peut être réexpédiée.",
                )

        recu_le = maintenant_texte()
        saisisseur = user["display_name"] if user else "Région"
        cur = conn.execute(
            "INSERT INTO documents (type, region, source_region, reference, note, "
            "carrier, idempotency_key, created_by, station, server_received_at, "
            "created_at, sector) VALUES ('REGIONAL_TRANSFER', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (destination, source, payload.reference, payload.note, payload.carrier,
             payload.idempotency_key, saisisseur, "", recu_le,
             payload.created_at or recu_le, secteur),
        )
        document_id = cur.lastrowid

        for product_id, quantite in quantites.items():
            conn.execute(
                "INSERT INTO document_lines (document_id, product_id, quantity) "
                "VALUES (?, ?, ?)",
                (document_id, product_id, quantite),
            )

        enregistrer_mouvements(
            conn,
            {"id": document_id, "type": "REGIONAL_TRANSFER",
             "region": destination, "source_region": source},
            [{"product_id": pid, "quantity": q} for pid, q in quantites.items()],
        )

        _journaliser_audit(conn, "document", document_id, "transfert_regional",
                           new={"source_region": source, "region": destination,
                                "lignes": len(quantites)},
                           username=user["username"] if user else saisisseur,
                           sector=secteur)
        log.info("transfert régional #%d : %s -> %s (%d ligne(s)) par %s",
                 document_id, source, destination, len(quantites), saisisseur)
        result = _fetch_document(conn, document_id)

    return result


@app.post("/documents/{document_id}/confirmer-reception", response_model=DocumentOut)
def confirmer_reception(document_id: int, payload: ReceptionConfirmIn,
                        user=Depends(exiger_ecriture)):
    """La région confirme ce qu'elle a RÉELLEMENT reçu.

    Second temps du transfert : le stock est crédité à l'entrepôt régional pour
    la quantité confirmée, jamais pour celle expédiée. Un écart entre les deux
    est une perte en transit — il est signalé, jamais bloquant : refuser la
    confirmation laisserait la région sans stock utilisable.
    """
    # Seule la région concernée confirme sa propre réception — un admin ne le
    # fait plus à sa place, même par téléphone : la trace `received_by`
    # porterait alors son nom et non celui de qui a réellement compté, et un
    # admin pourrait créditer un stock régional sans qu'aucun magasinier
    # n'ait rien confirmé sur place.
    if user and user.get("role") != "regional":
        raise HTTPException(
            status_code=403,
            detail="Seul le compte régional concerné peut confirmer une "
                   "réception. Demande à la région de confirmer elle-même.",
        )

    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)
        doc = conn.execute("SELECT * FROM documents WHERE id = ? AND sector = ?",
                           (document_id, secteur)).fetchone()
        if doc is None:
            raise HTTPException(status_code=404, detail=f"Bon {document_id} introuvable")
        if not attend_confirmation_reception(dict(doc)):
            raise HTTPException(
                status_code=422,
                detail="Ce bon n'est pas un transfert vers un entrepôt régional",
            )
        if doc["cancelled_at"]:
            raise HTTPException(status_code=409, detail="Ce bon a été annulé")
        if doc["received_at"]:
            raise HTTPException(
                status_code=409,
                detail=f"Réception déjà confirmée le {doc['received_at']} "
                       f"par {doc['received_by'] or 'inconnu'}",
            )

        # Un compte régional ne confirme que POUR SA région : sans ce contrôle,
        # une région pourrait clore les transferts d'une autre.
        if user and user.get("role") == "regional":
            sienne = _region_du_compte(conn, user)
            if sienne != doc["region"]:
                raise HTTPException(status_code=403,
                                    detail="Ce transfert ne concerne pas votre région")

        lignes = conn.execute(
            "SELECT dl.id, dl.product_id, dl.quantity, p.name, p.sku, p.unit "
            "FROM document_lines dl JOIN products p ON p.id = dl.product_id "
            "WHERE dl.document_id = ?",
            (document_id,),
        ).fetchall()
        attendus = {l["product_id"] for l in lignes}

        expediees = {l["product_id"]: l["quantity"] for l in lignes}
        noms = {l["product_id"]: l["name"] for l in lignes}

        recues = {}
        for ligne in payload.lines:
            if ligne.product_id not in attendus:
                raise HTTPException(
                    status_code=422,
                    detail=f"Le produit {ligne.product_id} ne figure pas sur ce bon",
                )
            # Recevoir PLUS que ce qui est parti est physiquement impossible :
            # le central n'a été débité que de la quantité expédiée. Créditer
            # davantage à la région fabriquerait du stock à partir de rien —
            # une faute de frappe (100 au lieu de 10) suffisait.
            if ligne.received_quantity > expediees[ligne.product_id] + 1e-9:
                raise HTTPException(
                    status_code=422,
                    detail=f"« {noms[ligne.product_id]} » : impossible de recevoir "
                           f"{ligne.received_quantity:g} alors que "
                           f"{expediees[ligne.product_id]:g} seulement ont été expédiés. "
                           f"Saisissez la quantité réellement reçue "
                           f"({expediees[ligne.product_id]:g} au maximum).",
                )
            # Deux lignes pour le même produit se contredisent : la dernière
            # écrasait silencieusement la première. Même refus que pour
            # l'inventaire régional, plutôt qu'un arbitrage invisible.
            if ligne.product_id in recues:
                raise HTTPException(
                    status_code=422,
                    detail=f"« {noms[ligne.product_id]} » figure deux fois dans la "
                           f"confirmation : indiquez une seule quantité reçue.",
                )
            recues[ligne.product_id] = ligne.received_quantity

        emplacement = id_entrepot_regional(conn, doc["region"])
        recu_le = maintenant_texte()
        auteur = user["display_name"] if user else "Région"

        ecarts = []
        for ligne in lignes:
            # Ligne non saisie = reçue conforme. La région n'a pas à retaper
            # des quantités identiques ligne par ligne.
            quantite_recue = arrondir(recues.get(ligne["product_id"], ligne["quantity"]))
            conn.execute("UPDATE document_lines SET received_quantity = ? WHERE id = ?",
                         (quantite_recue, ligne["id"]))
            if quantite_recue > 0:
                conn.execute(
                    "INSERT INTO stock_movements (document_id, product_id, "
                    "from_location_id, to_location_id, quantity) "
                    "VALUES (?, ?, NULL, ?, ?)",
                    (document_id, ligne["product_id"], emplacement, quantite_recue),
                )
            ecart = arrondir(quantite_recue - ligne["quantity"])
            if abs(ecart) > 1e-9:
                ecarts.append({
                    "product_id": ligne["product_id"], "sku": ligne["sku"],
                    "name": ligne["name"], "unit": ligne["unit"],
                    "expedie": ligne["quantity"], "recu": quantite_recue,
                    "ecart": ecart,
                })

        conn.execute("UPDATE documents SET received_at = ?, received_by = ? WHERE id = ?",
                     (recu_le, auteur, document_id))
        if payload.note:
            conn.execute(
                "UPDATE documents SET note = TRIM(COALESCE(note, '') || ?) WHERE id = ?",
                (f"\n[Réception {doc['region']}] {payload.note.strip()}", document_id),
            )

        _journaliser_audit(conn, "document", document_id, "confirmer_reception",
                           new={"received_at": recu_le, "received_by": auteur,
                                "region": doc["region"], "ecarts": ecarts},
                           username=user["username"] if user else auteur,
                           sector=secteur)

        log.info("bon #%d reçu par %s (%s) : %d écart(s)",
                 document_id, doc["region"], auteur, len(ecarts))
        result = _fetch_document(conn, document_id)
        result["ecarts_reception"] = ecarts

    _invalider_cache_dashboard()
    return result


def _completer_avec_catalogue(conn, lignes: list[dict],
                              sector: str = SECTEUR_DEFAUT) -> list[dict]:
    """Ajoute à quantité 0 les articles du catalogue absents du stock régional.

    Même patron que `/regional/catalogue` : un article que la région ne
    détient pas doit rester VISIBLE avec un zéro en face, plutôt que de
    disparaître de la liste. Absent, il laisse le magasinier se demander si
    l'article n'existe pas au catalogue ou s'il n'en a simplement plus ;
    affiché à zéro, la réponse est immédiate.

    Les archivés ne sont pas ajoutés : ils ne doivent plus figurer sur un
    nouveau bon. Ceux qui restent détenus par la région sont déjà dans
    `lignes` et y restent (`stock_entrepot_regional` les conserve).
    """
    connus = {l["product_id"] for l in lignes}
    manquants = [
        {"product_id": r["id"], "sku": r["sku"], "name": r["name"],
         "category": r["category"], "unit": r["unit"],
         "unit_type": r["unit_type"], "bidon_capacity": r["bidon_capacity"],
         "min_stock": r["min_stock"], "quantite": 0.0}
        for r in conn.execute(
            "SELECT id, sku, name, category, unit, unit_type, bidon_capacity, "
            "min_stock FROM products WHERE sector = ? AND COALESCE(archived, 0) = 0",
            (sector,))
        if r["id"] not in connus
    ]
    return sorted(lignes + manquants, key=lambda l: (l["name"] or "").lower())


@app.get("/regional/stock")
def stock_regional(region: Optional[str] = None,
                   inclure_catalogue: bool = False,
                   user=Depends(exiger_utilisateur)):
    """Stock détenu par l'entrepôt régional, calculé depuis les mouvements.

    `inclure_catalogue=true` complète la liste avec les articles actifs que
    la région ne détient pas, à quantité 0 (voir `_completer_avec_catalogue`).
    Éteint par défaut : les écrans qui montrent « ce que la région a » ne
    doivent pas se remplir de zéros.
    """
    with get_conn() as conn:
        region = _region_demandee(conn, user, region)
        secteur = _secteur_du_compte(conn, user)
        lignes = stock_entrepot_regional(conn, region, sector=secteur)
        if inclure_catalogue:
            lignes = _completer_avec_catalogue(conn, lignes, sector=secteur)
    for l in lignes:
        l["sous_seuil"] = bool(l["min_stock"]) and l["quantite"] <= l["min_stock"]
    return {"region": region, "articles": lignes}


@app.get("/regional/catalogue")
def catalogue_regional(user=Depends(exiger_utilisateur)):
    """Catalogue produit minimal pour choisir quoi signaler.

    Un compte régional n'a pas accès à `/products` (coût unitaire, stock
    central) : ce n'est pas son affaire. Il doit pourtant pouvoir signaler
    un produit qu'il n'a encore jamais reçu, donc absent de son propre
    stock — d'où ce sous-ensemble dédié plutôt qu'une ouverture de
    `/products` au rôle régional.
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        rows = conn.execute(
            "SELECT id, sku, name, unit FROM products "
            "WHERE sector = ? AND COALESCE(archived, 0) = 0 ORDER BY name",
            (secteur,),
        ).fetchall()
    return {"produits": [dict(r) for r in rows]}


@app.get("/regional/alertes/toutes")
def toutes_alertes_regionales(user=Depends(exiger_admin)):
    """Total des ruptures signalées, toutes régions confondues.

    Sert le badge numérique sur « Entrepôts régionaux » dans la nav admin :
    l'admin doit voir d'un coup d'œil qu'il y a des signalements en attente
    sans avoir à ouvrir chaque région une à une.
    """
    with get_conn() as conn:
        secteur = _secteur_du_compte(conn, user)
        rows = conn.execute(
            """
            SELECT s.id AS signal_id, s.region, s.product_id, s.note,
                   s.created_by, s.created_at, p.sku, p.name, p.unit
            FROM regional_alert_signals s
            JOIN products p ON p.id = s.product_id
            WHERE p.sector = ? AND s.resolved_at IS NULL
            ORDER BY s.created_at DESC, s.id DESC
            """,
            (secteur,),
        ).fetchall()
    alertes = [dict(r) for r in rows]
    return {"alertes": alertes, "nombre": len(alertes)}


@app.get("/regional/alertes")
def alertes_regionales(region: Optional[str] = None, historique: bool = False,
                       user=Depends(exiger_utilisateur)):
    """Signalements de rupture pour cette région.

    Par défaut, seulement les OUVERTS : volontairement PAS de calcul
    automatique par seuil — le stock d'un entrepôt régional ne baisse pas
    quand les techniciens se servent (rien n'est saisi), il ne bouge qu'à la
    réception ou au retour vers le central. Comparer ce stock à `min_stock`
    ne déclenchait donc jamais, ou déclenchait à tort. C'est la région qui
    signale, à la main, quand elle manque.

    `historique=true` : inclut aussi les signalements déjà résolus (pour
    l'écran "Mon historique" d'une région) — sinon un signalement disparaît
    sans laisser de trace dès que le central le résout.
    """
    filtre_resolu = "" if historique else "AND s.resolved_at IS NULL"
    with get_conn() as conn:
        region = _region_demandee(conn, user, region)
        secteur = _secteur_du_compte(conn, user)
        rows = conn.execute(
            f"""
            SELECT s.id AS signal_id, s.product_id, s.note, s.created_by,
                   s.created_at, s.resolved_at, s.resolved_by,
                   p.sku, p.name, p.unit
            FROM regional_alert_signals s
            JOIN products p ON p.id = s.product_id
            WHERE p.sector = ? AND s.region = ? {filtre_resolu}
            ORDER BY s.created_at DESC, s.id DESC
            """,
            (secteur, region,),
        ).fetchall()
    alertes = [dict(r) for r in rows]
    return {"region": region, "alertes": alertes, "nombre": len(alertes)}


@app.post("/regional/alertes/signaler")
def signaler_alerte_regionale(payload: RegionalAlertSignalIn,
                              user=Depends(exiger_ecriture)):
    """La région signale un produit bas ou en rupture. Un geste, pas un calcul."""
    with get_conn(write=True) as conn:
        region = _region_demandee(conn, user, payload.region)
        secteur = _secteur_du_compte(conn, user)
        produit = conn.execute(
            "SELECT id, sku, name, unit FROM products WHERE id = ? AND sector = ?",
            (payload.product_id, secteur)).fetchone()
        if produit is None:
            raise HTTPException(status_code=404,
                                detail=f"Produit {payload.product_id} introuvable")

        # Un doublon n'apporte aucune information : le central sait déjà. On
        # refuse explicitement plutôt que d'empiler des lignes identiques que
        # quelqu'un devrait résoudre une à une.
        deja = conn.execute(
            "SELECT id, created_at FROM regional_alert_signals "
            "WHERE region = ? AND sector = ? AND product_id = ? AND resolved_at IS NULL",
            (region, secteur, payload.product_id)).fetchone()
        if deja is not None:
            raise HTTPException(
                status_code=409,
                detail=f"« {produit['name']} » est déjà signalé pour {region} "
                       f"depuis le {(deja['created_at'] or '')[:16]}. "
                       f"L'entrepôt central le verra ; inutile de le signaler "
                       f"une seconde fois.",
            )

        auteur = user["display_name"] if user else "Région"
        cree_le = maintenant_texte()
        cur = conn.execute(
            "INSERT INTO regional_alert_signals "
            "(region, sector, product_id, note, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (region, secteur, payload.product_id, (payload.note or "").strip() or None,
             auteur, cree_le),
        )
        signal_id = cur.lastrowid

        _journaliser_audit(conn, "alerte_regionale", signal_id, "signaler",
                           new={"region": region, "product_id": payload.product_id,
                                "sku": produit["sku"], "note": payload.note},
                           username=user["username"] if user else auteur,
                           sector=secteur)
        log.info("rupture signalée : %s / %s par %s", region, produit["sku"], auteur)

    result = {"region": region, "signal_id": signal_id,
              "product_id": payload.product_id, "sku": produit["sku"],
              "name": produit["name"], "unit": produit["unit"],
              "note": (payload.note or "").strip() or None,
              "created_by": auteur, "created_at": cree_le,
              "message": f"« {produit['name']} » signalé à l'entrepôt central."}
    # Le central ne doit pas attendre d'ouvrir l'écran Alertes pour le savoir :
    # même toast que les ruptures de seuil au central (alert_triggered), pour
    # un signal cette fois manuel et régional.
    planifier(broadcast_regional_alert_signaled(result, role_filter="admin",
                                                sector=secteur))
    _notifier_email_alerte_regionale(result)
    return result


@app.post("/regional/alertes/{signal_id}/resoudre")
def resoudre_alerte_regionale(signal_id: int, user=Depends(exiger_admin)):
    """Le central confirme avoir traité le signalement (réapprovisionnement).

    Réservé à l'administrateur : c'est celui qui expédie qui sait si le besoin
    est couvert. Laisser la région clore son propre signalement ferait
    disparaître la demande sans que rien n'ait bougé.
    """
    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)
        row = conn.execute(
            "SELECT s.*, p.name FROM regional_alert_signals s "
            "JOIN products p ON p.id = s.product_id "
            "WHERE s.id = ? AND p.sector = ?",
            (signal_id, secteur)).fetchone()
        if row is None:
            raise HTTPException(status_code=404,
                                detail=f"Signalement {signal_id} introuvable")
        if row["resolved_at"]:
            raise HTTPException(
                status_code=409,
                detail=f"Ce signalement a déjà été marqué résolu le "
                       f"{row['resolved_at'][:16]} par "
                       f"{row['resolved_by'] or 'inconnu'}.",
            )

        auteur = user["display_name"] if user else "Central"
        resolu_le = maintenant_texte()
        conn.execute(
            "UPDATE regional_alert_signals SET resolved_at = ?, resolved_by = ? "
            "WHERE id = ?", (resolu_le, auteur, signal_id))

        _journaliser_audit(conn, "alerte_regionale", signal_id, "resoudre",
                           new={"resolved_at": resolu_le, "resolved_by": auteur,
                                "region": row["region"]},
                           username=user["username"] if user else auteur,
                           sector=secteur)
        log.info("signalement #%d résolu par %s", signal_id, auteur)

    return {"signal_id": signal_id, "region": row["region"],
            "resolved_at": resolu_le, "resolved_by": auteur,
            "message": f"Signalement « {row['name']} » marqué résolu."}


def _rejeu_idempotent_inventaire(conn, cle: Optional[str], secteur: str) -> Optional[dict]:
    """Renvoie le rapport déjà enregistré sous cette clé, s'il existe.

    MULTI-01. Même patron que `_rejeu_idempotent` (documents) : la
    vérification est faite DANS la transaction en écriture (BEGIN IMMEDIATE),
    donc deux tentatives simultanées ne peuvent pas passer toutes les deux.
    Distinct de `_rejeu_idempotent` (pas de simple réutilisation) parce qu'un
    comptage porte PLUSIEURS lignes sous la même clé — une par produit — et
    n'a pas d'id de document unique à relire ; la reconstruction porte donc
    sur l'ensemble des lignes de cette clé, pas sur une seule ligne.
    """
    if not cle:
        return None
    lignes = conn.execute(
        "SELECT region, created_at, created_by FROM regional_inventory_reports "
        "WHERE idempotency_key = ? AND sector = ?",
        (cle, secteur),
    ).fetchall()
    if not lignes:
        return None
    premiere = lignes[0]
    return {
        "region": premiere["region"], "lignes": len(lignes),
        "created_at": premiere["created_at"], "created_by": premiere["created_by"],
        "message": "Comptage enregistré. Il est informatif : aucun stock "
                   "n'a été modifié.",
    }


@app.post("/regional/inventaire")
def creer_inventaire_regional(payload: RegionalInventoryIn, response: Response,
                              user=Depends(exiger_utilisateur)):
    """Enregistre un comptage régional. N'ajuste AUCUN stock, par conception.

    L'écart éventuel avec le stock théorique n'est ni calculé ni corrigé ici :
    c'est l'administrateur central qui décide quoi en faire.
    """
    # Seule la région compte son propre stock physique — même raison que pour
    # la confirmation de réception : `created_by` porterait le nom du central
    # alors que personne n'a rien compté sur place, et ce comptage inventé
    # apparaîtrait dans l'historique de la région. L'écran admin est d'ailleurs
    # en lecture seule (client/views/inventaire_regional.py, mode_admin).
    if user and user.get("role") != "regional":
        raise HTTPException(
            status_code=403,
            detail="Seul le compte régional concerné peut saisir son "
                   "inventaire. Demande à la région de le saisir elle-même.",
        )
    if not payload.lines:
        raise HTTPException(status_code=400,
                            detail="L'inventaire doit contenir au moins une ligne")

    with get_conn(write=True) as conn:
        secteur = _secteur_du_compte(conn, user)
        deja = _rejeu_idempotent_inventaire(conn, payload.idempotency_key, secteur)
        if deja is not None:
            response.headers["X-Idempotent-Replay"] = "true"
            return deja

        region = _region_demandee(conn, user, payload.region)
        auteur = user["display_name"] if user else "Région"

        vus = set()
        for ligne in payload.lines:
            if ligne.product_id in vus:
                raise HTTPException(status_code=422,
                                    detail=f"Le produit {ligne.product_id} figure deux fois")
            vus.add(ligne.product_id)
            if conn.execute("SELECT 1 FROM products WHERE id = ? AND sector = ?",
                            (ligne.product_id, secteur)).fetchone() is None:
                raise HTTPException(status_code=404,
                                    detail=f"Produit {ligne.product_id} introuvable")

        # `created_at` fourni par le client = date du comptage PHYSIQUE, posée
        # avant une éventuelle mise en file d'attente hors ligne (voir modèle) ;
        # sans lui (appel direct, ancien client), on retombe sur maintenant.
        cree_le = payload.created_at or maintenant_texte()
        for ligne in payload.lines:
            conn.execute(
                "INSERT INTO regional_inventory_reports "
                "(region, sector, product_id, quantity, note, created_by, created_at, "
                "idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (region, secteur, ligne.product_id, ligne.quantity, payload.note, auteur,
                 cree_le, payload.idempotency_key),
            )

        log.info("inventaire régional %s par %s : %d ligne(s)",
                 region, auteur, len(payload.lines))

    return {"region": region, "lignes": len(payload.lines),
            "created_at": cree_le, "created_by": auteur,
            "message": "Comptage enregistré. Il est informatif : aucun stock "
                       "n'a été modifié."}


@app.get("/regional/documents", response_model=list[DocumentOut])
def documents_regionaux(region: Optional[str] = None,
                        type: Optional[str] = Query(None),
                        limit: int = Query(200, ge=1, le=MAX_PAGE),
                        offset: int = Query(0, ge=0),
                        user=Depends(exiger_utilisateur)):
    """Bons touchant UNE région — reçus ET envoyés — pour l'écran "Mon
    historique" d'un compte régional.

    `GET /documents` fait déjà tout ce calcul (filtre `region_expediteur_ou_
    destinataire`), mais reste hors de portée d'un compte régional : le
    middleware `restreindre_comptes_regionaux` ne laisse passer que les
    chemins sous `/regional/` — /documents montrerait sinon le catalogue et
    l'historique des AUTRES régions, ce que ce compte ne doit jamais voir.
    Cette route est ce même calcul, mais bornée à la région de l'appelant par
    `_region_demandee` (comme tous les autres écrans /regional/*), jamais à
    une région arbitraire.
    """
    with get_conn() as conn:
        region = _region_demandee(conn, user, region)
        secteur = _secteur_du_compte(conn, user)
        filtre, params = _filtre_documents(
            type, region, None, None, None, True, None,
            regions_autorisees=None, region_expediteur_ou_destinataire=True,
            sector=secteur)
        documents = _charger_page(conn, filtre, params, limit, offset)
    return documents


def _plage_inventaire_regional(date_from: Optional[str],
                               date_to: Optional[str]) -> tuple[str, list]:
    """Filtre SQL optionnel sur la date d'un comptage régional.

    Les deux bornes sont facultatives et INCLUSIVES. `date(r.created_at)`
    plutôt qu'une comparaison de chaînes, pour la même raison que dans
    `/reports/comptable/export` : `created_at` porte l'heure, et un simple
    `<= '2026-09-01'` exclurait toute la journée du 1er septembre.

    Renvoie un fragment à concaténer après un `WHERE` déjà ouvert, et ses
    paramètres — utilisé à l'identique par la liste et par son export, dont
    le contenu doit rester celui qu'on voit à l'écran.
    """
    debut = _borne_date(date_from, None, "date_from")
    fin = _borne_date(date_to, None, "date_to")
    if debut and fin and debut > fin:
        raise HTTPException(
            status_code=400,
            detail="La date de début est postérieure à la date de fin.")

    fragment, params = "", []
    if debut:
        fragment += " AND date(r.created_at) >= ?"
        params.append(debut.strftime(FORMAT_DATE))
    if fin:
        fragment += " AND date(r.created_at) <= ?"
        params.append(fin.strftime(FORMAT_DATE))
    return fragment, params


@app.get("/regional/inventaire")
def lister_inventaires_regionaux(region: Optional[str] = None,
                                 limit: int = Query(200, ge=1, le=1000),
                                 date_from: Optional[str] = Query(
                                     None, description="AAAA-MM-JJ inclus"),
                                 date_to: Optional[str] = Query(
                                     None, description="AAAA-MM-JJ inclus"),
                                 user=Depends(exiger_utilisateur)):
    """Historique des comptages régionaux, pour l'entrepôt central.

    `date_from` / `date_to` bornent la période (inclusives, facultatives) :
    sans elles l'admin devait faire défiler des mois de comptages pour
    retrouver ceux d'une période précise.
    """
    fragment, bornes = _plage_inventaire_regional(date_from, date_to)
    with get_conn() as conn:
        region = _region_demandee(conn, user, region)
        secteur = _secteur_du_compte(conn, user)
        rows = conn.execute(
            f"""
            SELECT r.id, r.region, r.product_id, r.quantity, r.note,
                   r.created_by, r.created_at, p.sku, p.name, p.unit
            FROM regional_inventory_reports r
            JOIN products p ON p.id = r.product_id
            WHERE p.sector = ? AND r.region = ?{fragment}
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT ?
            """,
            (secteur, region, *bornes, limit),
        ).fetchall()
    return {"region": region, "rapports": [dict(r) for r in rows]}


@app.get("/regional/inventaire/export")
def exporter_inventaires_regionaux(region: Optional[str] = None,
                                   date_from: Optional[str] = Query(
                                       None, description="AAAA-MM-JJ inclus"),
                                   date_to: Optional[str] = Query(
                                       None, description="AAAA-MM-JJ inclus"),
                                   user=Depends(exiger_utilisateur)):
    """Excel de l'historique des comptages — même filtrage que la liste."""
    fragment, bornes = _plage_inventaire_regional(date_from, date_to)
    with get_conn() as conn:
        region = _region_demandee(conn, user, region)
        secteur = _secteur_du_compte(conn, user)
        rows = conn.execute(
            f"""
            SELECT r.region, r.sku, r.name, r.unit, r.quantity, r.created_by, r.created_at
            FROM (
                SELECT r.id, r.region, p.sku, p.name, p.unit, r.quantity,
                       r.created_by, r.created_at
                FROM regional_inventory_reports r
                JOIN products p ON p.id = r.product_id
                WHERE p.sector = ? AND r.region = ?{fragment}
            ) r
            ORDER BY r.created_at DESC, r.id DESC
            """,
            (secteur, region, *bornes),
        ).fetchall()
    content = build_regional_inventaire_excel([dict(r) for r in rows])
    # Un nom de fichier ASCII : Content-Disposition avec des accents non
    # encodés (ex. « Cap-Haïtien ») casserait l'en-tête HTTP chez certains
    # clients.
    region_ascii = unicodedata.normalize("NFKD", region).encode(
        "ascii", "ignore").decode().replace(" ", "_").replace("-", "_") or "region"
    filename = export_filename(f"inventaire_{region_ascii}")
    _journaliser_export("inventaire_regional", user, region=region,
                        date_from=date_from, date_to=date_to,
                        nb_lignes=len(rows))
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------- websocket

from starlette.websockets import WebSocket, WebSocketDisconnect


def _secteur_de_la_connexion(user: dict) -> str:
    """Secteur du compte qui ouvre un WebSocket, jamais en erreur.

    Une base illisible ne doit pas empêcher la connexion : on retombe alors
    sur le secteur par défaut, qui ne donne accès qu'aux diffusions de ce
    secteur-là (défaut fermé côté `broadcast`).
    """
    try:
        with get_conn() as conn:
            return _secteur_du_compte(conn, user)
    except Exception:
        log.exception("secteur illisible pour la connexion WebSocket")
        return SECTEUR_DEFAUT


@app.post("/ws-ticket")
def creer_billet_websocket(x_user_token: str | None = Header(default=None),
                           user=Depends(exiger_utilisateur)):
    """Émet un billet à usage unique (30s) pour ouvrir le WebSocket.

    Évite que le token de session (8h) transite en clair dans l'URL du
    WebSocket, où il resterait dans les logs d'accès et tout proxy.
    """
    ticket = creer_ticket_ws(x_user_token)
    return {"ticket": ticket, "expires_in": 30}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    ticket = websocket.query_params.get("ticket")
    if not ticket:
        await websocket.close(code=1008)
        return
    token = await asyncio.to_thread(consommer_ticket_ws, ticket)
    if not token:
        await websocket.close(code=1008)
        return
    # `session_valide` est synchrone et peut prendre le verrou d'écriture
    # SQLite (prolongation de session) : appelée directement, elle bloquerait
    # la boucle d'événements — donc TOUT le serveur — jusqu'à 30 s si un autre
    # poste écrit au même moment.
    user = await asyncio.to_thread(session_valide, token)
    if not user:
        await websocket.close(code=1008)
        return
    # Secteur du compte, mémorisé sur la connexion : c'est lui qui décide
    # ensuite quelles diffusions cette socket reçoit (`broadcast(sector_filter=...)`).
    # `session_valide` ne le porte pas (la table `sessions` ne le stocke pas),
    # il est donc relu ici, une seule fois par connexion.
    user = dict(user)
    user["sector"] = await asyncio.to_thread(_secteur_de_la_connexion, user)
    # `manager.connect` accepte la connexion ET l'enregistre : sans cet
    # enregistrement, aucune diffusion n'atteignait jamais un client.
    if not await manager.connect(websocket, user):
        return
    try:
        while True:
            data = await websocket.receive_text()
            # Toute trame reçue prouve que le client est vivant : c'est le
            # seul endroit qui l'observe, donc le seul qui puisse dater la
            # connexion pour la détection des connexions mortes.
            manager.marquer_actif(websocket)
            # Simple ping/pong
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("connexion WebSocket interrompue : %s", e)
    finally:
        manager.disconnect(websocket)
