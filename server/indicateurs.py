"""Indicateurs de pilotage de l'entrepôt central — calculs purs.

Neuf mesures que ni le tableau de bord ni les rapports existants ne
donnaient, chacune répondant à une question qu'un administrateur se pose
réellement :

1. `precision_inventaire`    — « mon stock informatique est-il fiable ? »
2. `concentration_regions`   — « quelles régions font le volume ? »
3. `comptages_a_temps`       — « qui n'a pas rendu son comptage ce mois-ci ? »
4. `anomalies_mouvements`    — « quel mouvement du mois sort de l'ordinaire ? »
5. `consommation_regionale`  — « qui consomme quoi, mois par mois ? »
6. `produits_dormants`       — « quel argent dort sur mes étagères ? »
7. `resolution_alertes`      — « répond-on aux ruptures signalées, et en combien de temps ? »
8. `dette_consigne`          — « qui doit encore rendre des bouteilles ? »
9. `activite_operateurs`     — « qui saisit quoi, et à quelle heure ? »
10. `ecarts_reception`       — « qu'est-ce qui part et n'arrive jamais ? »
11. `delai_fournisseurs`     — « à quel rythme mes fournisseurs me livrent-ils ? »

Tout le filtrage et toute l'agrégation lourde restent en SQL : les boucles
Python de ce module ne parcourent que des ensembles déjà réduits par une
clause WHERE (les produits d'un seul cycle d'inventaire, les 13 régions, les
10 lignes retenues). Aucun balayage complet de `document_lines` côté Python.

Les fonctions sont pures et ne dépendent que de `conn` : elles sont donc
testables directement, et app.py ne fait que les exposer aux administrateurs.
"""
from datetime import datetime, timedelta

from horodatage import FORMAT, maintenant, normaliser
# Le secteur par défaut vient de `models` plutôt que d'être redéfini ici :
# deux constantes « CONSUMABLES » qui divergeraient un jour donneraient des
# indicateurs calculés sur le mauvais périmètre, en silence.
from models import SECTEUR_DEFAUT
from precision import arrondir
from reference_data import REGIONS_AVEC_ENTREPOT

# Les requêtes de ce module ne retiennent que RECEIVING et DELIVERY : un
# ajustement n'est pas un mouvement de marchandise, et le comparer à des
# livraisons fausserait toute moyenne historique.

# Seuil au-delà duquel un comptage est jugé conforme. 95 % est la cible
# usuelle d'un entrepôt. On ne compte PAS les comptages parfaitement exacts :
# `create_adjustment` refuse un ajustement à écart nul (« aucun écart
# constaté »), donc un comptage juste ne laisse aucune trace en base — un
# compteur d'exactitude vaudrait toujours zéro et tromperait le lecteur.
SEUIL_CONFORMITE = 95.0

# Plafond du détail ligne à ligne du rapport d'écarts de réception. L'agrégat
# par région/transporteur porte les totaux exacts ; le détail sert à ouvrir
# une enquête, et personne n'en lit mille lignes.
MAX_DETAIL_ECARTS = 300


def _bornes_mois(reference=None) -> tuple[str, str]:
    """Premier instant du mois calendaire courant, et du mois suivant.

    Bornes en texte au format de la base (heure locale de l'entrepôt, comme
    tout `created_at`) : la comparaison de chaînes ISO est équivalente à la
    comparaison de dates, et évite toute conversion ligne à ligne en SQL.
    """
    maintenant_ = reference or maintenant()
    debut = maintenant_.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Le 28 + 4 jours tombe toujours dans le mois suivant, quel que soit le
    # mois et les années bissextiles.
    suivant = (debut.replace(day=28) + timedelta(days=4)).replace(day=1)
    return debut.strftime(FORMAT), suivant.strftime(FORMAT)


# ------------------------------------------------------- 1. précision d'inventaire


