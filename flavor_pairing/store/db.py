"""SQLite connection and schema-initialization helpers.

All SQLite-specific behavior for the data-foundation phase is isolated here
(docs/DECISIONS.md §G, docs/DATA_FOUNDATION_PLAN.md §14): opening connections,
enabling foreign-key enforcement, and applying the portable DDL in
``schema.sql``. A future PostgreSQL migration touches this module plus
``schema.sql``, nothing else — no other module executes a PRAGMA statement or
otherwise depends on SQLite-only behavior.

CP2 scope only: no ingestion, no run ledger (``import_runs``/``run_rows``),
no parsing/normalization/review logic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Union

__all__ = ["SCHEMA_SQL_PATH", "connect", "initialize_schema", "open_database"]

SCHEMA_SQL_PATH = Path(__file__).with_name("schema.sql")

DbPath = Union[str, Path]


def connect(db_path: DbPath) -> sqlite3.Connection:
    """Open a SQLite connection with foreign-key enforcement turned on.

    ``db_path`` may be a filesystem path or the special string ``":memory:"``.
    """
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Apply the portable DDL in ``schema.sql`` to an open connection.

    Uses ``CREATE TABLE IF NOT EXISTS`` throughout, so this is safe to call
    against a database that has already been initialized.
    """
    schema_sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    connection.executescript(schema_sql)
    connection.commit()


def open_database(db_path: DbPath) -> sqlite3.Connection:
    """Open (creating if needed) a working database with the schema applied."""
    connection = connect(db_path)
    initialize_schema(connection)
    return connection
