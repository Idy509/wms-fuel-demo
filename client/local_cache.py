"""Cache local du poste : catalogue produits, contacts et brouillon de bon.

Contexte : le poste travaille avec des coupures électriques et réseau
fréquentes. Deux pertes sont inacceptables pour l'opérateur :

1. Serveur injoignable au démarrage → la liste des produits reste vide et
   plus rien ne peut être saisi, alors même que la file d'attente hors ligne
   existe pour permettre de continuer à travailler.
2. Coupure en pleine saisie d'un bon de vingt lignes → tout est perdu.

Ce module écrit donc en JSON, dans le même dossier utilisateur que la
configuration et la file d'attente (%APPDATA%\\WarehouseClient), une copie du
dernier catalogue connu et un brouillon du bon en cours.

Aucune fonction ne lève : un cache illisible ou un disque plein ne doit
jamais empêcher l'application de fonctionner en mode normal.
"""
import json
import threading
from pathlib import Path

_verrou = threading.Lock()


def _dossier() -> Path:
    from chemins_poste import dossier_utilisateur
    return dossier_utilisateur()


def _chemin(nom: str) -> Path:
    return _dossier() / nom


def _ecrire_json(chemin: Path, donnees) -> bool:
    try:
        chemin.parent.mkdir(parents=True, exist_ok=True)
        # Écriture puis remplacement : une coupure pendant l'écriture laisse
        # l'ancien cache intact plutôt qu'un fichier tronqué.
        provisoire = chemin.with_suffix(chemin.suffix + ".tmp")
        provisoire.write_text(json.dumps(donnees, ensure_ascii=False), encoding="utf-8")
        provisoire.replace(chemin)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _lire_json(chemin: Path, defaut):
    try:
        if chemin.exists():
            return json.loads(chemin.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        pass
    return defaut


# ------------------------------------------------------------------ produits

# Le poste est parfois partagé entre comptes de secteurs différents (le
# propre poste d'un admin multi-secteurs, notamment) : sans cette étiquette,
# le catalogue FUEL du dernier passage en ligne se retrouvait proposé tel
# quel — SKU, unités, fournisseurs d'un autre secteur — à un compte
# Consumables ou FON tombé hors ligne juste après. Constaté par exploration
# le 2026-09-09, jamais couvert par le correctif de reference_data.py (qui
# visait un défaut différent : une réponse serveur à tort traitée comme
# absente, pas un cache non étiqueté).
def sauvegarder_produits(produits: list[dict], sector: str | None = None) -> bool:
    """Mémorise le catalogue reçu du serveur. Renvoie False si l'écriture a échoué."""
    if not produits:
        return False
    with _verrou:
        return _ecrire_json(_chemin("cache_produits.json"),
                            {"sector": sector, "produits": produits})


def charger_produits(sector: str | None = None) -> list[dict]:
    """Dernier catalogue connu pour CE secteur, ou liste vide sinon.

    `sector=None` : compat historique (appelant qui ne connaît pas encore son
    secteur) — renvoie le cache tel quel, comme avant cette étiquette.
    """
    with _verrou:
        donnees = _lire_json(_chemin("cache_produits.json"), {})
    if isinstance(donnees, list):
        # Ancien format (avant l'étiquette de secteur) : accepté tel quel,
        # il sera réécrit avec l'étiquette au prochain passage en ligne.
        return donnees
    if not isinstance(donnees, dict):
        return []
    if sector is not None and donnees.get("sector") != sector:
        return []
    produits = donnees.get("produits")
    return produits if isinstance(produits, list) else []


# ------------------------------------------------------------------ contacts

def sauvegarder_contacts(genre: str, contacts: list[dict], sector: str | None = None) -> bool:
    """Mémorise les contacts d'un genre donné (``supplier``, ``customer``...)."""
    if not contacts:
        return False
    with _verrou:
        return _ecrire_json(_chemin(f"cache_contacts_{genre}.json"),
                            {"sector": sector, "contacts": contacts})


def charger_contacts(genre: str, sector: str | None = None) -> list[dict]:
    """Même étiquette de secteur que `charger_produits` — même raison."""
    with _verrou:
        donnees = _lire_json(_chemin(f"cache_contacts_{genre}.json"), {})
    if isinstance(donnees, list):
        return donnees
    if not isinstance(donnees, dict):
        return []
    if sector is not None and donnees.get("sector") != sector:
        return []
    contacts = donnees.get("contacts")
    return contacts if isinstance(contacts, list) else []


# --------------------------------------------------------------- référentiel

def sauvegarder_referentiel(data: dict, sector: str | None = None) -> bool:
    """Mémorise le référentiel régions/superviseurs reçu du serveur.

    Le serveur fait autorité. Sans ce cache, un poste hors ligne retombait sur
    une copie figée dans le code (``_REPLI``) qui divergeait silencieusement du
    serveur dès qu'une région ou un superviseur y était ajouté.

    Étiqueté par secteur, même raison et même défaut que `sauvegarder_produits`
    (voir plus haut) — repéré séparément le 2026-09-09 : ce cache-ci n'avait
    pas reçu le même correctif la première fois. Sans l'étiquette, un poste
    servant plusieurs secteurs pouvait proposer, hors ligne, les régions/
    superviseurs/opérateurs/techniciens d'un AUTRE secteur dans les menus
    déroulants de saisie (bons, bouteilles, inventaire).
    """
    if not data:
        return False
    donnees = dict(data)
    donnees["sector"] = sector
    with _verrou:
        return _ecrire_json(_chemin("cache_referentiel.json"), donnees)


def charger_referentiel(sector: str | None = None) -> dict | None:
    """Dernier référentiel connu pour CE secteur, ou None sinon.

    `sector=None` (appelant qui ne connaît pas encore son secteur — le tout
    premier chargement, avant connexion) : compat historique, rendu tel quel.
    """
    with _verrou:
        donnees = _lire_json(_chemin("cache_referentiel.json"), None)
    if not (isinstance(donnees, dict) and donnees):
        return None
    if sector is not None and donnees.get("sector") != sector:
        return None
    return donnees


# ----------------------------------------------------------------- brouillon

# Au-delà, un brouillon décrit une journée de travail déjà close : les
# quantités et le stock ont bougé depuis. Le reproposer ferait plus de dégâts
# qu'il n'en éviterait.
BROUILLON_VALIDITE_HEURES = 24


def sauvegarder_brouillon(doc_type: str, data: dict, sector: str | None = None) -> bool:
    """Enregistre le bon en cours de saisie (un brouillon par type de bon).

    Estampille date et auteur : le poste est partagé entre magasiniers, et
    « un bon non terminé a été trouvé » sans contexte ne dit pas à l'opérateur
    s'il s'agit de sa saisie d'il y a cinq minutes ou de celle d'un collègue
    la semaine passée.

    Estampille aussi le SECTEUR : le poste est parfois partagé entre comptes
    de secteurs différents (voir `sauvegarder_produits`, même raison) — sans
    cette étiquette, un brouillon FON interrompu (SKU, région, tiers) se
    proposait tel quel à un compte Consumables ou FON ouvrant ensuite le même
    type de bon. La soumission finale reste bloquée côté serveur
    (`create_document` valide région/technicien/produit contre le secteur de
    l'appelant), mais la fuite dans l'écran — avant tout blocage — avait déjà
    eu lieu. Constaté par exploration le 2026-09-09.
    """
    from datetime import datetime

    donnees = dict(data)
    donnees.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    if not donnees.get("username"):
        donnees["username"] = _operateur_courant()
    donnees["sector"] = sector
    with _verrou:
        return _ecrire_json(_chemin(f"brouillon_{doc_type}.json"), donnees)


def _operateur_courant() -> str:
    try:
        from config import load_config
        return (load_config().get("operator") or "").strip()
    except Exception:
        return ""


def brouillon_est_perime(brouillon: dict) -> bool:
    """Vrai si le brouillon dépasse la fenêtre de validité.

    Un brouillon sans date vient d'une version antérieure : on le considère
    périmé plutôt que de proposer de reprendre un bon d'âge inconnu.
    """
    from datetime import datetime, timedelta

    horodatage = brouillon.get("created_at")
    if not horodatage:
        return True
    try:
        cree_le = datetime.fromisoformat(horodatage)
    except (TypeError, ValueError):
        return True
    return datetime.now() - cree_le > timedelta(hours=BROUILLON_VALIDITE_HEURES)


def charger_brouillon(doc_type: str, sector: str | None = None) -> dict | None:
    """Brouillon en attente pour ce type de bon, ou None.

    Un brouillon périmé est traité comme inexistant ET effacé : le laisser en
    place le ferait réévaluer à chaque ouverture de l'onglet.

    `sector` fourni et différent de celui du brouillon : traité comme
    inexistant, mais PAS effacé — il reste disponible pour le compte du bon
    secteur qui le retrouvera plus tard. `sector=None` (appelant qui ne
    connaît pas encore son secteur) : compat historique, rendu tel quel.
    """
    with _verrou:
        donnees = _lire_json(_chemin(f"brouillon_{doc_type}.json"), None)
    if not (isinstance(donnees, dict) and donnees.get("lines")):
        return None
    if sector is not None and donnees.get("sector") != sector:
        return None
    if brouillon_est_perime(donnees):
        effacer_brouillon(doc_type)
        return None
    return donnees


def effacer_brouillon(doc_type: str) -> None:
    """Supprime le brouillon : le bon a été enregistré ou abandonné."""
    with _verrou:
        try:
            _chemin(f"brouillon_{doc_type}.json").unlink(missing_ok=True)
        except OSError:
            pass


# ------------------------------------------------------ comptage d'inventaire

# Un inventaire physique complet occupe une matinée : l'opérateur passe rayon
# par rayon avec le poste, et la session de comptage ne vivait qu'en mémoire
# dans le widget. Une fenêtre fermée par erreur, un plantage, une coupure de
# courant — et deux heures de comptage étaient à refaire, sans le moindre
# avertissement. Même mécanique que le brouillon de bon ci-dessus : écriture
# atomique après CHAQUE quantité saisie, et proposition de reprise à la
# réouverture de l'écran.


def _cle_fichier(cle: str) -> str:
    """Nom de fichier sûr à partir d'une clé de session (région, « general »).

    Les régions portent des accents et des espaces (« Grand'Anse ») : les
    poser tels quels dans un nom de fichier finit en erreur d'écriture selon
    le système de fichiers.
    """
    import re

    propre = re.sub(r"[^A-Za-z0-9]+", "_", str(cle or "general")).strip("_")
    return propre.lower() or "general"


def sauvegarder_comptage(cle: str, data: dict, sector: str | None = None) -> bool:
    """Enregistre le comptage en cours (un brouillon par session/région).

    `data` doit porter la clé `comptages` : {product_id: quantité comptée}.
    Rien n'est écrit si aucune quantité n'a encore été saisie — un brouillon
    vide n'a rien à reprendre et ferait poser une question inutile à la
    réouverture de l'écran.

    `sector` : même étiquette que `sauvegarder_brouillon`, même raison —
    `CLE_COMPTAGE` ("general") est un seul nom de fichier partagé par tous
    les secteurs du poste.
    """
    from datetime import datetime

    if not isinstance(data, dict) or not data.get("comptages"):
        return False
    donnees = dict(data)
    donnees.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    if not donnees.get("username"):
        donnees["username"] = _operateur_courant()
    donnees["sector"] = sector
    with _verrou:
        return _ecrire_json(
            _chemin(f"brouillon_comptage_{_cle_fichier(cle)}.json"), donnees)


def charger_comptage(cle: str, sector: str | None = None) -> dict | None:
    """Comptage interrompu pour cette session, ou None.

    Même fenêtre de validité que le brouillon de bon : au-delà, le stock a
    bougé et reprendre un comptage de la veille produirait des ajustements
    faux. Le brouillon périmé est effacé plutôt que réévalué à chaque
    ouverture.

    `sector` : même étiquette que `charger_brouillon`, même raison — pas
    effacé sur désaccord de secteur, juste traité comme inexistant.
    """
    chemin = _chemin(f"brouillon_comptage_{_cle_fichier(cle)}.json")
    with _verrou:
        donnees = _lire_json(chemin, None)
    if not (isinstance(donnees, dict) and donnees.get("comptages")):
        return None
    if sector is not None and donnees.get("sector") != sector:
        return None
    if brouillon_est_perime(donnees):
        effacer_comptage(cle)
        return None
    return donnees


def effacer_comptage(cle: str) -> None:
    """Supprime le brouillon : l'inventaire a été soumis ou abandonné."""
    with _verrou:
        try:
            _chemin(f"brouillon_comptage_{_cle_fichier(cle)}.json").unlink(
                missing_ok=True)
        except OSError:
            pass
