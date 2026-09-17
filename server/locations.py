"""Emplacements de stock et mouvements entre emplacements.

Modèle : l'entrepôt central ('TP WH') et un emplacement par couple
(région, superviseur). Une expédition ne fait pas disparaître le stock, elle le
déplace vers le superviseur, qui le détient jusqu'à consommation sur site.

`products.current_stock` reste le stock de l'entrepôt central — c'est la
signification qu'il avait déjà. Les mouvements ajoutent la visibilité sur ce
qui est parti et chez qui. L'égalité entre les deux est vérifiable à tout
moment via /stock/reconciliation.
"""

from models import SECTEUR_DEFAUT
from reference_data import a_entrepot_regional

REGION_NON_RENSEIGNEE = "Non renseignée"


def id_entrepot(conn) -> int:
    return conn.execute("SELECT id FROM locations WHERE type = 'WAREHOUSE'").fetchone()["id"]


def id_entrepot_regional(conn, region: str) -> int:
    """Emplacement de l'entrepôt régional d'une région, créé à la volée.

    Même principe que `id_emplacement_terrain`, mais volontairement distinct :
    un entrepôt régional détient le stock de la RÉGION, pas celui d'un
    superviseur. Les emplacements FIELD restent réservés aux détenteurs
    nominatifs (superviseurs, techniciens).
    """
    region = (region or "").strip()
    ligne = conn.execute(
        "SELECT id FROM locations WHERE type = 'REGIONAL_WAREHOUSE' AND region = ? "
        "AND supervisor = ''",
        (region,),
    ).fetchone()
    if ligne:
        return ligne["id"]
    cur = conn.execute(
        "INSERT INTO locations (type, region, supervisor, name) "
        "VALUES ('REGIONAL_WAREHOUSE', ?, '', ?)",
        (region, f"Entrepôt régional — {region}"),
    )
    return cur.lastrowid


def est_transfert_regional(document: dict) -> bool:
    """Vrai si ce bon est un transfert vers un entrepôt régional.

    Trois chemins de livraison coexistent, et un seul passe par le transit :
      - technicien nommé      -> emplacement FIELD du technicien (inchangé) ;
      - Port-au-Prince        -> emplacement FIELD du superviseur (inchangé) ;
      - une des 12 régions    -> entrepôt régional, EN DEUX TEMPS.
    """
    if document.get("type") != "DELIVERY":
        return False
    if (document.get("technician") or "").strip():
        return False
    return a_entrepot_regional(document.get("region"))


def est_transfert_inter_regional(document: dict) -> bool:
    """Vrai si ce bon est un transfert d'entrepôt régional à entrepôt régional.

    Même transit en deux temps que `est_transfert_regional`, mais l'origine
    n'est pas le central : c'est l'entrepôt régional de `source_region`.
    """
    return document.get("type") == "REGIONAL_TRANSFER"


def attend_confirmation_reception(document: dict) -> bool:
    """Vrai si ce bon se conclut par une confirmation de réception régionale.

    Les deux formes de transfert (central -> région, région -> région)
    partagent exactement le même second temps : c'est le point d'entrée
    unique du contrôle fait par `/documents/{id}/confirmer-reception`.
    """
    return est_transfert_regional(document) or est_transfert_inter_regional(document)


def id_emplacement_terrain(conn, region: str | None, supervisor: str | None) -> int:
    """Emplacement d'un superviseur, créé à la volée s'il n'existe pas."""
    region = (region or REGION_NON_RENSEIGNEE).strip() or REGION_NON_RENSEIGNEE
    supervisor = (supervisor or "").strip()

    ligne = conn.execute(
        "SELECT id FROM locations WHERE type = 'FIELD' AND region = ? AND supervisor = ?",
        (region, supervisor),
    ).fetchone()
    if ligne:
        return ligne["id"]

    nom = f"{region} — {supervisor}" if supervisor else region
    cur = conn.execute(
        "INSERT INTO locations (type, region, supervisor, name) VALUES ('FIELD', ?, ?, ?)",
        (region, supervisor, nom),
    )
    return cur.lastrowid


