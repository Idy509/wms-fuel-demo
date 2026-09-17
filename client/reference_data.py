"""Référentiel régions/superviseurs, récupéré depuis le serveur.

Le serveur fait autorité (server/reference_data.py). Ce module en garde une
copie sur disque (local_cache.cache_referentiel.json), rafraîchie à chaque
connexion réussie : un poste hors ligne repart donc du dernier référentiel
réellement vu, pas d'une liste figée dans le code.

``_REPLI`` ne sert plus qu'au tout premier démarrage d'un poste qui ne s'est
jamais connecté au serveur — sans lui, les écrans de saisie seraient vides.
"""
import unicodedata

_REPLI: dict[str, list[str]] = {
    "Port-au-Prince": ["Joseph Wisbene", "Romain Watson", "Alex Tondereau", "Lacroix Jean Yguet"],
    "Fort-Liberté": ["EPS"],
    "Central": ["EPS"],
    "Gonaïves": ["EPS"],
    "Port-de-Paix": ["EPS"],
    "Saint-Marc": ["EPS"],
    "Cap-Haïtien": ["Fridlande Jeune"],
    "Jacmel": ["Mackendy Nicolas", "Adam Jean Flonel"],
    "Aquin": ["Gens Descart", "Remire Desire"],
    "Cayes": ["Jean Junior Medina"],
    "Jérémie": ["Cancoul Eastonly"],
    "Léogâne": ["Samson Jhonny", "Cedor Lionel", "Remire Desire"],
    "Carrefour": ["Lacroix Jean Yguet"],
}

_OPERATEURS_REPLI: list[str] = ["Idens Meranvil", "Zacharie Montinard"]

# Techniciens livrés individuellement (consigne bouteille nominative).
_TECHNICIENS_REPLI: list[str] = [
    "Anson Francois",
    "Cham Jean Raymond",
    "Frantzdy Bellevue",
    "Guerlin Desias",
    "Harry Saint-val",
    "Luckner Louis-Jeune",
    "Providence Sobner",
    "Wenslaire Sanon",
]

_courant: dict[str, list[str]] = dict(_REPLI)
_OPERATEURS_ENTREPOT: list[str] = list(_OPERATEURS_REPLI)
_TECHNICIENS: list[str] = list(_TECHNICIENS_REPLI)


def _cle(valeur: str) -> str:
    """Clé de comparaison sans accent ni casse.

    Identique à `_cle()` de server/reference_data.py : sans cette
    normalisation, 'Cap-Haitien' saisi côté client ne retrouvait pas
    'Cap-Haïtien' du serveur.
    """
    sans_accent = "".join(
        c for c in unicodedata.normalize("NFD", valeur) if unicodedata.category(c) != "Mn"
    )
    return sans_accent.strip().casefold()


def _appliquer(data: dict) -> bool:
    """Aligne l'état courant sur un référentiel (serveur ou cache disque)."""
    global _courant, _OPERATEURS_ENTREPOT, _TECHNICIENS
    data = data or {}
    # Présence de la CLÉ, jamais vérité du contenu (`if not par_region`) :
    # FUEL n'a aujourd'hui AUCUNE région (exclu de `SECTEURS_AVEC_REGION`,
    # voir docs/SECTEUR_FUEL.md), donc `supervisors_by_region` y vaut
    # toujours `{}` — une réponse VALIDE, pas un payload cassé. L'ancien test
    # de vérité renvoyait `False` pour CHAQUE session FUEL, qui retombait
    # alors sur le cache disque (voire le repli codé en dur au tout premier
    # démarrage) sans jamais recevoir la liste d'opérateurs du serveur — la
    # clé `warehouse_operators` de la même réponse n'était donc jamais lue.
    # Seule une clé réellement ABSENTE (`{}` en entier : serveur injoignable,
    # payload manifestement cassé) doit encore être rejetée.
    if "supervisors_by_region" not in data:
        return False
    par_region = data.get("supervisors_by_region") or {}
    _courant = {r: list(par_region.get(r, [])) for r in data.get("regions", par_region)}
    # `is not None`, jamais `if ops:` : le serveur renvoie délibérément une
    # liste VIDE pour FUEL (`WAREHOUSE_OPERATORS_PAR_SECTEUR["FUEL"] = []`,
    # server/reference_data.py) — un secteur sans liste d'opérateurs
    # entrepôt dédiée, pas une absence de réponse. Un test de vérité
    # traitait les deux pareil et laissait `_OPERATEURS_ENTREPOT` sur son
    # repli Consumables (["Idens Meranvil", "Zacharie Montinard"]) pour
    # TOUTE session FUEL — constaté par Idy509 le 2026-09-09. Absent de la
    # clé (`None`, cache écrit par une version antérieure) reste le seul cas
    # qui garde le repli en place.
    ops = data.get("warehouse_operators")
    if ops is not None:
        _OPERATEURS_ENTREPOT = list(ops)
    techs = data.get("technicians")
    if techs is not None:
        _TECHNICIENS = list(techs)
    return True


