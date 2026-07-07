"""Storage layer: portable SQL schema plus SQLite <-> CSV support.

Public API re-exported from :mod:`flavor_pairing.store.db` and
:mod:`flavor_pairing.store.csv_io`. CP2 scope only — see
``docs/DATA_FOUNDATION_PLAN.md`` §14 and ``docs/DECISIONS.md`` §G.
"""

from flavor_pairing.store.csv_io import (
    TABLE_IMPORT_ORDER,
    TABLES,
    TableSpec,
    export_all,
    export_table,
    import_all,
    import_table,
)
from flavor_pairing.store.db import SCHEMA_SQL_PATH, connect, initialize_schema, open_database

__all__ = [
    "SCHEMA_SQL_PATH",
    "TABLE_IMPORT_ORDER",
    "TABLES",
    "TableSpec",
    "connect",
    "export_all",
    "export_table",
    "import_all",
    "import_table",
    "initialize_schema",
    "open_database",
]