def precision_inventaire(conn, jours_cycle: int = 7, sector: str = SECTEUR_DEFAUT) -> dict:
    """Fiabilité du stock informatique, mesurée sur le dernier cycle de comptage.

    Un ajustement enregistre `counted_quantity` (ce qui a été compté) et
    `quantity` (l'écart appliqué). Le stock théorique juste avant le comptage
    s'en déduit exactement, sans historique de stock : `théorique = compté −
    écart`. C'est la même décomposition que celle utilisée par les garde-fous
    de `create_adjustment` et par `/reports/ajustements`.

    Précision d'une ligne : `100 × (1 − |écart| / |théorique|)`, plafonnée à 0
    (un écart supérieur au théorique ne donne pas une précision négative, il
    donne « rien de juste »). Théorique nul : 100 % si l'écart est nul aussi,
    0 % sinon — sans quoi la division serait impossible.

    Deux agrégats, volontairement :
      - `precision_moyenne` : moyenne simple des lignes, chaque produit pèse
        pareil. C'est l'indicateur demandé (« moyenné sur tous les produits »).
      - `precision_ponderee` : `100 × (1 − Σ|écart| / Σ|théorique|)`, où un
        gros volume pèse plus. Un seul petit produit très faux n'y écrase pas
        la lecture.

    Le « cycle » est la fenêtre de `jours_cycle` jours qui se termine au
    dernier comptage enregistré — pas les N derniers jours à partir
    d'aujourd'hui : un inventaire fait il y a trois semaines doit rester
    lisible, sinon l'indicateur devient vide dès qu'on ne compte pas.

    Un produit compté plusieurs fois dans le cycle n'est retenu qu'une fois,
    sur son comptage le plus récent : le recomptage d'une erreur ne doit pas
    compter deux fois contre la précision.
    """
    dernier = conn.execute(
        """
        SELECT MAX(d.created_at) AS dernier
        FROM documents d
        JOIN document_lines dl ON dl.document_id = d.id
        WHERE d.type = 'ADJUSTMENT' AND d.cancelled_at IS NULL
          AND dl.counted_quantity IS NOT NULL
          AND d.sector = ?
        """,
        (sector,),
    ).fetchone()["dernier"]

    if not dernier:
        return {
            "jours_cycle": jours_cycle, "dernier_comptage": None, "depuis": None,
            "produits_comptes": 0, "produits_conformes": 0,
            "seuil_conformite": SEUIL_CONFORMITE,
            "precision_moyenne": None, "precision_ponderee": None,
            "ecart_absolu_total": 0.0, "theorique_total": 0.0, "lignes": [],
        }

    # Borne basse : `jours_cycle` jours avant le dernier comptage, ramenés au
    # début de journée pour qu'un cycle d'un jour couvre bien toute la journée.
    fin = _parse(dernier)
    depuis = (fin - timedelta(days=jours_cycle - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0).strftime(FORMAT)

    rows = conn.execute(
        """
        SELECT dl.product_id, p.sku, p.name, d.created_at, d.created_by,
               dl.counted_quantity AS compte, dl.quantity AS ecart
        FROM document_lines dl
        JOIN documents d ON d.id = dl.document_id
        JOIN products p ON p.id = dl.product_id
        WHERE d.type = 'ADJUSTMENT' AND d.cancelled_at IS NULL
          AND dl.counted_quantity IS NOT NULL
          AND d.created_at >= ?
          AND d.sector = ?
          -- Ne garde que le comptage le plus récent de chaque produit dans
          -- la fenêtre. `dl.id` départage deux comptages à la même seconde.
          AND NOT EXISTS (
              SELECT 1 FROM document_lines dl2
              JOIN documents d2 ON d2.id = dl2.document_id
              WHERE d2.type = 'ADJUSTMENT' AND d2.cancelled_at IS NULL
                AND dl2.counted_quantity IS NOT NULL
                AND dl2.product_id = dl.product_id
                AND d2.created_at >= ?
                AND d2.sector = ?
                AND (d2.created_at > d.created_at
                     OR (d2.created_at = d.created_at AND dl2.id > dl.id))
          )
        ORDER BY ABS(dl.quantity) DESC
        """,
        (depuis, sector, depuis, sector),
    ).fetchall()

    lignes = []
    somme_precision = 0.0
    ecart_total = 0.0
    theorique_total = 0.0
    conformes = 0

    for r in rows:
        ecart = r["ecart"] or 0.0
        theorique = arrondir(r["compte"] - ecart)
        ecart_absolu = abs(ecart)
        if abs(theorique) < 1e-9:
            precision = 100.0 if ecart_absolu < 1e-9 else 0.0
        else:
            precision = max(0.0, 100.0 * (1 - ecart_absolu / abs(theorique)))
        if precision >= SEUIL_CONFORMITE:
            conformes += 1
        somme_precision += precision
        ecart_total += ecart_absolu
        theorique_total += abs(theorique)
        lignes.append({
            "product_id": r["product_id"], "sku": r["sku"], "name": r["name"],
            "created_at": r["created_at"], "created_by": r["created_by"],
            "theorique": theorique, "compte": arrondir(r["compte"]),
            "ecart": arrondir(ecart), "precision": arrondir(precision),
        })

    nb = len(lignes)
    ponderee = (100.0 * (1 - ecart_total / theorique_total)
                if theorique_total > 1e-9 else None)
    return {
        "jours_cycle": jours_cycle,
        "dernier_comptage": dernier,
        "depuis": depuis,
        "produits_comptes": nb,
        "produits_conformes": conformes,
        "seuil_conformite": SEUIL_CONFORMITE,
        "precision_moyenne": arrondir(somme_precision / nb) if nb else None,
        "precision_ponderee": arrondir(max(0.0, ponderee)) if ponderee is not None else None,
        "ecart_absolu_total": arrondir(ecart_total),
        "theorique_total": arrondir(theorique_total),
        "lignes": lignes,
    }


def _parse(horodatage: str) -> datetime:
    """Relit un `created_at` de la base. Retombe sur l'heure courante si la
    valeur est illisible : un horodatage exotique doit dégrader l'indicateur,
    jamais faire tomber l'écran qui l'affiche en 500."""
    try:
        return datetime.strptime(normaliser(horodatage), FORMAT)
    except (ValueError, TypeError):
        return maintenant()


# --------------------------------------------------- 2. concentration régions


def concentration_regions(conn, jours: int = 90,
                          sector: str = SECTEUR_DEFAUT) -> dict:
    """Part de chaque région dans le volume livré, avec cumul (logique ABC).

    Somme des quantités des lignes de bons `DELIVERY` non annulés sur la
    fenêtre, groupée par région, triée décroissant. Le cumul permet de lire
    directement combien de régions font 80 % du volume : `regions_pour_80`.

    Les quantités `DELIVERY` sont stockées positives (le sens du mouvement est
    porté par le type du bon), donc `SUM` suffit — pas de valeur absolue.
    """
    depuis = (maintenant() - timedelta(days=jours)).strftime(FORMAT)
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(d.region), ''), 'Non renseignée') AS region,
               COALESCE(SUM(dl.quantity), 0) AS quantite,
               COUNT(DISTINCT d.id) AS nb_bons
        FROM documents d
        JOIN document_lines dl ON dl.document_id = d.id
        WHERE d.type = 'DELIVERY' AND d.cancelled_at IS NULL
          AND d.created_at >= ? AND d.sector = ?
        GROUP BY region
        HAVING quantite > 0
        ORDER BY quantite DESC, region
        """,
        (depuis, sector),
    ).fetchall()

    total = sum(r["quantite"] for r in rows)
    lignes = []
    cumul = 0.0
    regions_pour_80 = 0
    for r in rows:
        cumul += r["quantite"]
        part = 100.0 * r["quantite"] / total if total > 1e-9 else 0.0
        part_cumulee = 100.0 * cumul / total if total > 1e-9 else 0.0
        # Le rang qui FAIT franchir 80 % est inclus : « 3 régions font 80 % »
        # se lit correctement même si le cumul exact tombe à 82 %.
        if regions_pour_80 == 0 and part_cumulee >= 80.0 - 1e-9:
            regions_pour_80 = len(lignes) + 1
        lignes.append({
            "region": r["region"], "quantite": arrondir(r["quantite"]),
            "nb_bons": r["nb_bons"], "part": arrondir(part),
            "part_cumulee": arrondir(part_cumulee),
        })

    return {
        "jours": jours, "depuis": depuis,
        "total": arrondir(total),
        "nb_regions": len(lignes),
        "regions_pour_80": regions_pour_80,
        "lignes": lignes,
    }


# ------------------------------------------------ 3. comptages régionaux à temps


def comptages_a_temps(conn, reference=None,
                      sector: str = SECTEUR_DEFAUT) -> dict:
    """Régions à entrepôt ayant rendu au moins un comptage ce mois calendaire.

    Le périmètre est `REGIONS_AVEC_ENTREPOT` (12 régions) : Port-au-Prince est
    servi directement par le central et n'a pas d'entrepôt à compter, donc
    l'inclure ferait mécaniquement chuter le taux sans qu'aucune faute n'ait
    été commise.

    Un comptage saisi hors référentiel (région supprimée du référentiel, ou
    orthographe non normalisée d'avant la normalisation) n'est pas ignoré en
    silence : il ressort dans `hors_perimetre`.
    """
    debut, fin = _bornes_mois(reference)
    rows = conn.execute(
        """
        SELECT region, COUNT(*) AS nb_lignes,
               COUNT(DISTINCT DATE(created_at)) AS nb_jours,
               MAX(created_at) AS dernier
        FROM regional_inventory_reports
        WHERE created_at >= ? AND created_at < ? AND sector = ?
        GROUP BY region
        """,
        (debut, fin, sector),
    ).fetchall()

    par_region = {r["region"]: r for r in rows}
    a_temps, manquantes = [], []
    for region in REGIONS_AVEC_ENTREPOT:
        r = par_region.get(region)
        if r:
            a_temps.append({"region": region, "nb_lignes": r["nb_lignes"],
                            "nb_jours": r["nb_jours"], "dernier": r["dernier"]})
        else:
            manquantes.append(region)

    attendues = len(REGIONS_AVEC_ENTREPOT)
    hors_perimetre = sorted(set(par_region) - set(REGIONS_AVEC_ENTREPOT))
    return {
        "mois": debut[:7],
        "debut": debut, "fin": fin,
        "regions_attendues": attendues,
        "regions_a_temps": len(a_temps),
        "taux": arrondir(100.0 * len(a_temps) / attendues) if attendues else 0.0,
        "a_temps": a_temps,
        "manquantes": manquantes,
        "hors_perimetre": hors_perimetre,
    }


# ------------------------------------------------------- 4. anomalies du mois


def anomalies_mouvements(conn, facteur: float = 3.0, mois_historique: int = 12,
                         min_historique: int = 3, limite: int = 10,
                         reference=None, sector: str = SECTEUR_DEFAUT) -> dict:
    """Mouvements du mois anormalement gros au regard de l'habitude du produit.

    Différent du « Top 5 des sorties » du tableau de bord, qui classe par
    volume brut : un produit dont on sort 500 gallons chaque mois y figure
    toujours, sans que rien d'anormal ne se passe. Ici on compare chaque ligne
    de bon du mois à la moyenne HISTORIQUE de ce même produit (mois
    précédents), et on ne retient que celles qui la dépassent d'un facteur.

    Historique : lignes RECEIVING/DELIVERY non annulées des `mois_historique`
    mois précédant le mois en cours — le mois en cours en est exclu, sinon
    l'anomalie se comparerait à elle-même et se dissoudrait.

    `min_historique` lignes minimum : sans cela, un produit livré une seule
    fois dans sa vie déclencherait une « anomalie » au deuxième mouvement.

    Tri par `ratio = quantité / moyenne` décroissant : c'est l'écart à
    l'habitude qui trie, pas le volume.
    """
    debut_mois, fin_mois = _bornes_mois(reference)
    annee, mois = int(debut_mois[:4]), int(debut_mois[5:7])
    total_mois = annee * 12 + (mois - 1) - mois_historique
    debut_hist = f"{total_mois // 12:04d}-{total_mois % 12 + 1:02d}-01 00:00:00"

    rows = conn.execute(
        """
        WITH historique AS (
            SELECT dl.product_id,
                   AVG(ABS(dl.quantity)) AS moyenne,
                   COUNT(*) AS nb_mouvements
            FROM document_lines dl
            JOIN documents d ON d.id = dl.document_id
            WHERE d.cancelled_at IS NULL
              AND d.type IN ('RECEIVING', 'DELIVERY')
              AND d.created_at >= ? AND d.created_at < ?
              AND d.sector = ?
            GROUP BY dl.product_id
        )
        SELECT d.id AS document_id, d.type, d.reference, d.region, d.created_at,
               d.created_by, d.operator, p.sku, p.name,
               ABS(dl.quantity) AS quantite,
               h.moyenne, h.nb_mouvements,
               ABS(dl.quantity) / h.moyenne AS ratio
        FROM document_lines dl
        JOIN documents d ON d.id = dl.document_id
        JOIN products p ON p.id = dl.product_id
        JOIN historique h ON h.product_id = dl.product_id
        WHERE d.cancelled_at IS NULL
          AND d.type IN ('RECEIVING', 'DELIVERY')
          AND d.created_at >= ? AND d.created_at < ?
          AND d.sector = ?
          AND h.nb_mouvements >= ?
          AND h.moyenne > 0
          AND ABS(dl.quantity) >= h.moyenne * ?
        ORDER BY ratio DESC, quantite DESC
        LIMIT ?
        """,
        (debut_hist, debut_mois, sector, debut_mois, fin_mois, sector,
         min_historique, facteur, limite),
    ).fetchall()

    lignes = [{
        "document_id": r["document_id"], "type": r["type"],
        "reference": r["reference"], "region": r["region"],
        "created_at": r["created_at"],
        "created_by": r["created_by"] or r["operator"] or "",
        "sku": r["sku"], "name": r["name"],
        "quantite": arrondir(r["quantite"]),
        "moyenne_historique": arrondir(r["moyenne"]),
        "nb_mouvements_historique": r["nb_mouvements"],
        "ratio": arrondir(r["ratio"]),
    } for r in rows]

    return {
        "mois": debut_mois[:7],
        "debut": debut_mois, "fin": fin_mois,
        "debut_historique": debut_hist,
        "facteur": facteur,
        "min_historique": min_historique,
        "nb_anomalies": len(lignes),
        "lignes": lignes,
    }


# ------------------------------- 5. consommation par produit et par région


def _debut_fenetre_mois(mois: int, reference=None) -> str:
    """Premier instant du mois situé `mois - 1` mois avant le mois courant.

    Douze mois glissants incluent le mois en cours : la fenêtre commence donc
    onze mois avant lui, pas douze.
    """
    debut_courant, _fin = _bornes_mois(reference)
    annee, mois_courant = int(debut_courant[:4]), int(debut_courant[5:7])
    total = annee * 12 + (mois_courant - 1) - (mois - 1)
    return f"{total // 12:04d}-{total % 12 + 1:02d}-01 00:00:00"


def consommation_regionale(conn, mois: int = 12, reference=None,
                           sector: str = SECTEUR_DEFAUT) -> dict:
    """Sorties mensuelles par région et par produit sur les `mois` derniers mois.

    « Sortie » = marchandise qui QUITTE un entrepôt vers une région, sous ses
    deux formes :
      - `DELIVERY`          : le central sert une région (ou un technicien) ;
      - `REGIONAL_TRANSFER` : une région sert une autre région. La quantité
        est comptée pour la région d'ORIGINE (`source_region`), qui est celle
        qui s'en dessaisit — la région destinataire, elle, la recevra et la
        consommera plus tard, sans qu'on la compte deux fois.

    Les ajustements et les retours sont exclus, comme dans les autres
    indicateurs de ce module : un ajustement n'est pas une consommation, et
    un retour est un flux inverse dont l'inclusion (en négatif) rendrait la
    lecture d'un histogramme mensuel ambiguë.

    Ce qui sort de l'entrepôt d'une région est le seul relevé de consommation
    dont dispose le système : rien n'enregistre le service quotidien fait aux
    techniciens sur place. C'est donc une consommation APPROVISIONNÉE, ce que
    le libellé de l'écran dit explicitement.

    Renvoie une liste plate `{region, product_id, sku, name, unit, mois,
    quantite}` — directement exploitable par un tableau comme par un
    graphique, sans retraitement côté client.
    """
    depuis = _debut_fenetre_mois(mois, reference)
    _debut_courant, fin = _bornes_mois(reference)

    rows = conn.execute(
        """
        SELECT COALESCE(
                   NULLIF(TRIM(CASE WHEN d.type = 'REGIONAL_TRANSFER'
                                    THEN d.source_region ELSE d.region END), ''),
                   'Non renseignée') AS region,
               dl.product_id, p.sku, p.name, p.unit,
               SUBSTR(d.created_at, 1, 7) AS mois,
               ROUND(SUM(dl.quantity), 3) AS quantite
        FROM document_lines dl
        JOIN documents d ON d.id = dl.document_id
        JOIN products p ON p.id = dl.product_id
        WHERE d.cancelled_at IS NULL
          AND d.type IN ('DELIVERY', 'REGIONAL_TRANSFER')
          AND d.created_at >= ? AND d.created_at < ?
          AND d.sector = ?
        GROUP BY region, dl.product_id, mois
        HAVING SUM(dl.quantity) > 0
        ORDER BY region, p.name, mois
        """,
        (depuis, fin, sector),
    ).fetchall()

    lignes = [dict(r) for r in rows]

    # Totaux prêts à l'emploi : l'écran affiche d'abord « qui consomme le
    # plus », le détail mensuel n'arrive qu'ensuite. Les recalculer côté
    # client aurait imposé la même boucle dans chaque appelant.
    par_region: dict[str, float] = {}
    par_mois: dict[str, float] = {}
    for l in lignes:
        par_region[l["region"]] = par_region.get(l["region"], 0.0) + l["quantite"]
        par_mois[l["mois"]] = par_mois.get(l["mois"], 0.0) + l["quantite"]

    return {
        "mois": mois,
        "depuis": depuis,
        "jusqu_a": fin,
        "nb_lignes": len(lignes),
        "total": arrondir(sum(l["quantite"] for l in lignes)),
        "lignes": lignes,
        "totaux_par_region": [
            {"region": r, "quantite": arrondir(q)}
            for r, q in sorted(par_region.items(), key=lambda kv: -kv[1])
        ],
        "totaux_par_mois": [
            {"mois": m, "quantite": arrondir(q)} for m, q in sorted(par_mois.items())
        ],
    }


# ------------------------------------- 6. produits dormants et valeur immobilisée


# Six mois sans la moindre sortie : le produit n'est plus consommé, ou plus
# demandé. Six mois et non trois : certains consommables (filtres de gros
# entretien) ont un cycle naturellement long, et les signaler tous les
# trimestres noierait les vrais dormants dans le bruit.
JOURS_DORMANCE = 180


def produits_dormants(conn, jours: int = JOURS_DORMANCE,
                      sector: str = SECTEUR_DEFAUT) -> dict:
    """Produits actifs sans aucune SORTIE depuis `jours` jours, et ce qu'ils coûtent.

    « Sortie » se lit comme dans le reste du module, plus l'ajustement à la
    baisse :
      - `DELIVERY`           : le central sert une région ou un technicien ;
      - `REGIONAL_TRANSFER`  : une région se dessaisit au profit d'une autre ;
      - `ADJUSTMENT` négatif : un comptage a constaté moins que le théorique,
        donc de la marchandise a bel et bien quitté l'étagère, sans bon.

    Une RÉCEPTION ne réveille pas un produit : recevoir de la marchandise
    qu'on ne sort jamais est exactement le problème que cet indicateur montre.

    La valeur immobilisée est `current_stock × unit_cost`, comme partout
    ailleurs dans l'application. Un produit sans coût unitaire renseigné pèse
    donc zéro : il ressort quand même dans la liste (`valeur` à 0), et
    `nb_sans_cout` dit de combien de lignes le total est sous-estimé — un
    total qui ment en silence serait pire qu'un total absent.

    Les produits archivés sont hors périmètre : ils sont déjà sortis du
    catalogue, les signaler comme dormants n'apprendrait rien.
    """
    depuis = (maintenant() - timedelta(days=jours)).strftime(FORMAT)
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT p.id AS product_id, p.sku, p.name, p.category, p.unit,
                   COALESCE(p.current_stock, 0) AS current_stock,
                   COALESCE(p.unit_cost, 0) AS unit_cost,
                   p.unit_cost AS cout_renseigne,
                   ROUND(COALESCE(p.current_stock, 0)
                         * COALESCE(p.unit_cost, 0), 2) AS valeur,
                   (SELECT MAX(d.created_at)
                      FROM document_lines dl
                      JOIN documents d ON d.id = dl.document_id
                     WHERE dl.product_id = p.id
                       AND d.cancelled_at IS NULL
                       AND (d.type IN ('DELIVERY', 'REGIONAL_TRANSFER')
                            OR (d.type = 'ADJUSTMENT' AND dl.quantity < 0))
                   ) AS derniere_sortie
            FROM products p
            WHERE COALESCE(p.archived, 0) = 0 AND p.sector = ?
        )
        WHERE derniere_sortie IS NULL OR derniere_sortie < ?
        ORDER BY valeur DESC, name
        """,
        (sector, depuis),
    ).fetchall()

    lignes = []
    for r in rows:
        ligne = dict(r)
        cout_renseigne = ligne.pop("cout_renseigne")
        ligne["cout_renseigne"] = cout_renseigne is not None
        derniere = ligne["derniere_sortie"]
        ligne["jours_sans_sortie"] = (
            None if not derniere
            else max(0, (maintenant() - _parse(derniere)).days))
        lignes.append(ligne)

    avec_stock = [l for l in lignes if l["current_stock"] > 1e-9]
    return {
        "jours": jours,
        "depuis": depuis,
        "nb_produits": len(lignes),
        "nb_avec_stock": len(avec_stock),
        "nb_jamais_sorti": sum(1 for l in lignes if l["derniere_sortie"] is None),
        "nb_sans_cout": sum(1 for l in avec_stock if not l["cout_renseigne"]),
        "valeur_immobilisee": arrondir(sum(l["valeur"] for l in lignes)),
        "lignes": lignes,
    }


