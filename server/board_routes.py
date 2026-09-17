"""Routes du portail Board Management.

Lecture seule, multi-secteurs : le board voit tous les secteurs
(FUEL, CONSUMABLES, FON, RAN) sans pouvoir modifier quoi que ce soit.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from database import get_conn
from horodatage import FORMAT as FORMAT_DATETIME, maintenant
from models import ADMINS_MULTI_SECTEURS, est_admin_multi_secteurs
from reference_data import regions_avec_entrepot_for
from utilisateurs import exiger_board

router = APIRouter(prefix="/board/api", tags=["board"])

SECTEURS_VALIDES = ("FUEL", "CONSUMABLES", "FON", "RAN")

# Un comptage régional plus vieux que ce seuil est considéré périmé — même
# seuil que l'écran admin (/regions/tableau-de-bord) pour rester cohérent.
COMPTAGE_PERIME_JOURS = 35


def _jours_depuis(horodatage, aujourdhui):
    """Jours écoulés depuis un horodatage SQLite, ou None si illisible/absent."""
    if not horodatage:
        return None
    try:
        jour = date.fromisoformat(str(horodatage)[:10])
    except (ValueError, TypeError):
        return None
    return max(0, (aujourdhui - jour).days)


def _arrondir(v, n=2):
    if v is None:
        return 0
    return round(float(v), n)


# ------------------------------------------------------------------ overview

@router.get("/overview")
def board_overview(user=Depends(exiger_board)):
    """Résumé global : KPI par secteur."""
    with get_conn() as conn:
        secteurs = []
        for secteur in SECTEURS_VALIDES:
            row = conn.execute(
                """
                SELECT COUNT(*) AS nb_refs,
                       COALESCE(SUM(current_stock), 0) AS unites,
                       COALESCE(SUM(CASE WHEN current_stock <= 0 THEN 1 ELSE 0 END), 0)
                           AS en_rupture,
                       COALESCE(SUM(current_stock * unit_cost), 0) AS valeur
                FROM products
                WHERE COALESCE(archived, 0) = 0 AND sector = ?
                """,
                (secteur,),
            ).fetchone()
            depuis_30j = (maintenant() - timedelta(days=30)).strftime(FORMAT_DATETIME)
            mvt = conn.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN d.type='RECEIVING' THEN dl.quantity ELSE 0 END), 0) AS recu,
                       COALESCE(SUM(CASE WHEN d.type='DELIVERY' THEN dl.quantity ELSE 0 END), 0) AS livre
                FROM documents d
                JOIN document_lines dl ON dl.document_id = d.id
                WHERE d.cancelled_at IS NULL AND d.sector = ?
                  AND d.created_at >= ?
                """,
                (secteur, depuis_30j),
            ).fetchone()
            secteurs.append({
                "secteur": secteur,
                "nb_refs": row["nb_refs"],
                "unites": _arrondir(row["unites"]),
                "en_rupture": row["en_rupture"],
                "valeur": _arrondir(row["valeur"]),
                "recu_30j": _arrondir(mvt["recu"]),
                "livre_30j": _arrondir(mvt["livre"]),
            })
    return {"secteurs": secteurs}


# ------------------------------------------------------------------ fuel

