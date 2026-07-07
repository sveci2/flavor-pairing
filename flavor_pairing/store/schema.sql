-- Portable SQL schema mirroring docs/SCHEMA.md, one table per file (CP2 scope,
-- docs/DATA_FOUNDATION_PLAN.md §14). Portable subset only: TEXT/INTEGER types,
-- named PRIMARY KEY/UNIQUE/FOREIGN KEY constraints, no AUTOINCREMENT, no
-- reliance on SQLite rowid semantics. All SQLite-only setup (foreign-key
-- enforcement, connection handling) lives in store/db.py, not here.
--
-- NOT NULL is applied only to: primary-key columns (SQLite does not imply
-- NOT NULL from PRIMARY KEY on non-INTEGER keys, unlike standard SQL), fields
-- docs/SCHEMA.md §2 documents as required, and fields
-- docs/DATA_FOUNDATION_PLAN.md §5 documents as always set by the parser.
-- Every other column is left nullable rather than inventing undocumented
-- required-ness (CLAUDE.md: never invent missing information).
--
-- Out of scope for CP2 (see docs/DATA_FOUNDATION_PLAN.md §20-21): import_runs
-- and run_rows (ledger, CP3) and pairing_edges (derived aggregate, out of
-- scope this phase).

-- docs/SCHEMA.md §1 — provenance and rights registry.
CREATE TABLE IF NOT EXISTS sources (
    source_id       TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    source_format   TEXT NOT NULL,
    source_uri      TEXT,
    rights_status   TEXT NOT NULL,
    allowed_use     TEXT,
    notes           TEXT,
    PRIMARY KEY (source_id)
);

-- docs/SCHEMA.md §4 — canonical concepts. Self-referential parent_entity_id
-- (docs/DECISIONS.md §D); acyclicity is validated in code, not by this DDL.
CREATE TABLE IF NOT EXISTS entities (
    entity_id               TEXT NOT NULL,
    canonical_name          TEXT,
    display_name            TEXT,
    entity_type             TEXT,
    parent_entity_id        TEXT,
    normalization_status    TEXT,
    review_status           TEXT,
    notes                   TEXT,
    PRIMARY KEY (entity_id),
    FOREIGN KEY (parent_entity_id) REFERENCES entities (entity_id)
);

-- docs/SCHEMA.md §2 — immutable staging table. Required source content per
-- §2: source_order, subject_raw, entry_raw. Natural key (source_id,
-- source_record_id) used throughout downstream tables (plan §12).
CREATE TABLE IF NOT EXISTS raw_source_rows (
    source_id           TEXT NOT NULL,
    source_record_id    TEXT NOT NULL,
    source_order        INTEGER NOT NULL,
    subject_raw         TEXT NOT NULL,
    entry_raw           TEXT NOT NULL,
    quality_raw         TEXT,
    raw_payload_json    TEXT,
    PRIMARY KEY (source_id, source_record_id),
    FOREIGN KEY (source_id) REFERENCES sources (source_id)
);

-- docs/SCHEMA.md §3 — derived parser output, 1:1 with raw_source_rows.
-- row_type, parser_confidence, requires_review are always set (plan §5).
CREATE TABLE IF NOT EXISTS parsed_source_rows (
    source_id               TEXT NOT NULL,
    source_record_id        TEXT NOT NULL,
    row_type                TEXT NOT NULL,
    subject_clean            TEXT,
    entry_clean              TEXT,
    attribute_name           TEXT,
    attribute_value_raw      TEXT,
    strength_marker_raw      TEXT,
    strength_label           TEXT,
    strength_score           INTEGER,
    strength_method          TEXT,
    parser_confidence        TEXT NOT NULL,
    requires_review          INTEGER NOT NULL,
    PRIMARY KEY (source_id, source_record_id),
    FOREIGN KEY (source_id, source_record_id)
        REFERENCES raw_source_rows (source_id, source_record_id)
);

-- docs/SCHEMA.md §5 — source-string-to-entity mappings. Unique mapping per
-- (source_id, source_text, source_role) (docs/DECISIONS.md §A). entity_id is
-- nullable: a mapping may remain unresolved.
CREATE TABLE IF NOT EXISTS entity_source_names (
    source_name_id           TEXT NOT NULL,
    source_id                TEXT NOT NULL,
    source_text              TEXT NOT NULL,
    source_role              TEXT NOT NULL,
    entity_id                TEXT,
    normalization_status     TEXT,
    notes                    TEXT,
    PRIMARY KEY (source_name_id),
    UNIQUE (source_id, source_text, source_role),
    FOREIGN KEY (source_id) REFERENCES sources (source_id),
    FOREIGN KEY (entity_id) REFERENCES entities (entity_id)
);

