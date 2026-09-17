"""Gestion des comptes utilisateurs.

Hachage PBKDF2-SHA256 des mots de passe (stdlib), sessions persistantes en
SQLite. La colonne `sessions.token` ne porte PAS le jeton mais son empreinte
SHA-256 (`hacher_jeton`) : une base volée ne permet plus de rejouer une
session ouverte. Toute requête SQL sur `sessions.token`, où qu'elle soit
écrite, doit donc passer par `hacher_jeton`.
Tant qu'aucun utilisateur n'existe en base (tout premier lancement), le
système fonctionne sans authentification, le temps de créer le compte
administrateur. Dès qu'un compte existe, le token de session est exigé.

L'ancienne authentification par clé de poste a été supprimée : ce module est
désormais le seul point d'entrée de l'authentification.
"""
import hashlib
import os
import secrets
import threading
import time
from typing import Optional

from fastapi import Header, HTTPException

from database import get_conn
from journalisation import logger

log = logger(__name__)

ITERATIONS = 260_000
SESSION_DUREE_S = 8 * 3600

# Expiration par INACTIVITÉ, en plus de la durée absolue ci-dessus.
# Un poste d'entrepôt reste allumé et déverrouillé toute la journée : sans
# ça, une session ouverte le matin restait utilisable par n'importe qui
# jusqu'au soir, y compris pendant la pause ou après le départ de
# l'opérateur. Deux heures : assez pour ne jamais couper quelqu'un au
# travail (une saisie de bon dure quelques minutes), assez court pour
# qu'un poste laissé seul se referme de lui-même.
SESSION_INACTIVITE_S = 2 * 3600

# `last_seen_at` n'est réécrit que si la dernière écriture date de plus de
# ça. Sans ce palier, CHAQUE requête authentifiée déclencherait un UPDATE :
# sur SQLite en WAL, cela transforme toutes les lectures du client en
# écritures et sérialise l'ensemble du trafic derrière le verrou d'écriture.
PERIODE_RAFRAICHISSEMENT_ACTIVITE_S = 60

# Billets WebSocket : le token de session (8h) ne doit pas transiter en clair
# dans l'URL du WebSocket, où il finit dans les logs d'accès et tout proxy
# intermédiaire. Un billet à usage unique et de très courte durée limite
# l'exposition à la fenêtre de connexion. En mémoire (pas en base) : leur
# perte au redémarrage du serveur est sans conséquence, le client en redemande
# un aussitôt.
TICKET_WS_DUREE_S = 30
_tickets_ws: dict[str, tuple[str, float]] = {}
_verrou_tickets_ws = threading.Lock()

# --- Cache mémoire des sessions ---
#
# `session_valide` est appelée par la dépendance FastAPI de CHAQUE requête
# authentifiée, et `role_de_session` une seconde fois par le middleware qui
# ferme l'application aux comptes régionaux : deux lectures SQLite par requête
# HTTP, y compris pour afficher une liste. Sur SQLite en WAL, une lecture reste
# peu coûteuse, mais elle ouvre une connexion, prend une transaction et
# s'ajoute à toutes les autres — c'est du bruit pur sur un poste qui rafraîchit
# son écran.
#
# Cache très court, volontairement : cinq secondes bornent la fenêtre pendant
# laquelle une session révoquée ailleurs resterait acceptée. Toutes les
# révocations connues du système (déconnexion, changement de mot de passe,
# désactivation d'un compte, changement de périmètre régional, purge des
# sessions expirées) invalident explicitement le cache — la fenêtre de 5 s ne
# concerne donc qu'une suppression faite DIRECTEMENT en base, hors application.
#
# Ce que le cache ne contourne jamais :
#   - l'expiration absolue : l'entrée n'est jamais gardée au-delà de
#     `expires_at`, même si les 5 secondes ne sont pas écoulées ;
#   - l'expiration par inactivité : une entrée n'est mise en cache qu'après un
#     passage complet par `session_valide`, qui vient de constater l'activité
#     (`last_seen_at` a au plus 60 s), donc 5 s de cache ne peuvent pas franchir
#     les 2 heures d'inactivité ;
#   - un échec de validation : aucun résultat négatif n'est mis en cache, et
#     tout échec purge l'entrée éventuelle du jeton.
CACHE_SESSION_DUREE_S = 5.0