@router.get("/fuel/cuves")
def board_fuel_cuves(user=Depends(exiger_board)):
    """Toutes les cuves FUEL avec niveau, capacité, autonomie."""
    with get_conn() as conn:
        depuis_30j = (maintenant() - timedelta(days=30)).strftime(FORMAT_DATETIME)
        cuves = [dict(r) for r in conn.execute(
            """
            SELECT p.id, p.sku, p.name, p.current_stock, p.tank_capacity,
                   p.min_stock, p.site,
                   COALESCE(
                       (SELECT SUM(dl.quantity) FROM document_lines dl
                        JOIN documents d ON d.id = dl.document_id
                        WHERE dl.product_id = p.id AND d.type = 'DELIVERY'
                          AND d.cancelled_at IS NULL AND d.created_at >= ?),
                   0) / 30.0 AS conso_jour
            FROM products p
            WHERE p.sector = 'FUEL' AND COALESCE(p.archived, 0) = 0
            ORDER BY p.site, p.name
            """,
            (depuis_30j,),
        ).fetchall()]
        for c in cuves:
            conso = c["conso_jour"]
            c["conso_jour"] = _arrondir(conso)
            c["jours_restants"] = (
                _arrondir(c["current_stock"] / conso) if conso > 0 else None
            )
            c["current_stock"] = _arrondir(c["current_stock"])
            c["pct"] = (
                _arrondir(c["current_stock"] / c["tank_capacity"] * 100)
                if c.get("tank_capacity") and c["tank_capacity"] > 0
                else None
            )
    return {"cuves": cuves}


@router.get("/fuel/cuve/{product_id}/courbe")
def board_fuel_courbe(product_id: int, jours: int = Query(30, ge=1, le=365),
                      user=Depends(exiger_board)):
    """Courbe de niveau d'une cuve (même logique que /fuel/niveau-cuve)."""
    with get_conn() as conn:
        produit = conn.execute(
            "SELECT id, current_stock FROM products WHERE id = ? AND sector = 'FUEL'",
            (product_id,),
        ).fetchone()
        if not produit:
            raise HTTPException(status_code=404, detail="Cuve introuvable")

        depuis = (maintenant() - timedelta(days=jours)).strftime(FORMAT_DATETIME)
        lignes = conn.execute(
            """
            SELECT d.type, d.created_at, dl.quantity AS quantite
            FROM document_lines dl
            JOIN documents d ON d.id = dl.document_id
            WHERE dl.product_id = ? AND d.cancelled_at IS NULL
              AND d.created_at >= ?
              AND d.type IN ('RECEIVING','DELIVERY','RETURN',
                             'SUPPLIER_RETURN','ADJUSTMENT')
            ORDER BY d.created_at ASC, d.id ASC
            """,
            (product_id, depuis),
        ).fetchall()

    SIGNE = {"RECEIVING": 1, "RETURN": 1, "DELIVERY": -1, "SUPPLIER_RETURN": -1}
    total_periode = 0.0
    for l in lignes:
        delta = l["quantite"] if l["type"] == "ADJUSTMENT" else SIGNE[l["type"]] * l["quantite"]
        total_periode += delta

    niveau = _arrondir(produit["current_stock"] - total_periode)
    points = [{"date": depuis, "niveau": niveau}]
    for l in lignes:
        delta = l["quantite"] if l["type"] == "ADJUSTMENT" else SIGNE[l["type"]] * l["quantite"]
        niveau = _arrondir(niveau + delta)
        points.append({"date": l["created_at"], "niveau": niveau})

    return {"product_id": product_id, "points": points}


# ------------------------------------------------------------------ KPI riche

