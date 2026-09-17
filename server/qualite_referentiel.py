"""Santé du référentiel produits : repérage des défauts de saisie évidents.

Aucun blocage, aucune correction automatique. Le catalogue est saisi à la
main, sur plusieurs années, par plusieurs personnes : il accumule des fiches
sans catégorie, sans coût, ou saisies deux fois sous un nom légèrement
différent. Rien de tout cela n'empêche l'exploitation — mais tout cela fausse
les regroupements, la valorisation du stock et les recherches.

Ce module se contente de DIRE ce qui cloche. L'administrateur ouvre la fiche
et corrige (ou pas) : c'est lui qui sait si « Huile 15W40 » et « Huile 15W-40 »
sont le même produit ou deux références réellement distinctes.

La détection de doublon se fait à deux niveaux :

- `doublon_designation` — clé normalisée EXACTE (casse, accents, ponctuation
  et espaces ignorés) : « Huile 15W-40 » contre « huile 15w40 ».
- `doublon_probable` — rapprochement flou, à une ou deux lettres près :
  « Filtre a huile » contre « Filtre à huile », « Bougie NGK » contre
  « Bougie NKG ». C'est le vrai doublon de saisie, celui qui coupe un stock
  en deux sans que personne ne s'en aperçoive.

Le flou est borné par la même règle que la recherche du poste (0 faute en
dessous de 4 caractères, 1 jusqu'à 6, 2 au-delà), et les chiffres d'une
désignation ne sont jamais traités comme une faute : « Filtre à huile 10 » et
« Filtre à huile 12 » restent deux articles. Deux fiches aux noms voisins
mais dont les codes n'ont aucun lien ET dont les unités diffèrent ne sont pas
signalées : ce sont deux articles réellement distincts qui partagent un mot.
"""
import re
import unicodedata

# Codes stables (jamais affichés tels quels) -> libellé français affiché.
# Le client traduit ces libellés via i18n.t() ; les codes, eux, servent au
# filtrage et ne changent pas avec la langue.
DEFAUT_LIBELLES: dict[str, str] = {
    "categorie_absente": "Catégorie non renseignée",
    "capacite_bidon_absente": "Produit en volume sans capacité de bidon",
    "cout_absent": "Coût unitaire non renseigné",
    "doublon_designation": "Désignation très proche d'un autre produit actif",
    "doublon_probable": "Désignation presque identique à un autre produit actif",
    "nom_absent": "Désignation vide",
    "nom_egal_sku": "Désignation identique au code SKU",
}


