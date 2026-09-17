"""File d'attente hors ligne pour les bons non envoyés.

Quand le serveur est injoignable, le bon est sérialisé en JSON dans un
fichier local. Au retour du réseau, les bons sont rejoués dans l'ordre.

L'idempotence (clé unique par bon) protège contre les doublons : si le
serveur avait reçu le bon mais que la réponse s'était perdue, le rejeu
retourne le bon existant sans en créer un second.
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

_fichier: Path | None = None
_verrou = threading.Lock()


def _chemin() -> Path:
    global _fichier
    if _fichier is None:
        from chemins_poste import dossier_utilisateur
        _fichier = dossier_utilisateur() / "file_attente.json"
    return _fichier


def _chemin_refuses() -> Path:
    """Fichier des bons REFUSÉS au rejeu, à côté de la file principale.

    Dérivé de `_chemin()` : le dossier du poste est déterminé au même endroit,
    et un test qui repointe `_fichier` déplace les deux fichiers ensemble.
    """
    return _chemin().with_name("file_attente_refuses.json")


def _ecrire_atomique(chemin: Path, contenu_json: str) -> None:
    """Écriture atomique partagée par les deux fichiers (voir `_ecrire`)."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    provisoire = chemin.with_suffix(chemin.suffix + ".tmp")
    try:
        with open(provisoire, "w", encoding="utf-8") as f:
            f.write(contenu_json)
            f.flush()
            os.fsync(f.fileno())
        os.replace(provisoire, chemin)
    except OSError:
        provisoire.unlink(missing_ok=True)
        raise


def lire_refuses() -> list[dict]:
    """Bons refusés au rejeu, du plus ancien au plus récent (lecture seule).

    Chaque entrée : {"type_appel", "payload", "erreur", "refuse_le"}. Le
    payload est CONSERVÉ intégralement — c'est tout ce qui permet de ressaisir
    le bon, une fois la fenêtre d'avertissement refermée.

    Même patron défensif que `_lire` : un fichier illisible ou corrompu est
    ignoré, jamais propagé en exception à l'écran qui consulte.
    """
    chemin = _chemin_refuses()
    try:
        if not chemin.exists():
            return []
        contenu = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(contenu, list):
        return []
    return [b for b in contenu
            if isinstance(b, dict) and "type_appel" in b and "payload" in b]


def _consigner_refuses(nouveaux: list[dict]) -> None:
    """Ajoute des bons refusés au fichier local, sans jamais lever.

    Le rejeu ne doit pas s'interrompre parce que la trace n'a pas pu être
    écrite : les bons restants doivent quand même partir.
    """
    if not nouveaux:
        return
    try:
        _ecrire_atomique(
            _chemin_refuses(),
            json.dumps(lire_refuses() + nouveaux, ensure_ascii=False, indent=2))
    except OSError:
        pass


def _lire() -> list[dict]:
    """Relit la file. Un fichier illisible est MIS DE CÔTÉ, jamais effacé.

    L'ancienne version renvoyait `[]` sur un fichier corrompu. Comme le
    premier `_ecrire` qui suit réécrit le fichier de zéro, les bons qu'il
    contenait disparaissaient sans trace et sans message : la file passait
    silencieusement à zéro, et l'opérateur croyait ses bons partis. Ici le
    fichier fautif est renommé, donc récupérable à la main.
    """
    chemin = _chemin()
    try:
        if not chemin.exists():
            return []
        contenu = json.loads(chemin.read_text(encoding="utf-8"))
    except OSError:
        # Disque ou droits : ne rien conclure, surtout ne rien renommer.
        return []
    except (json.JSONDecodeError, UnicodeDecodeError):
        try:
            chemin.replace(chemin.with_suffix(chemin.suffix + ".corrompu"))
        except OSError:
            pass
        return []
    # Du JSON valide qui n'est pas une liste de bons ne doit pas non plus
    # faire lever `bons.append` chez l'appelant.
    if not isinstance(contenu, list):
        return []
    return [b for b in contenu
            if isinstance(b, dict) and "type_appel" in b and "payload" in b]


def _ecrire(bons: list[dict]) -> None:
    """Écrit la file de façon atomique.

    Un `write_text` direct tronque le fichier avant de le réécrire : une
    coupure de courant à cet instant — le quotidien d'un entrepôt régional —
    laissait un JSON incomplet, donc une file entière de bons non envoyés
    perdue. Même protection que `config.save_config` : fichier temporaire,
    fsync, puis `os.replace` atomique.
    """
    _ecrire_atomique(_chemin(), json.dumps(bons, ensure_ascii=False, indent=2))