@router.get("/{secteur}/kpis")
def board_secteur_kpis(secteur: str, user=Depends(exiger_board)):
    """KPI enrichis pour un secteur : tendances M/M, alertes détaillées,
    stock par catégorie, top produits sortants, couverture stock."""
    secteur = secteur.upper()
    if secteur not in SECTEURS_VALIDES:
        raise HTTPException(status_code=404, detail="Secteur inconnu")

    now = maintenant()
    j30 = (now - timedelta(days=30)).strftime(FORMAT_DATETIME)
    j60 = (now - timedelta(days=60)).strftime(FORMAT_DATETIME)

    with get_conn() as conn:
        # --- Mouvements 30j et M-1 (60-30j) pour tendances ---
        mvt_30 = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN d.type='RECEIVING' THEN dl.quantity ELSE 0 END),0) AS recu,
                 COALESCE(SUM(CASE WHEN d.type='DELIVERY' THEN dl.quantity ELSE 0 END),0) AS livre,
                 COALESCE(SUM(CASE WHEN d.type='RETURN' THEN dl.quantity ELSE 0 END),0) AS retour,
                 COUNT(DISTINCT d.id) AS nb_docs
               FROM documents d JOIN document_lines dl ON dl.document_id=d.id
               WHERE d.cancelled_at IS NULL AND d.sector=? AND d.created_at>=?""",
            (secteur, j30),
        ).fetchone()
        mvt_m1 = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN d.type='RECEIVING' THEN dl.quantity ELSE 0 END),0) AS recu,
                 COALESCE(SUM(CASE WHEN d.type='DELIVERY' THEN dl.quantity ELSE 0 END),0) AS livre,
                 COALESCE(SUM(CASE WHEN d.type='RETURN' THEN dl.quantity ELSE 0 END),0) AS retour,
                 COUNT(DISTINCT d.id) AS nb_docs
               FROM documents d JOIN document_lines dl ON dl.document_id=d.id
               WHERE d.cancelled_at IS NULL AND d.sector=?
                 AND d.created_at>=? AND d.created_at<?""",
            (secteur, j60, j30),
        ).fetchone()

        # --- Activité du jour ---
        aujourdhui = now.strftime("%Y-%m-%d")
        docs_jour = conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE cancelled_at IS NULL AND sector=? AND DATE(created_at)=?",
            (secteur, aujourdhui),
        ).fetchone()["n"]

        # --- Alertes stock détaillées ---
        alertes = [dict(r) for r in conn.execute(
            """SELECT p.sku, p.name, p.category, p.current_stock, p.min_stock
               FROM products p
               WHERE p.sector=? AND COALESCE(p.archived,0)=0
                 AND (p.current_stock<=0 OR (p.min_stock IS NOT NULL AND p.min_stock>0 AND p.current_stock<=p.min_stock))
               ORDER BY p.current_stock ASC, p.name""",
            (secteur,),
        ).fetchall()]
        for a in alertes:
            a["severite"] = "RUPTURE" if a["current_stock"] <= 0 else "BAS"

        # --- Stock par catégorie ---
        categories = [dict(r) for r in conn.execute(
            """SELECT COALESCE(p.category,'Sans catégorie') AS categorie,
                      COUNT(*) AS nb_refs,
                      ROUND(SUM(p.current_stock),1) AS unites
               FROM products p
               WHERE p.sector=? AND COALESCE(p.archived,0)=0
               GROUP BY categorie ORDER BY unites DESC""",
            (secteur,),
        ).fetchall()]

        # --- Top 10 produits sortants (30j) ---
        top_sorties = [dict(r) for r in conn.execute(
            """SELECT p.name, p.sku,
                      ROUND(SUM(dl.quantity),1) AS total_livre
               FROM document_lines dl
               JOIN documents d ON d.id=dl.document_id
               JOIN products p ON p.id=dl.product_id
               WHERE d.cancelled_at IS NULL AND d.sector=?
                 AND d.type='DELIVERY' AND d.created_at>=?
               GROUP BY dl.product_id ORDER BY total_livre DESC LIMIT 10""",
            (secteur, j30),
        ).fetchall()]

        # --- Couverture stock (jours restants par produit) ---
        couverture = [dict(r) for r in conn.execute(
            """SELECT p.name, p.sku, p.current_stock,
                      COALESCE(
                        (SELECT SUM(dl2.quantity) FROM document_lines dl2
                         JOIN documents d2 ON d2.id=dl2.document_id
                         WHERE dl2.product_id=p.id AND d2.type='DELIVERY'
                           AND d2.cancelled_at IS NULL AND d2.created_at>=?),0
                      )/30.0 AS conso_jour
               FROM products p
               WHERE p.sector=? AND COALESCE(p.archived,0)=0
                 AND p.current_stock>0
               ORDER BY p.name""",
            (j30, secteur),
        ).fetchall()]
        for c in couverture:
            cj = c["conso_jour"]
            c["conso_jour"] = _arrondir(cj)
            c["jours_restants"] = _arrondir(c["current_stock"] / cj) if cj > 0 else None
        couverture = [c for c in couverture if c["jours_restants"] is not None]
        couverture.sort(key=lambda c: c["jours_restants"])

    def _tendance(actuel, precedent):
        if precedent == 0:
            return None
        return round((actuel - precedent) / precedent * 100, 1)

    return {
        "secteur": secteur,
        "recu_30j": _arrondir(mvt_30["recu"]),
        "livre_30j": _arrondir(mvt_30["livre"]),
        "retour_30j": _arrondir(mvt_30["retour"]),
        "docs_30j": mvt_30["nb_docs"],
        "docs_jour": docs_jour,
        "tendance_recu": _tendance(mvt_30["recu"], mvt_m1["recu"]),
        "tendance_livre": _tendance(mvt_30["livre"], mvt_m1["livre"]),
        "tendance_retour": _tendance(mvt_30["retour"], mvt_m1["retour"]),
        "alertes": alertes,
        "nb_alertes": len(alertes),
        "nb_ruptures": sum(1 for a in alertes if a["severite"] == "RUPTURE"),
        "categories": categories,
        "top_sorties": top_sorties,
        "couverture_critique": couverture[:10],
    }