def _cle_comparaison(texte: str | None) -> str:
    """Clé de comparaison insensible à la casse, aux accents et aux espaces.

    'Huile  15W-40 ' et 'huile 15w40' donnent la même clé. Les caractères non
    alphanumériques sont supprimés plutôt que remplacés par un espace : c'est
    ce qui rapproche '15W-40' de '15W40'.
    """
    if not texte:
        return ""
    sans_accent = unicodedata.normalize("NFKD", str(texte))
    sans_accent = "".join(c for c in sans_accent if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", sans_accent.lower())


# ----------------------------------------- rapprochement flou des désignations
#
# La clé normalisée exacte ci-dessus n'attrape que « Huile 15W-40 » contre
# « huile 15w40 ». Elle laisse passer le vrai doublon de saisie : « Filtre à
# huile » et « Filtre a huile », « Bougie NGK » et « Bougie NKG » — une lettre
# de travers, deux fiches, deux stocks, et une réconciliation fausse sur les
# deux.
#
# La règle de tolérance est celle de la recherche floue du poste
# (`client/recherche.py`) : 0 faute en dessous de 4 caractères, 1 jusqu'à 6,
# 2 au-delà. Elle est RÉÉCRITE ici plutôt qu'importée : `client/` n'est pas sur
# le `sys.path` du serveur (les deux paquets ont chacun leur `reference_data`,
# et `serveur.spec` n'embarque que `server/`). Un test compare les deux
# implémentations valeur par valeur pour qu'elles ne divergent pas.


def _budget_fautes(texte: str) -> int:
    if len(texte) < 4:
        return 0
    if len(texte) < 7:
        return 1
    return 2


def _normaliser(texte) -> str:
    """Minuscules, sans accents, espaces compactés. Ponctuation conservée."""
    if texte is None:
        return ""
    decompose = unicodedata.normalize("NFD", str(texte).lower())
    sans_accent = "".join(c for c in decompose
                          if unicodedata.category(c) != "Mn")
    return " ".join(sans_accent.split())


def distance_bornee(a: str, b: str, budget: int) -> int:
    """Levenshtein abandonnée dès qu'elle dépasse `budget` (renvoie budget+1)."""
    if abs(len(a) - len(b)) > budget:
        return budget + 1
    if a == b:
        return 0
    precedente = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        courante = [i]
        minimum = i
        for j, cb in enumerate(b, start=1):
            cout = 0 if ca == cb else 1
            valeur = min(precedente[j] + 1,
                         courante[j - 1] + 1,
                         precedente[j - 1] + cout)
            courante.append(valeur)
            if valeur < minimum:
                minimum = valeur
        if minimum > budget:
            return budget + 1
        precedente = courante
    return precedente[-1]


def _chiffres(texte: str) -> str:
    return "".join(c for c in texte if c.isdigit())


def _quasi_identiques_normalises(a: str, b: str) -> bool:
    """Cœur du rapprochement, sur des noms DÉJÀ normalisés.

    Séparé de `noms_quasi_identiques` pour ne pas renormaliser des milliers de
    fois les mêmes chaînes dans la boucle de comparaison.
    """
    if not a or not b:
        return False
    # Les chiffres d'une désignation ne sont jamais une faute de frappe : ils
    # numérotent la référence. « Filtre à huile 10 » et « Filtre à huile 12 »
    # sont à une lettre l'un de l'autre et sont pourtant deux articles bien
    # distincts — c'est précisément le bruit qui rendrait l'écran inutile.
    if _chiffres(a) != _chiffres(b):
        return False
    budget = _budget_fautes(a if len(a) <= len(b) else b)
    if not budget:
        # Moins de 4 caractères : « AF1 » et « AF2 » sont deux produits, pas
        # une faute de frappe. Aucune tolérance, comme dans la recherche.
        return False
    return distance_bornee(a, b, budget) <= budget


def noms_quasi_identiques(nom_a: str, nom_b: str) -> bool:
    """Vrai si deux désignations ne diffèrent que d'une ou deux lettres."""
    return _quasi_identiques_normalises(_normaliser(nom_a), _normaliser(nom_b))


def _references_distinctes(a: dict, b: dict) -> bool:
    """Vrai si tout indique deux produits RÉELLEMENT différents.

    Deux fiches aux noms voisins mais dont les codes n'ont rien à voir ET dont
    les unités diffèrent ne sont pas un doublon de saisie : c'est « Huile
    moteur » en gallons et « Huile moteur » en pièces (bidon scellé), deux
    articles distincts qui partagent un mot. Les signaler chaque semaine à
    l'administrateur transformerait l'écran en bruit de fond.

    Les deux conditions sont cumulatives à dessein : deux fiches de MÊME unité
    aux noms voisins restent suspectes quels que soient leurs codes.
    """
    unite_a = _normaliser(a.get("unit"))
    unite_b = _normaliser(b.get("unit"))
    if not unite_a or not unite_b or unite_a == unite_b:
        return False
    return _skus_tres_differents(a.get("sku"), b.get("sku"))


def _skus_tres_differents(sku_a, sku_b) -> bool:
    """Vrai si les deux codes n'ont visiblement aucun lien.

    « AF-100 » et « AF100 » sont le même code mal saisi ; « AF-100 » et
    « HUI-22 » désignent deux familles différentes.
    """
    a = re.sub(r"[^a-z0-9]", "", _normaliser(sku_a))
    b = re.sub(r"[^a-z0-9]", "", _normaliser(sku_b))
    if not a or not b:
        return False
    if a in b or b in a:
        return False
    return distance_bornee(a, b, 2) > 2


def _cout_absent(valeur) -> bool:
    """Vrai si le coût unitaire est vide ou nul.

    Un coût à 0 est traité comme absent : il n'existe pas de consommable
    gratuit dans ce catalogue, et une valorisation de stock calculée dessus
    donnerait un total faux sans qu'aucune erreur ne le signale.
    """
    try:
        return valeur is None or float(valeur) <= 0
    except (TypeError, ValueError):
        return True


def _capacite_absente(valeur) -> bool:
    try:
        return valeur is None or float(valeur) <= 0
    except (TypeError, ValueError):
        return True


def _quasi_doublons(produits: list[dict]) -> dict[int, list[str]]:
    """Rapprochement flou : position du produit -> SKU de ses quasi-homonymes.

    Les paires déjà signalées comme doublon exact (`doublon_designation`) sont
    écartées : les afficher deux fois ne dirait rien de plus à l'administrateur.
    """
    normalises = [_normaliser(p.get("name")) for p in produits]
    cles = [_cle_comparaison(p.get("name")) for p in produits]

    # Index par longueur de nom. La distance est bornée à 2 : deux noms dont
    # les longueurs diffèrent de plus de 2 ne peuvent pas se rapprocher. Sans
    # ce filtre, un catalogue de quelques milliers de fiches produirait des
    # millions de comparaisons à chaque ouverture de l'écran.
    par_longueur: dict[int, list[int]] = {}
    for i, nom in enumerate(normalises):
        if nom:
            par_longueur.setdefault(len(nom), []).append(i)

    resultat: dict[int, list[str]] = {}
    for i, nom in enumerate(normalises):
        if not nom:
            continue
        # Chaque paire n'est examinée qu'une fois : on ne regarde que les noms
        # de longueur >= la sienne, et à longueur égale que les indices plus
        # grands.
        for longueur in range(len(nom), len(nom) + 3):
            for j in par_longueur.get(longueur, ()):
                if j <= i:
                    continue
                if cles[i] and cles[i] == cles[j]:
                    continue
                if not _quasi_identiques_normalises(nom, normalises[j]):
                    continue
                if _references_distinctes(produits[i], produits[j]):
                    continue
                resultat.setdefault(i, []).append(produits[j].get("sku") or "")
                resultat.setdefault(j, []).append(produits[i].get("sku") or "")
    return resultat


def analyser_produits(produits: list[dict]) -> list[dict]:
    """Renvoie les seuls produits en défaut, chacun avec ses défauts listés.

    `produits` : les produits ACTIFS (non archivés). Un produit archivé n'est
    plus proposé nulle part : signaler sa fiche incomplète n'aurait aucun
    intérêt et noierait les vrais défauts.
    """
    # Regroupement par désignation normalisée pour la détection de doublon.
    par_cle: dict[str, list[dict]] = {}
    for produit in produits:
        cle = _cle_comparaison(produit.get("name"))
        if cle:
            par_cle.setdefault(cle, []).append(produit)

    quasi = _quasi_doublons(produits)

    resultat: list[dict] = []
    for indice, produit in enumerate(produits):
        defauts: list[str] = []
        doublons: list[str] = []

        nom = (produit.get("name") or "").strip()
        sku = (produit.get("sku") or "").strip()

        if not nom:
            defauts.append("nom_absent")
        elif _cle_comparaison(nom) == _cle_comparaison(sku) and sku:
            defauts.append("nom_egal_sku")

        if not (produit.get("category") or "").strip():
            defauts.append("categorie_absente")

        if (produit.get("unit_type") or "") == "volume" \
                and _capacite_absente(produit.get("bidon_capacity")):
            defauts.append("capacite_bidon_absente")

        if _cout_absent(produit.get("unit_cost")):
            defauts.append("cout_absent")

        cle = _cle_comparaison(nom)
        if cle:
            jumeaux = [p for p in par_cle.get(cle, []) if p is not produit]
            if jumeaux:
                defauts.append("doublon_designation")
                doublons = [(p.get("sku") or "") for p in jumeaux]

        proches = quasi.get(indice) or []
        if proches:
            defauts.append("doublon_probable")

        if not defauts:
            continue

        entree = dict(produit)
        entree["defauts"] = defauts
        entree["defauts_libelles"] = [DEFAUT_LIBELLES[d] for d in defauts]
        entree["doublons_sku"] = doublons
        entree["doublons_probables_sku"] = proches
        resultat.append(entree)

    # Les fiches les plus abîmées d'abord : c'est par là que l'admin commence.
    resultat.sort(key=lambda p: (-len(p["defauts"]), (p.get("name") or "").lower()))
    return resultat
