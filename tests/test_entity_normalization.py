"""CP5 tests: entity/source-name resolution and derived normalized outputs
(docs/DATA_FOUNDATION_PLAN.md §6, §16 test_entity_normalization.py;
docs/DECISIONS.md §A, §C).

All sources, formats, and entities here are synthetic and generated; no
sample source IDs or fixed sample counts. Nothing touches data/ledger/ or
data/imports_private/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flavor_pairing.config.loaders import ConfigError
from flavor_pairing.normalize.entities import (
    ENTITY_TYPE_UNKNOWN,
    NORMALIZATION_STATUS_AUTO_MAPPED,
    NORMALIZATION_STATUS_HUMAN_MAPPED,
    NORMALIZATION_STATUS_UNRESOLVED,
    NormalizeError,
    create_reviewed_entity,
)
from flavor_pairing.normalize.pipeline import normalize_source
from flavor_pairing.parse.row_parser import parse_source
from flavor_pairing.store import db
from pipeline_helpers import (
    attribute_rows,
    full_config,
    ingest_rows,
    make_clock,
    mapping_row,
    observation_rows,
    run_full,
    seed_entity,
    seed_mapping,
    seed_source,
    table_snapshot,
)

RUNTIME_DIR = Path(__file__).resolve().parents[1] / "flavor_pairing"

SOURCE = "src_alpha"  # conftest DEFAULT_CONFIG's registered source


@pytest.fixture
def conn():
    connection = db.open_database(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def clock():
    return make_clock()


@pytest.fixture
def config(build_config):
    return full_config(build_config)


def entity_count(connection) -> int:
    return connection.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]


# ---------------------------------------------------------------------------
# Exact matching
# ---------------------------------------------------------------------------

def test_exact_canonical_match_resolves_subject_and_paired(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_cinnamon", "cinnamon")

    outcome = run_full(conn, config, SOURCE, [("APPLE", "cinnamon")], tmp_path, clock)

    subject = mapping_row(conn, SOURCE, "APPLE", "subject")
    paired = mapping_row(conn, SOURCE, "cinnamon", "pairing_entry")
    assert subject["entity_id"] == "ent_apple"
    assert subject["normalization_status"] == NORMALIZATION_STATUS_AUTO_MAPPED
    assert paired["entity_id"] == "ent_cinnamon"
    (observation,) = observation_rows(conn, SOURCE)
    assert observation["subject_entity_id"] == "ent_apple"
    assert observation["paired_entity_id"] == "ent_cinnamon"
    assert outcome.observations_written == 1
    assert outcome.unresolved_mappings == 0


def test_exact_match_is_trim_and_case_fold_only(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_olive_oil", "olive oil")

    # Raw texts differ in case/whitespace; clean forms match exactly.
    run_full(conn, config, SOURCE, [("  APPLE  ", "Olive Oil")], tmp_path, clock)

    assert mapping_row(conn, SOURCE, "  APPLE  ", "subject")["entity_id"] == "ent_apple"
    assert (
        mapping_row(conn, SOURCE, "Olive Oil", "pairing_entry")["entity_id"]
        == "ent_olive_oil"
    )


def test_marker_stripped_entry_matches_canonical(conn, config, tmp_path, clock):
    """'*CARAMEL' resolves via its clean form 'caramel' while the mapping
    keeps the exact raw surface text as source_text."""
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_caramel", "caramel")

    run_full(conn, config, SOURCE, [("APPLE", "*CARAMEL")], tmp_path, clock)

    row = mapping_row(conn, SOURCE, "*CARAMEL", "pairing_entry")
    assert row is not None  # keyed on the raw surface string
    assert row["entity_id"] == "ent_caramel"


def test_plural_and_fuzzy_variants_do_not_resolve(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")

    run_full(conn, config, SOURCE, [("APPLES", "apple juice")], tmp_path, clock)

    assert mapping_row(conn, SOURCE, "APPLES", "subject")["entity_id"] is None
    assert mapping_row(conn, SOURCE, "apple juice", "pairing_entry")["entity_id"] is None
    assert entity_count(conn) == 1  # nothing new was created


# ---------------------------------------------------------------------------
# Alias (reviewed mapping) matching
# ---------------------------------------------------------------------------

def test_reviewed_alias_mapping_resolves(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_tomato", "tomato")
    seed_entity(conn, "ent_love_apple", "love apple fruit")
    # Reviewed alias: surface string differs from every canonical name.
    seed_mapping(
        conn, SOURCE, "pomme d'amour", "pairing_entry",
        entity_id="ent_love_apple",
        normalization_status=NORMALIZATION_STATUS_HUMAN_MAPPED,
    )

    run_full(conn, config, SOURCE, [("TOMATO", "pomme d'amour")], tmp_path, clock)

    row = mapping_row(conn, SOURCE, "pomme d'amour", "pairing_entry")
    assert row["entity_id"] == "ent_love_apple"
    assert row["normalization_status"] == NORMALIZATION_STATUS_HUMAN_MAPPED  # untouched
    (observation,) = observation_rows(conn, SOURCE)
    assert observation["paired_entity_id"] == "ent_love_apple"


# ---------------------------------------------------------------------------
# Unresolved behavior: no placeholders, no automatic creation
# ---------------------------------------------------------------------------

def test_unmatched_names_stay_unresolved_with_no_entity_created(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)

    outcome = run_full(conn, config, SOURCE, [("APPLE", "cinnamon")], tmp_path, clock)

    for text, role in (("APPLE", "subject"), ("cinnamon", "pairing_entry")):
        row = mapping_row(conn, SOURCE, text, role)
        assert row["entity_id"] is None
        assert row["normalization_status"] == NORMALIZATION_STATUS_UNRESOLVED
    assert entity_count(conn) == 0  # no placeholder, no auto-created entity
    assert outcome.unresolved_mappings == 2
    assert outcome.observations_written == 0
    assert outcome.observations_skipped_unresolved_subject == 1


@pytest.mark.parametrize(
    "phrase",
    [
        "berries, esp. strawberries",
        "stone fruit, e.g. peach",
        "apple and cinnamon",
        "unknown descriptive text",
    ],
)
def test_composite_phrases_stay_unresolved_and_create_nothing(
    conn, config, tmp_path, clock, phrase
):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")

    run_full(conn, config, SOURCE, [("APPLE", phrase)], tmp_path, clock)

    row = mapping_row(conn, SOURCE, phrase, "pairing_entry")
    assert row["entity_id"] is None
    assert row["normalization_status"] == NORMALIZATION_STATUS_UNRESOLVED
    assert entity_count(conn) == 1  # only the seeded entity
    (observation,) = observation_rows(conn, SOURCE)
    assert observation["paired_entity_id"] is None
    assert observation["paired_text_raw"] == phrase
    assert observation["normalization_status"] == NORMALIZATION_STATUS_UNRESOLVED


def test_rejected_entity_name_stays_unresolved_and_is_not_recreated(
    conn, config, tmp_path, clock
):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_walnut", "walnut", review_status="rejected")
    rejected_before = conn.execute(
        "SELECT * FROM entities WHERE entity_id = 'ent_walnut'"
    ).fetchone()

    run_full(conn, config, SOURCE, [("APPLE", "WALNUT")], tmp_path, clock)

    row = mapping_row(conn, SOURCE, "WALNUT", "pairing_entry")
    assert row["entity_id"] is None  # never mapped to the rejected entity
    assert entity_count(conn) == 2  # not silently recreated either
    rejected_after = conn.execute(
        "SELECT * FROM entities WHERE entity_id = 'ent_walnut'"
    ).fetchone()
    assert tuple(rejected_before) == tuple(rejected_after)


def test_ambiguous_duplicate_canonical_names_stay_unresolved(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_basil_a", "basil")
    seed_entity(conn, "ent_basil_b", "basil")

    run_full(conn, config, SOURCE, [("APPLE", "basil")], tmp_path, clock)

    assert mapping_row(conn, SOURCE, "basil", "pairing_entry")["entity_id"] is None
    assert entity_count(conn) == 3


# ---------------------------------------------------------------------------
# Reviewed entity creation (the only creation path)
# ---------------------------------------------------------------------------

def test_create_reviewed_entity_stores_every_column_correctly(conn):
    seed_entity(conn, "ent_parent", "citrus")
    create_reviewed_entity(
        conn,
        entity_id="ent_yuzu",
        canonical_name="yuzu",
        entity_type="ingredient",
        display_name="Yuzu",
        parent_entity_id="ent_parent",
        review_status="approved",
        notes="reviewed seed",
    )
    row = conn.execute("SELECT * FROM entities WHERE entity_id = 'ent_yuzu'").fetchone()
    assert row["canonical_name"] == "yuzu"
    assert row["display_name"] == "Yuzu"
    assert row["entity_type"] == "ingredient"  # in its own column...
    assert row["parent_entity_id"] == "ent_parent"  # ...not shifted into parent
    assert row["normalization_status"] == NORMALIZATION_STATUS_HUMAN_MAPPED
    assert row["review_status"] == "approved"
    assert row["notes"] == "reviewed seed"


def test_create_reviewed_entity_defaults_type_unknown_and_null_display(conn):
    create_reviewed_entity(conn, entity_id="ent_mystery", canonical_name="mystery item")
    row = conn.execute(
        "SELECT * FROM entities WHERE entity_id = 'ent_mystery'"
    ).fetchone()
    assert row["entity_type"] == ENTITY_TYPE_UNKNOWN
    assert row["display_name"] is None
    assert row["parent_entity_id"] is None


def test_create_reviewed_entity_rejects_duplicate_without_overwriting(conn):
    create_reviewed_entity(
        conn, entity_id="ent_dup", canonical_name="original", notes="first"
    )
    before = conn.execute("SELECT * FROM entities WHERE entity_id = 'ent_dup'").fetchone()
    with pytest.raises(NormalizeError, match=r"already exists"):
        create_reviewed_entity(
            conn, entity_id="ent_dup", canonical_name="replacement", notes="second"
        )
    after = conn.execute("SELECT * FROM entities WHERE entity_id = 'ent_dup'").fetchone()
    assert tuple(before) == tuple(after)  # original row untouched


def test_create_reviewed_entity_validates_type_and_status(conn):
    with pytest.raises(NormalizeError, match=r"invalid entity_type"):
        create_reviewed_entity(conn, entity_id="ent_x", canonical_name="x", entity_type="flavor")
    with pytest.raises(NormalizeError, match=r"invalid review_status"):
        create_reviewed_entity(conn, entity_id="ent_x", canonical_name="x", review_status="maybe")
    with pytest.raises(NormalizeError, match=r"entity_id must not be blank"):
        create_reviewed_entity(conn, entity_id="  ", canonical_name="x")


def test_normalize_layer_has_exactly_one_entity_insert_path():
    """INSERT INTO entities exists only in the reviewed-creation helper."""
    counts = {}
    for path in sorted((RUNTIME_DIR / "normalize").glob("*.py")) + sorted(
        (RUNTIME_DIR / "review").glob("*.py")
    ):
        counts[path.name] = path.read_text(encoding="utf-8").upper().count(
            "INSERT INTO ENTITIES"
        )
    assert counts.pop("entities.py") == 1
    assert all(count == 0 for count in counts.values()), counts


# ---------------------------------------------------------------------------
# Attribute rows
# ---------------------------------------------------------------------------

def test_attribute_values_never_create_entities_or_mappings(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")

    run_full(
        conn, config, SOURCE,
        [("APPLE", "Techniques: bake, poach, raw"), ("APPLE", "Taste: sweet, tart")],
        tmp_path, clock,
    )

    assert entity_count(conn) == 1  # no technique/taste entities mined
    mappings = conn.execute(
        "SELECT source_text, source_role FROM entity_source_names WHERE source_id = ?",
        (SOURCE,),
    ).fetchall()
    assert [(m["source_text"], m["source_role"]) for m in mappings] == [("APPLE", "subject")]
    rows = attribute_rows(conn, SOURCE)
    assert {r["attribute_name"] for r in rows} == {"techniques", "taste"}
    assert {r["attribute_value_raw"] for r in rows} == {"bake, poach, raw", "sweet, tart"}
    assert all(r["entity_id"] == "ent_apple" for r in rows)
    assert all(r["attribute_value_normalized"] is None for r in rows)


def test_attribute_row_written_with_null_entity_when_subject_unresolved(
    conn, config, tmp_path, clock
):
    seed_source(conn, SOURCE)

    outcome = run_full(conn, config, SOURCE, [("APPLE", "Season: autumn")], tmp_path, clock)

    (row,) = attribute_rows(conn, SOURCE)
    assert row["entity_id"] is None
    assert row["attribute_name"] == "season"
    assert row["attribute_value_raw"] == "autumn"
    assert outcome.attributes_written == 1
    assert entity_count(conn) == 0


# ---------------------------------------------------------------------------
# Uniqueness, provenance, strength copying, skips
# ---------------------------------------------------------------------------

def test_one_mapping_row_per_key_regardless_of_repetition(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    rows = [
        ("APPLE", "cinnamon"),
        ("APPLE", "walnut"),
        ("APPLE", "Season: autumn"),
        ("cinnamon", "apple"),  # same text as subject: a distinct role key
    ]
    run_full(conn, config, SOURCE, rows, tmp_path, clock)

    mappings = conn.execute(
        "SELECT source_text, source_role, COUNT(*) AS n FROM entity_source_names "
        "WHERE source_id = ? GROUP BY source_text, source_role",
        (SOURCE,),
    ).fetchall()
    assert all(m["n"] == 1 for m in mappings)
    keys = {(m["source_text"], m["source_role"]) for m in mappings}
    assert keys == {
        ("APPLE", "subject"), ("cinnamon", "subject"),
        ("cinnamon", "pairing_entry"), ("walnut", "pairing_entry"),
        ("apple", "pairing_entry"),
    }


def test_provenance_and_strength_copied_exactly(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    for entity_id, name in (
        ("ent_apple", "apple"), ("ent_cinnamon", "cinnamon"), ("ent_walnut", "walnut"),
        ("ent_caramel", "caramel"), ("ent_chard", "chard"), ("ent_anchovy", "anchovy"),
    ):
        seed_entity(conn, entity_id, name)

    run_full(
        conn, config, SOURCE,
        [
            ("APPLE", "cinnamon"),            # plain -> null score
            ("APPLE", "WALNUT"),              # uppercase -> 3
            ("APPLE", "*CARAMEL"),            # asterisk_uppercase -> 4
            ("CHARD", "anchovy", "heaven"),   # explicit label -> 4
        ],
        tmp_path, clock,
    )

    parsed = {
        row["source_record_id"]: row
        for row in conn.execute(
            "SELECT * FROM parsed_source_rows WHERE row_type = 'pairing_candidate'"
        )
    }
    observations = observation_rows(conn, SOURCE)
    assert len(observations) == 4
    raw_keys = {
        row["source_record_id"]
        for row in conn.execute("SELECT source_record_id FROM raw_source_rows")
    }
    for observation in observations:
        parsed_row = parsed[observation["source_record_id"]]  # provenance intact
        assert observation["source_record_id"] in raw_keys
        assert observation["strength_label"] == parsed_row["strength_label"]
        assert observation["strength_score"] == parsed_row["strength_score"]
        assert observation["strength_method"] == parsed_row["strength_method"]
    by_text = {o["paired_text_raw"]: o for o in observations}
    assert by_text["*CARAMEL"]["strength_score"] == 4  # raw text kept verbatim
    assert by_text["cinnamon"]["strength_score"] is None  # no invented score
    assert by_text["WALNUT"]["strength_score"] == 3
    assert by_text["anchovy"]["strength_score"] == 4


def test_unresolved_subjects_skip_observations_and_are_counted(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    seed_entity(conn, "ent_cinnamon", "cinnamon")

    outcome = run_full(
        conn, config, SOURCE,
        [
            ("APPLE", "cinnamon"),    # subject resolved -> written
            ("MYSTERY ROOT", "cinnamon"),  # subject unresolved -> skipped
            ("MYSTERY ROOT", "walnut"),    # subject unresolved -> skipped
        ],
        tmp_path, clock,
    )

    assert outcome.observations_written == 1
    assert outcome.observations_skipped_unresolved_subject == 2
    assert len(observation_rows(conn, SOURCE)) == 1
    # The skipped rows' parsed rows and unresolved mappings are preserved.
    assert mapping_row(conn, SOURCE, "MYSTERY ROOT", "subject")["entity_id"] is None
    parsed_count = conn.execute(
        "SELECT COUNT(*) AS n FROM parsed_source_rows WHERE source_id = ?", (SOURCE,)
    ).fetchone()["n"]
    assert parsed_count == 3


# ---------------------------------------------------------------------------
# Preconditions and generality
# ---------------------------------------------------------------------------

def test_normalize_requires_parse_and_registration(conn, config, build_config, tmp_path, clock):
    seed_source(conn, SOURCE)
    with pytest.raises(ConfigError, match=r"unknown source_id"):
        normalize_source(conn, config, "src_never_registered")
    with pytest.raises(NormalizeError, match=r"no completed import run"):
        normalize_source(conn, config, SOURCE)

    ingest_rows(conn, tmp_path, SOURCE, [("APPLE", "cinnamon")], clock)
    with pytest.raises(NormalizeError, match=r"no parsed rows"):
        normalize_source(conn, config, SOURCE)

    parse_source(conn, config, SOURCE)
    normalize_source(conn, config, SOURCE)  # now fine
    # A new version without a re-parse is stale and must be refused.
    ingest_rows(conn, tmp_path, SOURCE, [("APPLE", "clove")], clock)
    with pytest.raises(NormalizeError, match=r"re-run the parser"):
        normalize_source(conn, config, SOURCE)


def test_normalize_never_touches_raw_parsed_or_ledger(conn, config, tmp_path, clock):
    seed_source(conn, SOURCE)
    seed_entity(conn, "ent_apple", "apple")
    ingest_rows(conn, tmp_path, SOURCE, [("APPLE", "cinnamon")], clock)
    parse_source(conn, config, SOURCE)
    before = {
        table: table_snapshot(conn, table)
        for table in ("raw_source_rows", "parsed_source_rows", "import_runs", "run_rows")
    }

    normalize_source(conn, config, SOURCE)

    for table, snapshot in before.items():
        assert table_snapshot(conn, table) == snapshot, f"{table} was modified"


def test_arbitrary_source_id_and_row_count(conn, build_config, tmp_path, clock):
    source_id = "src_generated_c41d"
    sources = [{
        "source_id": source_id,
        "source_name": "Generated normalization source",
        "source_format": "fmt_alpha",
        "rights_status": "project_owned_demo",
        "allowed_use": "software_testing",
    }]
    config = full_config(build_config, **{"sources.csv": sources})
    seed_source(conn, source_id)
    for i in range(6):
        seed_entity(conn, f"ent_gen_{i}", f"subject {i}")

    row_count = 31
    rows = [(f"SUBJECT {i % 6}", f"entry {i}") for i in range(row_count)]
    outcome = run_full(conn, config, source_id, rows, tmp_path, clock)

    assert outcome.observations_written == row_count  # all subjects resolved
    assert outcome.observations_skipped_unresolved_subject == 0
    assert all(o["paired_entity_id"] is None for o in observation_rows(conn, source_id))
