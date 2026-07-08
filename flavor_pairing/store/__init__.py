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
from flavor_pairing.store.ledger import (
    DEFAULT_LEDGER_ROOT,
    IMPORT_RUNS_LEDGER_COLUMNS,
    RUN_ROWS_LEDGER_COLUMNS,
    append_import_run,
    append_run_rows,
    ledger_paths,
    load_ledger_into_db,
    read_import_runs,
    read_run_rows,
)

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
    "DEFAULT_LEDGER_ROOT",
    "IMPORT_RUNS_LEDGER_COLUMNS",
    "RUN_ROWS_LEDGER_COLUMNS",
    "append_import_run",
    "append_run_rows",
    "ledger_paths",
    "load_ledger_into_db",
    "read_import_runs",
    "read_run_rows",
]