# --------------------------------------------------------- secteur générique

@router.get("/{secteur}/stock")
def board_secteur_stock(secteur: str, user=Depends(exiger_board)):
    """Stock complet d'un secteur (toutes régions, tous sites)."""
    secteur = secteur.upper()
    if secteur not in SECTEURS_VALIDES:
        raise HTTPException(status_code=404, detail="Secteur inconnu")
    with get_conn() as conn:
        produits = [dict(r) for r in conn.execute(
            """
            SELECT p.id, p.sku, p.name, p.category,
                   p.current_stock, p.min_stock, p.unit_cost,
                   p.tank_capacity, p.site
            FROM products p
            WHERE p.sector = ? AND COALESCE(p.archived, 0) = 0
            ORDER BY p.category, p.name
            """,
            (secteur,),
        ).fetchall()]
    return {"secteur": secteur, "produits": produits}


@router.get("/{secteur}/regions")
def board_secteur_regions(secteur: str, user=Depends(exiger_board)):
    """État de chaque entrepôt régional : dernier comptage déclaré, alertes de
    rupture ouvertes, ancienneté du comptage, note de contexte.

    Même logique que l'écran admin `/regions/tableau-de-bord` (une requête
    par information, agrégée par région), reprise ici pour le board qui n'a
    pas le rôle admin mais doit voir la même réalité opérationnelle — un
    entrepôt régional qui n'a rien déclaré depuis des semaines est un risque
    que le board doit pouvoir repérer, pas seulement le central.
    """
    secteur = secteur.upper()
    if secteur not in SECTEURS_VALIDES:
        raise HTTPException(status_code=404, detail="Secteur inconnu")
    aujourdhui = maintenant().date()
    with get_conn() as conn:
        derniers = {
            r["region"]: r["dernier"]
            for r in conn.execute(
                "SELECT region, MAX(created_at) AS dernier "
                "FROM regional_inventory_reports WHERE sector = ? GROUP BY region",
                (secteur,),
            )
        }
        totaux = {
            r["region"]: {"total": r["total"], "articles": r["articles"]}
            for r in conn.execute(
                """
                SELECT r.region,
                       ROUND(SUM(r.quantity), 3) AS total,
                       COUNT(*) AS articles
                FROM regional_inventory_reports r
                JOIN (SELECT region, MAX(created_at) AS dernier
                        FROM regional_inventory_reports WHERE sector = ?
                        GROUP BY region) m
                  ON m.region = r.region AND m.dernier = r.created_at
                WHERE r.sector = ?
                GROUP BY r.region
                """,
                (secteur, secteur),
            )
        }
        alertes = {
            r["region"]: r["n"]
            for r in conn.execute(
                "SELECT region, COUNT(*) AS n FROM regional_alert_signals "
                "WHERE resolved_at IS NULL AND sector = ? GROUP BY region",
                (secteur,),
            )
        }
        notes = {
            r["region"]: r["note"]
            for r in conn.execute(
                "SELECT region, note FROM region_notes WHERE TRIM(note) <> '' AND sector = ?",
                (secteur,),
            )
        }

    regions = []
    for region in regions_avec_entrepot_for(secteur):
        dernier = derniers.get(region)
        jours = _jours_depuis(dernier, aujourdhui)
        total = totaux.get(region) or {}
        regions.append({
            "region": region,
            "unites": _arrondir(total.get("total")),
            "nb_produits": total.get("articles", 0),
            "dernier_inventaire": dernier,
            "jours_depuis_comptage": jours,
            "comptage_perime": jours is None or jours >= COMPTAGE_PERIME_JOURS,
            "alertes_ouvertes": alertes.get(region, 0),
            "note": notes.get(region, ""),
        })
    regions.sort(key=lambda r: (-r["alertes_ouvertes"], -(r["unites"] or 0)))

    return {
        "secteur": secteur,
        "seuil_comptage_jours": COMPTAGE_PERIME_JOURS,
        "regions": regions,
        "total_alertes_ouvertes": sum(r["alertes_ouvertes"] for r in regions),
        "nombre_comptages_perimes": sum(1 for r in regions if r["comptage_perime"]),
    }


