"""Réinitialise l'historique et remet les stocks au-dessus des seuils.

OUTIL DE SIMULATION — DESTRUCTEUR. À n'utiliser que sur une base d'essai,
jamais sur la base d'exploitation de l'entrepôt.

Garde : produits (catalogue), utilisateurs, contacts, seuils.
Vide : documents, lignes, mouvements de stock, journal d'audit, compteurs,
       tentatives de connexion, historique de connexion, consignes de
       bouteilles, comptages et alertes des entrepôts régionaux.
Remet : stock initial et stock actuel de chaque produit au-dessus de son seuil.

SÉCURITÉ
--------
Le script ne fait plus rien tout seul : il affiche d'abord ce qu'il s'apprête
à effacer, copie la base de côté (`warehouse_avant_reset_*.db`), puis exige
que l'opérateur tape EFFACER en toutes lettres. Lancé par double-clic sans
console interactive, il s'arrête sans rien modifier.

Usage : python reset_pour_simulation.py
"""
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from chemins import chemin_base  # noqa: E402
from locations import ajuster_ouverture  # noqa: E402

# Tables d'historique vidées par le reset. bottle_ledger.document_id et les
# tables régionales référencent documents(id)/products(id) : les oublier
# faisait échouer tout le reset sur « FOREIGN KEY constraint failed » dès
# qu'une consigne de bouteille existait, et laissait sinon des écritures
# orphelines pointant vers des bons effacés.
TABLES_HISTORIQUE = [
    "document_lines",
    "bottle_ledger",
    "regional_inventory_reports",
    "regional_alert_signals",
    "documents",
    "stock_movements",
    "audit_log",
    "doc_counters",
    "login_attempts",
    "login_history",
]

PHRASE_CONFIRMATION = "EFFACER"


def _compter(conn, table: str) -> int:
    """Nombre de lignes, 0 si la table n'existe pas encore (base ancienne)."""
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return 0


def _sauvegarde_prealable(db_path: Path) -> Path:
    """Copie la base (et son WAL) avant toute écriture."""
    horodatage = datetime.now().strftime("%Y%m%d_%H%M%S")
    copie = db_path.parent / f"warehouse_avant_reset_{horodatage}.db"
    shutil.copy2(db_path, copie)
    for suffixe in ("-wal", "-shm"):
        source = Path(str(db_path) + suffixe)
        if source.exists():
            shutil.copy2(source, Path(str(copie) + suffixe))
    return copie


def _demander_confirmation() -> bool:
    """Confirmation explicite. Refuse quand il n'y a pas de console."""
    if not sys.stdin or not sys.stdin.isatty():
        print("\nCe script est destructeur et refuse de s'exécuter sans")
        print("confirmation. Ouvre une invite de commande et lance :")
        print("    python reset_pour_simulation.py")
        return False
    print()
    print("Cette opération est IRRÉVERSIBLE (une copie de la base est tout de")
    print("même conservée à côté, sous warehouse_avant_reset_*.db).")
    reponse = input(f"Taper {PHRASE_CONFIRMATION} en majuscules pour confirmer : ")
    return reponse.strip() == PHRASE_CONFIRMATION


def executer_reset(conn) -> int:
    """Vide l'historique et remet les stocks. Renvoie le nombre de produits.

    Séparé de main() pour être testable sur une base temporaire, sans jamais
    approcher la base d'exploitation.
    """
    conn.execute("BEGIN")
    try:
        for table in TABLES_HISTORIQUE:
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                # Table absente sur une base plus ancienne : rien à vider.
                pass

        produits = conn.execute(
            "SELECT id, sku, min_stock FROM products WHERE archived = 0"
        ).fetchall()

        for p in produits:
            seuil = p["min_stock"] or 0
            # Marge confortable au-dessus du seuil : 50% de plus, ou +20 si le
            # seuil est à 0, pour que rien n'apparaisse en alerte au départ.
            nouveau_stock = round(seuil * 1.5, 2) if seuil > 0 else 20.0

            conn.execute(
                "UPDATE products SET initial_stock = ?, current_stock = ?, "
                "initial_stock_date = date('now') WHERE id = ?",
                (nouveau_stock, nouveau_stock, p["id"]),
            )
            ajuster_ouverture(conn, p["id"], nouveau_stock)
            print(f"  {p['sku']:20s} -> stock {nouveau_stock:g} (seuil {seuil:g})")

        conn.execute("COMMIT")
        return len(produits)
    except Exception:
        conn.execute("ROLLBACK")
        raise


def main() -> int:
    db_path = chemin_base()

    print()
    print("=" * 66)
    print("  RÉINITIALISATION POUR SIMULATION — OUTIL DESTRUCTEUR")
    print("=" * 66)
    print()
    print(f"Base visée : {db_path}")
    print()

    if not db_path.exists():
        print("Aucune base à cet emplacement. Rien n'a été modifié.")
        return 1

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        lignes = [(t, _compter(conn, t)) for t in TABLES_HISTORIQUE]
        nb_produits = _compter(conn, "products")
        total = sum(n for _, n in lignes)

        print("Va être DÉFINITIVEMENT effacé de cette base :")
        for table, nombre in lignes:
            if nombre:
                print(f"  - {table:30s} {nombre:>7} ligne(s)")
        if not total:
            print("  (aucun historique : la base est déjà vierge)")
        print()
        print(f"Le stock des {nb_produits} produit(s) sera réécrit "
              f"au-dessus de leur seuil.")

        # Garde-fou : une base qui porte un historique conséquent est
        # probablement la base d'exploitation, pas une base d'essai.
        if total > 50:
            print()
            print("ATTENTION : cette base contient un historique important.")
            print("Il s'agit très probablement de la base d'EXPLOITATION de")
            print("l'entrepôt. Ce script est fait pour une base d'essai.")

        if not _demander_confirmation():
            print("\nAnnulé. Rien n'a été modifié.")
            return 1

        copie = _sauvegarde_prealable(db_path)
        print(f"\nCopie de sécurité : {copie.name}")
        print()

        nombre = executer_reset(conn)
        print(f"\n{nombre} produit(s) réinitialisé(s). Historique vidé.")
        print(f"Pour revenir en arrière : remettre {copie.name} à la place de "
              f"{db_path.name} (serveur arrêté).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrompu. Rien n'a été modifié.")
        sys.exit(1)
