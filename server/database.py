import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from chemins import (MESSAGE_BASE_RESEAU, chemin_base, chemin_schema,
                     est_chemin_reseau)
from journalisation import logger

log = logger(__name__)

DB_PATH = chemin_base()
SCHEMA_PATH = chemin_schema()


class BaseSurLecteurReseau(RuntimeError):
    """La base est placée sur un partage réseau : démarrage refusé.

    Levée depuis `init_db()` et non au chargement du module : c'est le seul
    endroit qui voit le chemin RÉELLEMENT utilisé (DB_PATH est repointé en
    test), et le seul point par lequel passe obligatoirement tout démarrage
    du serveur — `lancer_serveur.py` comme le lifespan de `app.py`.
    """


def _verifier_base_locale() -> None:
    if est_chemin_reseau(DB_PATH):
        message = MESSAGE_BASE_RESEAU.format(chemin=DB_PATH)
        log.error(message)
        raise BaseSurLecteurReseau(message)

# Migrations appliquées dans l'ordre, pilotées par PRAGMA user_version.
# Ne JAMAIS modifier une migration déjà livrée : en ajouter une nouvelle.
MIGRATIONS: list[tuple[int, str]] = [
    (
        2,
        # Reconstruction de products pour poser les contraintes CHECK.
        # SQLite ne sait pas ajouter un CHECK par ALTER TABLE.
        """
        CREATE TABLE products_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT 'pcs',
            initial_stock REAL NOT NULL DEFAULT 0 CHECK (initial_stock >= 0),
            current_stock REAL NOT NULL DEFAULT 0 CHECK (current_stock >= 0),
            min_stock REAL NOT NULL DEFAULT 0 CHECK (min_stock >= 0),
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO products_new (id, sku, name, unit, initial_stock, current_stock, min_stock, created_at)
            SELECT id, sku, name, unit,
                   MAX(initial_stock, 0), MAX(current_stock, 0), MAX(min_stock, 0), created_at
            FROM products;
        DROP TABLE products;
        ALTER TABLE products_new RENAME TO products;
        """,
    ),
    (
        3,
        # Empêche deux lignes du même produit dans un même bon (contournait le contrôle de stock).
        """
        DELETE FROM document_lines WHERE id NOT IN (
            SELECT MIN(id) FROM document_lines GROUP BY document_id, product_id
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_document_lines_doc_product
            ON document_lines(document_id, product_id);
        CREATE INDEX IF NOT EXISTS idx_document_lines_product ON document_lines(product_id);
        CREATE INDEX IF NOT EXISTS idx_documents_type_created ON documents(type, created_at DESC);
        """,
    ),
    (
        4,
        # La catégorie était noyée dans le nom ("Air Filter (Perkins/...)") :
        # on l'extrait en colonne pour permettre les agrégats du tableau de bord.
        """
        ALTER TABLE products ADD COLUMN category TEXT;
        UPDATE products
           SET category = TRIM(SUBSTR(name, 1, INSTR(name, ' (') - 1))
         WHERE INSTR(name, ' (') > 0;
        UPDATE products
           SET category = name
         WHERE category IS NULL OR TRIM(category) = '';
        CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
        """,
    ),
    (
        5,
        # Groupe de capacité du groupe électrogène (P11, P26, HPU (3/4/5)...) :
        # permet le détail d'une catégorie dans le tableau de bord.
        """
        ALTER TABLE products ADD COLUMN capacity_group TEXT;
        CREATE INDEX IF NOT EXISTS idx_products_capacity ON products(capacity_group);
        """,
    ),
    (
        6,
        # Annulation d'un bon par extourne. On ne supprime JAMAIS un document :
        # la trace de l'erreur et de sa correction fait partie de la piste d'audit.
        """
        ALTER TABLE documents ADD COLUMN cancelled_at TEXT;
        ALTER TABLE documents ADD COLUMN cancelled_by TEXT;
        ALTER TABLE documents ADD COLUMN cancel_reason TEXT;
        CREATE INDEX IF NOT EXISTS idx_documents_cancelled ON documents(cancelled_at);
        """,
    ),
    (
        7,
        # Bon d'ajustement d'inventaire (comptage physique).
        # Deux contraintes à faire évoluer, impossible par ALTER en SQLite :
        #  - documents.type doit accepter 'ADJUSTMENT'
        #  - document_lines.quantity doit accepter un écart négatif (comptage
        #    inférieur au stock théorique). Reste interdit d'être nul.
        # counted_quantity conserve la quantité réellement comptée, pour que
        # l'historique montre « compté 80, écart -20 » et pas seulement l'écart.
        """
        CREATE TABLE documents_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK (type IN ('RECEIVING', 'DELIVERY', 'ADJUSTMENT')),
            party TEXT,
            operator TEXT,
            region TEXT,
            reference TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            cancelled_at TEXT,
            cancelled_by TEXT,
            cancel_reason TEXT
        );
        INSERT INTO documents_new (id, type, party, operator, region, reference, note,
                                    created_at, cancelled_at, cancelled_by, cancel_reason)
            SELECT id, type, party, operator, region, reference, note,
                   created_at, cancelled_at, cancelled_by, cancel_reason FROM documents;
        DROP TABLE documents;
        ALTER TABLE documents_new RENAME TO documents;

        CREATE TABLE document_lines_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity REAL NOT NULL CHECK (quantity <> 0),
            counted_quantity REAL
        );
        INSERT INTO document_lines_new (id, document_id, product_id, quantity)
            SELECT id, document_id, product_id, quantity FROM document_lines;
        DROP TABLE document_lines;
        ALTER TABLE document_lines_new RENAME TO document_lines;

        CREATE INDEX IF NOT EXISTS idx_document_lines_document_id ON document_lines(document_id);
        CREATE INDEX IF NOT EXISTS idx_document_lines_product ON document_lines(product_id);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_document_lines_doc_product
            ON document_lines(document_id, product_id);
        CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at);
        CREATE INDEX IF NOT EXISTS idx_documents_type_created ON documents(type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_documents_cancelled ON documents(cancelled_at);
        """,
    ),
    (
        8,
        # Clé d'idempotence : protège de la double soumission quand la réponse
        # se perd (réseau instable). Le client garde la même clé tant que le bon
        # n'est pas confirmé enregistré ; le serveur renvoie alors le bon déjà
        # créé au lieu d'en créer un second.
        # Index partiel : les bons antérieurs (clé NULL) ne se gênent pas entre eux.
        """
        ALTER TABLE documents ADD COLUMN idempotency_key TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_idempotency
            ON documents(idempotency_key) WHERE idempotency_key IS NOT NULL;
        """,
    ),
    (
        9,
        # Stock par emplacement : l'entrepôt central et un emplacement par
        # (région, superviseur). Une expédition ne fait plus disparaître le
        # stock : elle le déplace vers le superviseur, qui le détient jusqu'à
        # consommation. C'est ce que le client suit dans Stock Levels.
        #
        # region/supervisor en NOT NULL DEFAULT '' et non NULL : en SQLite deux
        # NULL sont considérés distincts, l'unicité de l'entrepôt ne tiendrait pas.
        """
        CREATE TABLE locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK (type IN ('WAREHOUSE', 'FIELD')),
            region TEXT NOT NULL DEFAULT '',
            supervisor TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            UNIQUE (type, region, supervisor)
        );
        INSERT INTO locations (type, region, supervisor, name)
            VALUES ('WAREHOUSE', '', '', 'TP WH');

        CREATE TABLE stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            -- NULL pour un mouvement d'ouverture (stock initial d'un produit),
            -- qui ne provient d'aucun bon. Sans lui le grand livre serait
            -- incomplet : le stock de départ n'aurait aucune trace.
            document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            from_location_id INTEGER REFERENCES locations(id),
            to_location_id INTEGER REFERENCES locations(id),
            quantity REAL NOT NULL CHECK (quantity > 0)
        );
        CREATE INDEX idx_movements_document ON stock_movements(document_id);
        CREATE INDEX idx_movements_product ON stock_movements(product_id);
        CREATE INDEX idx_movements_from ON stock_movements(from_location_id);
        CREATE INDEX idx_movements_to ON stock_movements(to_location_id);
        """,
    ),
    (
        10,
        # Imputabilité : qui a saisi, depuis quel poste, et quand le serveur
        # a réellement reçu le bon.
        #  - created_by  : l'opérateur qui saisit, distinct de `operator` qui
        #    porte le superviseur sur une expédition. Sans cette colonne,
        #    l'identité du saisisseur était perdue sur toutes les sorties.
        #  - station     : vérifiée par clé, contrairement au nom déclaratif.
        #  - server_received_at : date non falsifiable. `created_at` est
        #    saisissable par l'opérateur, donc antidatable sans trace.
        # Colonnes ajoutées sans NOT NULL : ALTER TABLE n'accepte pas de
        # valeur par défaut non constante.
        """
        ALTER TABLE documents ADD COLUMN created_by TEXT;
        ALTER TABLE documents ADD COLUMN station TEXT;
        ALTER TABLE documents ADD COLUMN server_received_at TEXT;
        UPDATE documents SET server_received_at = created_at
            WHERE server_received_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_documents_created_by ON documents(created_by);
        """,
    ),
    (
        11,
        # Unités réelles : distingue les produits comptés en pièces (filtres,
        # courroies) de ceux mesurés en volume (huile, liquide de refroidissement).
        # Le stock est toujours stocké dans l'unité de base (pcs ou gls).
        # bidon_capacity permet la conversion bidon↔gallon à la saisie.
        """
        ALTER TABLE products ADD COLUMN unit_type TEXT NOT NULL DEFAULT 'piece';
        ALTER TABLE products ADD COLUMN bidon_capacity REAL;

        UPDATE products SET unit_type = 'volume', unit = 'gls'
        WHERE category IN ('Oil Engine', 'Coolant', 'Degreaser', 'Distilled Water', 'Acid');
        """,
    ),
    (
        12,
        # Comptes utilisateurs individuels. La clé de poste identifie la
        # machine, pas la personne. Avec cette table l'opérateur se connecte
        # et chaque mouvement porte son identité vérifiée.
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'magasinier'
                CHECK (role IN ('admin', 'magasinier', 'lecteur')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX idx_users_username ON users(username);
        """,
    ),
    (
        13,
        """
        ALTER TABLE products ADD COLUMN initial_stock_date TEXT;
        """,
    ),
    (
        14,
        """
        CREATE TABLE sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE INDEX idx_sessions_expires ON sessions(expires_at);
        """,
    ),
    (
        15,
        """
        CREATE TABLE IF NOT EXISTS login_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            success INTEGER NOT NULL DEFAULT 0,
            ip TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_login_history_user ON login_history(username);
        """,
    ),
    (
        16,
        """
        CREATE TABLE doc_counters (
            type_prefix TEXT NOT NULL,
            year INTEGER NOT NULL,
            seq INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (type_prefix, year)
        );
        """,
    ),
    (
        17,
        """
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            action TEXT NOT NULL,
            old_values TEXT,
            new_values TEXT,
            username TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX idx_audit_entity ON audit_log(entity_type, entity_id);
        CREATE INDEX idx_audit_created ON audit_log(created_at);
        """,
    ),
    (
        18,
        """
        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            type TEXT NOT NULL DEFAULT 'other'
                CHECK (type IN ('supplier', 'customer', 'other')),
            phone TEXT,
            email TEXT,
            address TEXT,
            notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX idx_contacts_type ON contacts(type);
        """,
    ),
    (
        19,
        """
        CREATE TABLE documents_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK (type IN ('RECEIVING', 'DELIVERY', 'ADJUSTMENT', 'RETURN')),
            party TEXT,
            operator TEXT,
            region TEXT,
            reference TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            cancelled_at TEXT,
            cancelled_by TEXT,
            cancel_reason TEXT,
            idempotency_key TEXT,
            created_by TEXT,
            station TEXT,
            server_received_at TEXT
        );
        INSERT INTO documents_v2 (id, type, party, operator, region, reference, note,
                                   created_at, cancelled_at, cancelled_by, cancel_reason,
                                   idempotency_key, created_by, station, server_received_at)
            SELECT id, type, party, operator, region, reference, note,
                   created_at, cancelled_at, cancelled_by, cancel_reason,
                   idempotency_key, created_by, station, server_received_at FROM documents;
        DROP TABLE documents;
        ALTER TABLE documents_v2 RENAME TO documents;

        CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at);
        CREATE INDEX IF NOT EXISTS idx_documents_type_created ON documents(type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_documents_cancelled ON documents(cancelled_at);
        CREATE INDEX IF NOT EXISTS idx_documents_created_by ON documents(created_by);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_idempotency
            ON documents(idempotency_key) WHERE idempotency_key IS NOT NULL;
        """,
    ),
    (
        20,
        """
        ALTER TABLE products ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_products_archived ON products(archived);
        """,
    ),
    (
        21,
        """
        ALTER TABLE documents ADD COLUMN carrier TEXT;
        """,
    ),
    (
        22,
        """
        CREATE TABLE login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            attempt_time TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            ip TEXT
        );
        CREATE INDEX idx_login_attempts_user ON login_attempts(username);
        CREATE INDEX idx_login_attempts_time ON login_attempts(attempt_time);
        """,
    ),
    (
        23,
        # Consigne bouteille : certains produits liquides (eau distillée) partent
        # en gallons mais voyagent dans des bouteilles de capacité fixe. La région
        # doit rapporter les contenants vides. Ce n'est PAS du stock : c'est un
        # solde de contenants, suivi à part dans bottle_ledger.
        #
        # consigne_bouteille est un champ dédié, distinct de bidon_capacity :
        # bidon_capacity sert à la saisie en bidons (conversion d'affichage) et
        # concerne d'autres produits. Mélanger les deux ferait apparaître une
        # dette de contenants sur des produits qui n'en ont pas.
        # NULL = produit non suivi en consigne.
        """
        ALTER TABLE products ADD COLUMN consigne_bouteille REAL;

        CREATE TABLE bottle_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            region TEXT NOT NULL,
            -- NULL pour un retour de bouteilles vides, qui ne provient d'aucun bon.
            document_id INTEGER REFERENCES documents(id),
            bottles REAL NOT NULL,
            note TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX idx_bottle_ledger_product_region ON bottle_ledger(product_id, region);
        """,
    ),
    (
        24,
        # Index composite : le calcul du plafond physique et la reprise des
        # lignes d'un bon filtrent toujours sur (document_id, product_id).
        # Les deux index simples existants obligeaient SQLite à choisir l'un
        # ou l'autre puis à filtrer le reste ligne à ligne.
        """
        CREATE INDEX IF NOT EXISTS idx_document_lines_doc_product
            ON document_lines(document_id, product_id);
        """,
    ),
    (
        25,
        # Borne du cycle en cours : dernier id de bon existant au moment du
        # « Réinitialiser stock début ».
        #
        # Le reset repose initial_stock = current_stock : l'historique déjà
        # passé est donc DÉJÀ inclus dans la nouvelle base. Sans borne, la
        # réconciliation le rajoutait une seconde fois et signalait un écart
        # permanent, non résorbable, sur tout produit ayant eu des mouvements.
        #
        # Pourquoi un id de bon et non une date :
        #  - initial_stock_date est une date sans heure : impossible de séparer
        #    les bons saisis avant et après un reset fait le jour même ;
        #  - created_at est antidatable par l'opérateur ;
        #  - server_received_at n'a qu'une précision d'une seconde.
        # Les id de documents suivent exactement l'ordre où le stock a bougé.
        """
        ALTER TABLE products ADD COLUMN initial_stock_reset_doc_id INTEGER;
        """,
    ),
    (
        26,
        # L'annulation d'un retour de bouteilles se détectait en cherchant une
        # ligne dont la note valait littéralement 'annulation #<id>' : un
        # opérateur qui saisit ce texte en note cassait la détection, et rien
        # ne rattachait formellement l'écriture d'annulation à celle annulée.
        # L'index UNIQUE (partiel : NULL ne compte pas comme une valeur pour
        # UNIQUE) empêche d'annuler deux fois la même écriture même en cas de
        # double clic simultané sur deux postes.
        """
        ALTER TABLE bottle_ledger ADD COLUMN cancels_entry_id INTEGER REFERENCES bottle_ledger(id);
        CREATE UNIQUE INDEX idx_bottle_ledger_cancels_entry_id
            ON bottle_ledger(cancels_entry_id) WHERE cancels_entry_id IS NOT NULL;
        """,
    ),
    (
        27,
        # Aucune valorisation du stock nulle part dans le schéma : impossible
        # de chiffrer un inventaire, une consommation, ou un écart
        # d'ajustement. NULL = coût non renseigné (produit toujours
        # utilisable, simplement absent des totaux de valeur).
        """
        ALTER TABLE products ADD COLUMN unit_cost REAL;
        """,
    ),
    (
        28,
        # documents.party est du texte libre alors que la table contacts
        # existe : pas d'historique fournisseur/client, fautes de frappe
        # créant des doublons. Lien optionnel — party reste utilisable seul
        # (saisie rapide, contact non encore enregistré).
        """
        ALTER TABLE documents ADD COLUMN party_contact_id INTEGER REFERENCES contacts(id);
        CREATE INDEX idx_documents_party_contact ON documents(party_contact_id);
        """,
    ),
    (
        29,
        # SQLite ne sait pas modifier un CHECK par ALTER TABLE (comme pour
        # products en migration 2) : reconstruction de la table pour ajouter
        # SUPPLIER_RETURN — marchandise non conforme renvoyée à un
        # fournisseur, un mouvement de stock qui n'existait pas.
        """
        CREATE TABLE documents_v3 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK (type IN
                ('RECEIVING', 'DELIVERY', 'ADJUSTMENT', 'RETURN', 'SUPPLIER_RETURN')),
            party TEXT,
            party_contact_id INTEGER REFERENCES contacts(id),
            operator TEXT,
            region TEXT,
            reference TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            cancelled_at TEXT,
            cancelled_by TEXT,
            cancel_reason TEXT,
            idempotency_key TEXT,
            created_by TEXT,
            station TEXT,
            server_received_at TEXT,
            carrier TEXT
        );
        INSERT INTO documents_v3 (id, type, party, party_contact_id, operator, region,
                                   reference, note, created_at, cancelled_at, cancelled_by,
                                   cancel_reason, idempotency_key, created_by, station,
                                   server_received_at, carrier)
            SELECT id, type, party, party_contact_id, operator, region,
                   reference, note, created_at, cancelled_at, cancelled_by,
                   cancel_reason, idempotency_key, created_by, station,
                   server_received_at, carrier FROM documents;
        DROP TABLE documents;
        ALTER TABLE documents_v3 RENAME TO documents;

        CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at);
        CREATE INDEX IF NOT EXISTS idx_documents_type_created ON documents(type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_documents_cancelled ON documents(cancelled_at);
        CREATE INDEX IF NOT EXISTS idx_documents_created_by ON documents(created_by);
        CREATE INDEX IF NOT EXISTS idx_documents_party_contact ON documents(party_contact_id);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_idempotency
            ON documents(idempotency_key) WHERE idempotency_key IS NOT NULL;
        """,
    ),
    (
        30,
        # Livraisons d'eau distillée à un technicien nommé : la consigne
        # n'était traçable que par région, ce qui ne dit pas LEQUEL des
        # techniciens doit encore rendre ses bouteilles.
        #
        # Colonne optionnelle des deux côtés : NULL = livraison régionale
        # ordinaire. Le suivi par région reste inchangé — une ligne portant un
        # technicien porte aussi sa région, donc les totaux régionaux
        # existants continuent de tout compter.
        """
        ALTER TABLE documents ADD COLUMN technician TEXT;
        ALTER TABLE bottle_ledger ADD COLUMN technician TEXT;
        CREATE INDEX IF NOT EXISTS idx_bottle_ledger_technician
            ON bottle_ledger(technician) WHERE technician IS NOT NULL;
        """,
    ),
    (
        31,
        # Numéro de la fiche papier signée à la reprise des bouteilles vides.
        # Sans lui, un retour enregistré n'était rattachable à aucun document
        # physique : impossible de retrouver la pièce justificative en cas de
        # contestation. Nullable : les retours déjà saisis n'en ont pas.
        """
        ALTER TABLE bottle_ledger ADD COLUMN reference TEXT;
        """,
    ),
    (
        32,
        # Entrepôts régionaux — Consommables. Le stock d'une région ne doit pas
        # se confondre avec les emplacements FIELD (un par couple
        # région/superviseur), qui restent inchangés pour Port-au-Prince et
        # pour les livraisons nominatives à un technicien.
        #
        # SQLite ne sait pas modifier un CHECK par ALTER TABLE (comme en
        # migration 2 et 29) : reconstruction de la table pour ajouter le type
        # 'REGIONAL_WAREHOUSE'. Les id sont conservés — stock_movements les
        # référence.
        """
        CREATE TABLE locations_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK (type IN ('WAREHOUSE', 'FIELD', 'REGIONAL_WAREHOUSE')),
            region TEXT NOT NULL DEFAULT '',
            supervisor TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            UNIQUE (type, region, supervisor)
        );
        INSERT INTO locations_v2 (id, type, region, supervisor, name)
            SELECT id, type, region, supervisor, name FROM locations;
        DROP TABLE locations;
        ALTER TABLE locations_v2 RENAME TO locations;
        """,
    ),
    (
        33,
        # Transfert en deux temps vers un entrepôt régional.
        #  - received_at / received_by : NULL tant que la région n'a pas
        #    confirmé. C'est ce qui distingue « En transit » de « Reçu ».
        #  - document_lines.received_quantity : quantité RÉELLEMENT reçue.
        #    Colonne dédiée et non counted_quantity : celle-ci porte déjà le
        #    comptage physique d'un ADJUSTMENT. Deux sens dans une même colonne
        #    rendraient tout export et toute requête ambigus.
        """
        ALTER TABLE documents ADD COLUMN received_at TEXT;
        ALTER TABLE documents ADD COLUMN received_by TEXT;
        ALTER TABLE document_lines ADD COLUMN received_quantity REAL;
        CREATE INDEX IF NOT EXISTS idx_documents_received
            ON documents(type, region, received_at);

        -- Reprise de l'historique. Tout bon ANTÉRIEUR à cette migration a déjà
        -- été livré dans la réalité : son stock est déjà chez son destinataire.
        -- Sans ce rattrapage, toutes les expéditions régionales déjà saisies
        -- basculeraient d'un coup en « En transit » et réapparaîtraient dans la
        -- liste des réceptions à confirmer de chaque région.
        -- Les régions sont normalisées à chaque démarrage : comparer le libellé
        -- à 'Port-au-Prince' suffit à écarter la région du central.
        UPDATE documents
           SET received_at = created_at, received_by = 'Reprise historique'
         WHERE type = 'DELIVERY'
           AND received_at IS NULL
           AND (technician IS NULL OR TRIM(technician) = '')
           AND region IS NOT NULL AND TRIM(region) <> ''
           AND region <> 'Port-au-Prince';

        UPDATE document_lines
           SET received_quantity = quantity
         WHERE received_quantity IS NULL
           AND document_id IN (SELECT id FROM documents WHERE received_at IS NOT NULL);
        """,
    ),
    (
        34,
        # Rôle 'regional' : compte d'une région, rattaché à UNE région.
        # Le CHECK sur role impose là encore une reconstruction de table.
        # region est NULL pour tous les autres rôles.
        """
        CREATE TABLE users_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'magasinier'
                CHECK (role IN ('admin', 'magasinier', 'lecteur', 'regional')),
            region TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO users_v2 (id, username, password_hash, display_name, role, active, created_at)
            SELECT id, username, password_hash, display_name, role, active, created_at FROM users;
        DROP TABLE users;
        ALTER TABLE users_v2 RENAME TO users;
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
        """,
    ),
    (
        35,
        # Inventaire régional : comptage déclaratif d'une région, purement
        # informatif. Aucun écart calculé, aucun ajustement de stock — il sert
        # à l'administrateur central pour décider du réapprovisionnement.
        """
        CREATE TABLE regional_inventory_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region TEXT NOT NULL,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity REAL NOT NULL CHECK (quantity >= 0),
            note TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX idx_regional_inventory_region
            ON regional_inventory_reports(region, created_at DESC);
        CREATE INDEX idx_regional_inventory_product
            ON regional_inventory_reports(product_id);
        """,
    ),
    (
        36,
        # Signalements manuels de rupture régionale. Remplace l'ancienne alerte
        # automatique par seuil : le stock d'une région ne diminue jamais avec
        # l'usage réel (les techniciens se servent sans passer par le système),
        # donc `min_stock` ne se déclenchait jamais ou de façon trompeuse.
        # C'est la région qui dit « je n'ai plus de X », le central qui résout.
        """
        CREATE TABLE regional_alert_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region TEXT NOT NULL,
            product_id INTEGER NOT NULL REFERENCES products(id),
            note TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            resolved_at TEXT,
            resolved_by TEXT
        );
        CREATE INDEX idx_regional_alert_ouverts
            ON regional_alert_signals(region, resolved_at);
        CREATE INDEX idx_regional_alert_product
            ON regional_alert_signals(product_id);
        """,
    ),
    (
        37,
        # Lecteurs restreints à une ou plusieurs régions.
        #
        # Le rôle 'regional' est rattaché à UNE seule région, portée par la
        # colonne `users.region`. Un lecteur, lui, peut avoir à surveiller
        # plusieurs régions (un responsable de zone sud, par exemple) : une
        # colonne unique ne suffit pas, d'où cette table de liaison.
        #
        # AUCUNE ligne pour un compte = AUCUNE restriction. C'est le
        # comportement historique (le lecteur voit tout), conservé tel quel
        # pour tous les comptes existants : la migration ne remplit rien.
        """
        CREATE TABLE user_regions (
            user_id INTEGER NOT NULL REFERENCES users(id),
            region TEXT NOT NULL,
            PRIMARY KEY (user_id, region)
        );
        CREATE INDEX idx_user_regions_user ON user_regions(user_id);
        """,
    ),
    (
        38,
        # Annotation libre par région : saisonnalité, accès dégradé
        # (« route difficile juin-septembre »). Purement informative — aucune
        # logique automatique ne s'y branche, c'est une note que l'admin lit
        # avant de planifier un transfert.
        #
        # Une table plutôt qu'un fichier de configuration : la note doit être
        # visible depuis tous les postes, et tracée (qui, quand).
        """
        CREATE TABLE region_notes (
            region TEXT PRIMARY KEY,
            note TEXT NOT NULL DEFAULT '',
            updated_by TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        """,
    ),
    (
        39,
        # Photos : identifier un article au visuel, et joindre une preuve à un
        # bon (colis abîmé, livraison signée).
        #
        # Seul le NOM du fichier est stocké ici, jamais les octets. Une base
        # SQLite est copiée en entier à chaque sauvegarde ; y verser des
        # images de 3 Mo ferait exploser la durée et la taille des sauvegardes
        # pour des données qu'aucune requête SQL n'interroge. Le contenu vit
        # dans `chemins.dossier_photos()`, comme les sauvegardes elles-mêmes.
        #
        # UNE colonne côté produit (une fiche a une photo, remplaçable), une
        # TABLE côté bon (un colis abîmé se photographie sous trois angles).
        """
        ALTER TABLE products ADD COLUMN photo_path TEXT;

        CREATE TABLE document_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id),
            filename TEXT NOT NULL,
            caption TEXT,
            uploaded_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX idx_document_photos_doc ON document_photos(document_id);
        """,
    ),
    (
        40,
        # Expiration des sessions par INACTIVITÉ, en plus de la durée absolue
        # de 8 h déjà portée par `expires_at`.
        #
        # Colonne NULLABLE et sans valeur par défaut, délibérément : les
        # sessions déjà ouvertes au moment de la mise à jour se retrouvent à
        # NULL, ce que `utilisateurs.session_valide` interprète comme « active
        # à l'instant » et date au premier passage. Un DEFAULT à 0 les aurait
        # toutes déconnectées d'un coup au redémarrage.
        """
        ALTER TABLE sessions ADD COLUMN last_seen_at REAL;
        """,
    ),
    (
        41,
        # Transfert d'entrepôt régional à entrepôt régional.
        #
        # Jusqu'ici, faire passer de la marchandise d'une région à une autre
        # obligeait à la faire remonter au central (un RETURN) puis à la
        # réexpédier (un DELIVERY) : deux bons, deux comptages, et un trajet
        # fictif que personne n'a fait. Le transfert direct est un mouvement à
        # part entière, avec le MÊME transit en deux temps que le transfert
        # central -> région (received_at / received_quantity).
        #
        # Nouveau TYPE de bon plutôt qu'un DELIVERY marqué : un DELIVERY
        # débite `products.current_stock` (le stock du central), ce qu'un
        # transfert entre deux régions ne doit surtout pas faire — la
        # marchandise a déjà quitté le central il y a des semaines. Le type
        # sépare les deux comptabilités sans qu'aucune requête existante
        # (toutes filtrées par type) n'ait à changer de sens.
        #
        # SQLite ne sait pas modifier un CHECK par ALTER TABLE (comme aux
        # migrations 2, 29, 32 et 34) : reconstruction de la table. Les id
        # sont conservés — document_lines, stock_movements, bottle_ledger et
        # document_photos les référencent.
        #
        # `source_region` : région d'ORIGINE. NULL pour tous les autres types
        # (le central est l'origine implicite d'un DELIVERY). `region` reste
        # la DESTINATION, quel que soit le type : la confirmation de réception
        # et le périmètre d'un compte régional s'appuient dessus sans
        # distinction de cas.
        """
        CREATE TABLE documents_v4 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK (type IN
                ('RECEIVING', 'DELIVERY', 'ADJUSTMENT', 'RETURN',
                 'SUPPLIER_RETURN', 'REGIONAL_TRANSFER')),
            party TEXT,
            party_contact_id INTEGER REFERENCES contacts(id),
            operator TEXT,
            region TEXT,
            reference TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            cancelled_at TEXT,
            cancelled_by TEXT,
            cancel_reason TEXT,
            idempotency_key TEXT,
            created_by TEXT,
            station TEXT,
            server_received_at TEXT,
            carrier TEXT,
            technician TEXT,
            received_at TEXT,
            received_by TEXT,
            source_region TEXT
        );
        INSERT INTO documents_v4 (id, type, party, party_contact_id, operator, region,
                                  reference, note, created_at, cancelled_at, cancelled_by,
                                  cancel_reason, idempotency_key, created_by, station,
                                  server_received_at, carrier, technician,
                                  received_at, received_by)
            SELECT id, type, party, party_contact_id, operator, region,
                   reference, note, created_at, cancelled_at, cancelled_by,
                   cancel_reason, idempotency_key, created_by, station,
                   server_received_at, carrier, technician,
                   received_at, received_by FROM documents;
        DROP TABLE documents;
        ALTER TABLE documents_v4 RENAME TO documents;

        CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at);
        CREATE INDEX IF NOT EXISTS idx_documents_type_created ON documents(type, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_documents_cancelled ON documents(cancelled_at);
        CREATE INDEX IF NOT EXISTS idx_documents_created_by ON documents(created_by);
        CREATE INDEX IF NOT EXISTS idx_documents_party_contact ON documents(party_contact_id);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_idempotency
            ON documents(idempotency_key) WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_documents_received
            ON documents(type, region, received_at);
        CREATE INDEX IF NOT EXISTS idx_documents_source_region
            ON documents(source_region, received_at);
        """,
    ),
    (
        42,
        # Code-barres du produit : socle du travail à la douchette.
        #
        # Une douchette USB se comporte comme un clavier (elle tape le code
        # puis Entrée) : rien de matériel n'est à coder, il suffit que le
        # code saisi puisse être RETROUVÉ. D'où une simple colonne texte et
        # un index — le reste est de l'interface.
        #
        # Colonne NULLABLE et sans valeur par défaut : aucun produit n'a de
        # code aujourd'hui, et un produit sans code doit continuer de vivre
        # normalement (saisie manuelle). Le code s'ajoute fiche par fiche.
        #
        # Index UNIQUE PARTIEL, comme `ux_documents_idempotency` (migration
        # 41) : deux produits ACTIFS ne peuvent pas partager un code — sinon
        # un scan serait ambigu et pointerait sur la mauvaise fiche. Trois
        # exclusions volontaires :
        #  - NULL : la quasi-totalité du catalogue, qui doit rester libre ;
        #  - '' : un champ vidé par l'écran d'édition vaut « pas de code » ;
        #  - archived = 1 : un produit archivé n'est plus scannable, son code
        #    doit pouvoir être réattribué à la fiche qui le remplace.
        # La validation applicative double cet index côté API pour rendre un
        # 409 lisible plutôt qu'une IntegrityError opaque.
        """
        ALTER TABLE products ADD COLUMN barcode TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_products_barcode
            ON products(barcode)
            WHERE barcode IS NOT NULL AND barcode != '' AND archived = 0;
        """,
    ),
    (
        43,
        # Fiche fournisseur enrichie. Quatre renseignements que l'entrepôt
        # gardait jusqu'ici sur un carnet papier, et qui manquaient au moment
        # de décider quand recommander et à quel prix.
        #
        # Toutes NULLABLES et sans valeur par défaut : la table `contacts`
        # porte aussi des clients et des « autres », pour qui aucun de ces
        # champs n'a de sens. Un contact sans rien de renseigné reste
        # parfaitement valide, exactement comme aujourd'hui.
        #
        #  - delai_jours         : délai habituel ANNONCÉ par le fournisseur,
        #    en jours. C'est une donnée déclarative, à ne pas confondre avec
        #    la régularité MESURÉE de `/reports/delai-fournisseurs`, qui se
        #    calcule sur les réceptions réelles. Les deux se lisent
        #    ensemble : l'écart entre la promesse et le constat est
        #    précisément ce qu'un acheteur veut voir.
        #  - devise              : texte libre ('HTG', 'USD'). Pas de CHECK ni
        #    de référentiel : l'entrepôt achète aussi hors des deux monnaies
        #    courantes, et aucun calcul de conversion n'est fait nulle part
        #    dans l'application — `unit_cost` reste un nombre nu. Contraindre
        #    la colonne donnerait l'illusion d'une comptabilité multidevise
        #    qui n'existe pas.
        #  - conditions_paiement : texte libre ('30 jours', 'comptant').
        #  - whatsapp            : numéro WhatsApp Business, DISTINCT de
        #    `phone`. Beaucoup de fournisseurs haïtiens répondent sur un
        #    numéro WhatsApp différent de leur ligne fixe ; écraser `phone`
        #    aurait fait perdre celui par lequel on les joint autrement.
        """
        ALTER TABLE contacts ADD COLUMN delai_jours INTEGER;
        ALTER TABLE contacts ADD COLUMN devise TEXT;
        ALTER TABLE contacts ADD COLUMN conditions_paiement TEXT;
        ALTER TABLE contacts ADD COLUMN whatsapp TEXT;
        """,
    ),
    (
        44,
        # Jetons de session : la colonne `sessions.token` portait le jeton EN
        # CLAIR. Elle porte désormais son empreinte SHA-256
        # (`utilisateurs.hacher_jeton`), pour qu'un vol du fichier de base —
        # ou d'une sauvegarde non chiffrée — ne suffise plus à REJOUER la
        # session d'un opérateur connecté sans connaître son mot de passe.
        #
        # VIDAGE, et non bascule des lignes existantes. Trois raisons :
        #  1. Une bascule serait impossible en SQL nu : SQLite n'a pas de
        #     fonction sha256(). Il faudrait relire les lignes en Python et
        #     les réécrire — du code de migration en dehors du mécanisme
        #     déclaratif de MIGRATIONS, pour un gain d'un seul jour.
        #  2. Les jetons à basculer sont précisément ceux qui ont déjà séjourné
        #     en clair sur le disque : les conserver prolongerait la validité
        #     de secrets qu'on doit considérer comme exposés. Les jeter est le
        #     comportement correct d'une rotation de secret.
        #  3. Le vidage est idempotent, instantané, et ne peut pas laisser une
        #     ligne à moitié convertie qui ferait échouer silencieusement
        #     l'authentification.
        #
        # CONSÉQUENCE À ANNONCER : au premier démarrage suivant cette mise à
        # jour, TOUTES les sessions ouvertes sont fermées. Chaque poste devra
        # se reconnecter une fois — mot de passe habituel, aucun compte n'est
        # touché, aucune donnée d'exploitation n'est affectée. À faire passer
        # en début de journée plutôt qu'au milieu d'une réception.
        """
        DELETE FROM sessions;
        """,
    ),
    (
        45,
        # MULTI-01 : idempotence de l'inventaire régional. C'était le seul
        # endpoint d'écriture du système sans protection contre le rejeu — un
        # comptage renvoyé après une coupure réseau (timeout, réponse perdue)
        # créait un second rapport, doublant silencieusement le total agrégé
        # du dernier comptage utilisé pour décider du réapprovisionnement.
        #
        # Index sur (idempotency_key, product_id), PAS sur la clé seule : un
        # comptage est un rapport de PLUSIEURS lignes (une par produit) qui
        # partagent la même clé — un index sur la clé seule aurait rejeté
        # toute deuxième ligne du même envoi légitime dès la première.
        #
        # Index UNIQUE PARTIEL, même patron que ux_documents_idempotency
        # (migration 41) : les rapports historiques à idempotency_key NULL
        # ne sont jamais en conflit entre eux.
        """
        ALTER TABLE regional_inventory_reports ADD COLUMN idempotency_key TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_regional_inventory_idempotency
            ON regional_inventory_reports(idempotency_key, product_id)
            WHERE idempotency_key IS NOT NULL;
        """,
    ),
    (
        47,
        # Isolation par secteur : FON (puis RAN/Fuel/Spare) partage la même
        # base et le même code que Consumables, sans jamais mélanger les
        # données. DEFAULT 'CONSUMABLES' bascule tout l'existant (Idy509,
        # tout le catalogue, tout l'historique) sans aucune valeur à corriger
        # à la main — comportement inchangé pour les comptes déjà en place.
        #
        # Pas de CHECK sur les valeurs acceptées : comme `region` (qui n'en a
        # pas non plus, validée en Python via normaliser_region), la liste
        # des secteurs vit dans SECTEURS_VALIDES (server/models.py) —
        # ajouter RAN/Fuel/Spare plus tard ne demande aucune migration.
        #
        # NUMÉROTÉE 47 (et pas 46) : un faux départ d'implémentation avait
        # déjà livré — et fait tourner sur au moins un poste — un ANCIEN
        # contenu de la migration 46 (une table `sector_items`, abandonnée
        # depuis). Réutiliser le numéro 46 pour ce contenu différent aurait
        # fait sauter cette migration sur toute base déjà passée à la
        # version 46 (PRAGMA user_version >= target => ignorée), la
        # laissant sans les colonnes `sector` alors que le code les suppose
        # déjà là — exactement ce qui a cassé la connexion. D'où la règle du
        # fichier : ne jamais modifier une migration déjà livrée, en ajouter
        # une nouvelle. `DROP TABLE` nettoie la table orpheline si elle
        # existe (base déjà passée par le faux départ) ; ne fait rien sinon
        # (base neuve, ou déjà nettoyée).
        """
        DROP TABLE IF EXISTS sector_items;
        ALTER TABLE users ADD COLUMN sector TEXT NOT NULL DEFAULT 'CONSUMABLES';
        ALTER TABLE products ADD COLUMN sector TEXT NOT NULL DEFAULT 'CONSUMABLES';
        ALTER TABLE documents ADD COLUMN sector TEXT NOT NULL DEFAULT 'CONSUMABLES';
        CREATE INDEX idx_products_sector ON products(sector);
        CREATE INDEX idx_documents_sector ON documents(sector);
        """,
    ),
    (
        48,
        # Le catalogue `products` ne portait que des champs pensés pour
        # Consumables (sku, nom, catégorie, unité...). Le Masterlist FON en
        # apporte d'autres (Part Number, Group, Sub Category, Vendor,
        # Supported Genset Models, RL_Apply, Discontinued) : sans ces
        # colonnes, l'import les perdait silencieusement. Ajoutées à
        # `products` (partagé par tous les secteurs) plutôt qu'à une table
        # séparée : Consumables ne les renseigne jamais (NULL/0 partout),
        # exactement comme bidon_capacity/consigne_bouteille aujourd'hui —
        # des champs qui n'ont de sens que pour certains produits.
        """
        ALTER TABLE products ADD COLUMN part_number TEXT;
        ALTER TABLE products ADD COLUMN group_name TEXT;
        ALTER TABLE products ADD COLUMN sub_category TEXT;
        ALTER TABLE products ADD COLUMN supported_genset_models TEXT;
        ALTER TABLE products ADD COLUMN vendor TEXT;
        ALTER TABLE products ADD COLUMN rl_apply INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE products ADD COLUMN discontinued INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        49,
        # `contacts` (fournisseurs/clients) n'avait pas de colonne `sector`,
        # contrairement à users/products/documents (migration 47) : un compte
        # FON voyait donc les fournisseurs Consumables dans le champ Source
        # de sa réception (FON n'a qu'une seule source, Digicel, pas de
        # gestion de fournisseurs). `name` reste UNIQUE globalement plutôt que
        # par secteur : même compromis assumé que `products.sku` (migration
        # 47) — un nom de contact partagé entre deux secteurs est un cas rare
        # qu'on préfère refuser plutôt que compliquer la contrainte.
        """
        ALTER TABLE contacts ADD COLUMN sector TEXT NOT NULL DEFAULT 'CONSUMABLES';
        CREATE INDEX idx_contacts_sector ON contacts(sector);
        """,
    ),
    (
        50,
        # FON reçoit ses propres entrepôts régionaux (Carrefour, Arcahaie,
        # North, South). "Carrefour" existe déjà comme région Consumables :
        # sans colonne secteur, un inventaire régional ou une note saisis sous
        # ce nom se mélangeraient entre les deux secteurs. `documents` et
        # `products` sont déjà scopés (migration 47) ; ces trois tables
        # régionales avaient été oubliées, car FON n'avait pas encore
        # d'activité régionale au moment de cette migration.
        #
        # `region_notes` a pour clé primaire `region` seul (migration 38) :
        # SQLite ne permet pas de changer une PK par ALTER, la table est donc
        # recréée avec une PK composite (region, sector). Les notes
        # existantes sont conservées, rattachées à CONSUMABLES.
        #
        # `rl_ledger` (reverse logistics) : équipement prêté qui doit être
        # physiquement retourné après livraison (les 2 routeurs GPON du
        # Masterlist FON, `products.rl_apply` — migration 48, jusqu'ici sans
        # aucune logique dessus). Même principe que `bottle_ledger`
        # (migrations 23/26/30/31) : dette créée à la livraison, retour =
        # ligne négative, annulation = contre-écriture, jamais de suppression.
        # Pas de colonne secteur : `product_id` référence déjà un produit d'un
        # secteur précis, exactement comme `bottle_ledger` aujourd'hui.
        """
        ALTER TABLE regional_inventory_reports ADD COLUMN sector TEXT NOT NULL DEFAULT 'CONSUMABLES';
        ALTER TABLE regional_alert_signals ADD COLUMN sector TEXT NOT NULL DEFAULT 'CONSUMABLES';
        CREATE INDEX idx_regional_inventory_sector ON regional_inventory_reports(sector, region);
        CREATE INDEX idx_regional_alert_sector ON regional_alert_signals(sector, region);

        CREATE TABLE region_notes_new (
            region TEXT NOT NULL,
            sector TEXT NOT NULL DEFAULT 'CONSUMABLES',
            note TEXT NOT NULL DEFAULT '',
            updated_by TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (region, sector)
        );
        INSERT INTO region_notes_new (region, sector, note, updated_by, updated_at)
            SELECT region, 'CONSUMABLES', note, updated_by, updated_at FROM region_notes;
        DROP TABLE region_notes;
        ALTER TABLE region_notes_new RENAME TO region_notes;

        CREATE TABLE rl_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            region TEXT NOT NULL,
            technician TEXT,
            document_id INTEGER REFERENCES documents(id),
            quantity REAL NOT NULL,
            note TEXT,
            reference TEXT,
            cancels_entry_id INTEGER REFERENCES rl_ledger(id),
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX idx_rl_ledger_product_region ON rl_ledger(product_id, region);
        CREATE UNIQUE INDEX idx_rl_ledger_cancels
            ON rl_ledger(cancels_entry_id) WHERE cancels_entry_id IS NOT NULL;
        """,
    ),
    (
        51,
        # Secteur FUEL : une cuve centrale (Diesel plafonné à 1300 gal,
        # Gasoline sans plafond) qui dispense du carburant à des véhicules
        # pour des « projets » (étiquette budgétaire interne, sans rapport
        # avec les secteurs applicatifs de même nom). Colonnes nullables
        # bolt-on sur `products`/`documents`, même principe que
        # `consigne_bouteille`/`rl_apply` : NULL partout pour Consumables/
        # FON, seul Fuel les renseigne.
        #
        # Pas de table `vehicles` dédiée pour ce lot (décision utilisateur) :
        # `vehicle_plate` est un simple texte libre, l'autocomplétion se
        # base sur l'historique des bons (`GET /fuel/vehicles`).
        """
        ALTER TABLE products ADD COLUMN tank_capacity REAL;

        ALTER TABLE documents ADD COLUMN vehicle_plate TEXT;
        ALTER TABLE documents ADD COLUMN vehicle_info TEXT;
        ALTER TABLE documents ADD COLUMN mileage REAL;
        ALTER TABLE documents ADD COLUMN project TEXT;
        ALTER TABLE documents ADD COLUMN receiver TEXT;
        ALTER TABLE documents ADD COLUMN fuel_card TEXT;
        """,
    ),
    (
        52,
        # Fuel a plusieurs sites (WH Central/Orelus, Canapé-Vert/Rijkaard...),
        # chacun avec ses propres cuves — mais pas encore la vraie
        # régionalisation (transferts, entrepôts régionaux) : juste assez pour
        # qu'un compte non-admin ne voie et ne manipule QUE les produits de
        # son propre site, alors qu'un admin (Rijkaard) garde la vue globale.
        # NULL partout pour Consumables/FON, et pour les produits/comptes pas
        # encore rattachés à un site — traité comme "visible par tous" (voir
        # `_site_du_compte` dans server/app.py), pour ne rien casser sur les
        # données déjà en place.
        """
        ALTER TABLE users ADD COLUMN site TEXT;
        ALTER TABLE products ADD COLUMN site TEXT;
        """,
    ),
    (
        53,
        # `CHECK (quantity <> 0)` interdisait toute ligne à quantité nulle.
        # C'est la bonne règle pour un MOUVEMENT (recevoir ou livrer zéro
        # n'existe pas), mais elle empêchait d'enregistrer un jaugeage de cuve
        # CONFORME : l'opérateur relève 170 gal sur une cuve à 170 gal, l'écart
        # est nul, et son passage ne laissait aucune trace (constaté par
        # Idy509 le 2026-09-08 — son jaugeage était introuvable dans
        # l'Historique). Pour du carburant, le relevé EST l'information.
        #
        # La contrainte est donc desserrée pour ce seul cas : une ligne de
        # COMPTAGE (counted_quantity renseignée) peut porter un écart nul.
        # Une ligne de mouvement ordinaire (counted_quantity NULL) reste
        # interdite à zéro, exactement comme avant.
        #
        # SQLite ne sait pas modifier un CHECK : la table est recréée, données
        # copiées, index refaits — même recette que les migrations
        # précédentes qui ont dû changer une contrainte.
        """
        CREATE TABLE document_lines_m53 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity REAL NOT NULL CHECK (quantity <> 0 OR counted_quantity IS NOT NULL),
            counted_quantity REAL,
            received_quantity REAL
        );
        INSERT INTO document_lines_m53 (id, document_id, product_id, quantity,
                                        counted_quantity, received_quantity)
            SELECT id, document_id, product_id, quantity,
                   counted_quantity, received_quantity FROM document_lines;
        DROP TABLE document_lines;
        ALTER TABLE document_lines_m53 RENAME TO document_lines;

        -- Les QUATRE index de la table, recréés à l'identique : supprimer la
        -- table supprime ses index, et `ux_document_lines_doc_product` est
        -- une contrainte d'unicité (une seule ligne par produit et par bon),
        -- pas un simple index de performance. L'oublier ici aurait
        -- silencieusement rendu possible ce qu'elle interdit.
        CREATE INDEX IF NOT EXISTS idx_document_lines_document_id ON document_lines(document_id);
        CREATE INDEX IF NOT EXISTS idx_document_lines_product ON document_lines(product_id);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_document_lines_doc_product
            ON document_lines(document_id, product_id);
        CREATE INDEX IF NOT EXISTS idx_document_lines_doc_product
            ON document_lines(document_id, product_id);
        """,
    ),
    (
        54,
        # `audit_log` ne portait pas de secteur : l'écran « Journal d'audit »
        # montrait à l'admin de N'IMPORTE quel secteur toutes les écritures de
        # tous les autres — qui a modifié quel produit, annulé quel bon, créé
        # quel compte. Dernière fuite inter-secteurs connue après les
        # indicateurs (voir docs/SECTEUR_FUEL.md).
        #
        # Reprise des lignes existantes : le secteur se déduit de l'entité
        # tracée. Ce qui reste NULL (entité supprimée depuis, ou trace système
        # comme un export ou un changement de station) demeure visible de tous
        # les admins — mieux vaut une trace orpheline lisible qu'une trace
        # perdue. Validé sur une copie de la base d'exploitation : 93 des 104
        # lignes ont retrouvé leur secteur, 11 restent système/orphelines.
        """
        ALTER TABLE audit_log ADD COLUMN sector TEXT;

        UPDATE audit_log SET sector = (
            SELECT p.sector FROM products p WHERE p.id = audit_log.entity_id)
            WHERE entity_type = 'product' AND sector IS NULL;

        UPDATE audit_log SET sector = (
            SELECT d.sector FROM documents d WHERE d.id = audit_log.entity_id)
            WHERE entity_type = 'document' AND sector IS NULL;

        UPDATE audit_log SET sector = (
            SELECT u.sector FROM users u WHERE u.id = audit_log.entity_id)
            WHERE entity_type = 'user' AND sector IS NULL;

        UPDATE audit_log SET sector = (
            SELECT s.sector FROM regional_alert_signals s
             WHERE s.id = audit_log.entity_id)
            WHERE entity_type = 'alerte_regionale' AND sector IS NULL;

        -- La consigne bouteille n'existe que pour Consumables.
        UPDATE audit_log SET sector = 'CONSUMABLES'
            WHERE entity_type IN ('bottle_ledger', 'bottle_return')
              AND sector IS NULL;

        CREATE INDEX IF NOT EXISTS idx_audit_log_sector ON audit_log(sector);
        """,
    ),
    (
        55,
        # L'index unique de la migration 42 ne portait QUE sur `barcode` :
        # unicité GLOBALE tous secteurs confondus. Le docstring de
        # `_produit_par_code_barres` (server/app.py) affirmait pourtant
        # explicitement « deux secteurs différents peuvent réutiliser le même
        # code-barres sans conflit » — faux au niveau base. Repéré par
        # exploration le 9 septembre 2026 : un admin FON qui posait un code
        # déjà pris par un produit CONSUMABLES actif se voyait bloqué par
        # l'index global, avec un message trompeur (« SKU déjà utilisé »,
        # l'exception générique du bloc juste en dessous dans le code, faute
        # de savoir distinguer un conflit de SKU d'un conflit de code-barres).
        #
        # Contrairement au SKU (limite globale documentée et assumée, chaque
        # secteur utilisant ses propres préfixes), le code-barres n'a jamais
        # eu cette justification : rien n'empêche deux secteurs de recevoir
        # un jour la même étiquette fournisseur. DROP + CREATE, pas ALTER :
        # SQLite ne sait pas modifier un index en place.
        """
        DROP INDEX IF EXISTS ux_products_barcode;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_products_barcode
            ON products(barcode, sector)
            WHERE barcode IS NOT NULL AND barcode != '' AND archived = 0;
        """,
    ),
    (
        56,
        # Même défaut que la migration 55, trouvé le même jour sur une autre
        # colonne : `ux_documents_idempotency` (posée migration 8, reposée
        # telle quelle aux migrations 15 et 41) ne porte QUE sur
        # `idempotency_key`, tous secteurs confondus — alors que
        # `_rejeu_idempotent` (server/app.py) est désormais scopée par
        # secteur à la LECTURE (corrigé le même jour). Sans ce second
        # correctif, la lecture aurait beau ignorer le bon d'un autre
        # secteur, la création d'un NOUVEAU bon avec une clé qui coïncide par
        # hasard avec celle d'un autre secteur heurtait quand même l'index
        # global et échouait en 500 brut au lieu de créer le bon attendu.
        #
        # `documents.sector` n'existait pas encore à la migration 41 (posée à
        # la 47) : impossible de scoper l'index avant aujourd'hui.
        """
        DROP INDEX IF EXISTS ux_documents_idempotency;
        CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_idempotency
            ON documents(idempotency_key, sector) WHERE idempotency_key IS NOT NULL;
        """,
    ),
    (
        57,
        # Le CHECK sur `role` dans la table `users` n'autorise que les quatre
        # rôles d'origine (admin, magasinier, lecteur, regional). Le nouveau
        # rôle « board » (portail de direction, lecture seule multi-secteurs)
        # doit y être ajouté — SQLite impose une reconstruction de table.
        """
        CREATE TABLE users_m57 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'magasinier'
                CHECK (role IN ('admin', 'magasinier', 'lecteur', 'regional', 'board')),
            region TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            sector TEXT NOT NULL DEFAULT 'CONSUMABLES',
            site TEXT
        );
        INSERT INTO users_m57 (id, username, password_hash, display_name,
                               role, region, active, created_at, sector, site)
            SELECT id, username, password_hash, display_name,
                   role, region, active, created_at, sector, site FROM users;
        DROP TABLE users;
        ALTER TABLE users_m57 RENAME TO users;
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
        """,
    ),
]


