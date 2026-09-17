"""Référentiel des régions et superviseurs — source unique de vérité.

Le serveur fait autorité : le client récupère ces listes via /reference/regions
au lieu d'en garder une copie. Sans cela, deux référentiels divergent et les
agrégats par région deviennent faux.

Les libellés canoniques ci-dessous sont ceux qui seront stockés en base, quelle
que soit la casse ou l'accentuation saisie.

Le référentiel est désormais PAR SECTEUR (Consumables, FON...) : chaque
secteur a sa propre liste de régions et de superviseurs. Cela évite qu'un
secteur voie/utilise les régions d'un autre (ex. un admin FON ne doit pas
proposer les régions Consumables dans son formulaire de livraison), et
autorise deux secteurs à réutiliser le même nom de région (ex. "Carrefour"
existe pour Consumables ET pour FON) sans ambiguïté, du moment que l'appelant
précise son secteur.

Certaines fonctions restent utilisables SANS secteur (paramètre optionnel) :
ce sont celles appelées depuis la validation Pydantic des modèles qui n'ont
pas accès à la session (donc pas au secteur de l'auteur). Dans ce cas, elles
valident contre l'UNION de tous les secteurs — un contrôle orthographique
large, pas une autorisation. La vérification stricte « cette région
appartient-elle au bon secteur » se fait ensuite côté serveur, une fois le
secteur de l'auteur connu (voir server/app.py, `_secteur_du_compte`).
"""
import unicodedata

