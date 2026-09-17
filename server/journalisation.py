"""Journalisation applicative.

Sans elle, un incident (coupure en cours d'écriture, échec de sauvegarde,
tentative d'accès non autorisée, exception inattendue) ne laisse aucune trace
au-delà de ce qu'un `print()` écrivait sur la console — perdu dès qu'un
service Windows tourne sans fenêtre visible, ou que la console défile.

Point de configuration unique, appelé une fois au démarrage (`configurer()`).
Chaque module récupère ensuite son propre logger via `logger(__name__)` et
n'a besoin de rien savoir de la configuration.

LOG_DIR est une variable de module (pas une constante figée à l'import) pour
que les tests puissent la rediriger vers un dossier temporaire — sinon la
suite écrirait dans les vrais journaux du poste qui l'exécute.
"""
import logging
import logging.handlers

from chemins import dossier_journaux

LOG_DIR = dossier_journaux()
NOM_FICHIER = "application.log"

_configuree = False


def configurer(niveau_console: int = logging.INFO) -> None:
    """Idempotent : un second appel ne duplique pas les handlers."""
    global _configuree
    if _configuree:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatteur = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fichier = logging.handlers.RotatingFileHandler(
        LOG_DIR / NOM_FICHIER, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    fichier.setLevel(logging.INFO)
    fichier.setFormatter(formatteur)

    console = logging.StreamHandler()
    console.setLevel(niveau_console)
    console.setFormatter(formatteur)

    racine = logging.getLogger("wms")
    racine.setLevel(logging.INFO)
    racine.handlers.clear()
    racine.addHandler(fichier)
    racine.addHandler(console)
    racine.propagate = False

    _configuree = True


def reinitialiser() -> None:
    """Pour les tests : force une reconfiguration avec un LOG_DIR différent."""
    global _configuree
    logging.getLogger("wms").handlers.clear()
    _configuree = False


def logger(nom: str) -> logging.Logger:
    return logging.getLogger(f"wms.{nom}")
