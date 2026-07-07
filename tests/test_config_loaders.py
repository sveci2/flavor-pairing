"""CP1 tests: configuration loaders and validation.

Covers the checked-in sample configuration, every required failure mode via
temporary synthetic configs, BOM handling, row-count independence, and the
meta-rules for runtime code (no hard-coded sample source IDs, no network
modules, no reference to data/imports_private/).
"""

from __future__ import annotations

import ast
import csv

import pytest

from flavor_pairing.config import ConfigError, load_config
from conftest import DEFAULT_CONFIG, RUNTIME_DIR, SAMPLE_DIR


# ---------------------------------------------------------------------------
# Valid configuration
# ---------------------------------------------------------------------------

def test_valid_sample_config_loads(sample_config_dir):
    config = load_config(sample_config_dir)
    assert config.sources, "sample sources.csv should register at least one source"
    # Every registered source's format is fully mapped.
    for source in config.sources.values():
        mapping = config.mapping_for(source.source_format)
        assert mapping["subject_raw"].input_column
        assert mapping["entry_raw"].input_column
        assert "quality_raw" in mapping
    # Every approved affinity rule is retrievable through the strict accessor.
    for source_format, rule in config.affinity_split_rules.items():
        if rule.review_status == "approved":
            assert config.require_affinity_rule(source_format) is rule
    # Attribute labels round-trip through the case-insensitive accessor.
    for source_format, labels in config.attribute_labels.items():
        for key, label in labels.items():
            assert key == label.source_label.lower()
            assert config.attribute_labels_for(source_format)[key] is label


def test_valid_default_fixture_config_loads(build_config):
    config = load_config(build_config())
    source = next(iter(config.sources.values()))
    assert config.mapping_for(source.source_format)["subject_raw"].required
    assert config.require_affinity_rule(source.source_format).member_delimiter == " + "


def test_strength_mapping_without_score_has_no_label(sample_config_dir):
    config = load_config(sample_config_dir)
    seen_scoreless = False
    for per_format in config.strength_mappings.values():
        for mapping in per_format.values():
            if mapping.normalized_score is None:
                assert mapping.normalized_label is None
                seen_scoreless = True
    assert seen_scoreless, "sample should include a typography-lossy (scoreless) marker"


# ---------------------------------------------------------------------------
# Structural failure modes
# ---------------------------------------------------------------------------

def test_missing_required_column(build_config):
    columns = {"sources.csv": [
        c for c in ["source_id", "source_name", "source_format", "source_uri",
                    "allowed_use", "notes"]  # rights_status removed
    ]}
    config_dir = build_config(columns=columns)
    with pytest.raises(ConfigError, match=r"sources\.csv.*rights_status"):
        load_config(config_dir)


def test_duplicate_source_ids(build_config):
    row = dict(DEFAULT_CONFIG["sources.csv"][0])
    config_dir = build_config(overrides={"sources.csv": [row, dict(row)]})
    with pytest.raises(ConfigError, match=r"duplicate source_id 'src_alpha'"):
        load_config(config_dir)


def test_duplicate_import_mapping_key(build_config):
    rows = [dict(r) for r in DEFAULT_CONFIG["import_mappings.csv"]]
    rows.append(dict(rows[0]))  # second subject_raw mapping for fmt_alpha
    config_dir = build_config(overrides={"import_mappings.csv": rows})
    with pytest.raises(ConfigError, match=r"duplicate mapping.*subject_raw"):
        load_config(config_dir)


def test_unknown_source_format(build_config):
    row = dict(DEFAULT_CONFIG["sources.csv"][0])
    row["source_id"] = "src_beta"
    row["source_format"] = "fmt_unregistered"
    config_dir = build_config(
        overrides={"sources.csv": [DEFAULT_CONFIG["sources.csv"][0], row]}
    )
    with pytest.raises(ConfigError, match=r"src_beta.*fmt_unregistered"):
        load_config(config_dir)


