"""Précision numérique des quantités de stock.

Les quantités sont accumulées par additions successives (`current_stock =
current_stock + delta`) sur des milliers de mouvements. En flottant IEEE 754,
ces additions ne s'annulent jamais exactement : un stock revenu à zéro après
des centaines d'allers-retours peut finir à 1.4e-14 au lieu de 0, et une
comparaison `current_stock <= min_stock` devient imprévisible.

Toute écriture arithmétique de current_stock est arrondie à PRECISION
décimales — au lieu de laisser l'erreur s'accumuler indéfiniment, chaque
écriture la ramène à zéro. PRECISION=3 couvre le cas réel (gallons d'huile en
dixièmes/centièmes) tout en restant très au-dessus du bruit flottant, donc la
tolérance de réconciliation (1e-9) reste valide sans modification.
"""

PRECISION = 3


def arrondir(valeur: float) -> float:
    return round(valeur, PRECISION)


def arrondir_saisie(valeur: float) -> float:
    """Arrondit une quantité VENANT DE L'EXTÉRIEUR (JSON, Excel).

    Refuse l'infini et NaN. Ce n'est pas de la paranoïa de typage : le décodeur
    JSON de la bibliothèque standard accepte `1e999`, `Infinity` et `NaN`, et
    aucune contrainte Pydantic ne les arrête (`inf >= 0` est vrai, `inf > 0`
    aussi). Une seule quantité infinie stockée dans `products.current_stock`
    empoisonne DÉFINITIVEMENT tous les agrégats : SQLite conserve `Inf`, donc
    `SUM(current_stock)` du tableau de bord vaut `Inf` pour toujours, et aucun
    ajustement ne peut le corriger (`compté - Inf` = `-Inf`). NaN, lui, est
    écrit comme NULL par sqlite3 et viole la contrainte NOT NULL.

    `arrondir()` reste volontairement pur : il est appelé sur des valeurs déjà
    en base, où lever une exception transformerait un simple affichage en 500.
    """
    valeur = float(valeur)
    # Deux comparaisons plutôt que math.isfinite : NaN != NaN, Inf - Inf = NaN.
    if valeur != valeur or valeur in (float("inf"), float("-inf")):
        raise ValueError(
            "Quantité invalide (valeur infinie ou indéterminée). "
            "Vérifie la cellule ou le champ saisi."
        )
    return round(valeur, PRECISION)
