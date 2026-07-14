"""Shared fixtures for the flavour-pairing test suite.

Failure-mode tests never touch checked-in data: ``build_config`` writes a
small, generic, self-consistent configuration set into ``tmp_path`` and lets
each test override individual files. Names used in the default set are
deliberately neutral (``fmt_alpha``, ``src_alpha``, ``col_subject`` …) so the
suite depends on no real source, no sample source ID, and no fixed row count.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "data" / "sample"
RUNTIME_DIR = REPO_ROOT / "flavor_pairing"

CONFIG_COLUMNS = {
    "sources.csv": [
        "source_id", "source_name", "source_format", "source_uri",
        "rights_status", "allowed_use", "notes",
    ],
    "import_mappings.csv": [
        "source_format", "input_column", "target_file", "target_field",
        "transform_rule", "required",
    ],
    "strength_mappings.csv": [
        "input_source_format", "marker_key", "source_value_or_marker",
        "normalized_label", "normalized_score", "mapping_confidence", "notes",
    ],
    "attribute_labels.csv": [
        "source_format", "source_label", "attribute_name", "notes",
    ],
    "affinity_split_rules.csv": [
        "source_format", "affinity_header_phrase", "member_delimiter",
        "review_status", "notes",
    ],
}

DEFAULT_CONFIG = {
    "sources.csv": [
        {
            "source_id": "src_alpha",
            "source_name": "Alpha demo source",
            "source_format": "fmt_alpha",
            "rights_status": "project_owned_demo",
            "allowed_use": "software_testing",
        },
    ],
    "import_mappings.csv": [
        {
            "source_format": "fmt_alpha",
            "input_column": "col_subject",
            "target_file": "raw_source_rows.csv",
            "target_field": "subject_raw",
            "transform_rule": "copy exactly",
            "required": "1",
        },
        {
            "source_format": "fmt_alpha",
            "input_column": "col_entry",
            "target_file": "raw_source_rows.csv",
            "target_field": "entry_raw",
            "transform_rule": "copy exactly",
            "required": "1",
        },
        {
            "source_format": "fmt_alpha",
            "input_column": "(not present)",
            "target_file": "raw_source_rows.csv",
            "target_field": "quality_raw",
            "transform_rule": "leave blank",
            "required": "0",
        },
    ],
    "strength_mappings.csv": [
        {
            "input_source_format": "fmt_alpha",
            "marker_key": "plain",
            "source_value_or_marker": "ordinary text",
            "mapping_confidence": "low",
        },
    ],
    "attribute_labels.csv": [
        {
            "source_format": "fmt_alpha",
            "source_label": "Note",
            "attribute_name": "note",
        },
    ],
    "affinity_split_rules.csv": [
        {
            "source_format": "fmt_alpha",
            "affinity_header_phrase": "Combinations",
            "member_delimiter": " + ",
            "review_status": "approved",
        },
    ],
}


def write_config_files(directory, rows_by_file, columns_by_file=None, encoding="utf-8-sig"):
    """Write configuration CSVs into ``directory`` and return it."""
    columns_by_file = columns_by_file or {}
    for name, rows in rows_by_file.items():
        columns = columns_by_file.get(name, CONFIG_COLUMNS[name])
        with (Path(directory) / name).open("w", newline="", encoding=encoding) as handle:
            writer = csv.DictWriter(
                handle, fieldnames=columns, restval="", extrasaction="ignore"
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
    return Path(directory)


@pytest.fixture
def sample_config_dir():
    """The checked-in sample configuration directory."""
    return SAMPLE_DIR


@pytest.fixture
def build_config(tmp_path):
    """Factory writing a valid default config set, with per-file overrides.

    ``overrides`` replaces a file's rows wholesale; ``columns`` replaces a
    file's header (for missing-column tests); ``encoding`` controls BOM
    presence (``utf-8-sig`` vs ``utf-8``).
    """

    def _build(overrides=None, columns=None, encoding="utf-8-sig"):
        rows_by_file = {
            name: [dict(row) for row in rows] for name, rows in DEFAULT_CONFIG.items()
        }
        for name, rows in (overrides or {}).items():
            rows_by_file[name] = [dict(row) for row in rows]
        return write_config_files(
            tmp_path, rows_by_file, columns_by_file=columns, encoding=encoding
        )

    return _build
