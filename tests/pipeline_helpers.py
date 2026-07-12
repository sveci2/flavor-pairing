"""Shared non-test helpers for the CP5 test files (not collected by pytest).

Everything here uses neutral generated names (fmt_alpha/src_alpha per
conftest's DEFAULT_CONFIG, or caller-supplied IDs) — no sample source IDs,
no fixed row counts. Ledger roots are always caller-supplied tmp paths;
nothing touches data/ledger/ or data/imports_private/.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from flavor_pairing.config.loaders import load_config
from flavor_pairing.ingest.identity import RawRowContent
from flavor_pairing.ingest.runs import record_completed_run
from flavor_pairing.normalize.affinities import AffinityOutcome, normalize_affinities
from flavor_pairing.normalize.entities import source_name_id_for
from flavor_pairing.normalize.pipeline import NormalizeOutcome, normalize_source
from flavor_pairing.parse.row_parser import parse_source

# fmt_alpha strength rows covering every CP4 resolution path.
STRENGTH_ROWS = [
    {"input_source_format": "fmt_alpha", "marker_key": "plain",
     "source_value_or_marker": "ordinary text", "mapping_confidence": "low"},
    {"input_source_format": "fmt_alpha", "marker_key": "uppercase",
     "source_value_or_marker": "uppercase text", "normalized_label": "very_high",
     "normalized_score": "3", "mapping_confidence": "medium"},
    {"input_source_format": "fmt_alpha", "marker_key": "asterisk_uppercase",
     "source_value_or_marker": "asterisk + uppercase", "normalized_label": "holy_grail",
     "normalized_score": "4", "mapping_confidence": "medium"},
    {"input_source_format": "fmt_alpha", "marker_key": "explicit_label:heaven",
     "source_value_or_marker": "heaven", "normalized_label": "holy_grail",
     "normalized_score": "4", "mapping_confidence": "high"},
]

LABEL_ROWS = [
    {"source_format": "fmt_alpha", "source_label": "Season", "attribute_name": "season"},
    {"source_format": "fmt_alpha", "source_label": "Taste", "attribute_name": "taste"},
    {"source_format": "fmt_alpha", "source_label": "Techniques", "attribute_name": "techniques"},
]


def full_config(build_config, **overrides):
    """A loaded config with parser-complete fmt_alpha rules, plus overrides.

    Override keys are config file names (e.g. ``"sources.csv"``), matching
    conftest's ``build_config`` contract.
    """
    merged = {
        "strength_mappings.csv": STRENGTH_ROWS,
        "attribute_labels.csv": LABEL_ROWS,
    }
    merged.update(overrides)
    return load_config(build_config(overrides=merged))


def make_clock(start: Optional[datetime] = None, step: timedelta = timedelta(seconds=1)):
    """A deterministic, monotonically increasing UTC clock (never real time)."""
    state = {"t": start or datetime(2026, 1, 1, tzinfo=timezone.utc)}

    def _clock():
        current = state["t"]
        state["t"] = current + step
        return current

    return _clock


def seed_source(connection, source_id: str, source_format: str = "fmt_alpha") -> None:
    connection.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES (?, 'Test source', ?, 'project_owned_demo')",
        (source_id, source_format),
    )
    connection.commit()


def seed_entity(
    connection,
    entity_id: str,
    canonical_name: str,
    *,
    entity_type: Optional[str] = "unknown",
    display_name: Optional[str] = None,
    parent_entity_id: Optional[str] = None,
    normalization_status: Optional[str] = None,
    review_status: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """Directly seed an entity row in an arbitrary state (test setup only)."""
    connection.execute(
        "INSERT INTO entities (entity_id, canonical_name, display_name, entity_type, "
        "parent_entity_id, normalization_status, review_status, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (entity_id, canonical_name, display_name, entity_type, parent_entity_id,
         normalization_status, review_status, notes),
    )
    connection.commit()


def seed_mapping(
    connection,
    source_id: str,
    source_text: str,
    source_role: str,
    *,
    entity_id: Optional[str] = None,
    normalization_status: Optional[str] = None,
    notes: Optional[str] = None,
) -> str:
    """Directly seed an entity_source_names row (test setup only)."""
    source_name_id = source_name_id_for(source_id, source_text, source_role)
    connection.execute(
        "INSERT INTO entity_source_names (source_name_id, source_id, source_text, "
        "source_role, entity_id, normalization_status, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_name_id, source_id, source_text, source_role, entity_id,
         normalization_status, notes),
    )
    connection.commit()
    return source_name_id


def ingest_rows(
    connection,
    tmp_path: Path,
    source_id: str,
    rows: Sequence[Tuple],
    clock,
):
    """Record one completed run from (subject, entry[, quality]) tuples."""
    content = [
        RawRowContent(
            subject_raw=row[0],
            entry_raw=row[1],
            quality_raw=(row[2] if len(row) > 2 else None),
        )
        for row in rows
    ]
    return record_completed_run(
        connection, source_id, content, clock=clock, ledger_root=tmp_path / "ledger"
    )


def run_full(
    connection,
    config,
    source_id: str,
    rows: Sequence[Tuple],
    tmp_path: Path,
    clock,
) -> NormalizeOutcome:
    """Ingest -> parse -> normalize one version of one source."""
    ingest_rows(connection, tmp_path, source_id, rows, clock)
    parse_source(connection, config, source_id)
    return normalize_source(connection, config, source_id)


def table_snapshot(connection, table: str) -> List[Tuple]:
    """Full table contents as a sorted list of tuples (order-independent)."""
    rows = connection.execute(f"SELECT * FROM {table}").fetchall()
    return sorted(tuple(row) for row in rows)


def mapping_row(connection, source_id: str, source_text: str, source_role: str):
    """One entity_source_names row (or None) for an exact key."""
    return connection.execute(
        "SELECT * FROM entity_source_names WHERE source_id = ? AND source_text = ? "
        "AND source_role = ?",
        (source_id, source_text, source_role),
    ).fetchone()


def observation_rows(connection, source_id: str) -> List:
    return connection.execute(
        "SELECT * FROM pairing_observations WHERE source_id = ? ORDER BY observation_id",
        (source_id,),
    ).fetchall()


def attribute_rows(connection, source_id: str) -> List:
    return connection.execute(
        "SELECT * FROM entity_attributes WHERE source_id = ? ORDER BY attribute_id",
        (source_id,),
    ).fetchall()


def run_full_with_affinities(
    connection,
    config,
    source_id: str,
    rows: Sequence[Tuple],
    tmp_path: Path,
    clock,
) -> AffinityOutcome:
    """Ingest -> parse -> normalize -> normalize_affinities for one version."""
    ingest_rows(connection, tmp_path, source_id, rows, clock)
    parse_source(connection, config, source_id)
    normalize_source(connection, config, source_id)
    return normalize_affinities(connection, config, source_id)


def group_rows(connection, source_id: str) -> List:
    return connection.execute(
        "SELECT * FROM affinity_groups WHERE source_id = ? ORDER BY affinity_id",
        (source_id,),
    ).fetchall()


def member_rows(connection, affinity_id: str) -> List:
    return connection.execute(
        "SELECT * FROM affinity_members WHERE affinity_id = ? ORDER BY member_order",
        (affinity_id,),
    ).fetchall()