# ------------------------- 7. résolution des signalements d'alerte régionale


def resolution_alertes(conn, jours: int = 90,
                       sector: str = SECTEUR_DEFAUT) -> dict:
    """Signalements de rupture régionale : résolus, ouverts, et en combien de temps.

    Un signalement (`regional_alert_signals`) est ouvert par une région qui
    dit « je n'ai plus de X », et fermé par le central quand il a
    réapprovisionné. Sans cette mesure, personne ne sait si le central répond
    en une journée ou en trois semaines — ni s'il répond du tout à certaines
    régions.

    Fenêtre sur `created_at` : ce sont les demandes des `jours` derniers jours
    qu'on juge, résolues ou non. Filtrer sur `resolved_at` aurait exclu les
    demandes anciennes toujours ouvertes, c'est-à-dire précisément les pires.

    Le délai est calculé par SQLite (`julianday`), en heures, et plancher à
    zéro : un `resolved_at` antérieur au `created_at` ne peut venir que d'un
    réglage d'horloge, et une moyenne négative ne voudrait rien dire.

    L'ancienneté du plus vieux signalement encore ouvert accompagne le taux :
    une région à 90 % de résolution dont la demande restante traîne depuis
    deux mois n'est pas bien servie.
    """
    depuis = (maintenant() - timedelta(days=jours)).strftime(FORMAT)
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(region), ''), 'Non renseignée') AS region,
               COUNT(*) AS total,
               SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolus,
               SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS ouverts,
               AVG(CASE WHEN resolved_at IS NOT NULL
                        THEN MAX(0, (julianday(resolved_at)
                                     - julianday(created_at)) * 24.0)
                   END) AS delai_moyen_h,
               MAX(CASE WHEN resolved_at IS NOT NULL
                        THEN MAX(0, (julianday(resolved_at)
                                     - julianday(created_at)) * 24.0)
                   END) AS delai_max_h,
               MIN(CASE WHEN resolved_at IS NULL THEN created_at END) AS plus_ancien_ouvert
        FROM regional_alert_signals
        WHERE created_at >= ? AND sector = ?
        GROUP BY region
        ORDER BY ouverts DESC, total DESC, region
        """,
        (depuis, sector),
    ).fetchall()

    lignes = []
    for r in rows:
        ligne = dict(r)
        for champ in ("delai_moyen_h", "delai_max_h"):
            ligne[champ] = (None if ligne[champ] is None
                            else arrondir(max(0.0, ligne[champ])))
        ligne["taux_resolution"] = (
            arrondir(100.0 * ligne["resolus"] / ligne["total"])
            if ligne["total"] else 0.0)
        ancien = ligne["plus_ancien_ouvert"]
        ligne["jours_plus_ancien_ouvert"] = (
            None if not ancien else max(0, (maintenant() - _parse(ancien)).days))
        lignes.append(ligne)

    total = sum(l["total"] for l in lignes)
    resolus = sum(l["resolus"] for l in lignes)
    # Moyenne PONDÉRÉE par le nombre de signalements résolus : faire la moyenne
    # des moyennes régionales donnerait le même poids à une région qui a eu un
    # seul signalement et à une qui en a eu trente.
    pesee = sum(l["delai_moyen_h"] * l["resolus"]
                for l in lignes if l["delai_moyen_h"] is not None)
    return {
        "jours": jours,
        "depuis": depuis,
        "total": total,
        "resolus": resolus,
        "ouverts": total - resolus,
        "taux_resolution": arrondir(100.0 * resolus / total) if total else 0.0,
        "delai_moyen_h": arrondir(pesee / resolus) if resolus else None,
        "lignes": lignes,
    }


# ---------------------------------------------- 8. dette de consigne bouteille


def dette_consigne(conn, sector: str = SECTEUR_DEFAUT) -> dict:
    """Bouteilles consignées non rendues, par région et par technicien.

    `bottle_ledger` porte un mouvement par ligne : positif quand des bouteilles
    partent (elles sont dues), négatif quand elles reviennent. Le solde net est
    donc leur somme — même convention que `/bottles/balance`, qui reste la vue
    produit par produit.

    Ici on agrège au niveau du DÉTENTEUR (région, et technicien quand la
    livraison était nominative), pas du produit : la question posée n'est pas
    « quelles bouteilles manquent » mais « à qui les réclamer ». Une ligne
    portant un technicien porte aussi sa région : elle apparaît donc sur la
    ligne de ce technicien, jamais en double avec celle de la région.

    Les mouvements rattachés à un bon ANNULÉ sont ignorés (le bon a été
    extourné, les bouteilles ne sont jamais parties) ; les retours saisis à la
    main n'ont pas de bon et sont toujours comptés — même règle que
    `/bottles/balance`.

    Seuls les soldes non nuls sont renvoyés. Un solde négatif (plus de retours
    que de sorties) n'est pas filtré : il signale une erreur de saisie qu'il
    vaut mieux voir que masquer.
    """
    rows = conn.execute(
        """
        SELECT region, technician,
               SUM(bouteilles) AS solde,
               COUNT(*) AS nb_mouvements,
               MAX(created_at) AS dernier_mouvement
        FROM (
            -- Sous-requête indispensable : `documents` porte AUSSI une colonne
            -- `region`, donc un GROUP BY sur l'alias serait ambigu.
            SELECT COALESCE(NULLIF(TRIM(bl.region), ''), 'Non renseignée') AS region,
                   bl.technician, bl.created_at,
                   CASE WHEN d.id IS NULL OR d.cancelled_at IS NULL
                        THEN bl.bottles ELSE 0 END AS bouteilles
            FROM bottle_ledger bl
            LEFT JOIN documents d ON d.id = bl.document_id
            -- `bottle_ledger` ne porte pas de secteur : il vient du produit
            -- consigné. La jointure est INNER à dessein — une ligne dont le
            -- produit a disparu ne peut plus être rattachée à personne, et la
            -- compter dans le secteur de l'appelant serait une invention.
            JOIN products p ON p.id = bl.product_id AND p.sector = ?
        )
        GROUP BY region, technician
        HAVING ABS(solde) > 1e-9
        ORDER BY solde DESC, region
        """,
        (sector,),
    ).fetchall()

    lignes = []
    for r in rows:
        ligne = dict(r)
        ligne["solde"] = arrondir(ligne["solde"])
        dernier = ligne["dernier_mouvement"]
        ligne["jours_depuis_dernier"] = (
            None if not dernier else max(0, (maintenant() - _parse(dernier)).days))
        lignes.append(ligne)

    par_region: dict[str, float] = {}
    for l in lignes:
        par_region[l["region"]] = par_region.get(l["region"], 0.0) + l["solde"]

    dues = [l for l in lignes if l["solde"] > 1e-9]
    return {
        "nb_detenteurs": len(dues),
        "bouteilles_dues": arrondir(sum(l["solde"] for l in dues)),
        "nb_soldes_negatifs": sum(1 for l in lignes if l["solde"] < -1e-9),
        "plus_ancienne_dette_jours": max(
            (l["jours_depuis_dernier"] for l in dues
             if l["jours_depuis_dernier"] is not None), default=None),
        "totaux_par_region": [
            {"region": r, "solde": arrondir(s)}
            for r, s in sorted(par_region.items(), key=lambda kv: -kv[1])
        ],
        "lignes": lignes,
    }


# --------------------------------------------------- 9. activité par opérateur


# Bornes de la journée de travail de l'entrepôt. Une saisie hors de cette plage
# n'est pas une faute — un camion peut arriver tard — mais elle mérite d'être
# vue : c'est aussi la signature d'un compte utilisé par quelqu'un d'autre que
# son titulaire.
HEURE_OUVRABLE_DEBUT = 6
HEURE_OUVRABLE_FIN = 18


def activite_operateurs(conn, jours: int = 30,
                        sector: str = SECTEUR_DEFAUT) -> dict:
    """Nombre de bons saisis par compte, et leur répartition par heure.

    Deux opérateurs se partagent la saisie : savoir qui a fait quoi permet
    d'équilibrer la charge, et la répartition horaire fait ressortir une saisie
    faite à trois heures du matin — qui mérite une question, pas une sanction.

    Compte les bons CRÉÉS sur la fenêtre, annulés compris : un bon annulé a
    quand même été saisi, c'est du travail fait. Leur nombre est isolé dans
    `annules`, parce qu'un opérateur qui annule la moitié de ses bons dit autre
    chose qu'un opérateur qui n'en annule aucun.

    Le regroupement se fait sur `created_by` — le compte connecté — et non sur
    `operator`, qui nomme le superviseur destinataire du bon et ne dit rien de
    qui a tapé.

    L'agrégation par (opérateur, heure) est faite en SQL : la boucle Python ne
    parcourt au pire que 24 lignes par opérateur, jamais les bons un à un.
    """
    depuis = (maintenant() - timedelta(days=jours)).strftime(FORMAT)
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(created_by), ''), 'Non renseigné') AS operateur,
               CAST(SUBSTR(REPLACE(created_at, 'T', ' '), 12, 2) AS INTEGER) AS heure,
               COUNT(*) AS nb,
               SUM(CASE WHEN cancelled_at IS NOT NULL THEN 1 ELSE 0 END) AS annules,
               MAX(created_at) AS dernier
        FROM documents
        WHERE created_at >= ? AND sector = ?
        GROUP BY operateur, heure
        """,
        (depuis, sector),
    ).fetchall()

    par_operateur: dict[str, dict] = {}
    par_heure_global = [0] * 24
    for r in rows:
        op = par_operateur.setdefault(r["operateur"], {
            "operateur": r["operateur"], "nb_bons": 0, "annules": 0,
            "hors_heures": 0, "par_heure": [0] * 24, "dernier": None,
        })
        op["nb_bons"] += r["nb"]
        op["annules"] += r["annules"]
        if r["dernier"] and (op["dernier"] is None or r["dernier"] > op["dernier"]):
            op["dernier"] = r["dernier"]
        heure = r["heure"]
        # Un horodatage illisible (heure hors 0-23) n'est pas rangé dans une
        # case arbitraire : il compte dans le total, pas dans l'histogramme.
        if heure is None or not 0 <= heure <= 23:
            continue
        op["par_heure"][heure] += r["nb"]
        par_heure_global[heure] += r["nb"]
        if heure < HEURE_OUVRABLE_DEBUT or heure >= HEURE_OUVRABLE_FIN:
            op["hors_heures"] += r["nb"]

    lignes = sorted(par_operateur.values(),
                    key=lambda o: (-o["nb_bons"], o["operateur"]))
    for op in lignes:
        pic = max(range(24), key=lambda h: op["par_heure"][h])
        op["heure_pic"] = pic if op["par_heure"][pic] else None
        op["part_hors_heures"] = (
            arrondir(100.0 * op["hors_heures"] / op["nb_bons"])
            if op["nb_bons"] else 0.0)

    return {
        "jours": jours,
        "depuis": depuis,
        "heure_ouvrable_debut": HEURE_OUVRABLE_DEBUT,
        "heure_ouvrable_fin": HEURE_OUVRABLE_FIN,
        "nb_operateurs": len(lignes),
        "total_bons": sum(o["nb_bons"] for o in lignes),
        "total_hors_heures": sum(o["hors_heures"] for o in lignes),
        "par_heure": par_heure_global,
        "lignes": lignes,
    }


