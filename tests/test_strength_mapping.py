"""CP4 tests: marker-key strength resolution (docs/DATA_FOUNDATION_PLAN.md
§11, §16 test_strength_mapping.py; docs/DECISIONS.md §F).

Resolution is asserted generically — keyed on source_format, never on any
source_id — over both the checked-in sample configuration (data-driven, no
hard-coded formats or row counts) and synthetic configs from build_config.
"""

from __future__ import annotations

import pytest

from conftest import DEFAULT_CONFIG
from flavor_pairing.config.loaders import EXPLICIT_LABEL_PREFIX, load_config
from flavor_pairing.parse.strength import (
    STRENGTH_METHOD_EXPLICIT,
    STRENGTH_METHOD_TYPOGRAPHIC,
    STRENGTH_METHOD_UNAVAILABLE,
    STRENGTH_METHODS,
    resolve_strength,
)


# ---------------------------------------------------------------------------
# Every sample config row resolves via its own marker_key (data-driven)
# ---------------------------------------------------------------------------

def test_every_sample_mapping_row_resolves_by_marker_key(sample_config_dir):
    config = load_config(sample_config_dir)
    assert config.strength_mappings, "sample should declare strength mappings"
    seen_scored = seen_scoreless = False
    for source_format, per_format in config.strength_mappings.items():
        for marker_key, mapping in per_format.items():
            resolution = resolve_strength(per_format, marker_key)
            assert resolution.mapped is True
            assert resolution.strength_score == mapping.normalized_score
            assert resolution.strength_label == mapping.normalized_label
            assert resolution.strength_method in STRENGTH_METHODS
            if mapping.normalized_score is None:
                seen_scoreless = True
                assert resolution.strength_method == STRENGTH_METHOD_UNAVAILABLE
            else:
                seen_scored = True
                expected = (
                    STRENGTH_METHOD_EXPLICIT
                    if marker_key.startswith(EXPLICIT_LABEL_PREFIX)
                    else STRENGTH_METHOD_TYPOGRAPHIC
                )
                assert resolution.strength_method == expected
    assert seen_scored and seen_scoreless, (
        "sample should exercise both scored and scoreless resolution paths"
    )


# ---------------------------------------------------------------------------
# Typography-lossy 'plain' policy: declared blank score -> no score, ever
# ---------------------------------------------------------------------------

def test_plain_with_blank_score_resolves_to_no_score(build_config):
    config = load_config(build_config())
    resolution = resolve_strength(config.strength_mappings_for("fmt_alpha"), "plain")
    assert resolution.mapped is True
    assert resolution.strength_score is None
    assert resolution.strength_label is None
    assert resolution.strength_method == STRENGTH_METHOD_UNAVAILABLE


def test_plain_policy_applies_to_any_source_sharing_the_format(build_config):
    """docs/DECISIONS.md §F: the rule is keyed on format, not source_id."""
    sources = [dict(DEFAULT_CONFIG["sources.csv"][0])]
    second = dict(sources[0])
    second["source_id"] = "src_second_same_format"
    second["source_name"] = "Second source, same format"
    sources.append(second)
    config = load_config(build_config(overrides={"sources.csv": sources}))

    for source in config.sources.values():
        per_format = config.strength_mappings_for(source.source_format)
        resolution = resolve_strength(per_format, "plain")
        assert resolution.strength_score is None
        assert resolution.strength_method == STRENGTH_METHOD_UNAVAILABLE


# ---------------------------------------------------------------------------
# Unmapped markers: no invention, flagged as unmapped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "marker_key",
    ["uppercase", "asterisk_uppercase", "explicit_label:banana", "explicit_label:Heaven"],
)
def test_unmapped_marker_yields_no_score_and_mapped_false(build_config, marker_key):
    # fmt_alpha's default config maps only 'plain'.
    config = load_config(build_config())
    resolution = resolve_strength(config.strength_mappings_for("fmt_alpha"), marker_key)
    assert resolution.mapped is False
    assert resolution.strength_score is None
    assert resolution.strength_label is None
    assert resolution.strength_method == STRENGTH_METHOD_UNAVAILABLE


def test_resolver_has_no_default_for_empty_format_mappings():
    resolution = resolve_strength({}, "uppercase")
    assert resolution.mapped is False
    assert resolution.strength_score is None
    assert resolution.strength_method == STRENGTH_METHOD_UNAVAILABLE


# ---------------------------------------------------------------------------
# Method assignment for scored mappings
# ---------------------------------------------------------------------------

SCORED_ROWS = [
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


def test_scored_markers_report_their_method_and_config_values(build_config):
    config = load_config(build_config(overrides={"strength_mappings.csv": SCORED_ROWS}))
    per_format = config.strength_mappings_for("fmt_alpha")

    explicit = resolve_strength(per_format, "explicit_label:heaven")
    assert explicit.strength_method == STRENGTH_METHOD_EXPLICIT
    assert (explicit.strength_label, explicit.strength_score) == ("holy_grail", 4)

    uppercase = resolve_strength(per_format, "uppercase")
    assert uppercase.strength_method == STRENGTH_METHOD_TYPOGRAPHIC
    assert (uppercase.strength_label, uppercase.strength_score) == ("very_high", 3)

    asterisk = resolve_strength(per_format, "asterisk_uppercase")
    assert asterisk.strength_method == STRENGTH_METHOD_TYPOGRAPHIC
    assert (asterisk.strength_label, asterisk.strength_score) == ("holy_grail", 4)


def test_plain_with_a_declared_score_is_typographic_marker(build_config):
    """A format may legitimately declare that plain text carries a score;
    the resolver applies the config row verbatim and reports the
    non-explicit method. It still never invents anything: the score comes
    from the config table."""
    rows = [
        {"input_source_format": "fmt_alpha", "marker_key": "plain",
         "source_value_or_marker": "plain text is a tier here",
         "normalized_label": "recommended", "normalized_score": "1",
         "mapping_confidence": "high"},
    ]
    config = load_config(build_config(overrides={"strength_mappings.csv": rows}))
    resolution = resolve_strength(config.strength_mappings_for("fmt_alpha"), "plain")
    assert resolution.mapped is True
    assert (resolution.strength_label, resolution.strength_score) == ("recommended", 1)
    assert resolution.strength_method == STRENGTH_METHOD_TYPOGRAPHIC