REGION_SUPERVISORS: dict[str, list[str]] = {
    "TP-WH": [],
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

# Équipe régionale FON (fichier "Team FON") : les 3 zones "Ouest Pap" du
# fichier source sont regroupées en une seule région "Port-au-Prince" (3
# superviseurs au choix), comme pour Consumables — livraison directe depuis
# le central FON, sans entrepôt régional. Carrefour/Arcahaie/North/South sont
# les 4 vrais entrepôts régionaux FON.
REGION_SUPERVISORS_FON: dict[str, list[str]] = {
    "TP-WH": [],
    "Port-au-Prince": ["Jimmy Elveus", "Boursiquot Luckner", "Witzer Jerome"],
    "Carrefour": ["Cesar Edson"],
    "Arcahaie": ["Jean Camy Daphenis"],
    "North": ["Ronald Napoleon"],
    "South": ["Sasi Kumar"],
}

# Équipe régionale RAN (liste « Region / Receiver » transmise par Idy509 le
# 12 septembre 2026) : un destinataire nommé par région, livraison directe —
# pas d'entrepôt régional (comme Port-au-Prince pour Consumables/FON), la
# marchandise est remise à ce destinataire sans étape de transit à confirmer.
# Deux régions peuvent partager le même destinataire (Arcahaie/Saint-Marc
# -> Kervens Dalzon, Cap-Haïtien/Fort-Liberté -> Edmond Wiguens), comme
# Remire Desire pour plusieurs régions Consumables.
REGION_SUPERVISORS_RAN: dict[str, list[str]] = {
    "TP-WH": [],
    "Jacmel": ["Alexis Evens"],
    "Arcahaie": ["Kervens Dalzon"],
    "Saint-Marc": ["Kervens Dalzon"],
    "Petit-Goâve": ["Carmin St. Louis"],
    "Thiotte": ["Charles Belair"],
    "Cap-Haïtien": ["Edmond Wiguens"],
    "Fort-Liberté": ["Edmond Wiguens"],
    "Hinche": ["Herard Jean"],
    "Léogâne": ["James Etienne"],
    "Port-de-Paix": ["Jean Mary Merisier"],
    "Gonaïves": ["Jeff Latorture"],
    "Jérémie": ["Eder Jean"],
    "Carrefour": ["Jeff Arrisda"],
    "Cayes": ["Gedeon Jean Benoit"],
}

REGION_SUPERVISORS_PAR_SECTEUR: dict[str, dict[str, list[str]]] = {
    "CONSUMABLES": REGION_SUPERVISORS,
    "FON": REGION_SUPERVISORS_FON,
    # Fuel n'a qu'une seule cuve centrale, pas d'entrepôt régional : sans
    # cette entrée, regions_for("FUEL") retomberait sur les 14 régions
    # Consumables (comportement par défaut de .get()).
    "FUEL": {"TP-WH": []},
    "RAN": REGION_SUPERVISORS_RAN,
}

# Secteurs qui utilisent le système région/superviseur pour une expédition
# ou un retour (région obligatoire, superviseur vérifié contre elle). Fuel
# (et le futur Spare tant qu'il n'a pas d'entrepôt régional) en est exclu :
# son bon s'organise autour d'un autre champ (véhicule).
SECTEURS_AVEC_REGION: set[str] = {"CONSUMABLES", "FON", "RAN"}

# Localités rattachées à une région : elles ne constituent pas une région de
# gestion à part. Saisies telles quelles, elles sont enregistrées sous leur
# région de rattachement, pour que les totaux restent justes. Propre à
# Consumables aujourd'hui (aucun alias FON pour l'instant).
ALIAS_REGIONS: dict[str, str] = {
    "Petit-Goâve": "Aquin",
    "Nippes": "Aquin",
}

ALIAS_REGIONS_PAR_SECTEUR: dict[str, dict[str, str]] = {
    "CONSUMABLES": ALIAS_REGIONS,
    "FON": {},
}

# TP-WH est l'entrepôt central, pas une région de livraison terrain.
# Il figure dans REGION_SUPERVISORS(_FON) pour la normalisation, mais
# n'apparaît pas dans la liste des régions sélectionnables par l'opérateur.
REGIONS: list[str] = [r for r in REGION_SUPERVISORS if r != "TP-WH"]
REGIONS_FON: list[str] = [r for r in REGION_SUPERVISORS_FON if r != "TP-WH"]
REGIONS_RAN: list[str] = [r for r in REGION_SUPERVISORS_RAN if r != "TP-WH"]

REGIONS_PAR_SECTEUR: dict[str, list[str]] = {
    "CONSUMABLES": REGIONS,
    "FON": REGIONS_FON,
    "RAN": REGIONS_RAN,
}

# Union de toutes les régions, tous secteurs confondus : sert uniquement au
# contrôle orthographique fait par la validation Pydantic (qui ne connaît pas
# le secteur de l'auteur). Ne PAS utiliser pour autoriser une action.
TOUTES_REGIONS: list[str] = sorted({r for regions in REGIONS_PAR_SECTEUR.values() for r in regions})

ALL_SUPERVISORS: list[str] = sorted({
    s for sups in REGION_SUPERVISORS.values() for s in sups
} | {
    s for sups in REGION_SUPERVISORS_FON.values() for s in sups
} | {
    s for sups in REGION_SUPERVISORS_RAN.values() for s in sups
})

# --------------------------------------------------- entrepôts régionaux
# Port-au-Prince est desservi directement par l'entrepôt central : une
# expédition vers PAP reste une remise immédiate au superviseur, sans transit.
# Les autres régions disposent d'un entrepôt régional : la marchandise y
# voyage, et n'est créditée qu'une fois la réception confirmée sur place.
# Même convention pour FON.
#
# Liste statique, comme REGION_SUPERVISORS : il n'y a pas de configuration
# d'entrepôt à gérer en base tant que la liste tient en une ligne de Python.
REGION_ENTREPOT_CENTRAL = "Port-au-Prince"

REGIONS_AVEC_ENTREPOT: list[str] = [r for r in REGIONS if r != REGION_ENTREPOT_CENTRAL]
REGIONS_AVEC_ENTREPOT_FON: list[str] = [r for r in REGIONS_FON if r != REGION_ENTREPOT_CENTRAL]

REGIONS_AVEC_ENTREPOT_PAR_SECTEUR: dict[str, list[str]] = {
    "CONSUMABLES": REGIONS_AVEC_ENTREPOT,
    "FON": REGIONS_AVEC_ENTREPOT_FON,
    # RAN a des régions (REGIONS_RAN) mais aucun entrepôt régional : chaque
    # région est livrée directement à son destinataire (REGION_SUPERVISORS_RAN),
    # comme Port-au-Prince pour Consumables/FON — pas de transit à confirmer.
    # Entrée explicite : sans elle, le board RAN afficherait à tort les
    # entrepôts régionaux Consumables (fallback silencieux de .get()).
    "RAN": [],
}

TOUTES_REGIONS_AVEC_ENTREPOT: list[str] = sorted(
    {r for regions in REGIONS_AVEC_ENTREPOT_PAR_SECTEUR.values() for r in regions}
)


def regions_for(sector: str | None) -> list[str]:
    """Régions du secteur donné (central inclus). Secteur inconnu -> Consumables."""
    return list(REGIONS_PAR_SECTEUR.get(sector, REGIONS))


def regions_avec_entrepot_for(sector: str | None) -> list[str]:
    """Régions du secteur donné qui ont un entrepôt régional (central exclu)."""
    return list(REGIONS_AVEC_ENTREPOT_PAR_SECTEUR.get(sector, REGIONS_AVEC_ENTREPOT))


def region_valide_pour_secteur(region: str | None, sector: str | None) -> bool:
    """Vrai si la région (canonique) appartient bien au secteur donné."""
    return bool(region) and region in regions_for(sector)


def a_entrepot_regional(region: str | None, sector: str | None = None) -> bool:
    """Vrai si la région dispose d'un entrepôt régional (transit en deux temps).

    Sans `sector` (compat historique / validation Pydantic sans contexte de
    secteur) : vérifie l'UNION de tous les secteurs — contrôle large, pas une
    autorisation. Avec `sector` : vérifie strictement dans CE secteur.
    """
    if sector is not None:
        return (region or "") in regions_avec_entrepot_for(sector)
    return (region or "") in TOUTES_REGIONS_AVEC_ENTREPOT


WAREHOUSE_OPERATORS: list[str] = ["Idens Meranvil", "Zacharie Montinard"]

# Techniciens terrain livrés individuellement en eau distillée. Le suivi de
# consigne par région ne suffit pas à savoir QUI doit rendre ses bouteilles :
# ce champ, optionnel sur une expédition, nomme la personne.
# Liste statique, comme les superviseurs : il n'y a pas de gestion de tiers à
# faire ici (les contacts restent réservés aux fournisseurs).
TECHNICIANS: list[str] = sorted([
    "Anson Francois",
    "Cham Jean Raymond",
    "Frantzdy Bellevue",
    "Guerlin Desias",
    "Harry Saint-val",
    "Luckner Louis-Jeune",
    "Providence Sobner",
    "Wenslaire Sanon",
])

# Personnel et consigne bouteille n'ont de sens que pour Consumables. FON
# n'a ni superviseur régional ni technicien livré en eau : lui envoyer ces
# listes reviendrait à proposer des noms d'un autre secteur dans son
# formulaire de réception/inventaire (bug réel constaté : Anderson, admin
# FON, voyait Idens Meranvil et Zacharie Montinard — deux opérateurs
# Consumables — dans son sélecteur d'inventaire).
WAREHOUSE_OPERATORS_PAR_SECTEUR: dict[str, list[str]] = {
    "CONSUMABLES": WAREHOUSE_OPERATORS,
    "FON": ["Anderson Mathieu"],
    # Fuel n'a ni superviseur régional ni technicien livré en eau non plus :
    # « qui a dispensé le carburant » est déjà porté par `created_by` (le
    # compte connecté), pas besoin d'une liste dédiée ici.
    "FUEL": [],
    # RAN : même opérateur central que FON (Anderson Mathieu gère les deux
    # secteurs pour le warehouse central), mais deux comptes et deux
    # catalogues séparés — jamais mélangés.
    "RAN": ["Anderson Mathieu"],
}

TECHNICIANS_PAR_SECTEUR: dict[str, list[str]] = {
    "CONSUMABLES": TECHNICIANS,
    "FON": [],
    "FUEL": [],
    "RAN": [],
}


def warehouse_operators_for(sector: str) -> list[str]:
    return list(WAREHOUSE_OPERATORS_PAR_SECTEUR.get(sector, WAREHOUSE_OPERATORS))


def technicians_for(sector: str) -> list[str]:
    return list(TECHNICIANS_PAR_SECTEUR.get(sector, TECHNICIANS))


def _cle(valeur: str) -> str:
    """Clé de comparaison : sans accent, sans casse, sans espaces superflus.

    Permet de reconnaître 'aquin', 'AQUIN', 'Cap-Haitien' et 'Cap-Haïtien'.
    """
    sans_accent = "".join(
        c for c in unicodedata.normalize("NFD", valeur) if unicodedata.category(c) != "Mn"
    )
    return sans_accent.strip().casefold()


def _construire_index(regions: list[str], alias: dict[str, str]) -> dict[str, str]:
    index = {_cle(r): r for r in regions}
    index.update({_cle(a): region for a, region in alias.items()})
    return index


_INDEX_PAR_SECTEUR: dict[str, dict[str, str]] = {
    secteur: _construire_index(REGIONS_PAR_SECTEUR[secteur],
                               ALIAS_REGIONS_PAR_SECTEUR.get(secteur, {}))
    for secteur in REGIONS_PAR_SECTEUR
}
# Union tous secteurs confondus : sert au contrôle orthographique Pydantic
# (pas de contexte de secteur disponible à ce niveau). Construite en DEUX
# passes — régions littérales d'abord, alias ensuite — pour qu'un alias
# gagne toujours en cas de collision entre secteurs. Cas réel : « Petit-Goâve »
# est un ALIAS Consumables (-> Aquin) mais une VRAIE région RAN à part
# entière ; sans cette priorité, l'ordre d'insertion des secteurs déciderait
# arbitrairement laquelle des deux significations l'emporte ici. Sans
# secteur connu, ce n'est qu'un contrôle orthographique large de toute façon
# (voir docstring de `normaliser_region`) — l'appel avec secteur (fait par
# `server/app.py::create_document` une fois le secteur de l'auteur connu)
# reste la seule vérification qui fait autorité, et résout correctement les
# DEUX sens depuis son propre index par secteur (`_INDEX_PAR_SECTEUR`).
_INDEX_TOUS: dict[str, str] = {}
for _secteur_idx in REGIONS_PAR_SECTEUR:
    _INDEX_TOUS.update({_cle(r): r for r in REGIONS_PAR_SECTEUR[_secteur_idx]})
for _secteur_idx in ALIAS_REGIONS_PAR_SECTEUR:
    _INDEX_TOUS.update({
        _cle(a): region for a, region in ALIAS_REGIONS_PAR_SECTEUR[_secteur_idx].items()
    })

_SUPERVISOR_INDEX = {_cle(supervisor): supervisor for supervisor in ALL_SUPERVISORS}
# Union tous secteurs confondus, comme `_INDEX_TOUS` pour les régions : le
# contrôle orthographique Pydantic n'a pas le secteur de l'auteur. Construit
# depuis `TECHNICIANS_PAR_SECTEUR` (pas la seule liste `TECHNICIANS`,
# Consumables) — sinon un futur technicien FON/FUEL, une fois sa liste
# renseignée, serait rejeté ici avant même d'atteindre la vérification
# stricte de secteur.
_TECHNICIAN_INDEX = {
    _cle(technicien): technicien
    for techniciens in TECHNICIANS_PAR_SECTEUR.values()
    for technicien in techniciens
}

# Clés (casefold, sans accent) ambiguës ENTRE secteurs : une même clé qui est
# à la fois une région littérale dans un secteur ET un alias dans un autre
# (ex. « petit-goave » : vraie région RAN, mais alias Consumables -> Aquin).
# Sans secteur connu, `normaliser_region` ne peut PAS choisir laquelle des
# deux significations retenir — la choisir quand même (comme le ferait une
# simple union) transformerait irréversiblement la valeur AVANT que le
# secteur ne soit connu (Pydantic n'a pas cette info), et la vraie
# signification serait perdue pour la suite du traitement. Constaté en réel
# le 13 septembre 2026 : un bon RAN vers « Petit-Goâve » se voyait rejeté
# ensuite pour « Aquin », une région qui n'existe pas pour RAN.
_CLES_LITERALES: set[str] = {
    _cle(r) for regions in REGIONS_PAR_SECTEUR.values() for r in regions
}
_CLES_ALIAS: set[str] = {
    _cle(a) for aliases in ALIAS_REGIONS_PAR_SECTEUR.values() for a in aliases
}
_CLES_REGION_AMBIGUES: set[str] = _CLES_LITERALES & _CLES_ALIAS


def normaliser_region(valeur: str | None, sector: str | None = None) -> str | None:
    """Renvoie le libellé canonique, ou None si la valeur est vide.

    Sans `sector` : reconnaît une région de N'IMPORTE QUEL secteur (contrôle
    orthographique large, utilisé par la validation Pydantic qui n'a pas le
    contexte de secteur). Avec `sector` : n'accepte que les régions DE CE
    secteur — lève ValueError sinon, y compris si la région existe mais dans
    un AUTRE secteur.

    Lève ValueError si la région est inconnue — mieux vaut refuser la saisie
    que polluer la base avec une variante qui faussera les agrégats.

    Cas particulier SANS secteur : si la clé est ambiguë entre secteurs
    (`_CLES_REGION_AMBIGUES`), la valeur n'est PAS canonicalisée — juste
    nettoyée (espaces) — pour ne rien trancher avant que le secteur ne soit
    connu. L'appelant (Pydantic, contrôle orthographique) valide juste que
    la valeur EST reconnue quelque part ; la vraie canonicalisation se fait
    en rappelant cette fonction AVEC le secteur, une fois connu (voir
    `server/app.py::create_document`).
    """
    if valeur is None:
        return None
    valeur = valeur.strip()
    if not valeur:
        return None
    if sector is not None:
        index = _INDEX_PAR_SECTEUR.get(sector, _INDEX_PAR_SECTEUR.get("CONSUMABLES", {}))
        regions_possibles = regions_for(sector)
        canonique = index.get(_cle(valeur))
        if canonique is None:
            raise ValueError(
                f"Région inconnue : {valeur!r}. Valeurs attendues : {', '.join(regions_possibles)}"
            )
        return canonique
    cle = _cle(valeur)
    if cle in _CLES_REGION_AMBIGUES:
        return valeur
    canonique = _INDEX_TOUS.get(cle)
    if canonique is None:
        raise ValueError(
            f"Région inconnue : {valeur!r}. Valeurs attendues : {', '.join(TOUTES_REGIONS)}"
        )
    return canonique


def supervisors_for(region: str | None, sector: str | None = None) -> list[str]:
    # Copie et non la liste vivante du dictionnaire : ces listes sont
    # exposées telles quelles par /reference/regions et réutilisées par
    # plusieurs appelants. Rendre la liste interne laisserait n'importe
    # lequel d'entre eux corrompre le référentiel du processus en place.
    if sector is not None:
        table = REGION_SUPERVISORS_PAR_SECTEUR.get(sector, REGION_SUPERVISORS)
        if not region:
            return sorted({s for sups in table.values() for s in sups})
        try:
            canonique = normaliser_region(region, sector)
        except ValueError:
            return []
        return list(table.get(canonique, []))

    # Mode large (pas de secteur connu, ex. validation Pydantic) : union des
    # superviseurs de TOUS les secteurs qui connaissent cette région. Sans
    # ça, une région au nom partagé entre deux secteurs (ex. "Carrefour",
    # Consumables ET FON) ne renverrait que les superviseurs du premier
    # secteur trouvé, et rejetterait à tort un superviseur légitime de
    # l'autre secteur.
    if not region:
        return list(ALL_SUPERVISORS)
    try:
        canonique = normaliser_region(region, None)
    except ValueError:
        return list(ALL_SUPERVISORS)
    resultat: list[str] = []
    for table in REGION_SUPERVISORS_PAR_SECTEUR.values():
        resultat.extend(table.get(canonique, []))
    return resultat or list(ALL_SUPERVISORS)


def regions_for_supervisor(supervisor: str | None, sector: str | None = None) -> list[str]:
    """Renvoie les régions auxquelles un superviseur est rattaché.

    Un superviseur présent dans une seule région permet de pré-remplir la
    région dans le formulaire. Deux superviseurs (Lacroix Jean Yguet et Remire
    Desire) interviennent dans plusieurs régions : leur région ne doit jamais
    être devinée.
    """
    if not supervisor:
        return []
    cle = _cle(supervisor)
    tables = [REGION_SUPERVISORS_PAR_SECTEUR.get(sector, REGION_SUPERVISORS)] if sector \
        else list(REGION_SUPERVISORS_PAR_SECTEUR.values())
    resultat = []
    for table in tables:
        resultat.extend(region for region, superviseurs in table.items()
                        if any(_cle(nom) == cle for nom in superviseurs))
    return resultat


def normaliser_supervisor(valeur: str | None) -> str | None:
    """Renvoie le nom canonique du superviseur, sans créer de doublon.

    Contrôle orthographique large (tous secteurs confondus) : la validation
    Pydantic n'a pas le contexte de secteur. La vérification stricte
    « ce superviseur correspond-il à cette région pour ce secteur » se fait
    via `supervisor_est_valide(region, supervisor, sector)`.
    """
    if valeur is None:
        return None
    valeur = valeur.strip()
    if not valeur:
        return None
    canonique = _SUPERVISOR_INDEX.get(_cle(valeur))
    if canonique is None:
        raise ValueError(f"Superviseur inconnu : {valeur!r}")
    return canonique


def normaliser_technicien(valeur: str | None) -> str | None:
    """Renvoie le nom canonique du technicien, ou None si vide.

    Même principe que le superviseur : un nom hors référentiel est refusé
    plutôt que stocké, sinon le solde de consigne par technicien se scinderait
    entre deux orthographes de la même personne.
    """
    if valeur is None:
        return None
    valeur = valeur.strip()
    if not valeur:
        return None
    canonique = _TECHNICIAN_INDEX.get(_cle(valeur))
    if canonique is None:
        raise ValueError(
            f"Technicien inconnu : {valeur!r}. Valeurs attendues : "
            f"{', '.join(sorted(_TECHNICIAN_INDEX.values()))}"
        )
    return canonique


def supervisor_est_valide(region: str | None, supervisor: str | None,
                          sector: str | None = None) -> bool:
    """Vérifie le couple région/superviseur d'une expédition, pour un secteur donné.

    Sans `sector` : vérifie l'union de tous les secteurs (compat historique).
    """
    if not region or not supervisor:
        return True
    return any(_cle(nom) == _cle(supervisor) for nom in supervisors_for(region, sector))


def technicien_valide_pour_secteur(technicien: str | None, sector: str | None = None) -> bool:
    """Vrai si le technicien (canonique) appartient bien au secteur donné.

    Même principe que `region_valide_pour_secteur` : la validation Pydantic
    (`normaliser_technicien`, sans contexte de secteur) ne fait qu'un
    contrôle orthographique large contre l'union de tous les secteurs — un
    bon FON pourrait sinon porter un technicien Consumables (ou l'inverse).
    La vérification stricte se fait ici, une fois le secteur de l'auteur
    connu (voir server/app.py::create_document), toujours appelée avec un
    `sector` concret — jamais None en pratique.
    """
    if not technicien:
        return True
    return any(_cle(nom) == _cle(technicien) for nom in technicians_for(sector))


# ------------------------------------------------------------------- Fuel
#
# Fuel n'a ni région ni entrepôt régional (une seule cuve centrale) : ses
# livraisons s'organisent autour d'un véhicule et d'un « projet » — une
# étiquette budgétaire interne (qui consomme le carburant), sans rapport
# avec les secteurs applicatifs de même nom (le « projet FON » d'un véhicule
# n'a aucun lien avec le compte/catalogue FON).
FUEL_PROJECTS: list[str] = ["Contractor", "FON", "Hybrid", "Location", "OOS",
                            "Power", "RAN", "SGA", "Support", "Warehouse"]

_FUEL_PROJECT_INDEX = {p.casefold(): p for p in FUEL_PROJECTS}


def normaliser_projet_fuel(valeur: str | None) -> str | None:
    """Canonicalise le Projet s'il est connu (insensible à la casse), sinon
    renvoie la valeur telle quelle (nettoyée des espaces superflus).

    Contrairement à `normaliser_region`, ne lève JAMAIS : le Projet est une
    étiquette budgétaire, pas une autorisation — un nom encore inconnu
    (nouveau projet) doit pouvoir être saisi sans blocage.
    """
    if valeur is None:
        return None
    valeur = valeur.strip()
    if not valeur:
        return None
    return _FUEL_PROJECT_INDEX.get(valeur.casefold(), valeur)
