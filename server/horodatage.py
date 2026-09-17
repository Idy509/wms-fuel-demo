"""Source unique de l'heure, en heure locale de l'entrepôt.

SQLite `datetime('now')` renvoie de l'UTC. Le client, lui, envoyait l'heure
locale. Les deux cohabitaient dans la même colonne, avec 4 heures d'écart en
Haïti : un bon saisi à 20 h locale était enregistré au lendemain, donc
rattaché au mauvais jour et parfois au mauvais mois de rapport.

Le serveur tourne dans l'entrepôt : son heure locale EST l'heure de référence
métier. C'est elle qui doit être stockée, puisque c'est celle que l'opérateur
saisit et celle sur laquelle les cycles mensuels sont calculés.

WMS_UTC_OFFSET permet de forcer un décalage (ex. "-05:00") si le serveur est
hébergé dans un autre fuseau que l'entrepôt.
"""
import os
import re
from datetime import datetime, timedelta, timezone

FORMAT = "%Y-%m-%d %H:%M:%S"
_FORMATS_ACCEPTES = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


def _decalage_force() -> timezone | None:
    brut = (os.getenv("WMS_UTC_OFFSET") or "").strip()
    if not brut:
        return None
    correspondance = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", brut)
    if not correspondance:
        raise RuntimeError(
            f"WMS_UTC_OFFSET invalide : {brut!r}. Format attendu : +HH:MM ou -HH:MM"
        )
    signe, heures, minutes = correspondance.groups()
    delta = timedelta(hours=int(heures), minutes=int(minutes))
    return timezone(-delta if signe == "-" else delta)


def maintenant() -> datetime:
    """Heure courante de l'entrepôt, sans information de fuseau."""
    fuseau = _decalage_force()
    if fuseau is None:
        return datetime.now()
    return datetime.now(timezone.utc).astimezone(fuseau).replace(tzinfo=None)


def maintenant_texte() -> str:
    return maintenant().strftime(FORMAT)


def normaliser(valeur: str | None) -> str | None:
    """Ramène une date saisie au format de stockage.

    Lève ValueError sur une valeur non reconnue : mieux vaut refuser que
    stocker une date que le tri et les regroupements ne sauront pas lire.
    """
    if valeur is None:
        return None
    valeur = valeur.strip()
    if not valeur:
        return None
    for format_essai in _FORMATS_ACCEPTES:
        try:
            return datetime.strptime(valeur, format_essai).strftime(FORMAT)
        except ValueError:
            continue
    raise ValueError(
        f"Date invalide : {valeur!r}. Format attendu : AAAA-MM-JJ ou AAAA-MM-JJ HH:MM"
    )