def ajouter(type_appel: str, payload: dict, username: str | None = None) -> int:
    """Ajoute un bon à la file. Retourne le nouveau nombre de bons en attente.

    `username` : compte qui a saisi CE bon — le poste est parfois partagé
    entre comptes (shift change), et sans cette étiquette, `rejouer()`
    envoyait au retour du réseau tout ce qui était en attente sous la
    session ALORS connectée, quelle qu'elle soit. Un bon de secteur/site
    différent était refusé côté serveur (sans dommage, mais silencieusement
    au nom du mauvais compte) ; un bon du MÊME secteur/site, lui, était
    accepté et enregistré (`created_by`) sous le nom de qui l'a rejoué, pas
    de qui l'a réellement saisi — une fausse attribution dans la piste
    d'audit. Constaté par exploration le 2026-09-09.
    """
    # Défensif : un appelant qui passe autre chose qu'une chaîne (test à
    # l'ancienne avec un objet factice, absence d'attribut...) ne doit pas
    # faire échouer l'écriture JSON du bon lui-même pour une étiquette
    # annexe. `None` équivaut à « auteur inconnu », traité comme avant cette
    # étiquette (voir `rejouer`).
    if not isinstance(username, str):
        username = None
    with _verrou:
        bons = _lire()
        bons.append({"type_appel": type_appel, "payload": payload, "username": username})
        _ecrire(bons)
        return len(bons)


def nombre_en_attente() -> int:
    with _verrou:
        return len(_lire())


def rejouer(api) -> list[dict]:
    """Rejoue tous les bons en attente. Retourne les résultats.

    Chaque bon réussi est retiré de la file. En cas d'échec réseau, on
    s'arrête (le serveur est retombé). En cas d'erreur métier (stock
    insuffisant, produit inconnu), le bon est retiré et l'erreur est
    rapportée — il ne servirait à rien de le rejouer indéfiniment.

    Un bon ainsi retiré est d'abord RECOPIÉ, payload complet, dans
    `file_attente_refuses.json` (voir `lire_refuses`). Jusqu'ici seul le
    message d'erreur survivait, dans une fenêtre d'avertissement : produit,
    quantité, région et référence disparaissaient à sa fermeture, sans aucun
    moyen de les retrouver pour les ressaisir.

    Un bon dont l'auteur (`username`, voir `ajouter`) diffère du compte
    ACTUELLEMENT connecté (`api.username`) est laissé en attente, ni rejoué
    ni refusé : le poste est parfois partagé (shift change), et l'envoyer
    sous la mauvaise session l'aurait soit fait refuser au nom de qui ne l'a
    pas saisi, soit — pire — accepté et enregistré (`created_by`) sous le nom
    de qui ne l'a pas saisi. Un bon sans `username` (file écrite par une
    version antérieure) est rejoué comme avant, sans ce contrôle : il n'y a
    rien à comparer.
    """
    identite = getattr(api, "username", None)
    if not isinstance(identite, str):
        identite = None

    with _verrou:
        bons = _lire()
        if not bons:
            return []

        resultats = []
        restants = list(bons)
        refuses: list[dict] = []

        def _refuser(bon, message):
            """Retire un bon de la file en gardant sa trace complète."""
            restants.remove(bon)
            refuses.append({
                "type_appel": bon["type_appel"],
                "payload": bon["payload"],
                "erreur": message,
                "refuse_le": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

        for bon in bons:
            auteur = bon.get("username")
            if auteur and identite and auteur != identite:
                continue
            type_appel = bon["type_appel"]
            payload = bon["payload"]
            try:
                if type_appel == "document":
                    resultat = api.create_document(**payload)
                elif type_appel == "adjustment":
                    resultat = api.create_adjustment(**payload)
                elif type_appel == "regional_inventory":
                    resultat = api.creer_inventaire_regional(**payload)
                elif type_appel == "regional_transfer":
                    resultat = api.creer_transfert_regional(**payload)
                elif type_appel == "confirmation_reception":
                    # Seul type qui agit sur un bon DÉJÀ EXISTANT, désigné par
                    # son id. Si ce bon a été annulé entre la mise en attente
                    # et le rejeu, le serveur refuse (erreur métier, pas
                    # réseau) : le bloc `except` ci-dessous le retire de la
                    # file et rapporte l'erreur, exactement comme pour un
                    # stock insuffisant. Aucun traitement particulier n'est
                    # nécessaire ici — surtout pas une nouvelle tentative en
                    # boucle sur un bon qui n'existe plus.
                    resultat = api.confirmer_reception(**payload)
                else:
                    message = f"Type inconnu : {type_appel}"
                    _refuser(bon, message)
                    resultats.append({"ok": False, "erreur": message})
                    continue

                restants.remove(bon)
                resultats.append({"ok": True, "type": type_appel, "id": resultat.get("id"),
                                  "rejeu": resultat.get("_rejeu", False)})

            except Exception as e:
                erreur = str(e)
                if "contacter le serveur" in erreur.lower() or "timeout" in erreur.lower():
                    break
                import requests
                if isinstance(e, requests.exceptions.RequestException):
                    break
                _refuser(bon, erreur)
                resultats.append({"ok": False, "type": type_appel, "erreur": erreur})

        # Consignés AVANT de réécrire la file : si l'écriture de la file
        # échoue, la trace du refus existe déjà.
        _consigner_refuses(refuses)
        _ecrire(restants)

    return resultats


def vider() -> int:
    """Vide la file. Retourne le nombre de bons supprimés."""
    with _verrou:
        bons = _lire()
        n = len(bons)
        _ecrire([])
        return n