# ------------------------------------- 10. écarts de réception cumulés


# Un écart de réception se lit ligne par ligne à la confirmation, puis
# disparaît de l'écran. Rien n'était conservé sous forme agrégée — donc
# impossible de répondre à « quelle région, quel transporteur nous perd de la
# marchandise, et combien ». Ce rapport le reconstitue à partir de ce qui EST
# déjà en base : `document_lines.received_quantity`, écrit par
# `/documents/{id}/confirmer-reception`, face à `document_lines.quantity`.
# Aucune table nouvelle : un écart recalculé depuis les quantités réelles ne
# peut pas diverger de la vérité, un écart recopié ailleurs le pourrait.
#
# CHOIX DE LECTURE : manquants et excédents sont comptés SÉPARÉMENT, en
# valeurs positives, et le net n'est donné qu'en complément. Les additionner
# d'emblée effacerait les deux problèmes l'un par l'autre : une région qui
# reçoit 10 de moins sur un article et 10 de plus sur un autre affiche un net
# nul alors qu'elle a DEUX anomalies. Le manquant est le chiffre qui compte
# (marchandise partie et jamais arrivée) ; l'excédent signale une erreur de
# comptage ou d'expédition, tout aussi réelle.
#
# À savoir sur les excédents : `confirmer-reception` refuse aujourd'hui de
# recevoir PLUS que ce qui est parti (créditer davantage fabriquerait du stock
# à partir de rien). Aucun excédent ne peut donc plus naître par l'API — la
# colonne porte les lignes d'avant ce garde-fou et celles qu'une reprise de
# données réintroduirait. La retirer reviendrait à taire des lignes fausses.


