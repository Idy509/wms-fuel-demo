import hashlib
import io
import re
import unicodedata
from datetime import datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from precision import arrondir, arrondir_saisie

# Un même champ métier porte des noms différents selon le fichier source.
# Le fichier réel du client utilise « Part Number », « Category »,
# « Capacity Group » et « Red Threshold ».
ALIAS_COLONNES: dict[str, set[str]] = {
    "sku": {"sku", "part number", "partnumber", "part no", "reference", "référence",
            "ref", "code", "code article", "item_code", "item code", "itemcode"},
    "name": {"nom", "name", "item", "item description", "designation", "désignation",
             "description", "libelle", "libellé", "produit"},
    "category": {"categorie", "catégorie", "category", "famille", "type"},
    "capacity_group": {"groupe", "capacity group", "capacity", "capacite", "capacité",
                        "groupe de capacite", "groupe de capacité"},
    "unit": {"unite", "unité", "unit", "uom", "u.m.", "mesure"},
    # « stock actualise » et « seuil mini » sont les en-têtes que produit
    # build_stock_excel : sans eux, réimporter l'export de l'application
    # elle-même ne reconnaissait ni la quantité ni le seuil — chaque produit
    # revenait avec min_stock = 0, effaçant tous les seuils configurés.
    "current_stock": {"stock initial", "stock", "qty", "quantity", "quantite", "quantité",
                       "group stock", "actual stock", "atual stock", "stock actuel",
                       "stock actualise", "stock actualisé",
                       "stock reel", "stock réel", "on hand"},
    "min_stock": {"stock min", "min stock", "stock minimum", "red threshold", "seuil",
                   "seuil rouge", "seuil min", "seuil mini"},
    # Colonnes du Masterlist FON. Alias volontairement étroits (« part nb/sn »,
    # pas « part number ») pour ne jamais chevaucher les alias déjà pris par
    # `sku` ci-dessus (le fichier client utilise « Part Number » comme
    # référence produit — un chevauchement lui ferait perdre sa colonne SKU).
    "part_number": {"part nb/sn", "part nb"},
    "group_name": {"group"},
    "sub_category": {"sub category", "subcategory", "sous categorie",
                      "sous-catégorie", "sous catégorie"},
    "supported_genset_models": {"supported genset models", "genset models"},
    "vendor": {"vendor / manufacturer", "vendor", "manufacturer", "fournisseur"},
    "rl_apply": {"rl_apply", "rl apply"},
    "discontinued": {"discontinued", "discontinue"},
}

# Certains fichiers ont deux colonnes candidats pour le même champ : le
# fichier client porte « Capacity » (HPU3) et « Capacity Group » (HPU (3/4/5)).
# La seconde est la maille de gestion : elle l'emporte.
ALIAS_PRIORITAIRES: dict[str, set[str]] = {
    "capacity_group": {"capacity group", "groupe de capacite", "groupe de capacité", "groupe"},
    "current_stock": {"stock initial", "stock actuel", "stock actualise",
                       "stock actualisé", "actual stock", "atual stock"},
}

# Formules cassées ou non calculées : la valeur est absente, pas invalide.
ERREURS_EXCEL = {"#REF!", "#N/A", "#VALUE!", "#DIV/0!", "#NAME?", "#NULL!", "#NUM!", "#SPILL!"}


def _oui_non(valeur) -> bool:
    """« Yes »/« Oui » (et variantes) -> True. Toute autre valeur -> False."""
    if valeur is None:
        return False
    return str(valeur).strip().casefold() in {"yes", "oui", "y", "1", "true"}


def _texte_ou_none(valeur) -> str | None:
    """Texte nettoyé, ou None pour une cellule vide/« N/A »/formule cassée.

    Le Masterlist FON écrit « N/A » plutôt que de laisser la cellule vide
    pour tout champ non applicable à un article — sans ce nettoyage, chaque
    fiche produit afficherait littéralement « N/A » au lieu de rien.
    """
    if valeur is None:
        return None
    texte = str(valeur).strip()
    if not texte or texte.upper() in ERREURS_EXCEL or texte.casefold() == "n/a":
        return None
    return texte


