import json
import os

from chemins_poste import chemin_config, chemin_config_defaut, reprendre_ancienne_config

DEFAULTS = {
    "server_host": "127.0.0.1",
    "server_port": 8000,
    "operator": "",
    # Préférence de poste : proposer l'impression après chaque bon enregistré.
    # Les postes qui n'impriment jamais peuvent couper la question via la case
    # « Ne plus demander » du dialogue.
    "proposer_impression": True,
    # Langue de l'interface du poste : "fr" (défaut) ou "en". Certains
    # dirigeants sont anglophones ; chaque poste mémorise son propre choix.
    # Appliquée au démarrage par main.py (voir i18n.definir_langue).
    "language": "fr",
    # Patience réseau du poste, en secondes. Les valeurs par défaut visent un
    # poste DISTANT (entrepôt régional relié par Internet, éventuellement en
    # 3G/4G), pas le poste central branché sur le même commutateur que le
    # serveur : sur une liaison lente, une poignée de main TCP dépasse
    # couramment 2 secondes, et l'ancien délai de connexion faisait échouer en
    # « Impossible de contacter le serveur » des envois qui seraient passés.
    # Un poste central peut les redescendre pour retrouver un échec rapide.
    "timeout_connexion": 10.0,
    "timeout_lecture": 60.0,
}


def lire_preference(cle: str, defaut=None):
    """Lit une préférence de poste, sans jamais lever."""
    try:
        return load_config().get(cle, DEFAULTS.get(cle, defaut))
    except Exception:
        return DEFAULTS.get(cle, defaut)


def enregistrer_preference(cle: str, valeur) -> bool:
    """Persiste une préférence de poste. Renvoie False si l'écriture a échoué.

    Relit la configuration juste avant d'écrire : le poste peut avoir changé
    d'adresse serveur entre-temps, et un dictionnaire mémorisé trop tôt
    écraserait ce réglage.
    """
    try:
        config = load_config()
        config[cle] = valeur
        save_config(config)
        return True
    except Exception:
        return False


def _lire(chemin) -> dict:
    """Lit un fichier de configuration, en ignorant ce qui est illisible.

    Une configuration corrompue ne doit pas empêcher l'application de
    démarrer : l'opérateur pourra la corriger depuis l'écran Paramètres.
    """
    try:
        if chemin.exists():
            contenu = json.loads(chemin.read_text(encoding="utf-8"))
            # Un fichier peut être du JSON parfaitement valide sans être un
            # objet : « null », « [] », « "abc" » passent json.loads mais font
            # ensuite lever `{**DEFAULTS, **contenu}` (TypeError), et le poste
            # ne démarre plus du tout. On l'ignore comme un fichier corrompu.
            if isinstance(contenu, dict):
                return contenu
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError):
        pass
    return {}


def load_config() -> dict:
    # Reprend la configuration d'un poste installé avant le déplacement du
    # fichier, sinon sa clé serait perdue et ses écritures refusées.
    # Jamais bloquant : un ancien fichier illisible (droits, disque, encodage)
    # ne doit pas empêcher le poste de démarrer sur ses valeurs par défaut.
    try:
        reprendre_ancienne_config()
    except (OSError, UnicodeDecodeError):
        pass
    # Les réglages du poste priment sur les valeurs livrées par défaut,
    # qui priment sur les valeurs de repli du code.
    return {**DEFAULTS, **_lire(chemin_config_defaut()), **_lire(chemin_config())}


def save_config(config: dict) -> None:
    """Écrit la configuration de façon atomique.

    Un write_text direct tronque le fichier avant de le réécrire : une
    coupure de courant à cet instant laisse un JSON incomplet, et le poste
    perd l'adresse du serveur au redémarrage. On passe donc par un fichier
    temporaire puis os.replace(), atomique sur Windows
    comme ailleurs — le fichier final est soit l'ancien, soit le nouveau,
    jamais un mélange des deux.
    """
    chemin = chemin_config()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    provisoire = chemin.with_suffix(chemin.suffix + ".tmp")
    contenu = json.dumps(config, indent=2, ensure_ascii=False)
    try:
        with open(provisoire, "w", encoding="utf-8") as f:
            f.write(contenu)
            f.flush()
            os.fsync(f.fileno())
        os.replace(provisoire, chemin)
    except OSError:
        provisoire.unlink(missing_ok=True)
        raise


def server_base_url(config: dict) -> str:
    """Adresse du serveur, toujours utilisable.

    Appelée au tout premier écran de `main.py` : elle ne doit jamais lever.
    Une valeur absente, nulle ou aberrante (host à `null`, port écrit en
    lettres) retombe sur la valeur par défaut — l'opérateur corrigera depuis
    l'écran Paramètres, ce qu'il ne peut pas faire si l'application ne
    s'ouvre pas.
    """
    host = str(config.get("server_host") or "").strip()
    if not host:
        host = DEFAULTS["server_host"]
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    try:
        port = int(config.get("server_port") or DEFAULTS["server_port"])
    except (TypeError, ValueError):
        port = DEFAULTS["server_port"]
    if not 1 <= port <= 65535:
        port = DEFAULTS["server_port"]
    return f"http://{host}:{port}"


def websocket_url(config: dict) -> str:
    """URL du WebSocket, DÉRIVÉE de l'adresse HTTP au lieu d'être reconstruite.

    `server_host` accepte une URL complète (« https://wms.exemple.com ») —
    c'est le seul moyen d'atteindre le serveur derrière un proxy HTTPS, donc
    exactement ce qu'un poste régional distant utilisera. Le client WebSocket
    fabriquait pourtant son URL en collant « ws:// » devant l'hôte et « : » +
    port derrière, ce qui donnait « ws://https://wms.exemple.com:8000/ws » :
    une URL invalide. Le temps réel mourait alors sur tous les postes
    distants, définitivement et sans message.

    Un serveur joint en HTTPS impose `wss://` : `ws://` sur un proxy TLS est
    refusé au niveau du transport.
    """
    base = server_base_url(config)
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/ws"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/ws"
    return "ws://" + base + "/ws"


def timeouts(config: dict) -> tuple[float, float]:
    """Délais (connexion, lecture) du poste, toujours utilisables.

    Appelée à la construction du client HTTP, avant tout écran : une valeur
    aberrante dans le fichier de configuration ne doit pas empêcher le poste
    de démarrer, elle retombe sur le défaut.
    """
    valeurs = []
    for cle in ("timeout_connexion", "timeout_lecture"):
        try:
            valeur = float(config.get(cle) or DEFAULTS[cle])
        except (TypeError, ValueError):
            valeur = DEFAULTS[cle]
        if not 0 < valeur <= 600:
            valeur = DEFAULTS[cle]
        valeurs.append(valeur)
    return (valeurs[0], valeurs[1])