def ecarts_reception(conn, jours: int = 90,
                     sector: str = SECTEUR_DEFAUT) -> dict:
    """Écarts entre quantité expédiée et quantité confirmée reçue.

    Périmètre : les bons dont la réception a été confirmée sur la fenêtre
    (`received_at`), non annulés. Les deux formes de transfert sont
    concernées, central -> région (DELIVERY) comme région -> région
    (REGIONAL_TRANSFER) : elles partagent la même confirmation.

    `received_quantity IS NULL` = ligne d'un bon jamais confirmé, ou d'un bon
    d'un autre type : elle est hors sujet, pas un écart de zéro.

    L'agrégation est faite en SQL, par région destinataire et par
    transporteur. Les boucles Python ne parcourent que ces groupes (au pire
    quelques dizaines de lignes), jamais les lignes de bons une à une.
    """
    depuis = (maintenant() - timedelta(days=jours)).strftime(FORMAT)

    groupes = conn.execute(
        """
        SELECT d.region AS region,
               COALESCE(NULLIF(TRIM(d.carrier), ''), '') AS carrier,
               COUNT(DISTINCT d.id) AS nb_bons,
               COUNT(*) AS nb_lignes,
               SUM(CASE WHEN dl.received_quantity < dl.quantity - 1e-9
                        THEN 1 ELSE 0 END) AS nb_lignes_manquantes,
               SUM(CASE WHEN dl.received_quantity > dl.quantity + 1e-9
                        THEN 1 ELSE 0 END) AS nb_lignes_excedentaires,
               SUM(dl.quantity) AS expedie,
               SUM(dl.received_quantity) AS recu,
               SUM(CASE WHEN dl.received_quantity < dl.quantity
                        THEN dl.quantity - dl.received_quantity ELSE 0 END) AS manquants,
               SUM(CASE WHEN dl.received_quantity > dl.quantity
                        THEN dl.received_quantity - dl.quantity ELSE 0 END) AS excedents
        FROM document_lines dl
        JOIN documents d ON d.id = dl.document_id
        WHERE d.received_at >= ?
          AND d.cancelled_at IS NULL
          AND dl.received_quantity IS NOT NULL
          AND d.sector = ?
        GROUP BY d.region, carrier
        ORDER BY manquants DESC, d.region
        """,
        (depuis, sector),
    ).fetchall()

    lignes = []
    for r in groupes:
        ligne = dict(r)
        for champ in ("expedie", "recu", "manquants", "excedents"):
            ligne[champ] = arrondir(ligne[champ] or 0)
        ligne["net"] = arrondir(ligne["recu"] - ligne["expedie"])
        # Part de la marchandise expédiée qui n'est jamais arrivée. C'est le
        # seul chiffre comparable d'un transporteur à l'autre : 50 unités
        # manquantes sur 100 expédiées et sur 100 000 ne disent pas la même
        # chose.
        ligne["taux_manquant"] = (
            arrondir(100.0 * ligne["manquants"] / ligne["expedie"])
            if ligne["expedie"] else 0.0)
        lignes.append(ligne)

    # Détail des lignes en écart, les plus grosses d'abord : c'est par là
    # qu'une enquête commence. Borné, l'agrégat au-dessus portant le total.
    detail = [dict(r) for r in conn.execute(
        """
        SELECT d.id AS document_id, d.region, d.carrier, d.type,
               d.received_at, d.received_by, d.source_region,
               p.sku, p.name, p.unit,
               dl.quantity AS expedie, dl.received_quantity AS recu,
               dl.received_quantity - dl.quantity AS ecart
        FROM document_lines dl
        JOIN documents d ON d.id = dl.document_id
        JOIN products p ON p.id = dl.product_id
        WHERE d.received_at >= ?
          AND d.cancelled_at IS NULL
          AND dl.received_quantity IS NOT NULL
          AND ABS(dl.received_quantity - dl.quantity) > 1e-9
          AND d.sector = ?
        ORDER BY ABS(dl.received_quantity - dl.quantity) DESC, d.id DESC
        LIMIT ?
        """,
        (depuis, sector, MAX_DETAIL_ECARTS),
    )]
    for d in detail:
        d["expedie"] = arrondir(d["expedie"] or 0)
        d["recu"] = arrondir(d["recu"] or 0)
        d["ecart"] = arrondir(d["ecart"] or 0)

    # Dénominateur : sans lui, « 3 bons en écart » ne veut rien dire. Doit
    # être filtré par secteur comme le détail ci-dessus, sinon un secteur
    # partageant la base avec d'autres voit son ratio faussé par leurs
    # réceptions à eux.
    receptions = conn.execute(
        "SELECT COUNT(*) AS n FROM documents "
        "WHERE received_at >= ? AND cancelled_at IS NULL AND sector = ?",
        (depuis, sector),
    ).fetchone()["n"]
    bons_en_ecart = len({d["document_id"] for d in detail})

    manquants = arrondir(sum(l["manquants"] for l in lignes))
    excedents = arrondir(sum(l["excedents"] for l in lignes))
    expedie = arrondir(sum(l["expedie"] for l in lignes))
    return {
        "jours": jours,
        "depuis": depuis,
        "nb_receptions": receptions,
        # Recompté sur le détail, donc borné par MAX_DETAIL_ECARTS : le
        # chiffre exact vit dans `nb_lignes_manquantes` / `_excedentaires`.
        "nb_bons_en_ecart": bons_en_ecart,
        "detail_tronque": len(detail) >= MAX_DETAIL_ECARTS,
        "expedie_total": expedie,
        "recu_total": arrondir(sum(l["recu"] for l in lignes)),
        "manquants_total": manquants,
        "excedents_total": excedents,
        "net_total": arrondir(excedents - manquants),
        "taux_manquant": arrondir(100.0 * manquants / expedie) if expedie else 0.0,
        "lignes": lignes,
        "detail": detail,
    }


