"""CP2 tests: CSV <-> SQLite conventions (docs/DATA_FOUNDATION_PLAN.md §14).

Two distinct guarantees, tested separately per the approved design:

- Semantic round trip: CSV -> SQLite -> CSV preserves all values, NULLs,
  columns, and rows. Byte-identical output is *not* required here, since the
  input fixtures are not themselves canonical (one file uses a reordered
  header on purpose, to prove import does not depend on column order).
- Canonical byte stability: export -> canonical CSV -> re-import that
  canonical CSV -> export again produces byte-identical output. This is
  where BOM, template column order, deterministic row order, and LF
  newlines are actually enforced.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from flavor_pairing.store import TABLES, csv_io, db

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "store"


def _read_raw_rows(path: Path):
    """Read a CSV as plain dicts, BOM-safe, without assuming column order."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _normalize(rows, columns):
    """Rows as order-independent (column -> value) sets, "" treated as None."""
    normalized = []
    for row in rows:
        normalized.append(tuple((column, row.get(column) or None) for column in columns))
    return sorted(normalized)


@pytest.fixture
def conn():
    connection = db.open_database(":memory:")
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# Semantic round trip
# ---------------------------------------------------------------------------

def test_semantic_roundtrip_preserves_all_tables(conn, tmp_path):
    csv_io.import_all(conn, FIXTURES_DIR)
    export_dir = tmp_path / "export"
    csv_io.export_all(conn, export_dir)

    for name, spec in TABLES.items():
        original_rows = _read_raw_rows(FIXTURES_DIR / spec.template_filename)
        exported_rows = _read_raw_rows(export_dir / spec.template_filename)
        assert _normalize(exported_rows, spec.columns) == _normalize(original_rows, spec.columns), (
            f"{name}: exported content does not match source fixture"
        )


def test_semantic_roundtrip_preserves_row_count(conn, tmp_path):
    counts_in = csv_io.import_all(conn, FIXTURES_DIR)
    export_dir = tmp_path / "export"
    counts_out = csv_io.export_all(conn, export_dir)
    assert counts_in == counts_out


def test_semantic_roundtrip_preserves_nulls(conn, tmp_path):
    csv_io.import_all(conn, FIXTURES_DIR)
    row = conn.execute(
        "SELECT entity_id FROM entity_source_names WHERE source_name_id = 'sn_test_0003'"
    ).fetchone()
    assert row["entity_id"] is None  # blank in the fixture -> SQL NULL

    export_dir = tmp_path / "export"
    csv_io.export_all(conn, export_dir)
    exported = _read_raw_rows(export_dir / "entity_source_names.csv")
    exported_row = next(r for r in exported if r["source_name_id"] == "sn_test_0003")
    assert exported_row["entity_id"] == ""  # SQL NULL -> "" on export


def test_semantic_roundtrip_preserves_embedded_special_characters(conn, tmp_path):
    csv_io.import_all(conn, FIXTURES_DIR)
    export_dir = tmp_path / "export"
    csv_io.export_all(conn, export_dir)
    exported = _read_raw_rows(export_dir / "sources.csv")
    beta = next(r for r in exported if r["source_id"] == "src_test_beta")
    assert beta["notes"] == "Multi-line notes.\nSecond line."
    alpha = next(r for r in exported if r["source_id"] == "src_test_alpha")
    assert alpha["source_name"] == "Test Alpha, Source"


def test_import_is_order_independent_of_source_column_order(conn):
    # entities.csv fixture is deliberately written with a reordered header.
    with (FIXTURES_DIR / "entities.csv").open(newline="", encoding="utf-8-sig") as handle:
        header = next(csv.reader(handle))
    assert header != list(TABLES["entities"].columns), (
        "fixture must use a reordered header to exercise this guarantee"
    )
    csv_io.import_table(conn, TABLES["entities"], FIXTURES_DIR / "entities.csv")
    row = conn.execute("SELECT canonical_name, entity_type FROM entities WHERE entity_id = 'ent_test_0001'").fetchone()
    assert row["canonical_name"] == "apple"
    assert row["entity_type"] == "ingredient"


# ---------------------------------------------------------------------------
# Canonical byte stability
# ---------------------------------------------------------------------------