# Certains catalogues (Masterlist fournisseur) marquent un article pas encore
# codifié par ce texte au lieu de laisser la cellule vide. Sans traitement,
# ces lignes valent toutes le même « SKU » littéral : la fusion par référence
# les regroupe en UN SEUL produit, gardant la dernière ligne lue et effaçant
# silencieusement toutes les autres (des dizaines d'articles distincts
# disparaissaient réellement ainsi lors d'un import réel). Plutôt que de les
# ignorer, on leur fabrique une référence temporaire dérivée de leur libellé
# (PREFIXE_REFERENCE_TEMPORAIRE) : stable d'un import à l'autre tant que le
# libellé ne change pas (un ré-import met à jour la même fiche au lieu d'en
# recréer une), et signalée à l'utilisateur pour qu'il la remplace dès qu'un
# code officiel est attribué.
REFERENCE_NON_ATTRIBUEE = "to be assigned"
PREFIXE_REFERENCE_TEMPORAIRE = "TBA-"


def _reference_temporaire(nom: str | None, index: int, deja_utilisees,
                          sector: str | None = None) -> str:
    """`sector` entre dans l'empreinte : `products.sku` est UNIQUE pour TOUTE
    la base (pas composite avec `sector`), donc deux secteurs au catalogue
    proche (RAN et FON partagent des patchcords fibre au nom identique)
    généreraient sinon la MÊME référence temporaire pour le même libellé, et
    l'import du second secteur échouerait (UNIQUE constraint failed) — vécu
    en réel le 12 septembre 2026 à l'import du Masterlist RAN, FON ayant déjà
    ses propres TBA-xxxxxxxx sur les mêmes noms de patchcords.
    """
    base = f"{sector or ''}|{(nom or f'ligne {index}').strip().casefold()}"
    empreinte = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8].upper()
    candidate = f"{PREFIXE_REFERENCE_TEMPORAIRE}{empreinte}"
    suffixe = 2
    while candidate in deja_utilisees:
        candidate = f"{PREFIXE_REFERENCE_TEMPORAIRE}{empreinte}-{suffixe}"
        suffixe += 1
    return candidate

# Protection contre l'injection Excel : préfixes qui indiquent une formule
# dangereuse. « - » et « + » en sont volontairement absents : un fichier réel
# contient des quantités négatives ("-5"), et les traiter comme une injection
# les transformait silencieusement en 0. Le risque d'exécution vient de « = »
# et « @ » ; « \t » et « \r » servent à masquer le préfixe réel.
EXCEL_FORMULA_PREFIXES = {"=", "@", "\t", "\r"}

# Limites de sécurité pour prévenir les attaques par déni de service
MAX_STRING_LENGTH = 32767  # Limite de longueur de cellule Excel
MAX_SHEET_NAME_LENGTH = 31  # Limite de longueur de nom de feuille Excel

# Caractères de contrôle refusés par le format XLSX : openpyxl lève
# IllegalCharacterError à l'enregistrement. Une seule note contenant un \x0b
# rendait l'export définitivement impossible (500 à chaque tentative).
CARACTERES_ILLEGAUX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _cellule(valeur):
    """Rend une valeur sûre pour une cellule Excel.

    Les non-chaînes (dates, nombres, None) passent inchangées. Pour une
    chaîne : retire les caractères de contrôle interdits, tronque à la
    limite Excel, et neutralise les formules en préfixant d'une apostrophe.
    Un texte saisi par un opérateur ne doit jamais s'exécuter chez le
    destinataire du fichier.
    """
    if not isinstance(valeur, str):
        return valeur
    texte = CARACTERES_ILLEGAUX.sub("", valeur)
    if texte[:1] in EXCEL_FORMULA_PREFIXES:
        texte = "'" + texte
    if len(texte) > MAX_STRING_LENGTH:
        texte = texte[:MAX_STRING_LENGTH]
    return texte