# Plafond de sécurité : le cache ne doit pas devenir une fuite mémoire si des
# milliers de jetons distincts défilent (balayage automatisé). Au-delà, on
# élague les entrées expirées ; si cela ne suffit pas, on vide tout — perdre le
# cache ne coûte qu'une lecture SQLite de plus.
CACHE_SESSION_MAX = 5_000

# {token: (instant d'expiration du cache, dict de session)}
_cache_sessions: dict[str, tuple[float, dict]] = {}
_verrou_cache_sessions = threading.Lock()


def _cache_session_lire(token: str) -> Optional[dict]:
    """Copie de la session en cache, ou None si absente/périmée."""
    with _verrou_cache_sessions:
        entree = _cache_sessions.get(token)
        if entree is None:
            return None
        expire_cache, donnees = entree
        if time.time() >= expire_cache:
            del _cache_sessions[token]
            return None
        # Copie : l'appelant ne doit jamais pouvoir modifier l'entrée partagée.
        return dict(donnees)


def _cache_session_ecrire(token: str, donnees: dict, expires_at: float) -> None:
    """Met la session en cache, sans jamais dépasser son expiration absolue."""
    now = time.time()
    limite = min(now + CACHE_SESSION_DUREE_S, float(expires_at))
    if limite <= now:
        return
    with _verrou_cache_sessions:
        if len(_cache_sessions) >= CACHE_SESSION_MAX:
            for jeton in [j for j, (exp, _) in _cache_sessions.items() if exp <= now]:
                del _cache_sessions[jeton]
            if len(_cache_sessions) >= CACHE_SESSION_MAX:
                _cache_sessions.clear()
        _cache_sessions[token] = (limite, dict(donnees))


def invalider_cache_session(token: Optional[str] = None) -> None:
    """Oublie un jeton (ou tout le cache si `token` vaut None).

    Appelée à chaque révocation. Un cache qui survivrait à une déconnexion
    laisserait le jeton utilisable pendant cinq secondes de plus : c'est le
    seul vrai danger de ce cache, et il est traité ici.
    """
    with _verrou_cache_sessions:
        if token is None:
            _cache_sessions.clear()
        else:
            _cache_sessions.pop(token, None)


def invalider_cache_sessions_utilisateur(user_id: int) -> None:
    """Oublie tous les jetons d'un compte (mot de passe changé, compte désactivé).

    Les endpoints qui révoquent des sessions le font par `user_id`, pas par
    jeton : sans ce balayage, le jeton révoqué resterait accepté jusqu'à cinq
    secondes. Le cache est borné (CACHE_SESSION_MAX), le parcours est donc
    négligeable et ne se produit qu'à ces rares écritures.
    """
    with _verrou_cache_sessions:
        for token in [j for j, (_exp, d) in _cache_sessions.items()
                      if d.get("user_id") == user_id]:
            del _cache_sessions[token]


def creer_ticket_ws(token: str) -> str:
    """Émet un billet à usage unique valable TICKET_WS_DUREE_S, lié à `token`."""
    ticket = secrets.token_urlsafe(24)
    with _verrou_tickets_ws:
        _purger_tickets_ws_expires()
        _tickets_ws[ticket] = (token, time.time() + TICKET_WS_DUREE_S)
    return ticket


def consommer_ticket_ws(ticket: str) -> Optional[str]:
    """Renvoie le token associé au billet et l'invalide (usage unique)."""
    with _verrou_tickets_ws:
        entree = _tickets_ws.pop(ticket, None)
    if entree is None:
        return None
    token, expire_a = entree
    if time.time() > expire_a:
        return None
    return token


