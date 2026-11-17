from contextlib import contextmanager
from pathlib import Path
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    login TEXT NOT NULL,
    account_name TEXT NOT NULL,
    game TEXT NOT NULL,
    platform TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_current_login ON accounts(login)
    WHERE archived = 0;

CREATE TABLE IF NOT EXISTS rentals (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    order_id TEXT,
    marketplace TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    closed_at INTEGER,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_open_rental ON rentals(account_id)
    WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS rentals_account ON rentals(account_id);
CREATE INDEX IF NOT EXISTS open_rentals_expiry ON rentals(expires_at)
    WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    game TEXT NOT NULL,
    minutes INTEGER NOT NULL CHECK (minutes BETWEEN 1 AND 43200),
    price_cents INTEGER NOT NULL CHECK (price_cents BETWEEN 0 AND 1000000000),
    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS refunds (
    rental_id INTEGER PRIMARY KEY REFERENCES rentals(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    recorded_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS buyer_links (
    rental_id INTEGER PRIMARY KEY REFERENCES rentals(id),
    token_digest BLOB NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    buyer_portal_enabled INTEGER NOT NULL DEFAULT 1 CHECK (buyer_portal_enabled IN (0, 1)),
    buyer_portal_ttl_hours INTEGER NOT NULL DEFAULT 0 CHECK (buyer_portal_ttl_hours BETWEEN 0 AND 720),
    buyer_portal_help TEXT NOT NULL DEFAULT '',
    rental_reminder_minutes INTEGER NOT NULL DEFAULT 30 CHECK (rental_reminder_minutes BETWEEN 0 AND 1440)
);
INSERT OR IGNORE INTO settings(id) VALUES (1);

CREATE TABLE IF NOT EXISTS notification_reads (
    rental_id INTEGER NOT NULL REFERENCES rentals(id),
    kind TEXT NOT NULL CHECK (kind IN ('due', 'overdue')),
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (rental_id, kind, expires_at)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self, *, write: bool = False):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(write=True) as connection:
            for statement in SCHEMA.strip().split(";"):
                if statement.strip():
                    connection.execute(statement)
            columns = {column["name"] for column in connection.execute("PRAGMA table_info(accounts)")}
            migrations = {
                "game_subgroup": "ALTER TABLE accounts ADD COLUMN game_subgroup TEXT NOT NULL DEFAULT ''",
                "purchase_cents": "ALTER TABLE accounts ADD COLUMN purchase_cents INTEGER NOT NULL DEFAULT 0",
                "expense_cents": "ALTER TABLE accounts ADD COLUMN expense_cents INTEGER NOT NULL DEFAULT 0",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)
            rental_columns = {column["name"] for column in connection.execute("PRAGMA table_info(rentals)")}
            if "game_at_start" not in rental_columns:
                connection.execute("ALTER TABLE rentals ADD COLUMN game_at_start TEXT")
                connection.execute("ALTER TABLE rentals ADD COLUMN platform_at_start TEXT")
                connection.execute(
                    """UPDATE rentals SET
                        game_at_start = (SELECT game FROM accounts WHERE id = rentals.account_id),
                        platform_at_start = (SELECT platform FROM accounts WHERE id = rentals.account_id)"""
                )
            if "lot_title" not in rental_columns:
                connection.execute("ALTER TABLE rentals ADD COLUMN lot_title TEXT")
            link_columns = {column["name"] for column in connection.execute("PRAGMA table_info(buyer_links)")}
            if "link_expires_at" not in link_columns:
                connection.execute("ALTER TABLE buyer_links ADD COLUMN link_expires_at INTEGER")