def charger_depuis_cache(sector: str | None = None) -> bool:
    """Restaure le dernier référentiel connu du poste POUR CE SECTEUR. False si aucun.

    Import différé : ce module doit rester chargeable sans le reste du client
    (outils et tests l'importent isolément).

    `sector` : voir `local_cache.charger_referentiel` — sans lui, un poste
    servant plusieurs secteurs pouvait retomber, hors ligne, sur les régions/
    superviseurs d'un AUTRE secteur que celui réellement connecté. Constaté
    par exploration le 2026-09-09, dans la continuité du correctif déjà posé
    sur `sauvegarder_produits`/`sauvegarder_contacts` le même jour — ce
    cache-ci ne l'avait pas reçu à ce moment-là.
    """
    try:
        import local_cache
        donnees = local_cache.charger_referentiel(sector=sector)
    except Exception:
        return False
    return _appliquer(donnees or {})


def _persister(data: dict, sector: str | None = None) -> None:
    try:
        import local_cache
        local_cache.sauvegarder_referentiel(data, sector=sector)
    except Exception:
        # Un cache non écrit ne doit jamais empêcher de travailler.
        pass


def charger_depuis_serveur(api, sector: str | None = None) -> bool:
    """Aligne le référentiel local sur celui du serveur. False si injoignable.

    Le référentiel obtenu est persisté : au prochain démarrage hors ligne, le
    poste repart de cette copie et non de ``_REPLI``.

    `sector` : secteur du compte qui vient de se connecter (`None` au tout
    premier chargement, avant toute connexion — le secteur n'est alors pas
    encore connu, et le cache est lu/écrit sans étiquette, comme avant ce
    correctif).
    """
    try:
        data = api.get_reference_regions()
    except Exception:
        # Serveur injoignable : on se rabat sur la dernière copie disque.
        charger_depuis_cache(sector=sector)
        return False
    try:
        data = dict(data or {})
        data["technicians"] = api.get_reference_technicians().get("technicians") or []
    except Exception:
        # Serveur plus ancien (endpoint absent) : le reste du référentiel a
        # bien été récupéré, ce n'est pas un échec de chargement.
        pass
    if not _appliquer(data):
        # Le serveur a répondu, mais avec un référentiel inutilisable (payload
        # vide, `supervisors_by_region` absent). Sans ce repli, on gardait
        # `_REPLI` — la liste figée dans le code — tout en renvoyant True,
        # donc en affirmant que le poste était aligné sur le serveur : les
        # régions et superviseurs affichés divergeaient en silence. On
        # retombe sur la dernière copie disque, comme quand le serveur est
        # injoignable, et on le signale par la valeur de retour.
        charger_depuis_cache(sector=sector)
        return False
    _persister(data, sector=sector)
    return True


def regions() -> list[str]:
    return list(_courant)


def supervisors_for(region: str) -> list[str]:
    if not region:
        return sorted({s for sups in _courant.values() for s in sups})
    return _courant.get(region, sorted({s for sups in _courant.values() for s in sups}))


def regions_for_supervisor(supervisor: str) -> list[str]:
    """Régions du superviseur sélectionné, dans l'ordre du référentiel."""
    cle = _cle(supervisor or "")
    if not cle:
        return []
    return [region for region, superviseurs in _courant.items()
            if any(_cle(nom) == cle for nom in superviseurs)]


def warehouse_operators() -> list[str]:
    return list(_OPERATEURS_ENTREPOT)


def technicians() -> list[str]:
    """Techniciens livrés individuellement, pour le champ du même nom."""
    return list(_TECHNICIENS)