def test_canonical_export_is_a_fixed_point(conn, tmp_path):
    csv_io.import_all(conn, FIXTURES_DIR)
    first_export = tmp_path / "export_1"
    csv_io.export_all(conn, first_export)

    reimported = db.open_database(":memory:")
    csv_io.import_all(reimported, first_export)
    second_export = tmp_path / "export_2"
    csv_io.export_all(reimported, second_export)
    reimported.close()

    for spec in TABLES.values():
        first_bytes = (first_export / spec.template_filename).read_bytes()
        second_bytes = (second_export / spec.template_filename).read_bytes()
        assert first_bytes == second_bytes, f"{spec.name}: canonical export is not a fixed point"


def test_canonical_export_has_utf8_bom(conn, tmp_path):
    csv_io.import_all(conn, FIXTURES_DIR)
    export_dir = tmp_path / "export"
    csv_io.export_all(conn, export_dir)
    for spec in TABLES.values():
        raw_bytes = (export_dir / spec.template_filename).read_bytes()
        assert raw_bytes.startswith(b"\xef\xbb\xbf"), f"{spec.name}: missing UTF-8 BOM"


def test_canonical_export_uses_lf_newlines_only(conn, tmp_path):
    csv_io.import_all(conn, FIXTURES_DIR)
    export_dir = tmp_path / "export"
    csv_io.export_all(conn, export_dir)
    for spec in TABLES.values():
        raw_bytes = (export_dir / spec.template_filename).read_bytes()
        assert b"\r\n" not in raw_bytes, f"{spec.name}: contains CRLF"
        # sources.csv has an embedded '\n' inside a quoted field: that's a
        # legitimate LF inside data, not a line terminator.


def test_canonical_export_column_order_matches_template(conn, tmp_path):
    templates_dir = Path(__file__).resolve().parents[1] / "data" / "templates"
    csv_io.import_all(conn, FIXTURES_DIR)
    export_dir = tmp_path / "export"
    csv_io.export_all(conn, export_dir)
    for spec in TABLES.values():
        with (templates_dir / spec.template_filename).open(newline="", encoding="utf-8-sig") as handle:
            template_header = next(csv.reader(handle))
        with (export_dir / spec.template_filename).open(newline="", encoding="utf-8-sig") as handle:
            exported_header = next(csv.reader(handle))
        assert exported_header == template_header, f"{spec.name}: column order does not match template"


def test_canonical_export_row_order_is_deterministic(conn, tmp_path):
    csv_io.import_all(conn, FIXTURES_DIR)
    export_dir_1 = tmp_path / "export_1"
    export_dir_2 = tmp_path / "export_2"
    csv_io.export_all(conn, export_dir_1)
    csv_io.export_all(conn, export_dir_2)
    for spec in TABLES.values():
        rows_1 = _read_raw_rows(export_dir_1 / spec.template_filename)
        rows_2 = _read_raw_rows(export_dir_2 / spec.template_filename)
        assert rows_1 == rows_2, f"{spec.name}: row order not stable across repeated exports"

        sort_values = [tuple(row[key] for key in spec.sort_key) for row in rows_1]
        assert sort_values == sorted(sort_values), f"{spec.name}: rows are not sorted by sort_key"


def test_canonical_export_sort_key_independent_of_insertion_order(tmp_path):
    conn_forward = db.open_database(":memory:")
    conn_reverse = db.open_database(":memory:")
    conn_forward.execute("INSERT INTO entities (entity_id) VALUES ('ent_a')")
    conn_forward.execute("INSERT INTO entities (entity_id) VALUES ('ent_b')")
    conn_reverse.execute("INSERT INTO entities (entity_id) VALUES ('ent_b')")
    conn_reverse.execute("INSERT INTO entities (entity_id) VALUES ('ent_a')")

    out_forward = tmp_path / "forward"
    out_reverse = tmp_path / "reverse"
    csv_io.export_table(conn_forward, TABLES["entities"], out_forward / "entities.csv")
    csv_io.export_table(conn_reverse, TABLES["entities"], out_reverse / "entities.csv")
    conn_forward.close()
    conn_reverse.close()

    assert (out_forward / "entities.csv").read_bytes() == (out_reverse / "entities.csv").read_bytes()