def enregistrer_mouvements(conn, document: dict, lignes: list[dict]) -> None:
    """Trace le déplacement de stock correspondant à un bon.

    RECEIVING         : extérieur -> entrepôt
    DELIVERY          : entrepôt  -> superviseur de la région
    RETURN            : superviseur de la région -> entrepôt
    SUPPLIER_RETURN   : entrepôt  -> fournisseur (extérieur, pas d'emplacement)
    ADJUSTMENT        : écart positif = entrée dans l'entrepôt, négatif = sortie
    REGIONAL_TRANSFER : entrepôt régional source -> (transit) -> entrepôt
                        régional destinataire, en deux temps comme un
                        transfert central -> région.
    """
    entrepot = id_entrepot(conn)
    type_doc = document["type"]

    # Origine d'un transfert région -> région. Reste None pour tous les
    # autres types, dont l'origine est l'entrepôt central.
    origine_regionale = None

    # Entrepôt régional d'arrivée, une fois la réception confirmée. Reste None
    # tant que le bon est en transit : le crédit d'arrivée est alors un SECOND
    # mouvement, posé par confirmer_reception avec la quantité réellement reçue.
    arrivee_regionale = None

    if type_doc == "DELIVERY":
        if est_transfert_regional(document):
            # Transfert vers un entrepôt régional : la marchandise quitte
            # l'entrepôt central immédiatement, mais n'est créditée nulle part
            # tant que la région n'a pas confirmé (received_at NULL). Même
            # forme qu'un ajustement négatif : une sortie sans destination.
            destination = None
            if document.get("received_at"):
                arrivee_regionale = id_entrepot_regional(conn, document["region"])
        else:
            # Un technicien ne dépend d'aucun superviseur : quand le bon lui est
            # nominativement adressé et qu'aucun superviseur n'est renseigné,
            # c'est LUI qui détient le stock livré. Sans cela, la marchandise
            # tomberait dans l'emplacement anonyme de la région et on ne saurait
            # plus chez qui elle est.
            porteur = (document.get("operator") or "").strip() or document.get("technician")
            destination = id_emplacement_terrain(conn, document.get("region"), porteur)
    elif type_doc == "REGIONAL_TRANSFER":
        # La marchandise quitte immédiatement l'entrepôt régional d'origine,
        # exactement comme elle quitte le central sur un transfert descendant,
        # et n'est créditée nulle part tant que la région destinataire n'a pas
        # confirmé. `products.current_stock` (le stock du CENTRAL) n'est pas
        # concerné : cette marchandise en est sortie il y a longtemps.
        origine_regionale = id_entrepot_regional(conn, document["source_region"])
        if document.get("received_at"):
            arrivee_regionale = id_entrepot_regional(conn, document["region"])
    elif type_doc == "RETURN" and document.get("region"):
        # Une région dotée d'un entrepôt régional y détient désormais son
        # stock confirmé : un retour doit en débiter CET emplacement, pas
        # l'ancien panier par superviseur qui ne reçoit plus rien depuis le
        # transfert en deux temps (sinon l'entrepôt régional ne bougeait
        # jamais et le panier superviseur pouvait devenir négatif).
        if a_entrepot_regional(document.get("region")):
            origine_terrain = id_entrepot_regional(conn, document["region"])
        else:
            origine_terrain = id_emplacement_terrain(conn, document.get("region"), document.get("operator"))

    for ligne in lignes:
        quantite = ligne["quantity"]

        if type_doc == "RECEIVING":
            source, cible = None, entrepot
        elif type_doc == "DELIVERY":
            source, cible = entrepot, destination
        elif type_doc == "RETURN":
            source = origine_terrain if document.get("region") else None
            cible = entrepot
        elif type_doc == "SUPPLIER_RETURN":
            source, cible = entrepot, None
        elif type_doc == "REGIONAL_TRANSFER":
            source, cible = origine_regionale, None
        else:  # ADJUSTMENT : la quantité est l'écart signé
            if quantite >= 0:
                source, cible = None, entrepot
            else:
                source, cible = entrepot, None
            quantite = abs(quantite)

        if quantite != 0:
            conn.execute(
                "INSERT INTO stock_movements (document_id, product_id, from_location_id, "
                "to_location_id, quantity) VALUES (?, ?, ?, ?, ?)",
                (document["id"], ligne["product_id"], source, cible, quantite),
            )

        # Second temps du transfert régional, quand le bon est DÉJÀ confirmé
        # reçu (reprise d'un historique). Le cas courant — confirmation après
        # coup — passe par confirmer_reception, pas par ici.
        if arrivee_regionale is not None:
            recue = ligne.get("received_quantity")
            if recue is None:
                recue = ligne["quantity"]
            if recue > 0:
                conn.execute(
                    "INSERT INTO stock_movements (document_id, product_id, from_location_id, "
                    "to_location_id, quantity) VALUES (?, ?, NULL, ?, ?)",
                    (document["id"], ligne["product_id"], arrivee_regionale, recue),
                )