def _baseline(conn: sqlite3.Connection) -> None:
    """Amène une base neuve OU existante (pré-versionnement) à la version 1."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    if "initial_stock" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN initial_stock REAL NOT NULL DEFAULT 0")
        conn.execute("UPDATE products SET initial_stock = current_stock")

    doc_columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "region" not in doc_columns:
        conn.execute("ALTER TABLE documents ADD COLUMN region TEXT")
    if "reference" not in doc_columns:
        conn.execute("ALTER TABLE documents ADD COLUMN reference TEXT")


def _normaliser_regions_existantes(conn: sqlite3.Connection) -> int:
    """Aligne les régions déjà saisies sur le libellé canonique.

    Fait en Python et non en SQL : la comparaison doit ignorer accents et casse
    ('aquin', 'AQUIN', 'Cap-Haitien' -> 'Aquin', 'Cap-Haïtien').
    Une valeur non reconnue est laissée telle quelle et signalée : on ne devine
    pas à la place de l'utilisateur.
    """
    from reference_data import normaliser_region

    corriges = 0
    valeurs = [r[0] for r in conn.execute(
        "SELECT DISTINCT region FROM documents WHERE region IS NOT NULL AND TRIM(region) <> ''"
    )]
    for valeur in valeurs:
        try:
            canonique = normaliser_region(valeur)
        except ValueError:
            log.warning("région non reconnue, laissée en l'état : %r", valeur)
            continue
        if canonique and canonique != valeur:
            cur = conn.execute(
                "UPDATE documents SET region = ? WHERE region = ?", (canonique, valeur)
            )
            corriges += cur.rowcount
    return corriges


SUFFIXE_SAUVEGARDE_MIGRATION = ".pre_migration_backup"


def _chemin_sauvegarde_migration() -> Path:
    return DB_PATH.with_name(DB_PATH.name + SUFFIXE_SAUVEGARDE_MIGRATION)


def _preserver_sauvegarde_orpheline() -> None:
    """Met de côté une copie de sécurité laissée par un démarrage interrompu.

    La copie n'est supprimée qu'après le succès de TOUTES les migrations. Si
    elle est encore là au démarrage suivant, c'est que le processus précédent
    a été tué pendant la phase de migration. L'écraser reviendrait à remplacer
    l'état d'AVANT la mise à jour (le seul état sûr) par l'état d'après une
    interruption. On l'archive sous un nom horodaté et on la laisse en place.
    """
    orpheline = _chemin_sauvegarde_migration()
    if not orpheline.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = orpheline.with_name(f"{orpheline.name}.{stamp}")
    try:
        orpheline.replace(archive)
    except OSError:
        log.exception("impossible d'archiver la copie de sécurité orpheline %s",
                      orpheline.name)
        return
    log.warning(
        "démarrage précédent interrompu pendant une migration : "
        "copie de sécurité conservée sous %s", archive.name,
    )


def _sauvegarder_avant_migrations(conn: sqlite3.Connection) -> Path | None:
    """Copie le fichier de base AVANT la première migration.

    `conn.executescript()` déclenche un COMMIT implicite : le `BEGIN` posé
    juste avant ne protège donc rien, et le `rollback()` du gestionnaire
    d'erreur ne peut rien annuler. La seule protection fiable est une copie
    du fichier, restaurée telle quelle si une migration échoue.

    Le WAL est d'abord replié dans le fichier principal (checkpoint TRUNCATE)
    pour que la copie contienne bien la totalité des données.
    """
    if not DB_PATH.exists():
        return None
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        # Base neuve ou WAL indisponible : la copie reste valable.
        log.warning("checkpoint WAL impossible avant migration", exc_info=True)
    sauvegarde = _chemin_sauvegarde_migration()
    shutil.copy2(DB_PATH, sauvegarde)
    log.info("copie de sécurité avant migrations : %s", sauvegarde.name)
    return sauvegarde


def _restaurer_sauvegarde_migration(sauvegarde: Path) -> None:
    """Remet la base dans l'état exact d'avant les migrations."""
    for suffixe in ("-wal", "-shm"):
        annexe = DB_PATH.with_name(DB_PATH.name + suffixe)
        try:
            annexe.unlink(missing_ok=True)
        except OSError:
            log.warning("suppression de %s impossible", annexe.name)
    shutil.copy2(sauvegarde, DB_PATH)
    log.warning("base restaurée depuis %s", sauvegarde.name)


def _script_transactionnel(sql: str, target: int) -> str:
    """Emballe une migration et son numéro de version dans UNE transaction.

    Sans ça, `executescript()` exécute chaque instruction en auto-commit, puis
    `PRAGMA user_version` était posé dans une instruction séparée. Un processus
    tué entre les deux (coupure de courant, taskkill /f) laissait une base
    RÉELLEMENT migrée mais marquée à l'ancienne version : au redémarrage la
    même migration était rejouée et échouait (`duplicate column name`), ce qui
    déclenchait la restauration de la copie de sécurité — donc la perte de tout
    ce que la migration avait déjà fait.

    En SQLite le DDL est transactionnel (contrairement à MySQL/Oracle) et
    `PRAGMA user_version` s'écrit dans l'en-tête du fichier sous le contrôle de
    la transaction : le couple « migration + version » devient donc atomique.
    Après une coupure, la base est soit entièrement à l'ancienne version, soit
    entièrement à la nouvelle. Jamais entre les deux.
    """
    corps = sql.strip()
    if not corps.endswith(";"):
        corps += ";"
    return (
        "BEGIN IMMEDIATE;\n"
        f"{corps}\n"
        f"PRAGMA user_version = {target};\n"
        "COMMIT;"
    )


class MigrationConcurrente(RuntimeError):
    """Une autre instance du serveur a migré la base entre-temps."""


def _appliquer_migrations(conn: sqlite3.Connection, version: int) -> int:
    """Applique les migrations en attente. Renvoie la version atteinte."""
    for target, sql in MIGRATIONS:
        if target <= version:
            continue

        # Relecture juste avant d'appliquer : si un second serveur a été lancé
        # en parallèle (double clic sur LANCER.bat) et a migré la base pendant
        # qu'on lisait la version, rejouer la migration échouerait et
        # déclencherait la restauration de la copie de sécurité, écrasant le
        # travail de l'autre processus. On s'arrête proprement à la place.
        courante = conn.execute("PRAGMA user_version").fetchone()[0]
        if courante >= target:
            log.warning(
                "migration %d déjà appliquée par un autre processus "
                "(version en base : %d), arrêt des migrations",
                target, courante,
            )
            raise MigrationConcurrente(courante)

        # Hors transaction : PRAGMA foreign_keys est sans effet à l'intérieur.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executescript(_script_transactionnel(sql, target))
        except Exception as e:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                log.warning("ROLLBACK impossible après l'échec de la migration %d", target)
            # Second filet contre le double démarrage : la relecture ci-dessus a
            # lieu AVANT de prendre le verrou d'écriture. Un autre processus
            # peut donc avoir commité la même migration entre les deux, ce qui
            # fait échouer la nôtre sur « duplicate column name ». Sans cette
            # vérification, l'échec déclenchait la restauration de la copie de
            # sécurité et effaçait le travail de l'autre processus.
            try:
                courante = conn.execute("PRAGMA user_version").fetchone()[0]
            except sqlite3.Error:
                courante = version
            if courante >= target:
                log.warning(
                    "migration %d appliquée par un autre processus pendant la nôtre "
                    "(version en base : %d) : aucune restauration", target, courante,
                )
                raise MigrationConcurrente(courante) from e
            log.exception("migration %d échouée, base laissée en version %d", target, version)
            raise RuntimeError(
                f"Migration {target} échouée, base laissée en version {version} : {e}"
            ) from e
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        log.info("migration %d appliquée", target)
        version = target
    return version


def init_db() -> None:
    # Avant toute création de fichier : une base ouverte en WAL sur un
    # partage réseau se corrompt en silence (voir MESSAGE_BASE_RESEAU).
    _verifier_base_locale()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        # Coupures de courant fréquentes : la durabilité prime sur la vitesse.
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 30000")

        version = conn.execute("PRAGMA user_version").fetchone()[0]

        if version == 0:
            _baseline(conn)
            conn.execute("PRAGMA user_version = 1")
            version = 1

        en_attente = [t for t, _ in MIGRATIONS if t > version]
        if en_attente:
            _preserver_sauvegarde_orpheline()
            sauvegarde = _sauvegarder_avant_migrations(conn)
            try:
                version = _appliquer_migrations(conn, version)
            except MigrationConcurrente as e:
                # Un autre serveur a migré la base : sa version fait foi, et
                # surtout on ne restaure RIEN — restaurer effacerait son
                # travail. La copie devenue inutile est retirée.
                version = e.args[0]
                if sauvegarde is not None:
                    sauvegarde.unlink(missing_ok=True)
            except Exception:
                if sauvegarde is not None and sauvegarde.exists():
                    conn.close()
                    _restaurer_sauvegarde_migration(sauvegarde)
                raise
            else:
                if sauvegarde is not None:
                    sauvegarde.unlink(missing_ok=True)

        # Après migrations : normalisation du référentiel régions.
        # Idempotent, donc rejoué à chaque démarrage sans effet si tout est propre.
        conn.execute("BEGIN IMMEDIATE")
        try:
            corriges = _normaliser_regions_existantes(conn)

            # Génère les mouvements des bons antérieurs au suivi par emplacement.
            from locations import (rattraper_documents_sans_mouvement,
                                    rattraper_ouvertures_manquantes)
            # Ouvertures d'abord : le stock de départ précède les mouvements.
            rattraper_ouvertures_manquantes(conn)
            rattrapes = rattraper_documents_sans_mouvement(conn)

            conn.commit()
            if corriges:
                log.info("%d document(s) : région normalisée", corriges)
            if rattrapes:
                log.info("%d document(s) : mouvements générés", rattrapes)
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


@contextmanager
def get_conn(write: bool = False):
    """Connexion transactionnelle.

    write=True ouvre en BEGIN IMMEDIATE : le verrou d'écriture est pris AVANT
    le premier SELECT, donc la vérification de stock et la mise à jour forment
    une seule opération atomique. Sans cela, deux postes peuvent valider
    simultanément une sortie sur le même produit et rendre le stock négatif.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = FULL")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