# ------------------------------- 11. régularité d'approvisionnement fournisseur
#
# LIMITE DU SCHÉMA, À LIRE AVANT D'INTERPRÉTER CE RAPPORT
# =======================================================
# Le titre naturel de cette mesure serait « délai de livraison » : le nombre de
# jours entre la commande passée à un fournisseur et l'arrivée de la
# marchandise. CE CALCUL EST IMPOSSIBLE ICI, et il ne faut pas faire semblant.
#
# Le schéma ne modélise AUCUNE date de commande. Vérifié colonne par colonne :
# un bon `RECEIVING` porte `created_at` (date de saisie de la réception,
# saisissable donc antidatable par l'opérateur), `server_received_at` (date à
# laquelle le serveur a reçu la saisie) et `received_at` (confirmation de
# réception d'un transfert vers une région — jamais renseignée sur une
# réception fournisseur). Il n'existe ni table de commandes, ni bon de
# commande, ni colonne « date de commande » : le WMS commence son histoire au
# moment où la marchandise entre dans l'entrepôt. Fabriquer une « date de
# commande » à partir de ce qui existe reviendrait à inventer un chiffre.
#
# Ce que l'on PEUT mesurer avec ce qui est réellement en base, c'est le RYTHME :
# l'intervalle entre deux réceptions successives d'un même fournisseur. C'est
# un proxy de régularité d'approvisionnement, pas un délai de livraison, et
# l'écran le dit en toutes lettres. Il répond quand même à des questions
# concrètes : « ce fournisseur me livre-t-il toutes les trois semaines ou
# n'importe quand ? », « lequel n'a plus rien livré depuis deux fois son
# rythme habituel ? ».
#
# Le délai ANNONCÉ par le fournisseur (`contacts.delai_jours`, saisi à la main
# sur sa fiche) est renvoyé à côté, sans être mélangé au constat : c'est une
# promesse commerciale, pas une mesure. Les mettre l'un près de l'autre est
# exactement ce qui rend le rapport utile à un acheteur.
#
# Regroupement par JOUR et non par bon : trois bons saisis le même jour pour
# un même fournisseur sont un seul approvisionnement découpé en plusieurs
# pièces (un camion, trois bordereaux). Les compter séparément produirait des
# intervalles de zéro jour qui écraseraient toutes les moyennes.