def enregistrer_ouverture(conn, product_id: int, quantite: float) -> None:
    """Mouvement d'ouverture : le stock de départ entre dans l'entrepôt.

    Sans lui, le stock initial n'aurait aucune trace dans le grand livre et
    le stock par emplacement ne correspondrait pas au stock produit.
    """
    if quantite <= 0:
        return
    conn.execute(
        "INSERT INTO stock_movements (document_id, product_id, from_location_id, "
        "to_location_id, quantity) VALUES (NULL, ?, NULL, ?, ?)",
        (product_id, id_entrepot(conn), quantite),
    )


def ajuster_ouverture(conn, product_id: int, quantite: float) -> None:
    """Réaligne le mouvement d'ouverture sur le stock initial du produit.

    Sans cela, modifier `initial_stock` déplace `current_stock` mais laisse le
    grand livre des mouvements sur l'ancienne valeur : /stock/reconciliation
    signale alors un écart permanent qu'aucune saisie ne peut résorber.
    """
    ligne = conn.execute(
        "SELECT id FROM stock_movements WHERE product_id = ? AND document_id IS NULL",
        (product_id,),
    ).fetchone()

    if quantite <= 0:
        if ligne:
            conn.execute("DELETE FROM stock_movements WHERE id = ?", (ligne["id"],))
        return

    if ligne:
        conn.execute(
            "UPDATE stock_movements SET quantity = ?, to_location_id = ?, "
            "from_location_id = NULL WHERE id = ?",
            (quantite, id_entrepot(conn), ligne["id"]),
        )
    else:
        enregistrer_ouverture(conn, product_id, quantite)


def rattraper_ouvertures_manquantes(conn) -> int:
    """Crée les mouvements d'ouverture des produits antérieurs. Idempotent."""
    produits = conn.execute(
        """
        SELECT p.id, p.initial_stock FROM products p
        WHERE p.initial_stock > 0
          AND NOT EXISTS (
              SELECT 1 FROM stock_movements m
              WHERE m.product_id = p.id AND m.document_id IS NULL
          )
        """
    ).fetchall()
    for produit in produits:
        enregistrer_ouverture(conn, produit["id"], produit["initial_stock"])
    return len(produits)


def rattraper_documents_sans_mouvement(conn) -> int:
    """Génère les mouvements des bons antérieurs à cette fonctionnalité.

    Idempotent : ne traite que les documents qui n'ont aucun mouvement.
    """
    documents = conn.execute(
        """
        SELECT d.* FROM documents d
        WHERE NOT EXISTS (SELECT 1 FROM stock_movements m WHERE m.document_id = d.id)
        ORDER BY d.id
        """
    ).fetchall()

    traites = 0
    for doc in documents:
        doc = dict(doc)
        lignes = [
            dict(r) for r in conn.execute(
                "SELECT product_id, quantity, received_quantity FROM document_lines "
                "WHERE document_id = ?",
                (doc["id"],),
            )
        ]
        if not lignes:
            continue
        # Un bon antérieur au transit régional a déjà été livré dans la
        # réalité : le laisser « en transit » ferait disparaître son stock du
        # grand livre régional pour toujours. On le considère reçu tel
        # qu'expédié. Idempotent : une fois received_at posé, il ne bouge plus.
        if est_transfert_regional(doc) and not doc.get("received_at"):
            conn.execute(
                "UPDATE documents SET received_at = created_at, "
                "received_by = 'Reprise historique' WHERE id = ?",
                (doc["id"],),
            )
            conn.execute(
                "UPDATE document_lines SET received_quantity = quantity "
                "WHERE document_id = ? AND received_quantity IS NULL",
                (doc["id"],),
            )
            doc["received_at"] = doc["created_at"]
        enregistrer_mouvements(conn, doc, lignes)
        traites += 1
    return traites