def _cle_colonne(valeur) -> str:
    """Normalise un en-tête de colonne pour le comparer à `ALIAS_COLONNES`.

    Deux défauts trouvés par audit d'exactitude du parsing le 9 septembre
    2026 sur des fichiers réels :
    - une faute de frappe courante (« Part  Number », deux espaces) faisait
      manquer TOUT le fichier — "sku" est une colonne obligatoire, aucun
      alias ne matchait, message générique « aucune colonne de référence
      trouvée » sans indice sur la vraie cause ;
    - un en-tête accentué en forme Unicode NFD (accent combiné séparé,
      produite par certains outils hors Windows) ne matchait pas la forme
      NFC précomposée codée en dur dans `ALIAS_COLONNES` — même symptôme.
    `unicodedata.normalize("NFD", ...)` + suppression des marques
    combinantes (catégorie Unicode "Mn") résout les deux : elle force aussi
    bien la forme NFD que NFC vers une même clé SANS accent, ce qui rend
    par ailleurs redondantes (mais toujours correctes, donc laissées telles
    quelles) les paires accentué/non-accentué déjà énumérées à la main dans
    `ALIAS_COLONNES` (« categorie »/« catégorie », etc.) — même mécanisme
    que `server/reference_data.py::_cle`, déjà éprouvé pour le même
    problème sur les noms de région/opérateur.
    """
    if valeur is None:
        return ""
    sans_accent = "".join(
        c for c in unicodedata.normalize("NFD", str(valeur))
        if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", sans_accent).strip().casefold()


# ALIAS_COLONNES/ALIAS_PRIORITAIRES sont écrits à la main avec des accents
# (« référence », « désignation »...) : comparés tels quels à une clé
# désormais désaccentuée par `_cle_colonne`, ils ne matcheraient plus JAMAIS
# — il faut leur appliquer la même normalisation, une seule fois, pas à
# chaque en-tête de chaque fichier importé.
_ALIAS_COLONNES_NORM: dict[str, set[str]] = {
    champ: {_cle_colonne(a) for a in alias} for champ, alias in ALIAS_COLONNES.items()
}
_ALIAS_PRIORITAIRES_NORM: dict[str, set[str]] = {
    champ: {_cle_colonne(a) for a in alias} for champ, alias in ALIAS_PRIORITAIRES.items()
}


def _detecter_entetes(rows: list[tuple]) -> tuple[int, dict[str, int]]:
    """Trouve la ligne d'en-têtes et l'indice de chaque champ reconnu.

    Cherche sur les 10 premières lignes : certains fichiers commencent par un
    titre ou des lignes vides. Retient la ligne qui reconnaît le plus de champs.
    """
    meilleure_ligne, meilleure_carte = -1, {}
    for index, row in enumerate(rows[:10]):
        if row is None:
            continue
        carte = {}
        for colonne, valeur in enumerate(row):
            cle = _cle_colonne(valeur)
            if not cle:
                continue
            for champ, alias in _ALIAS_COLONNES_NORM.items():
                if cle not in alias:
                    continue
                prioritaire = cle in _ALIAS_PRIORITAIRES_NORM.get(champ, ())
                if champ not in carte or prioritaire:
                    carte[champ] = colonne
        if len(carte) > len(meilleure_carte):
            meilleure_ligne, meilleure_carte = index, carte
    return meilleure_ligne, meilleure_carte


def _nombre(valeur, ligne: int, colonne: str, avertissements: list[str]) -> float:
    """Convertit en nombre. Une erreur Excel vaut « absent », pas « invalide »."""
    if valeur is None or valeur == "":
        return 0.0
    if isinstance(valeur, str):
        texte = valeur.strip()
        # Protection contre l'injection Excel : rejeter les valeurs commençant par des opérateurs de formule
        if texte and texte[0] in EXCEL_FORMULA_PREFIXES:
            avertissements.append(f"__injection_excel__|{colonne}|{texte[:50]}")
            return 0.0
        if texte.upper() in ERREURS_EXCEL:
            # Regroupé plutôt que répété : un fichier peut porter la même
            # formule cassée sur des centaines de lignes.
            avertissements.append(f"__formule__|{colonne}|{texte}")
            return 0.0
        texte = texte.replace(" ", "").replace(" ", "").replace(",", ".")
        if not texte:
            return 0.0
        try:
            # arrondir_saisie : `float("inf")`, `float("1e999")` et
            # `float("nan")` réussissent tous. Sans ce refus, une cellule
            # contenant « 1e999 » créait un produit à stock infini — que
            # SQLite conserve tel quel et qui rend `SUM(current_stock)`
            # infini pour toujours dans le tableau de bord.
            return arrondir_saisie(float(texte))
        except ValueError:
            raise ValueError(f"ligne {ligne}, colonne « {colonne} » : "
                             f"valeur non numérique ({valeur!r})")
    try:
        return arrondir_saisie(float(valeur))
    except (TypeError, ValueError):
        raise ValueError(f"ligne {ligne}, colonne « {colonne} » : "
                         f"valeur non numérique ({valeur!r})")


def _resumer_avertissements(avertissements: list[str]) -> list[str]:
    """Condense les avertissements répétitifs en une ligne comptée."""
    from collections import Counter

    formules = Counter()
    injections = Counter()
    autres = []
    for message in avertissements:
        if message.startswith("__formule__|"):
            _, colonne, texte = message.split("|", 2)
            formules[(colonne, texte)] += 1
        elif message.startswith("__injection_excel__|"):
            _, colonne, texte = message.split("|", 2)
            injections[(colonne, texte)] += 1
        else:
            autres.append(message)

    resume = [
        f"Colonne « {colonne} » : {nb} ligne(s) contiennent {texte} "
        f"(formule cassée dans le fichier) — valeur lue comme 0"
        for (colonne, texte), nb in formules.items()
    ]
    resume.extend(
        f"Colonne « {colonne} » : {nb} ligne(s) commencent par un caractère de "
        f"formule Excel ({texte}) — refusé par sécurité, valeur lue comme 0. "
        f"Corrige la cellule dans le fichier source."
        for (colonne, texte), nb in injections.items()
    )
    # Limite l'affichage : au-delà, l'opérateur ne lit plus.
    if len(autres) > 20:
        resume.extend(autres[:20])
        resume.append(f"… et {len(autres) - 20} autre(s) avertissement(s)")
    else:
        resume.extend(autres)
    return resume


def parse_products_excel(file_bytes: bytes, sector: str | None = None) -> tuple[list[dict], dict]:
    """Lit un fichier produits, quelle que soit sa nomenclature de colonnes.

    `sector` : secteur cible de l'import, utilisé UNIQUEMENT pour distinguer
    les références temporaires générées pour un article sans code officiel
    (voir `_reference_temporaire`). Optionnel — sans lui (ex. tests), le
    comportement historique (référence dérivée du seul libellé) est conservé.

    Renvoie (produits, rapport). Le rapport détaille ce qui a été ignoré ou
    corrigé, pour que rien ne disparaisse en silence.
    """
    wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    avertissements: list[str] = []
    erreurs: list[str] = []

    if not rows:
        return [], {"avertissements": [], "erreurs": ["Le fichier est vide"], "lignes_lues": 0}

    ligne_entete, carte = _detecter_entetes(rows)
    if "sku" not in carte:
        attendus = ", ".join(sorted(ALIAS_COLONNES["sku"])[:6])
        return [], {
            "avertissements": [],
            "erreurs": [f"Aucune colonne de référence trouvée. En-têtes attendus : {attendus}…"],
            "lignes_lues": 0,
        }

    if "name" not in carte and "category" not in carte:
        return [], {
            "avertissements": [],
            "erreurs": ["Aucune colonne de libellé ni de catégorie : impossible de nommer "
                        "les produits"],
            "lignes_lues": 0,
        }

    def valeur(row, champ):
        colonne = carte.get(champ)
        return row[colonne] if colonne is not None and colonne < len(row) else None

    # Un même article peut apparaître sur plusieurs lignes (une par capacité de
    # groupe électrogène). On les fusionne au lieu de laisser la dernière écraser
    # les précédentes.
    regroupes: dict[str, dict] = {}
    lignes_lues = 0

    for index, row in enumerate(rows[ligne_entete + 1:], start=ligne_entete + 2):
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue

        brut_sku = valeur(row, "sku")
        sku_absente = brut_sku is None or str(brut_sku).strip() == ""
        sku = "" if sku_absente else str(brut_sku).strip()
        if not sku_absente and sku.upper() in ERREURS_EXCEL:
            avertissements.append(f"Ligne {index} : référence illisible ({sku}) — ignorée")
            continue

        try:
            stock = _nombre(valeur(row, "current_stock"), index, "stock", avertissements)
            mini = _nombre(valeur(row, "min_stock"), index, "stock min", avertissements)
        except ValueError as e:
            erreurs.append(str(e))
            continue

        if stock < 0 or mini < 0:
            erreurs.append(f"ligne {index} : quantité négative ({sku})")
            continue

        categorie = valeur(row, "category")
        categorie = str(categorie).strip().title() if categorie else None
        groupe = valeur(row, "capacity_group")
        groupe = str(groupe).strip() if groupe and str(groupe).upper() not in ERREURS_EXCEL else None

        nom = valeur(row, "name")
        nom = str(nom).strip() if nom else None
        if not nom:
            # Fichier sans libellé : on le compose depuis la catégorie et le
            # groupe, ou à défaut la référence elle-même (si elle existe).
            nom = f"{categorie} ({groupe})" if categorie and groupe else (
                categorie or (sku if not sku_absente else None))

        # Cellule de référence vide ou valant littéralement « to be assigned » :
        # même traitement. Un fichier source (Masterlist RAN, entre autres)
        # laisse la cellule vide pour un article pas encore codifié au lieu
        # d'y écrire le texte ; sans nom ET sans référence, la ligne n'a rien
        # d'identifiable et reste ignorée.
        if sku_absente or sku.casefold() == REFERENCE_NON_ATTRIBUEE:
            if not nom:
                avertissements.append(f"Ligne {index} : sans référence ni libellé — ignorée")
                continue
            sku = _reference_temporaire(nom, index, regroupes, sector)
            avertissements.append(
                f"Ligne {index} : référence pas encore attribuée — importée sous "
                f"{sku}, à remplacer dès qu'un code officiel est assigné")

        unite = valeur(row, "unit")
        unite = str(unite).strip() if unite else "pcs"

        lignes_lues += 1
        existant = regroupes.get(sku)
        if existant is None:
            regroupes[sku] = {
                "sku": sku, "name": nom, "category": categorie,
                "_groupes": {groupe} if groupe else set(),
                "unit": unite, "current_stock": stock, "min_stock": mini,
                "_lignes": [index],
                # Champs FON : présents seulement si le fichier a ces
                # colonnes (carte.get renvoie None sinon via `valeur`) —
                # restent None/False pour un fichier Consumables classique.
                "part_number": _texte_ou_none(valeur(row, "part_number")),
                "group_name": _texte_ou_none(valeur(row, "group_name")),
                "sub_category": _texte_ou_none(valeur(row, "sub_category")),
                "supported_genset_models":
                    _texte_ou_none(valeur(row, "supported_genset_models")),
                "vendor": _texte_ou_none(valeur(row, "vendor")),
                "rl_apply": _oui_non(valeur(row, "rl_apply")),
                "discontinued": _oui_non(valeur(row, "discontinued")),
            }
            continue

        # Même référence sur plusieurs lignes : on fusionne les groupes de
        # capacité et on garde la quantité la plus élevée, en le signalant.
        existant["_lignes"].append(index)
        if groupe:
            existant["_groupes"].add(groupe)
        if abs(existant["current_stock"] - stock) > 1e-9:
            avertissements.append(
                f"Référence {sku} : quantités différentes selon les lignes "
                f"({existant['current_stock']:g} et {stock:g}) — la plus élevée est retenue"
            )
            existant["current_stock"] = max(existant["current_stock"], stock)
        existant["min_stock"] = max(existant["min_stock"], mini)

    produits = []
    for entree in regroupes.values():
        groupes = sorted(entree.pop("_groupes"))
        lignes = entree.pop("_lignes")
        entree["capacity_group"] = " / ".join(groupes) if groupes else None
        if len(lignes) > 1 and groupes:
            # Le libellé reflète tous les groupes servis par la pièce.
            if entree["category"]:
                entree["name"] = f"{entree['category']} ({entree['capacity_group']})"
        produits.append(entree)

    rapport = {
        "avertissements": _resumer_avertissements(avertissements),
        "erreurs": erreurs,
        "lignes_lues": lignes_lues,
        "references_uniques": len(produits),
        "colonnes_reconnues": sorted(carte),
    }
    return produits, rapport


def build_stock_excel(products: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    # « Seuil Mini » n'était pas exporté : le fichier ne pouvait donc pas servir
    # de base à un réimport (le seuil de chaque produit serait revenu à 0).
    ws.append(["SKU", "Nom", "Unite", "Stock Debut", "Stock Actualise",
               "Seuil Mini", "Cout Unitaire", "Valeur"])
    for p in products:
        cout = p.get("unit_cost")
        # arrondir() : sans lui, la colonne « Valeur » de CET export portait
        # le bruit flottant brut (ex. 139.96500000000001) alors que
        # build_comptable_excel, plus bas dans ce même fichier, arrondit le
        # même calcul (quantité × coût) à 3 décimales — deux valeurs
        # différentes pour le même produit selon l'export ouvert. Repéré par
        # audit d'exactitude métier le 9 septembre 2026.
        valeur = arrondir(cout * p["current_stock"]) if cout is not None else None
        ws.append([_cellule(v) for v in
                   (p["sku"], p["name"], p["unit"], p["initial_stock"], p["current_stock"],
                    p.get("min_stock"), cout, valeur)])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_regional_inventaire_excel(rapports: list[dict]) -> bytes:
    """Historique des comptages régionaux : ce que chaque région a déclaré avoir.

    `rapports` : dicts avec region, sku, name, unit, quantity, created_by,
    created_at (forme renvoyée par `GET /regional/inventaire`).
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventaire regional"
    ws.append(["Region", "SKU", "Nom", "Unite", "Quantite comptee",
               "Saisi par", "Date du comptage"])
    for r in rapports:
        ws.append([_cellule(v) for v in
                   (r.get("region"), r.get("sku"), r.get("name"), r.get("unit"),
                    r.get("quantity"), r.get("created_by"), r.get("created_at"))])
    for col_letter, width in (("A", 16), ("B", 14), ("C", 30), ("D", 8),
                              ("E", 16), ("F", 20), ("G", 18)):
        ws.column_dimensions[col_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_audit_log_excel(entrees: list[dict]) -> bytes:
    """Journal d'audit d'une période, une ligne par entrée.

    `entrees` : dicts de la table `audit_log` (created_at, username, action,
    entity_type, entity_id, old_values, new_values).

    Les colonnes « Avant » / « Après » gardent le JSON brut, volontairement :
    ce fichier sert de pièce justificative, pas de tableau de bord. Le
    reformater ferait perdre les champs qu'aucune version de l'écran ne sait
    encore afficher.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Journal d'audit"
    ws.append(["Date", "Utilisateur", "Action", "Entite", "ID entite",
               "Avant", "Apres"])
    for e in entrees:
        ws.append([_cellule(v) for v in
                   (e.get("created_at"), e.get("username"), e.get("action"),
                    e.get("entity_type"), e.get("entity_id"),
                    e.get("old_values"), e.get("new_values"))])
    for col_letter, width in (("A", 20), ("B", 18), ("C", 16), ("D", 16),
                              ("E", 10), ("F", 50), ("G", 50)):
        ws.column_dimensions[col_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_reappro_excel(lignes: list[dict]) -> bytes:
    """Liste des produits sous leur seuil, avec la quantité à commander.

    `lignes` : dicts avec sku, name, category, unit, current_stock, min_stock,
    quantite_a_commander (déjà calculée côté appelant).
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Reapprovisionnement"
    ws.append(["SKU", "Nom", "Categorie", "Unite", "Stock Actuel", "Seuil Mini",
               "Quantite a commander"])
    for l in lignes:
        ws.append([_cellule(v) for v in
                   (l["sku"], l["name"], l.get("category") or "", l["unit"],
                    l["current_stock"], l["min_stock"], l["quantite_a_commander"])])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


HEADER_FONT = Font(bold=True)
DATE_FORMAT = "mm-dd-yy"


def _parse_datetime(created_at: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(created_at, fmt)
        except ValueError:
            continue
    return None


def _write_header(ws, headers: list[str]) -> None:
    ws.append([_cellule(h) for h in headers])
    for cell in ws[1]:
        cell.font = HEADER_FONT


def _apply_table_style(ws, table_name: str, num_columns: int) -> None:
    if ws.max_row <= 1:
        return
    end_col = get_column_letter(num_columns)
    table = Table(displayName=table_name, ref=f"A1:{end_col}{ws.max_row}")
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2", showRowStripes=True, showFirstColumn=False,
        showLastColumn=False, showColumnStripes=False,
    )
    ws.add_table(table)


def _write_row_with_month_formula(ws, row_values: list, date_col_index: int, month_col_index: int) -> None:
    # Les valeurs viennent de saisies libres (party, note, reference) :
    # assainies avant écriture. La formule de mois ci-dessous est posée
    # directement par le serveur, elle n'est donc pas concernée.
    ws.append([_cellule(v) for v in row_values])
    row_num = ws.max_row
    date_letter = get_column_letter(date_col_index)
    month_letter = get_column_letter(month_col_index)
    date_cell = ws[f"{date_letter}{row_num}"]
    date_cell.number_format = DATE_FORMAT
    ws[f"{month_letter}{row_num}"] = f'=TEXT({date_letter}{row_num}, "mmmm")'


def build_documents_excel(documents: list[dict], type_filter: str | None = None) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)

    receiving_docs = [d for d in documents if d["type"] == "RECEIVING"]
    delivery_docs = [d for d in documents if d["type"] == "DELIVERY"]
    adjustment_docs = [d for d in documents if d["type"] == "ADJUSTMENT"]

    if type_filter in (None, "RECEIVING"):
        ws = wb.create_sheet("Receiving Tracker")
        _write_header(ws, ["SOURCE", "ITEM", "Part Number", "QTY", "Receiving Date", "Month", "Packing Slip #", "Note"])
        for doc in receiving_docs:
            date = _parse_datetime(doc["created_at"])
            for line in doc["lines"]:
                _write_row_with_month_formula(
                    ws,
                    [doc["party"] or "", line["name"], line["sku"], line["quantity"],
                     date, None, doc.get("reference") or "", doc["note"] or ""],
                    date_col_index=5, month_col_index=6,
                )
        for col_letter, width in (("A", 14), ("B", 30), ("C", 14), ("D", 8), ("E", 13), ("F", 11), ("G", 16), ("H", 24)):
            ws.column_dimensions[col_letter].width = width
        _apply_table_style(ws, "ReceivingTracker", 8)

    if type_filter in (None, "DELIVERY"):
        ws = wb.create_sheet("Delivery Tracker")
        # Part Number juste après ITEM, comme dans le Receiving Tracker : sans
        # lui, une sortie exportée était ambiguë dès que plusieurs SKU
        # partageaient le même libellé (ex. 6 références "Fuel Filter (P11/P16)").
        _write_header(ws, ["Location", "ITEM", "Part Number", "Delivery Date", "Month", "QTY",
                            "Receipt #", "RECEIVER", "REGION", "SUPERVISOR", "Name", "Comments"])
        for doc in delivery_docs:
            date = _parse_datetime(doc["created_at"])
            for line in doc["lines"]:
                _write_row_with_month_formula(
                    ws,
                    ["TP WH", line["name"], line["sku"], date, None, line["quantity"],
                     doc.get("reference") or "", doc["party"] or "", doc.get("region") or "",
                     doc["operator"] or "", "", doc["note"] or ""],
                    date_col_index=4, month_col_index=5,
                )
        for col_letter, width in (("A", 14), ("B", 30), ("C", 14), ("D", 13), ("E", 11), ("F", 8),
                                   ("G", 11), ("H", 16), ("I", 16), ("J", 20), ("K", 10), ("L", 30)):
            ws.column_dimensions[col_letter].width = width
        _apply_table_style(ws, "DeliveryTracker", 12)

    # Les ajustements ne sont ni des entrées ni des sorties : onglet séparé,
    # sinon ils fausseraient les totaux « Received » et « Used » du client.
    # `type_filter == "ADJUSTMENT"` (export filtré sur « Inventaires ») doit
    # produire CETTE feuille, pas les deux Receiving/Delivery Tracker vides
    # d'à côté — un vrai rapport d'inventaire, pas un gabarit d'expédition
    # sans rien dedans (constaté en réel le 13 septembre 2026, secteur RAN).
    if type_filter in (None, "ADJUSTMENT") and adjustment_docs:
        ws = wb.create_sheet("Ajustements")
        _write_header(ws, ["Date", "Month", "Part Number", "ITEM", "Stock theorique",
                            "Stock compte", "Ecart", "Motif", "REGION", "Operateur", "Note"])
        for doc in adjustment_docs:
            date = _parse_datetime(doc["created_at"])
            for line in doc["lines"]:
                compte = line.get("counted_quantity")
                theorique = (compte - line["quantity"]) if compte is not None else None
                _write_row_with_month_formula(
                    ws,
                    [date, None, line["sku"], line["name"], theorique, compte, line["quantity"],
                     doc.get("reference") or "", doc.get("region") or "",
                     doc["operator"] or "", doc["note"] or ""],
                    date_col_index=1, month_col_index=2,
                )
        for col_letter, width in (("A", 13), ("B", 11), ("C", 14), ("D", 30), ("E", 15),
                                   ("F", 14), ("G", 10), ("H", 28), ("I", 16), ("J", 16), ("K", 24)):
            ws.column_dimensions[col_letter].width = width
        _apply_table_style(ws, "Ajustements", 11)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


NOTE_COUT_ACTUEL = (
    "NOTE : la valeur des mouvements est calculee avec le cout unitaire ACTUEL "
    "du produit. Le schema ne conserve pas l'historique des prix : un cout "
    "modifie depuis revalorise retroactivement les mouvements passes. "
    "A verifier avant tout usage fiscal."
)


def build_comptable_excel(produits: list[dict], mouvements: list[dict],
                           depuis: str, jusqu_a: str) -> bytes:
    """Classeur comptable : valeur du stock + mouvements valorises de la periode.

    `produits` : sku, name, unit, current_stock, unit_cost (peut etre None).
    `mouvements` : created_at, type, sku, name, quantity, unit_cost (None
    possible), region, reference, operator.

    Un cout absent vaut 0 dans la colonne valeur — jamais une erreur : un
    catalogue reel comporte toujours des references non valorisees, et un
    export qui plante dessus ne sert a personne. Le nombre de lignes
    concernees est rappele en bas de chaque feuille.
    """
    wb = Workbook()
    wb.remove(wb.active)

    # --- Feuille 1 : valeur du stock a la date d'export ---
    ws = wb.create_sheet("Valeur du stock")
    ws.append([_cellule(
        f"Valeur du stock au {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
        "Les produits archives sont exclus. Un cout unitaire absent compte pour 0."
    )])
    ws["A1"].font = HEADER_FONT
    ws.append([])
    entetes = ["SKU", "Nom", "Unite", "Stock actuel", "Cout unitaire", "Valeur"]
    ws.append([_cellule(h) for h in entetes])
    for cell in ws[3]:
        cell.font = HEADER_FONT

    total_valeur = 0.0
    sans_cout = 0
    for p in produits:
        cout = p.get("unit_cost")
        stock = p.get("current_stock") or 0
        if cout is None:
            sans_cout += 1
            valeur = 0.0
        else:
            valeur = arrondir(float(cout) * float(stock))
        total_valeur += valeur
        ws.append([_cellule(v) for v in
                   (p.get("sku"), p.get("name"), p.get("unit"), stock, cout, valeur)])

    ws.append([])
    ligne_total = ws.max_row + 1
    ws.append([_cellule("TOTAL"), None, None, None, None,
               _cellule(arrondir(total_valeur))])
    for cell in ws[ligne_total]:
        cell.font = HEADER_FONT
    if sans_cout:
        ws.append([_cellule(
            f"{sans_cout} produit(s) sans cout unitaire, comptes pour 0 dans le total."
        )])
    for col_letter, width in (("A", 16), ("B", 34), ("C", 10), ("D", 14),
                              ("E", 14), ("F", 16)):
        ws.column_dimensions[col_letter].width = width

    # --- Feuille 2 : mouvements de la periode ---
    ws = wb.create_sheet("Mouvements de la periode")
    ws.append([_cellule(f"Mouvements du {depuis} au {jusqu_a}. "
                        "Bons annules exclus.")])
    ws["A1"].font = HEADER_FONT
    ws.append([_cellule(NOTE_COUT_ACTUEL)])
    ws.append([])
    entetes = ["Date", "Type", "SKU", "Nom", "Quantite", "Cout unitaire",
               "Valeur", "Region", "Reference", "Operateur"]
    ws.append([_cellule(h) for h in entetes])
    for cell in ws[4]:
        cell.font = HEADER_FONT

    total_mouvements = 0.0
    mouvements_sans_cout = 0
    for m in mouvements:
        cout = m.get("unit_cost")
        quantite = m.get("quantity") or 0
        if cout is None:
            mouvements_sans_cout += 1
            valeur = 0.0
        else:
            valeur = arrondir(float(cout) * float(quantite))
        total_mouvements += valeur
        ws.append([_cellule(v) for v in
                   (m.get("created_at"), m.get("type"), m.get("sku"), m.get("name"),
                    quantite, cout, valeur, m.get("region") or "",
                    m.get("reference") or "", m.get("operator") or "")])

    ws.append([])
    ligne_total = ws.max_row + 1
    ws.append([_cellule("TOTAL"), None, None, None, None, None,
               _cellule(arrondir(total_mouvements))])
    for cell in ws[ligne_total]:
        cell.font = HEADER_FONT
    if mouvements_sans_cout:
        ws.append([_cellule(
            f"{mouvements_sans_cout} ligne(s) sans cout unitaire, comptees pour 0."
        )])
    for col_letter, width in (("A", 20), ("B", 16), ("C", 16), ("D", 34),
                              ("E", 12), ("F", 14), ("G", 16), ("H", 16),
                              ("I", 18), ("J", 18)):
        ws.column_dimensions[col_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_filename(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