def test_incomplete_flat_tabular_mapping(build_config):
    rows = [dict(DEFAULT_CONFIG["import_mappings.csv"][0])]  # subject_raw only
    config_dir = build_config(overrides={"import_mappings.csv": rows})
    with pytest.raises(ConfigError, match=r"incomplete.*fmt_alpha.*entry_raw"):
        load_config(config_dir)


def test_required_target_without_input_column(build_config):
    rows = [dict(r) for r in DEFAULT_CONFIG["import_mappings.csv"]]
    rows[0]["input_column"] = "(not present)"  # subject_raw still required
    config_dir = build_config(overrides={"import_mappings.csv": rows})
    with pytest.raises(ConfigError, match=r"subject_raw.*required.*no input column"):
        load_config(config_dir)


# ---------------------------------------------------------------------------
# Strength-mapping failure modes
# ---------------------------------------------------------------------------

def test_invalid_marker_key(build_config):
    rows = [dict(DEFAULT_CONFIG["strength_mappings.csv"][0])]
    rows[0]["marker_key"] = "bold"
    config_dir = build_config(overrides={"strength_mappings.csv": rows})
    with pytest.raises(ConfigError, match=r"invalid marker_key 'bold'"):
        load_config(config_dir)


def test_explicit_label_marker_requires_value(build_config):
    rows = [dict(DEFAULT_CONFIG["strength_mappings.csv"][0])]
    rows[0]["marker_key"] = "explicit_label:"
    config_dir = build_config(overrides={"strength_mappings.csv": rows})
    with pytest.raises(ConfigError, match=r"invalid marker_key 'explicit_label:'"):
        load_config(config_dir)


def test_duplicate_marker_key_for_same_format(build_config):
    row = dict(DEFAULT_CONFIG["strength_mappings.csv"][0])
    config_dir = build_config(overrides={"strength_mappings.csv": [row, dict(row)]})
    with pytest.raises(ConfigError, match=r"duplicate marker_key 'plain'.*fmt_alpha"):
        load_config(config_dir)


def test_label_and_score_must_agree(build_config):
    rows = [dict(DEFAULT_CONFIG["strength_mappings.csv"][0])]
    rows[0]["normalized_label"] = "very_high"  # label without score
    config_dir = build_config(overrides={"strength_mappings.csv": rows})
    with pytest.raises(ConfigError, match=r"both be present or both be blank"):
        load_config(config_dir)


# ---------------------------------------------------------------------------
# Parser-rule capability accessors
# ---------------------------------------------------------------------------

def test_missing_parser_rule_configuration(build_config):
    config_dir = build_config(
        overrides={"attribute_labels.csv": [], "affinity_split_rules.csv": []}
    )
    config = load_config(config_dir)  # loading alone must not fail
    assert config.attribute_labels_for("fmt_alpha") == {}
    with pytest.raises(ConfigError, match=r"no attribute labels.*fmt_alpha"):
        config.require_attribute_labels("fmt_alpha")
    with pytest.raises(ConfigError, match=r"no affinity split rule.*fmt_alpha"):
        config.require_affinity_rule("fmt_alpha")


def test_unapproved_affinity_rule(build_config):
    rows = [dict(DEFAULT_CONFIG["affinity_split_rules.csv"][0])]
    rows[0]["review_status"] = "needs_review"
    config_dir = build_config(overrides={"affinity_split_rules.csv": rows})
    config = load_config(config_dir)  # a pending rule may load...
    assert config.affinity_rule_for("fmt_alpha").review_status == "needs_review"
    with pytest.raises(ConfigError, match=r"needs_review.*only approved"):
        config.require_affinity_rule("fmt_alpha")  # ...but never be used


def test_invalid_affinity_review_status(build_config):
    rows = [dict(DEFAULT_CONFIG["affinity_split_rules.csv"][0])]
    rows[0]["review_status"] = "maybe"
    config_dir = build_config(overrides={"affinity_split_rules.csv": rows})
    with pytest.raises(ConfigError, match=r"invalid review_status 'maybe'"):
        load_config(config_dir)