def _purger_tickets_ws_expires() -> None:
    """Appelée sous _verrou_tickets_ws : évite l'accumulation de billets non consommés."""
    now = time.time()
    expires = [t for t, (_, exp) in _tickets_ws.items() if exp < now]
    for t in expires:
        del _tickets_ws[t]


def hacher_jeton(token: str) -> str:
    """Empreinte SHA-256 d'un jeton de session, telle qu'elle est STOCKÉE.

    La table `sessions` portait le jeton en clair. Un vol du fichier de base
    — ou d'une sauvegarde non chiffrée — donnait donc de quoi REJOUER
    immédiatement la session d'un opérateur connecté, sans jamais connaître
    son mot de passe, jusqu'à expiration. Le jeton ne vit plus sur disque :
    seule son empreinte y est écrite, et une empreinte ne se présente pas au
    serveur (elle ne se transforme pas en jeton valide).

    SHA-256 nu, sans sel ni PBKDF2, DÉLIBÉRÉMENT — et c'est l'inverse du
    choix fait pour `hacher_mot_de_passe` :
      - un mot de passe humain est à faible entropie, donc attaquable par
        dictionnaire : il exige un hachage lent et salé, et ne se hache
        qu'une fois, à la connexion ;
      - un jeton est déjà 32 octets tirés de `secrets.token_urlsafe`, donc
        hors de portée de toute recherche exhaustive : un sel n'ajouterait
        rien (pas de table précalculée possible) et PBKDF2 imposerait ses
        260 000 itérations à CHAQUE requête HTTP authentifiée, deux fois par
        requête (dépendance + middleware). Ce serait payer très cher une
        protection qui ne protège rien de plus ici.

    Déterministe et sans sel, l'empreinte reste utilisable comme clé primaire
    et se cherche par index — ce que la table faisait déjà avec le jeton.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def hacher_mot_de_passe(mot_de_passe: str) -> str:
    sel = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode(), sel, ITERATIONS)
    return f"{sel.hex()}:{h.hex()}"


def verifier_mot_de_passe(mot_de_passe: str, hash_stocke: str) -> bool:
    try:
        sel_hex, h_hex = hash_stocke.split(":")
        sel = bytes.fromhex(sel_hex)
        attendu = bytes.fromhex(h_hex)
    except (ValueError, AttributeError):
        return False
    h = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode(), sel, ITERATIONS)
    return secrets.compare_digest(h, attendu)


def creer_session(user_id: int, username: str, display_name: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires_at = now + SESSION_DUREE_S
    with get_conn(write=True) as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, username, display_name, role, "
            "expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            # En base : l'EMPREINTE. Le jeton en clair n'est rendu qu'à
            # l'appelant, qui le transmet au client — il ne touche pas le
            # disque. Voir `hacher_jeton`.
            (hacher_jeton(token), user_id, username, display_name, role,
             expires_at, now),
        )
    return token


def session_valide(token: str) -> Optional[dict]:
    # Le cache reste indexé par jeton EN CLAIR, volontairement : il vit en
    # mémoire du processus, jamais sur disque, donc le risque que traite
    # `hacher_jeton` (vol du fichier de base ou d'une sauvegarde) ne le
    # concerne pas. Le garder en clair évite un SHA-256 sur le chemin le plus
    # chaud du serveur, et laisse les invalidations par jeton
    # (`invalider_cache_session`, appelée depuis `fermer_session` et les
    # endpoints) fonctionner sans rien recalculer.
    en_cache = _cache_session_lire(token)
    if en_cache is not None:
        return en_cache

    empreinte = hacher_jeton(token)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token = ?", (empreinte,)
        ).fetchone()
    if not row:
        invalider_cache_session(token)
        return None
    now = time.time()
    if now > row["expires_at"]:
        invalider_cache_session(token)
        with get_conn(write=True) as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (empreinte,))
        return None

    # Inactivité : une session encore dans sa fenêtre de 8 h expire quand
    # même si plus rien n'est arrivé depuis SESSION_INACTIVITE_S.
    # `last_seen_at` est NULL pour les sessions ouvertes AVANT la migration
    # qui a ajouté la colonne : on les traite comme actives à l'instant et on
    # les date au passage, plutôt que de déconnecter tout le monde au
    # redémarrage qui suit la mise à jour.
    derniere_activite = row["last_seen_at"]
    if derniere_activite is not None and now - derniere_activite > SESSION_INACTIVITE_S:
        invalider_cache_session(token)
        with get_conn(write=True) as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (empreinte,))
        log.info("session fermée pour inactivité : %s", row["username"])
        return None

    mi_vie = row["expires_at"] - SESSION_DUREE_S / 2
    renouveler = now > mi_vie
    marquer_activite = (derniere_activite is None
                        or now - derniere_activite > PERIODE_RAFRAICHISSEMENT_ACTIVITE_S)
    new_expires = now + SESSION_DUREE_S if renouveler else row["expires_at"]
    if renouveler or marquer_activite:
        with get_conn(write=True) as conn:
            conn.execute(
                "UPDATE sessions SET expires_at = ?, last_seen_at = ? WHERE token = ?",
                (new_expires, now, empreinte),
            )
    session = {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "expire": new_expires,
    }
    # `new_expires` sert aussi de borne au cache : une session renouvelée à
    # l'instant est valide 8 h, une session en fin de vie ne l'est plus que
    # quelques secondes — le cache ne doit pas la prolonger.
    _cache_session_ecrire(token, session, session["expire"])
    return session


def role_de_session(token: str | None) -> Optional[str]:
    """Rôle porté par un jeton de session encore valide, sans effet de bord.

    Sert au middleware qui ferme l'application aux comptes régionaux : il
    s'exécute avant toute dépendance, donc avant `session_valide`, et ne doit
    ni prolonger la session ni la supprimer.

    Le cache est LU (une session validée il y a moins de cinq secondes porte
    forcément le même rôle) mais jamais ÉCRIT ici : cette requête ne vérifie
    pas l'inactivité, elle ne doit donc pas fabriquer une entrée que
    `session_valide` prendrait ensuite pour argent comptant.
    """
    if not token:
        return None
    en_cache = _cache_session_lire(token)
    if en_cache is not None:
        return en_cache.get("role")
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT role FROM sessions WHERE token = ? AND expires_at > ?",
                (hacher_jeton(token), time.time()),
            ).fetchone()
    except Exception:
        log.exception("lecture de session impossible")
        return None
    return row["role"] if row else None


def fermer_session(token: str) -> bool:
    invalider_cache_session(token)
    with get_conn(write=True) as conn:
        cur = conn.execute("DELETE FROM sessions WHERE token = ?",
                           (hacher_jeton(token),))
        return cur.rowcount > 0


def nettoyer_sessions_expirees():
    """Purge les sessions périmées : durée absolue dépassée OU inactives.

    Le nettoyage périodique couvre le cas de la session que plus personne ne
    présente : `session_valide` ne la verrait jamais, et sa ligne resterait
    en base — donc réutilisable par qui remettrait la main sur le token.
    """
    now = time.time()
    with get_conn(write=True) as conn:
        n = conn.execute(
            "DELETE FROM sessions WHERE expires_at < ? "
            "   OR (last_seen_at IS NOT NULL AND last_seen_at < ?)",
            (now, now - SESSION_INACTIVITE_S),
        ).rowcount
        if n:
            log.info("sessions expirees nettoyees : %d", n)
    if n:
        # La purge supprime par condition, pas par jeton : on ne sait pas
        # lesquels sont tombés. Vider tout le cache coûte au pire une lecture
        # SQLite de plus par session encore ouverte — laisser en mémoire un
        # jeton que la base vient de refuser coûterait beaucoup plus cher.
        invalider_cache_session()


def utilisateurs_existent(conn) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return row["n"] > 0


def utilisateur_courant(x_user_token: str | None = Header(default=None)):
    """Dépendance FastAPI : identifie l'utilisateur connecté.

    Si aucun utilisateur n'existe en base (premier lancement), renvoie None
    pour ne pas bloquer l'utilisation. Dès qu'un utilisateur est créé, le
    token devient obligatoire pour les écritures.
    """
    if not x_user_token:
        return None
    s = session_valide(x_user_token)
    if not s:
        return None
    return s


MESSAGE_NON_INITIALISE = (
    "Installation non initialisée — créez le premier compte administrateur."
)

# Échappatoire RÉSERVÉE À LA SUITE DE TESTS (posée par tests/conftest.py).
#
# Près de mille tests exercent la logique métier sur une base neuve, sans
# jamais créer de compte : leur imposer une authentification ne testerait rien
# de plus et ne dirait rien de la sécurité du produit. La variable n'est
# jamais posée en exploitation — ni par les .bat, ni par le service Windows,
# ni par l'installateur — et personne ne peut la poser à distance : ce n'est
# donc pas un contournement exploitable, juste un mode de test explicite.
#
# Lue à chaque appel (et non figée à l'import) pour qu'un test puisse la
# retirer et vérifier le vrai comportement du premier démarrage.
VARIABLE_TESTS_SANS_AUTH = "WMS_TESTS_SANS_AUTH"


def _mode_tests_sans_auth() -> bool:
    return bool(os.getenv(VARIABLE_TESTS_SANS_AUTH))


def exiger_utilisateur(x_user_token: str | None = Header(default=None)):
    """Comme utilisateur_courant, mais lève 401 si des comptes existent en base.

    Base VIDE de comptes (tout premier lancement, ou sauvegarde restaurée de
    travers) : 503 et rien d'autre. C'était auparavant un passe-droit général
    — sans un seul compte en base, TOUTE l'API s'ouvrait sans jeton : le
    catalogue, le stock, les bons, les exports, l'administration. Le seul
    besoin réel du premier démarrage est de pouvoir créer le premier compte,
    et `POST /users` ne dépend pas de cette fonction (il utilise
    `utilisateur_courant`, qui reste permissif). `/health` et
    `/users/status`, eux, n'ont aucune dépendance d'authentification.
    """
    u = utilisateur_courant(x_user_token)
    if u is None:
        with get_conn() as conn:
            if utilisateurs_existent(conn):
                raise HTTPException(status_code=401, detail="Connexion requise")
        if not _mode_tests_sans_auth():
            raise HTTPException(status_code=503, detail=MESSAGE_NON_INITIALISE)
    return u


MESSAGE_LECTEUR_ECRITURE = (
    "Un compte lecteur est en lecture seule : il ne peut pas enregistrer "
    "cette opération."
)


def exiger_ecriture(x_user_token: str | None = Header(default=None)):
    """Comme `exiger_utilisateur`, mais refuse le rôle « lecteur ».

    Ce contrôle était recopié à la main dans une dizaine d'endpoints, chacun
    avec son propre message. Un endpoint d'écriture ajouté sans y penser
    devenait ouvert aux lecteurs sans que rien ne le signale : le seul moyen
    de s'en apercevoir était de relire les dix autres. En dépendance, il est
    posé une fois pour toutes et se lit dans la signature.

    Distinct de `exiger_admin`, qui garde un tout autre rôle : réserver une
    fonction d'ADMINISTRATION. Ici, on autorise magasiniers et comptes
    régionaux — on n'exclut que le lecteur.
    """
    u = exiger_utilisateur(x_user_token)
    if u is not None and u.get("role") == "lecteur":
        raise HTTPException(status_code=403, detail=MESSAGE_LECTEUR_ECRITURE)
    return u


def exiger_board(x_user_token: str | None = Header(default=None)):
    """Exige un utilisateur avec le rôle « board » ou « admin »."""
    u = exiger_utilisateur(x_user_token)
    if u is not None and u.get("role") not in ("board", "admin"):
        raise HTTPException(status_code=403, detail="Réservé aux membres du board")
    return u


def exiger_admin(x_user_token: str | None = Header(default=None)):
    u = exiger_utilisateur(x_user_token)
    if u is not None and u["role"] != "admin":
        raise HTTPException(status_code=403, detail="Réservé aux administrateurs")
    return u