# Un mouvement compte tant que son bon n'est pas annulé. Écrit en anti-jointure
# sur la liste (courte) des bons annulés plutôt qu'en LEFT JOIN ligne à ligne :
# le LEFT JOIN forçait une recherche dans `documents` pour CHACUN des
# mouvements, deux fois (une par branche de l'UNION). Ici SQLite ne lit que
# l'index idx_documents_cancelled, une seule fois par branche.
_MOUVEMENT_ACTIF = (
    "(m.document_id IS NULL OR m.document_id NOT IN "
    " (SELECT id FROM documents WHERE cancelled_at IS NOT NULL))"
)


def stock_entrepot_regional(conn, region: str, sector: str = SECTEUR_DEFAUT) -> list[dict]:
    """Stock détenu par l'entrepôt régional d'une région, produit par produit.

    Calculé depuis le grand livre des mouvements, comme tout le reste : seule
    la quantité CONFIRMÉE reçue y figure, jamais celle expédiée. Les produits
    archivés restent listés s'ils ont du stock — la région doit pouvoir les
    compter et les signaler.

    L'emplacement est résolu AVANT l'agrégation. Filtrer sur `l.region` après
    coup obligeait SQLite à additionner la totalité du grand livre — toutes
    régions, tout l'entrepôt central, tout l'historique — pour n'en garder
    qu'une région : 1,1 s sur cinq ans de mouvements, à chaque ouverture de
    l'écran régional ET à l'intérieur de la transaction d'annulation d'un bon,
    où elle bloquait toute autre écriture. Le filtre posé sur l'emplacement
    laisse au contraire jouer idx_movements_to / idx_movements_from.

    Scopé par `sector` (via le produit), comme `stock_par_emplacement` :
    `locations` n'a pas de colonne secteur (une région comme « Carrefour »
    existe pour Consumables ET pour FON, voir reference_data.py), donc
    `id_entrepot_regional` résout les DEUX au même emplacement physique.
    Sans ce filtre, un compte régional FON de Carrefour voyait le stock
    Consumables de Carrefour (et réciproquement) — constaté le 2026-09-09,
    jamais corrigé alors que `stock_par_emplacement` l'était déjà.
    """
    # Le schéma autorise plusieurs emplacements REGIONAL_WAREHOUSE par région
    # (unicité sur type+region+supervisor) : on les prend tous, comme le
    # faisait le filtre `l.region = ?` précédent.
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM locations WHERE type = 'REGIONAL_WAREHOUSE' AND region = ?",
        (region,),
    )]
    if not ids:
        return []
    marqueurs = ",".join("?" * len(ids))
    params = tuple(ids) * 2 + (sector,)

    return [dict(r) for r in conn.execute(
        f"""
        SELECT p.id AS product_id, p.sku, p.name, p.category, p.unit,
               p.unit_type, p.bidon_capacity, p.min_stock,
               ROUND(SUM(mvt.quantite), 3) AS quantite
        FROM (
            SELECT m.product_id, m.quantity AS quantite
            FROM stock_movements m
            WHERE m.to_location_id IN ({marqueurs}) AND {_MOUVEMENT_ACTIF}
            UNION ALL
            SELECT m.product_id, -m.quantity
            FROM stock_movements m
            WHERE m.from_location_id IN ({marqueurs}) AND {_MOUVEMENT_ACTIF}
        ) AS mvt
        JOIN products p ON p.id = mvt.product_id AND p.sector = ?
        GROUP BY p.id
        HAVING ABS(SUM(mvt.quantite)) > 1e-9
        ORDER BY p.name
        """,
        params,
    )]