@router.get("/{secteur}/regions/{region}/detail")
def board_secteur_region_detail(secteur: str, region: str, user=Depends(exiger_board)):
    """Détail produit par produit du DERNIER comptage soumis par une région.

    Un board manager qui clique sur un entrepôt régional veut voir ce qui a
    été compté, pas seulement le total — même esprit que le clic sur une
    cuve FUEL qui ouvre sa courbe de niveau.
    """
    secteur = secteur.upper()
    if secteur not in SECTEURS_VALIDES:
        raise HTTPException(status_code=404, detail="Secteur inconnu")
    with get_conn() as conn:
        dernier = conn.execute(
            "SELECT MAX(created_at) AS dernier FROM regional_inventory_reports "
            "WHERE sector = ? AND region = ?",
            (secteur, region),
        ).fetchone()["dernier"]
        if not dernier:
            return {"secteur": secteur, "region": region,
                    "dernier_inventaire": None, "produits": []}
        produits = [dict(r) for r in conn.execute(
            """
            SELECT p.sku, p.name, p.category, r.quantity, r.note, r.created_by
            FROM regional_inventory_reports r
            JOIN products p ON p.id = r.product_id
            WHERE r.sector = ? AND r.region = ? AND r.created_at = ?
            ORDER BY p.category, p.name
            """,
            (secteur, region, dernier),
        ).fetchall()]
    return {"secteur": secteur, "region": region,
            "dernier_inventaire": dernier, "produits": produits}


@router.get("/{secteur}/activite")
def board_secteur_activite(secteur: str,
                           jours: int = Query(30, ge=1, le=365),
                           user=Depends(exiger_board)):
    """Activité récente : réceptions + livraisons par jour."""
    secteur = secteur.upper()
    if secteur not in SECTEURS_VALIDES:
        raise HTTPException(status_code=404, detail="Secteur inconnu")
    depuis = (maintenant() - timedelta(days=jours)).strftime(FORMAT_DATETIME)
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            """
            SELECT DATE(d.created_at) AS jour,
                   SUM(CASE WHEN d.type='RECEIVING' THEN dl.quantity ELSE 0 END) AS recu,
                   SUM(CASE WHEN d.type='DELIVERY' THEN dl.quantity ELSE 0 END) AS livre
            FROM documents d
            JOIN document_lines dl ON dl.document_id = d.id
            WHERE d.cancelled_at IS NULL AND d.sector = ?
              AND d.created_at >= ?
            GROUP BY jour
            ORDER BY jour
            """,
            (secteur, depuis),
        ).fetchall()]
    return {"secteur": secteur, "activite": rows}
