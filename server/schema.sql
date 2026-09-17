-- SCHÉMA DE BASE (version 1) — NE PLUS MODIFIER.
--
-- Ce fichier décrit l'état initial de la base et n'est appliqué qu'une fois,
-- à la création. Toute évolution ultérieure du schéma passe EXCLUSIVEMENT par
-- la liste MIGRATIONS de server/database.py, pilotée par PRAGMA user_version.
--
-- Raison : `CREATE TABLE IF NOT EXISTS` est silencieusement ignoré sur une base
-- existante. Décrire ici l'état cible donnerait deux sources de vérité et des
-- installations divergentes — un ajout de colonne présent ici ET en migration
-- fait échouer la migration avec « duplicate column name ».

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'pcs',
    current_stock REAL NOT NULL DEFAULT 0,
    min_stock REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN ('RECEIVING', 'DELIVERY')),
    party TEXT,
    operator TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS document_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity REAL NOT NULL CHECK (quantity > 0)
);

CREATE INDEX IF NOT EXISTS idx_document_lines_document_id ON document_lines(document_id);
CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at);