def delai_fournisseurs(conn, jours: int = 365, min_receptions: int = 2,
                       sector: str = SECTEUR_DEFAUT) -> dict:
    """Régularité d'approvisionnement mesurée par fournisseur.

    PROXY, PAS UN DÉLAI DE LIVRAISON : voir le commentaire ci-dessus. Le
    schéma ne porte aucune date de commande, donc rien ne permet de calculer
    un délai commande -> réception. Ce qui est mesuré est l'intervalle entre
    deux réceptions successives du même fournisseur.

    Périmètre : bons `RECEIVING` non annulés, rattachés à un contact
    (`party_contact_id`), sur la fenêtre. Une réception saisie en texte libre
    (`party` seul, contact non enregistré) n'est rattachable à aucun
    fournisseur : elle est comptée dans `nb_receptions_sans_contact` plutôt
    qu'ignorée en silence — c'est la mesure de ce que le rapport ne voit pas.

    `min_receptions` fournisseurs : avec une seule réception dans la fenêtre,
    aucun intervalle n'existe. Ces fournisseurs ne sont pas jetés, ils sortent
    dans `sans_historique` avec la date de leur unique réception : « ce
    fournisseur n'a livré qu'une fois cette année » est une information.

    `retard` : jours écoulés depuis la dernière réception MOINS l'intervalle
    moyen. Positif = le fournisseur a dépassé son propre rythme habituel.
    C'est la seule colonne actionnable du rapport ; les autres décrivent.
    """
    depuis = (maintenant() - timedelta(days=jours)).strftime(FORMAT)

    rows = conn.execute(
        """
        WITH receptions AS (
            -- Un approvisionnement = un fournisseur, un JOUR (voir en-tête).
            SELECT d.party_contact_id AS contact_id,
                   DATE(d.created_at) AS jour,
                   COUNT(*) AS nb_bons
            FROM documents d
            WHERE d.type = 'RECEIVING'
              AND d.cancelled_at IS NULL
              AND d.party_contact_id IS NOT NULL
              AND d.created_at >= ?
              AND d.sector = ?
            GROUP BY contact_id, jour
        ),
        intervalles AS (
            SELECT contact_id, jour, nb_bons,
                   julianday(jour) - julianday(
                       LAG(jour) OVER (PARTITION BY contact_id ORDER BY jour)
                   ) AS ecart
            FROM receptions
        )
        SELECT c.id AS contact_id, c.name, c.active,
               c.delai_jours AS delai_annonce,
               c.devise, c.conditions_paiement,
               COUNT(*) AS nb_receptions,
               SUM(i.nb_bons) AS nb_bons,
               MIN(i.jour) AS premiere,
               MAX(i.jour) AS derniere,
               -- AVG/MIN/MAX ignorent le NULL de la toute première réception
               -- (elle n'a pas de précédente), ce qui est exactement voulu.
               AVG(i.ecart) AS intervalle_moyen,
               MIN(i.ecart) AS intervalle_min,
               MAX(i.ecart) AS intervalle_max
        FROM intervalles i
        JOIN contacts c ON c.id = i.contact_id AND c.sector = ?
        GROUP BY i.contact_id
        ORDER BY nb_receptions DESC, c.name
        """,
        (depuis, sector, sector),
    ).fetchall()

    lignes, sans_historique = [], []
    aujourd_hui = maintenant()
    for r in rows:
        ligne = dict(r)
        ligne["active"] = bool(ligne["active"])
        derniere = ligne["derniere"]
        # `derniere` est une DATE nue (YYYY-MM-DD) : _parse attend un horodatage
        # complet, d'où le complément à minuit. Un jour illisible retombe sur
        # l'heure courante et donne 0 jour, jamais une exception.
        jours_depuis = max(0, (aujourd_hui - _parse(f"{derniere} 00:00:00")).days)
        ligne["jours_depuis_derniere"] = jours_depuis

        if ligne["nb_receptions"] < min_receptions or ligne["intervalle_moyen"] is None:
            for champ in ("intervalle_moyen", "intervalle_min", "intervalle_max"):
                ligne[champ] = None
            ligne["retard"] = None
            ligne["ecart_annonce"] = None
            ligne["regulier"] = None
            sans_historique.append(ligne)
            continue

        for champ in ("intervalle_moyen", "intervalle_min", "intervalle_max"):
            ligne[champ] = arrondir(ligne[champ])
        moyen = ligne["intervalle_moyen"]
        ligne["retard"] = arrondir(jours_depuis - moyen)
        # Écart entre le rythme CONSTATÉ et le délai ANNONCÉ sur la fiche.
        # None si le fournisseur n'a rien annoncé : une comparaison avec une
        # valeur absente vaudrait zéro et se lirait « conforme », le contraire
        # de la vérité.
        annonce = ligne["delai_annonce"]
        ligne["ecart_annonce"] = (None if annonce is None
                                  else arrondir(moyen - float(annonce)))
        # « Régulier » : l'amplitude entre le plus court et le plus long
        # intervalle reste sous la moitié du rythme moyen. Un fournisseur qui
        # livre à 20, 22 et 21 jours est régulier ; à 3, 40 et 20 jours, non —
        # même moyenne, planification impossible.
        etendue = ligne["intervalle_max"] - ligne["intervalle_min"]
        ligne["etendue"] = arrondir(etendue)
        ligne["regulier"] = bool(moyen > 1e-9 and etendue <= moyen / 2)
        lignes.append(ligne)

    sans_contact = conn.execute(
        "SELECT COUNT(*) AS n FROM documents "
        "WHERE type = 'RECEIVING' AND cancelled_at IS NULL "
        "AND party_contact_id IS NULL AND created_at >= ? AND sector = ?",
        (depuis, sector),
    ).fetchone()["n"]

    mesures = [l["intervalle_moyen"] for l in lignes]
    en_retard = [l for l in lignes if (l["retard"] or 0) > 0]
    return {
        "jours": jours,
        "depuis": depuis,
        "min_receptions": min_receptions,
        # Rappel machine du choix de modélisation : un client qui affiche ce
        # rapport doit pouvoir dire à l'utilisateur ce qu'il regarde sans
        # avoir à le coder en dur de son côté.
        "mesure": "intervalle_entre_receptions",
        "nb_fournisseurs": len(lignes),
        "nb_sans_historique": len(sans_historique),
        "nb_receptions_sans_contact": sans_contact,
        "intervalle_moyen_global": (arrondir(sum(mesures) / len(mesures))
                                    if mesures else None),
        "nb_en_retard": len(en_retard),
        "nb_reguliers": sum(1 for l in lignes if l["regulier"]),
        "lignes": lignes,
        "sans_historique": sans_historique,
    }