-- docs/SCHEMA.md §6 — named key/value attribute observations. entity_id is
-- nullable: an attribute observation may be preserved and reviewed even
-- before its subject entity is resolved (approved design decision).
CREATE TABLE IF NOT EXISTS entity_attributes (
    attribute_id                   TEXT NOT NULL,
    source_id                      TEXT NOT NULL,
    source_record_id               TEXT NOT NULL,
    entity_id                      TEXT,
    attribute_name                 TEXT,
    attribute_value_raw            TEXT,
    attribute_value_normalized     TEXT,
    normalization_method           TEXT,
    review_status                  TEXT,
    PRIMARY KEY (attribute_id),
    FOREIGN KEY (source_id, source_record_id)
        REFERENCES raw_source_rows (source_id, source_record_id),
    FOREIGN KEY (entity_id) REFERENCES entities (entity_id)
);

-- docs/SCHEMA.md §7 — one row per source observation. Unique (source_id,
-- source_record_id): the 1:1 observation-per-row invariant (plan §8/§12).
-- paired_entity_id is nullable (§7: may be blank for an unresolved composite
-- or ambiguous phrase); subject_entity_id is required.
CREATE TABLE IF NOT EXISTS pairing_observations (
    observation_id           TEXT NOT NULL,
    source_id                TEXT NOT NULL,
    source_record_id         TEXT NOT NULL,
    subject_entity_id        TEXT NOT NULL,
    paired_entity_id         TEXT,
    paired_text_raw          TEXT,
    strength_label           TEXT,
    strength_score           INTEGER,
    strength_method          TEXT,
    normalization_status     TEXT,
    review_status            TEXT,
    PRIMARY KEY (observation_id),
    UNIQUE (source_id, source_record_id),
    FOREIGN KEY (source_id, source_record_id)
        REFERENCES raw_source_rows (source_id, source_record_id),
    FOREIGN KEY (subject_entity_id) REFERENCES entities (entity_id),
    FOREIGN KEY (paired_entity_id) REFERENCES entities (entity_id)
);

-- docs/SCHEMA.md §8 — multi-ingredient affinity groups. Unique (source_id,
-- source_record_id) matching pairing_observations. subject_entity_id is
-- nullable: a group must be preservable even when unresolved, remaining
-- visible with review_status = needs_review (approved design decision).
CREATE TABLE IF NOT EXISTS affinity_groups (
    affinity_id            TEXT NOT NULL,
    source_id               TEXT NOT NULL,
    source_record_id        TEXT NOT NULL,
    subject_entity_id       TEXT,
    affinity_text_raw       TEXT,
    review_status           TEXT,
    PRIMARY KEY (affinity_id),
    UNIQUE (source_id, source_record_id),
    FOREIGN KEY (source_id, source_record_id)
        REFERENCES raw_source_rows (source_id, source_record_id),
    FOREIGN KEY (subject_entity_id) REFERENCES entities (entity_id)
);

-- docs/SCHEMA.md §8 — affinity members. Natural key (affinity_id,
-- member_order) (plan §12). member_entity_id is nullable: members follow the
-- same normalization path as any other entity mention and may stay
-- unresolved (docs/SCHEMA.md §6, §9).
CREATE TABLE IF NOT EXISTS affinity_members (
    affinity_id              TEXT NOT NULL,
    member_order             INTEGER NOT NULL,
    member_entity_id         TEXT,
    member_text_raw          TEXT,
    normalization_status     TEXT,
    PRIMARY KEY (affinity_id, member_order),
    FOREIGN KEY (affinity_id) REFERENCES affinity_groups (affinity_id),
    FOREIGN KEY (member_entity_id) REFERENCES entities (entity_id)
);

-- docs/SCHEMA.md §10 — declares, per source_format, which input column feeds
-- which raw target field. Key is (source_format, target_file, target_field):
-- target_file is part of the declared mapping contract (approved design
-- decision), even though every current row targets raw_source_rows.csv.
CREATE TABLE IF NOT EXISTS import_mappings (
    source_format      TEXT NOT NULL,
    input_column       TEXT,
    target_file        TEXT NOT NULL,
    target_field       TEXT NOT NULL,
    transform_rule     TEXT,
    required           INTEGER,
    PRIMARY KEY (source_format, target_file, target_field)
);

-- docs/SCHEMA.md §10 — strength-evidence normalization, keyed exactly on
-- (input_source_format, marker_key).
CREATE TABLE IF NOT EXISTS strength_mappings (
    input_source_format       TEXT NOT NULL,
    marker_key                TEXT NOT NULL,
    source_value_or_marker    TEXT,
    normalized_label          TEXT,
    normalized_score          INTEGER,
    mapping_confidence        TEXT,
    notes                     TEXT,
    PRIMARY KEY (input_source_format, marker_key)
);

-- docs/SCHEMA.md §10 — one row per recognized attribute-line label per
-- source format.
CREATE TABLE IF NOT EXISTS attribute_labels (
    source_format      TEXT NOT NULL,
    source_label       TEXT NOT NULL,
    attribute_name     TEXT,
    notes              TEXT,
    PRIMARY KEY (source_format, source_label)
);

-- docs/SCHEMA.md §10 — one row per source format registering how affinities
-- are recognized and split.
CREATE TABLE IF NOT EXISTS affinity_split_rules (
    source_format             TEXT NOT NULL,
    affinity_header_phrase    TEXT,
    member_delimiter          TEXT,
    review_status             TEXT,
    notes                     TEXT,
    PRIMARY KEY (source_format)
);