def solde_regional_detail(conn, region: str, product_id: int) -> dict:
    """H (reçu confirmé), X (déjà sorti) et S = H − X pour UN produit d'une région.

    Même résolution d'emplacement et même filtre `_MOUVEMENT_ACTIF` que
    `stock_entrepot_regional`, mais H et X séparés plutôt que déjà nettés :
    STOCK-4 doit distinguer « jamais rien reçu » (H = 0, à refuser
    définitivement) de « solde épuisé mais historique réel » (H > 0, refus
    motivé mais pas la même nature d'anomalie) — un solde net seul ne permet
    pas cette distinction.
    """
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM locations WHERE type = 'REGIONAL_WAREHOUSE' AND region = ?",
        (region,),
    )]
    if not ids:
        return {"recu": 0.0, "sorti": 0.0, "solde": 0.0}
    marqueurs = ",".join("?" * len(ids))

    recu = conn.execute(
        f"SELECT COALESCE(SUM(m.quantity), 0) AS q FROM stock_movements m "
        f"WHERE m.to_location_id IN ({marqueurs}) AND m.product_id = ? "
        f"AND {_MOUVEMENT_ACTIF}",
        (*ids, product_id),
    ).fetchone()["q"]
    sorti = conn.execute(
        f"SELECT COALESCE(SUM(m.quantity), 0) AS q FROM stock_movements m "
        f"WHERE m.from_location_id IN ({marqueurs}) AND m.product_id = ? "
        f"AND {_MOUVEMENT_ACTIF}",
        (*ids, product_id),
    ).fetchone()["q"]
    return {
        "recu": round(recu, 3),
        "sorti": round(sorti, 3),
        "solde": round(recu - sorti, 3),
    }


def stock_par_emplacement(conn, product_id: int | None = None,
                          sector: str = SECTEUR_DEFAUT,
                          site: str | None = None) -> list[dict]:
    """Stock détenu par chaque emplacement, calculé depuis les mouvements.

    Les bons annulés sont exclus : leur mouvement n'a plus lieu d'être.
    Scopé par `sector` (via le produit) : un secteur ne doit jamais voir le
    stock par emplacement d'un autre.

    Le solde est agrégé par (emplacement, produit) AVANT la jointure sur
    `locations` et `products` : sans cette pré-agrégation, SQLite construisait
    un index temporaire sur les centaines de milliers de lignes brutes du
    grand livre pour n'en sortir que quelques milliers de soldes (837 ms sur
    cinq ans d'historique, contre 212 ms ici — résultats identiques).
    """
    filtre = "AND m.product_id = ?" if product_id else ""
    params = (product_id, product_id) if product_id else ()

    return [dict(r) for r in conn.execute(
        f"""
        SELECT l.id AS location_id, l.type, l.region, l.supervisor, l.name,
               p.id AS product_id, p.sku, p.name AS product_name, p.category, p.unit,
               -- ROUND comme dans stock_entrepot_regional : une somme de
               -- flottants dérive (0.1 + 0.1 + 0.1 = 0.30000000000000004) et
               -- l'écran Emplacements affichait cette dérive telle quelle,
               -- alors que le stock régional du même produit montrait 0,3.
               ROUND(SUM(mvt.quantite), 3) AS quantite
        FROM (
            SELECT location_id, product_id, SUM(quantite) AS quantite
            FROM (
                -- document_id NULL : mouvement d'ouverture, sans bon.
                SELECT m.to_location_id AS location_id, m.product_id,
                       m.quantity AS quantite
                FROM stock_movements m
                WHERE m.to_location_id IS NOT NULL AND {_MOUVEMENT_ACTIF} {filtre}
                UNION ALL
                SELECT m.from_location_id, m.product_id, -m.quantity
                FROM stock_movements m
                WHERE m.from_location_id IS NOT NULL AND {_MOUVEMENT_ACTIF} {filtre}
            )
            GROUP BY location_id, product_id
        ) AS mvt
        JOIN locations l ON l.id = mvt.location_id
        JOIN products p ON p.id = mvt.product_id AND p.sector = ?
            {"AND (p.site IS NULL OR p.site = ?)" if site else ""}
        GROUP BY l.id, p.id
        HAVING ABS(SUM(mvt.quantite)) > 1e-9
        ORDER BY l.type DESC, l.region, l.supervisor, p.name
        """,
        params + (sector,) + ((site,) if site else ()),
    )]
