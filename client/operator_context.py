"""Synchronisation sûre du nom d'opérateur dans les formulaires ouverts."""


def remplacer_operateur_defaut(valeur_actuelle: str, ancien: str, nouveau: str) -> str:
    """Remplace seulement la valeur automatique, jamais une saisie manuelle."""
    if valeur_actuelle.strip() == ancien.strip():
        return nouveau
    return valeur_actuelle