# ---------------------------------------------------------------------------
# Encoding and scale independence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-8"])
def test_bom_safe_loading(build_config, encoding):
    config = load_config(build_config(encoding=encoding))
    # Field names must be clean regardless of BOM presence.
    assert "src_alpha" in config.sources
    assert config.sources["src_alpha"].source_format == "fmt_alpha"


def test_no_dependence_on_row_counts(build_config):
    """Loaders must scale to arbitrary numbers of sources, formats, and rows."""
    formats = [f"fmt_{i:03d}" for i in range(7)]
    sources, mappings, strengths, labels, rules = [], [], [], [], []
    for i, fmt in enumerate(formats):
        for j in range(4):  # several sources per format
            sources.append({
                "source_id": f"src_{i:03d}_{j}",
                "source_name": f"Generated source {i}-{j}",
                "source_format": fmt,
                "rights_status": "project_owned_demo",
                "allowed_use": "software_testing",
            })
        mappings.extend([
            {"source_format": fmt, "input_column": "a", "target_file": "raw_source_rows.csv",
             "target_field": "subject_raw", "transform_rule": "copy exactly", "required": "1"},
            {"source_format": fmt, "input_column": "b", "target_file": "raw_source_rows.csv",
             "target_field": "entry_raw", "transform_rule": "copy exactly", "required": "1"},
            {"source_format": fmt, "input_column": "(not present)", "target_file": "raw_source_rows.csv",
             "target_field": "quality_raw", "transform_rule": "leave blank", "required": "0"},
        ])
        strengths.append({
            "input_source_format": fmt, "marker_key": "plain",
            "source_value_or_marker": "ordinary text", "mapping_confidence": "low",
        })
        labels.append({"source_format": fmt, "source_label": "Note", "attribute_name": "note"})
        rules.append({
            "source_format": fmt, "affinity_header_phrase": "Combinations",
            "member_delimiter": " + ", "review_status": "approved",
        })
    config_dir = build_config(overrides={
        "sources.csv": sources,
        "import_mappings.csv": mappings,
        "strength_mappings.csv": strengths,
        "attribute_labels.csv": labels,
        "affinity_split_rules.csv": rules,
    })
    config = load_config(config_dir)
    assert len(config.sources) == len(sources)
    for fmt in formats:
        assert config.mapping_for(fmt)["subject_raw"].input_column == "a"
        assert config.require_affinity_rule(fmt).review_status == "approved"


# ---------------------------------------------------------------------------
# Meta-rules for runtime code
# ---------------------------------------------------------------------------

FORBIDDEN_NETWORK_MODULES = {
    "socket", "ssl", "http", "urllib", "requests", "ftplib", "smtplib",
    "poplib", "imaplib", "telnetlib", "xmlrpc", "asyncio",
}


def _runtime_files():
    files = sorted(RUNTIME_DIR.rglob("*.py"))
    assert files, "runtime package should contain Python files"
    return files


def test_runtime_has_no_hardcoded_sample_source_ids():
    with (SAMPLE_DIR / "sources.csv").open(newline="", encoding="utf-8-sig") as handle:
        sample_ids = [row["source_id"] for row in csv.DictReader(handle)]
    assert sample_ids, "sample sources.csv should register at least one source"
    for path in _runtime_files():
        text = path.read_text(encoding="utf-8")
        for source_id in sample_ids:
            assert source_id not in text, (
                f"{path} hard-codes sample source ID '{source_id}'"
            )


def test_runtime_imports_no_network_modules():
    for path in _runtime_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                assert root not in FORBIDDEN_NETWORK_MODULES, (
                    f"{path} imports network-capable module '{name}'"
                )


def test_runtime_never_references_imports_private():
    for path in _runtime_files():
        assert "imports_private" not in path.read_text(encoding="utf-8"), (
            f"{path} references data/imports_private/"
        )
