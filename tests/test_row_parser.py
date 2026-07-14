"""CP4 tests: stateful row classification and current-version parsing
(docs/DATA_FOUNDATION_PLAN.md §5, §16 test_row_parser.py).

Two levels:

- Pure classification via ``classify_rows`` — classifier chain, subject-block
  state transitions, marker/strength integration — with synthetic configs
  from conftest's ``build_config`` (neutral fmt_alpha/src_alpha names; no
  sample source IDs, no fixed row counts).
- Orchestration via ``parse_source`` against an in-memory SQLite database,
  with versions created through ``record_completed_run`` and ledgers always
  routed to tmp_path (never the real data/ledger/). Never touches
  data/imports_private/.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG
from flavor_pairing.config.loaders import ConfigError, load_config
from flavor_pairing.ingest.identity import RawRowContent
from flavor_pairing.ingest.runs import record_completed_run
from flavor_pairing.parse.row_parser import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    PARSER_CONFIDENCES,
    ROW_TYPES,
    ROW_TYPE_AFFINITY_GROUP,
    ROW_TYPE_AFFINITY_HEADER,
    ROW_TYPE_ATTRIBUTE,
    ROW_TYPE_NOTE,
    ROW_TYPE_PAIRING_CANDIDATE,
    ROW_TYPE_UNCLASSIFIED,
    ParseError,
    ParseInputRow,
    classify_rows,
    parse_source,
)
from flavor_pairing.parse.strength import (
    STRENGTH_METHOD_EXPLICIT,
    STRENGTH_METHOD_TYPOGRAPHIC,
    STRENGTH_METHOD_UNAVAILABLE,
)
from flavor_pairing.store import db

RUNTIME_PARSE_DIR = Path(__file__).resolve().parents[1] / "flavor_pairing" / "parse"

# fmt_alpha strength rows covering every resolution path (format-keyed; the
# same rows serve every source registered with this format).
ALPHA_STRENGTH = [
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
    {"input_source_format": "fmt_alpha", "marker_key": "explicit_label:rec",
     "source_value_or_marker": "rec", "normalized_label": "recommended",
     "normalized_score": "1", "mapping_confidence": "high"},
]

ALPHA_LABELS = [
    {"source_format": "fmt_alpha", "source_label": "Season", "attribute_name": "season"},
    {"source_format": "fmt_alpha", "source_label": "Taste", "attribute_name": "taste"},
]

# conftest's default affinity rule for fmt_alpha:
# header "Combinations", delimiter " + ", review_status approved.


def load_alpha(build_config, **overrides):
    merged = {
        "strength_mappings.csv": ALPHA_STRENGTH,
        "attribute_labels.csv": ALPHA_LABELS,
    }
    merged.update(overrides)
    return load_config(build_config(overrides=merged))


def classify(config, rows, source_id="src_alpha", source_format="fmt_alpha"):
    return classify_rows(
        source_id,
        rows,
        attribute_labels=config.attribute_labels_for(source_format),
        affinity_rule=config.affinity_rule_for(source_format),
        strength_mappings=config.strength_mappings_for(source_format),
    )


def rows_for(items):
    """Build ParseInputRows from (subject, entry[, quality]) tuples."""
    built = []
    for index, item in enumerate(items, start=1):
        subject, entry = item[0], item[1]
        quality = item[2] if len(item) > 2 else None
        built.append(ParseInputRow(f"rec_{index:03d}", subject, entry, quality))
    return built


def make_clock(start=None, step=timedelta(seconds=1)):
    """A deterministic, monotonically increasing UTC clock (never real time)."""
    state = {"t": start or datetime(2026, 1, 1, tzinfo=timezone.utc)}

    def _clock():
        current = state["t"]
        state["t"] = current + step
        return current

    return _clock


@pytest.fixture
def conn():
    connection = db.open_database(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def clock():
    return make_clock()


def seed_source(connection, source_id, source_format="fmt_alpha"):
    connection.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES (?, 'Parser test source', ?, 'project_owned_demo')",
        (source_id, source_format),
    )
    connection.commit()


def ingest(connection, tmp_path, rows, source_id="src_alpha", clock=None):
    content = [
        RawRowContent(subject_raw=s, entry_raw=e, quality_raw=(q[0] if q else None))
        for s, e, *q in rows
    ]
    return record_completed_run(
        connection, source_id, content, clock=clock, ledger_root=tmp_path / "ledger"
    )


def parsed_by_entry(connection, source_id="src_alpha"):
    rows = connection.execute(
        "SELECT p.*, r.subject_raw, r.entry_raw FROM parsed_source_rows p "
        "JOIN raw_source_rows r ON p.source_id = r.source_id "
        "AND p.source_record_id = r.source_record_id WHERE p.source_id = ?",
        (source_id,),
    ).fetchall()
    return {(row["subject_raw"], row["entry_raw"]): row for row in rows}


# ---------------------------------------------------------------------------
# Attribute classification
# ---------------------------------------------------------------------------

def test_registered_attribute_label_classifies_as_attribute(build_config):
    config = load_alpha(build_config)
    parsed = classify(config, rows_for([
        ("APPLE", "Season: autumn"),
        ("APPLE", "Taste: sweet, tart"),
    ]))
    season, taste = parsed
    assert season.row_type == ROW_TYPE_ATTRIBUTE
    assert season.attribute_name == "season"
    assert season.attribute_value_raw == "autumn"
    assert season.entry_clean == "Season: autumn"  # case preserved, stripped
    assert season.parser_confidence == CONFIDENCE_HIGH
    assert season.requires_review == 0
    assert season.strength_score is None
    assert season.strength_method == STRENGTH_METHOD_UNAVAILABLE
    # Attribute values are never split on commas.
    assert taste.attribute_name == "taste"
    assert taste.attribute_value_raw == "sweet, tart"


def test_attribute_label_matched_case_insensitively(build_config):
    config = load_alpha(build_config)
    (parsed,) = classify(config, rows_for([("APPLE", "sEaSoN: winter")]))
    assert parsed.row_type == ROW_TYPE_ATTRIBUTE
    assert parsed.attribute_name == "season"
    assert parsed.attribute_value_raw == "winter"


@pytest.mark.parametrize("entry", ["Harvest: late", "chocolate: dark", "Foo: a + b"])
def test_unregistered_label_shape_is_unclassified_not_guessed(build_config, entry):
    config = load_alpha(build_config)
    (parsed,) = classify(config, rows_for([("APPLE", entry)]))
    assert parsed.row_type == ROW_TYPE_UNCLASSIFIED
    assert parsed.parser_confidence == CONFIDENCE_LOW
    assert parsed.requires_review == 1
    assert parsed.attribute_name is None
    assert parsed.strength_score is None


def test_colon_with_empty_prefix_is_not_label_shaped(build_config):
    config = load_alpha(build_config)
    (parsed,) = classify(config, rows_for([("APPLE", ": stray colon text")]))
    assert parsed.row_type == ROW_TYPE_PAIRING_CANDIDATE


# ---------------------------------------------------------------------------
# Affinity header/group and subject-block state
# ---------------------------------------------------------------------------

def test_header_sets_state_and_following_group_is_high_confidence(build_config):
    config = load_alpha(build_config)
    header, group = classify(config, rows_for([
        ("APPLE", "Combinations"),
        ("APPLE", "apple + cinnamon + walnut"),
    ]))
    assert header.row_type == ROW_TYPE_AFFINITY_HEADER
    assert header.entry_clean == "combinations"
    assert header.parser_confidence == CONFIDENCE_HIGH
    assert header.requires_review == 0
    assert group.row_type == ROW_TYPE_AFFINITY_GROUP
    assert group.entry_clean == "apple + cinnamon + walnut"
    assert group.parser_confidence == CONFIDENCE_HIGH
    assert group.requires_review == 0
    assert group.strength_score is None


def test_header_match_trims_whitespace_and_ignores_case(build_config):
    config = load_alpha(build_config)
    # Fully uppercase, so this also proves the header check precedes the
    # pairing fallback's typography detection.
    (parsed,) = classify(config, rows_for([("APPLE", "  COMBINATIONS  ")]))
    assert parsed.row_type == ROW_TYPE_AFFINITY_HEADER


def test_delimiter_without_header_is_medium_confidence_and_review(build_config):
    config = load_alpha(build_config)
    (parsed,) = classify(config, rows_for([("APPLE", "apple + cinnamon")]))
    assert parsed.row_type == ROW_TYPE_AFFINITY_GROUP
    assert parsed.parser_confidence == CONFIDENCE_MEDIUM
    assert parsed.requires_review == 1


def test_header_state_resets_at_subject_block_boundary(build_config):
    config = load_alpha(build_config)
    _, apple_group, tomato_group = classify(config, rows_for([
        ("APPLE", "Combinations"),
        ("APPLE", "apple + cinnamon"),
        ("TOMATO", "tomato + basil"),
    ]))
    assert apple_group.parser_confidence == CONFIDENCE_HIGH
    assert apple_group.requires_review == 0
    # New subject block: APPLE's header must not vouch for TOMATO's row.
    assert tomato_group.row_type == ROW_TYPE_AFFINITY_GROUP
    assert tomato_group.parser_confidence == CONFIDENCE_MEDIUM
    assert tomato_group.requires_review == 1


def test_no_inference_across_nonconsecutive_repeats_of_a_subject(build_config):
    config = load_alpha(build_config)
    parsed = classify(config, rows_for([
        ("APPLE", "Combinations"),
        ("TOMATO", "basil"),
        ("APPLE", "apple + walnut"),  # same subject text, but a new block
    ]))
    late_group = parsed[2]
    assert late_group.row_type == ROW_TYPE_AFFINITY_GROUP
    assert late_group.parser_confidence == CONFIDENCE_MEDIUM
    assert late_group.requires_review == 1


def test_unapproved_rule_matches_become_unclassified(build_config):
    rules = [dict(DEFAULT_CONFIG["affinity_split_rules.csv"][0])]
    rules[0]["review_status"] = "needs_review"
    config = load_alpha(build_config, **{"affinity_split_rules.csv": rules})
    header_row, group_row, plain_row = classify(config, rows_for([
        ("APPLE", "Combinations"),
        ("APPLE", "apple + cinnamon"),
        ("APPLE", "cinnamon"),
    ]))
    for matched in (header_row, group_row):
        assert matched.row_type == ROW_TYPE_UNCLASSIFIED
        assert matched.requires_review == 1
    # Non-matching rows are unaffected by the pending rule.
    assert plain_row.row_type == ROW_TYPE_PAIRING_CANDIDATE


def test_no_rule_registered_means_detectors_inactive(build_config):
    config = load_alpha(build_config, **{"affinity_split_rules.csv": []})
    header_text, delimiter_text = classify(config, rows_for([
        ("APPLE", "Combinations"),
        ("APPLE", "apple + cinnamon"),
    ]))
    assert header_text.row_type == ROW_TYPE_PAIRING_CANDIDATE
    assert delimiter_text.row_type == ROW_TYPE_PAIRING_CANDIDATE
    assert delimiter_text.entry_clean == "apple + cinnamon"  # never split


# ---------------------------------------------------------------------------
# No speculative splitting
# ---------------------------------------------------------------------------

def test_no_speculative_splitting_on_comma_eg_esp(build_config):
    config = load_alpha(build_config)
    inputs = rows_for([
        ("APPLE", "berries, esp. strawberries"),
        ("APPLE", "stone fruit, e.g. peach, nectarine"),
    ])
    parsed = classify(config, inputs)
    assert len(parsed) == len(inputs)  # strictly 1:1, no fragment rows
    assert parsed[0].row_type == ROW_TYPE_PAIRING_CANDIDATE
    assert parsed[0].entry_clean == "berries, esp. strawberries"
    assert parsed[1].entry_clean == "stone fruit, e.g. peach, nectarine"


# ---------------------------------------------------------------------------
# Blank entries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["", "   ", "\t"])
def test_blank_entry_is_unclassified_for_review(build_config, entry):
    config = load_alpha(build_config)
    (parsed,) = classify(config, rows_for([("APPLE", entry)]))
    assert parsed.row_type == ROW_TYPE_UNCLASSIFIED
    assert parsed.requires_review == 1
    assert parsed.strength_score is None


# ---------------------------------------------------------------------------
# Marker detection + strength resolution on pairing candidates
# ---------------------------------------------------------------------------

def test_plain_entry_gets_null_score_when_mapping_score_is_blank(build_config):
    config = load_alpha(build_config)
    (parsed,) = classify(config, rows_for([("APPLE", "cinnamon")]))
    assert parsed.row_type == ROW_TYPE_PAIRING_CANDIDATE
    assert parsed.strength_score is None
    assert parsed.strength_label is None
    assert parsed.strength_marker_raw is None
    assert parsed.strength_method == STRENGTH_METHOD_UNAVAILABLE
    assert parsed.requires_review == 0  # mapped policy, nothing suspicious


def test_uppercase_and_asterisk_uppercase_resolve_from_config(build_config):
    config = load_alpha(build_config)
    upper, asterisk = classify(config, rows_for([
        ("APPLE", "WALNUT"),
        ("APPLE", "*CARAMEL"),
    ]))
    assert (upper.strength_label, upper.strength_score) == ("very_high", 3)
    assert upper.strength_method == STRENGTH_METHOD_TYPOGRAPHIC
    assert upper.strength_marker_raw == "uppercase"
    assert upper.entry_clean == "walnut"
    assert (asterisk.strength_label, asterisk.strength_score) == ("holy_grail", 4)
    assert asterisk.strength_marker_raw == "*+uppercase"
    assert asterisk.entry_clean == "caramel"  # leading asterisk stripped


def test_explicit_quality_label_resolves_and_takes_precedence(build_config):
    config = load_alpha(build_config)
    heaven, precedence = classify(config, rows_for([
        ("CHARD", "anchovy", "heaven"),
        ("CHARD", "WALNUT", "rec"),  # explicit label beats uppercase typography
    ]))
    assert (heaven.strength_label, heaven.strength_score) == ("holy_grail", 4)
    assert heaven.strength_method == STRENGTH_METHOD_EXPLICIT
    assert heaven.strength_marker_raw == "heaven"
    assert (precedence.strength_label, precedence.strength_score) == ("recommended", 1)
    assert precedence.strength_method == STRENGTH_METHOD_EXPLICIT


@pytest.mark.parametrize("quality", ["unheard_of", "Heaven"])  # unknown + unexpected casing
def test_unmapped_explicit_label_yields_null_score_and_review(build_config, quality):
    config = load_alpha(build_config)
    (parsed,) = classify(config, rows_for([("CHARD", "anchovy", quality)]))
    assert parsed.row_type == ROW_TYPE_PAIRING_CANDIDATE
    assert parsed.strength_score is None
    assert parsed.strength_label is None
    assert parsed.strength_method == STRENGTH_METHOD_UNAVAILABLE
    assert parsed.strength_marker_raw == quality  # evidence stays visible
    assert parsed.requires_review == 1


def test_asterisk_without_uppercase_is_plain_null_score_review(build_config):
    config = load_alpha(build_config)
    (parsed,) = classify(config, rows_for([("APPLE", "*Caramel")]))
    assert parsed.row_type == ROW_TYPE_PAIRING_CANDIDATE
    assert parsed.strength_score is None
    assert parsed.strength_method == STRENGTH_METHOD_UNAVAILABLE
    assert parsed.requires_review == 1  # suspicious marker stays visible
    assert parsed.entry_clean == "caramel"


# ---------------------------------------------------------------------------
# Output invariants: enums always set, note never emitted
# ---------------------------------------------------------------------------

def test_every_row_has_valid_enums_and_note_is_never_emitted(build_config):
    config = load_alpha(build_config)
    parsed = classify(config, rows_for([
        ("APPLE", "Season: autumn"),
        ("APPLE", "cinnamon"),
        ("APPLE", "WALNUT"),
        ("APPLE", "*CARAMEL"),
        ("APPLE", "*Mixed"),
        ("APPLE", "Combinations"),
        ("APPLE", "apple + cinnamon"),
        ("APPLE", "Unknown: thing"),
        ("APPLE", ""),
        ("CHARD", "anchovy", "heaven"),
        ("CHARD", "bacon", "no_such_label"),
    ]))
    assert ROW_TYPE_NOTE in ROW_TYPES  # legal schema value...
    for row in parsed:
        assert row.row_type in ROW_TYPES
        assert row.row_type != ROW_TYPE_NOTE  # ...but never emitted in CP4
        assert row.parser_confidence in PARSER_CONFIDENCES
        assert row.requires_review in (0, 1)
        assert row.strength_method  # always set, never blank


# ---------------------------------------------------------------------------
# parse_source: current version, rebuild, idempotency, provenance
# ---------------------------------------------------------------------------

def test_parse_writes_one_row_per_current_raw_row_with_provenance(
    build_config, conn, tmp_path, clock
):
    config = load_alpha(build_config)
    seed_source(conn, "src_alpha")
    outcome_run = ingest(conn, tmp_path, [
        ("APPLE", "Season: autumn"),
        ("APPLE", "cinnamon"),
        ("APPLE", "WALNUT"),
        ("APPLE", "Combinations"),
        ("APPLE", "apple + cinnamon + walnut"),
    ], clock=clock)

    outcome = parse_source(conn, config, "src_alpha")

    assert outcome.run_id == outcome_run.run_id
    assert outcome.row_count == 5
    parsed_keys = {
        row["source_record_id"]
        for row in conn.execute(
            "SELECT source_record_id FROM parsed_source_rows WHERE source_id = 'src_alpha'"
        )
    }
    assert parsed_keys == set(outcome_run.run_row_source_record_ids)  # 1:1, provenance intact
    assert outcome.row_type_counts == {
        ROW_TYPE_ATTRIBUTE: 1,
        ROW_TYPE_PAIRING_CANDIDATE: 2,
        ROW_TYPE_AFFINITY_HEADER: 1,
        ROW_TYPE_AFFINITY_GROUP: 1,
    }
    assert outcome.requires_review_count == 0


def test_parse_uses_current_version_only_and_removes_stale_parsed_rows(
    build_config, conn, tmp_path, clock
):
    config = load_alpha(build_config)
    seed_source(conn, "src_alpha")
    ingest(conn, tmp_path, [
        ("APPLE", "cinnamon"), ("APPLE", "walnut"), ("APPLE", "clove"),
    ], clock=clock)
    assert parse_source(conn, config, "src_alpha").row_count == 3

    # v2 removes 'clove'; historical raw row is preserved but must not be parsed.
    ingest(conn, tmp_path, [("APPLE", "cinnamon"), ("APPLE", "walnut")], clock=clock)
    outcome = parse_source(conn, config, "src_alpha")

    assert outcome.row_count == 2
    parsed = parsed_by_entry(conn)
    assert ("APPLE", "clove") not in parsed
    assert set(parsed) == {("APPLE", "cinnamon"), ("APPLE", "walnut")}
    raw_count = conn.execute(
        "SELECT COUNT(*) AS n FROM raw_source_rows WHERE source_id = 'src_alpha'"
    ).fetchone()["n"]
    assert raw_count == 3  # raw history untouched


def test_parse_after_edit_covers_new_record_not_old(build_config, conn, tmp_path, clock):
    config = load_alpha(build_config)
    seed_source(conn, "src_alpha")
    ingest(conn, tmp_path, [("APPLE", "cinnamon")], clock=clock)
    ingest(conn, tmp_path, [("APPLE", "cinnamon-toast")], clock=clock)  # edited row

    outcome = parse_source(conn, config, "src_alpha")

    assert outcome.row_count == 1
    parsed = parsed_by_entry(conn)
    assert set(parsed) == {("APPLE", "cinnamon-toast")}


def test_parse_rerun_is_idempotent(build_config, conn, tmp_path, clock):
    config = load_alpha(build_config)
    seed_source(conn, "src_alpha")
    ingest(conn, tmp_path, [
        ("APPLE", "Season: autumn"), ("APPLE", "cinnamon"), ("APPLE", "*CARAMEL"),
    ], clock=clock)

    parse_source(conn, config, "src_alpha")
    first = conn.execute(
        "SELECT * FROM parsed_source_rows ORDER BY source_record_id"
    ).fetchall()
    parse_source(conn, config, "src_alpha")
    second = conn.execute(
        "SELECT * FROM parsed_source_rows ORDER BY source_record_id"
    ).fetchall()

    assert [tuple(row) for row in first] == [tuple(row) for row in second]
    assert len(first) == 3  # no duplicates


def test_parse_without_completed_run_raises(build_config, conn):
    config = load_alpha(build_config)
    seed_source(conn, "src_alpha")
    with pytest.raises(ParseError, match=r"no completed import run"):
        parse_source(conn, config, "src_alpha")


def test_parse_unknown_source_raises_config_error(build_config, conn):
    config = load_alpha(build_config)
    with pytest.raises(ConfigError, match=r"unknown source_id"):
        parse_source(conn, config, "src_never_registered")


def test_parse_format_without_strength_mappings_fails_fast(build_config, conn):
    config = load_config(build_config(overrides={"strength_mappings.csv": []}))
    seed_source(conn, "src_alpha")
    with pytest.raises(ConfigError, match=r"no strength mappings.*fmt_alpha"):
        parse_source(conn, config, "src_alpha")


def test_parse_never_modifies_raw_source_rows(build_config, conn, tmp_path, clock):
    config = load_alpha(build_config)
    seed_source(conn, "src_alpha")
    ingest(conn, tmp_path, [("APPLE", "cinnamon"), ("APPLE", "WALNUT")], clock=clock)
    before = [
        tuple(row)
        for row in conn.execute("SELECT * FROM raw_source_rows ORDER BY source_record_id")
    ]

    parse_source(conn, config, "src_alpha")

    after = [
        tuple(row)
        for row in conn.execute("SELECT * FROM raw_source_rows ORDER BY source_record_id")
    ]
    assert before == after


def test_parse_code_contains_no_raw_writes():
    paths = sorted(RUNTIME_PARSE_DIR.glob("*.py"))
    assert paths, "expected the parse package to contain Python files"
    for path in paths:
        text_upper = path.read_text(encoding="utf-8").upper()
        assert "INSERT INTO RAW_SOURCE_ROWS" not in text_upper, f"{path} inserts raw rows"
        assert "UPDATE RAW_SOURCE_ROWS" not in text_upper, f"{path} updates raw rows"
        assert "DELETE FROM RAW_SOURCE_ROWS" not in text_upper, f"{path} deletes raw rows"


def test_parse_works_for_arbitrary_source_id_and_row_count(
    build_config, conn, tmp_path, clock
):
    source_id = "src_generated_4b7e"
    sources = [{
        "source_id": source_id,
        "source_name": "Generated parser source",
        "source_format": "fmt_alpha",
        "rights_status": "project_owned_demo",
        "allowed_use": "software_testing",
    }]
    config = load_alpha(build_config, **{"sources.csv": sources})
    seed_source(conn, source_id)

    row_count = 29
    rows = [(f"SUBJECT_{i % 5}", f"entry {i}") for i in range(row_count)]
    ingest(conn, tmp_path, rows, source_id=source_id, clock=clock)

    outcome = parse_source(conn, config, source_id)
    assert outcome.row_count == row_count
    assert outcome.row_type_counts == {ROW_TYPE_PAIRING_CANDIDATE: row_count}


def test_outcome_counts_match_table_contents(build_config, conn, tmp_path, clock):
    config = load_alpha(build_config)
    seed_source(conn, "src_alpha")
    ingest(conn, tmp_path, [
        ("APPLE", "Season: autumn"),
        ("APPLE", "Mystery: value"),   # unregistered label -> unclassified + review
        ("APPLE", "cinnamon"),
        ("APPLE", "apple + cinnamon"),  # delimiter without header -> review
    ], clock=clock)

    outcome = parse_source(conn, config, "src_alpha")

    table_review_count = conn.execute(
        "SELECT COUNT(*) AS n FROM parsed_source_rows WHERE requires_review = 1"
    ).fetchone()["n"]
    assert outcome.requires_review_count == table_review_count == 2
    for row_type, count in outcome.row_type_counts.items():
        table_count = conn.execute(
            "SELECT COUNT(*) AS n FROM parsed_source_rows WHERE row_type = ?",
            (row_type,),
        ).fetchone()["n"]
        assert table_count == count
