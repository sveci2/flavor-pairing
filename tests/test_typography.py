"""CP4 tests: typography/marker detection (docs/DATA_FOUNDATION_PLAN.md §11,
§16 test_typography.py).

The detector is a pure function; no database, config, or sample data is
involved. The closed marker-key set is asserted against the single source of
truth in flavor_pairing.config.loaders.
"""

from __future__ import annotations

import pytest

from flavor_pairing.config.loaders import EXPLICIT_LABEL_PREFIX, FIXED_MARKER_KEYS
from flavor_pairing.parse.typography import (
    MARKER_ASTERISK_UPPERCASE,
    MARKER_PLAIN,
    MARKER_RAW_ASTERISK_UPPERCASE,
    MARKER_RAW_UPPERCASE,
    MARKER_UPPERCASE,
    detect_marker,
)


def _is_in_closed_set(marker_key: str) -> bool:
    if marker_key in FIXED_MARKER_KEYS:
        return True
    return (
        marker_key.startswith(EXPLICIT_LABEL_PREFIX)
        and len(marker_key) > len(EXPLICIT_LABEL_PREFIX)
    )


# ---------------------------------------------------------------------------
# Explicit quality labels
# ---------------------------------------------------------------------------

def test_explicit_label_detected_from_quality():
    marker = detect_marker("walnut", "heaven")
    assert marker.marker_key == f"{EXPLICIT_LABEL_PREFIX}heaven"
    assert marker.strength_marker_raw == "heaven"
    assert marker.ambiguous is False


def test_explicit_label_strips_surrounding_whitespace_only():
    marker = detect_marker("walnut", "  heaven \t")
    assert marker.marker_key == f"{EXPLICIT_LABEL_PREFIX}heaven"
    assert marker.strength_marker_raw == "heaven"


def test_explicit_label_is_not_case_folded():
    # Approved decision: exact value, no case-folding — unexpected casing
    # becomes an unmapped key that surfaces for review downstream.
    marker = detect_marker("walnut", "Heaven")
    assert marker.marker_key == f"{EXPLICIT_LABEL_PREFIX}Heaven"
    assert marker.strength_marker_raw == "Heaven"


def test_explicit_label_takes_precedence_over_typography():
    marker = detect_marker("*WALNUT", "rec")
    assert marker.marker_key == f"{EXPLICIT_LABEL_PREFIX}rec"


@pytest.mark.parametrize("blank_quality", [None, "", "   ", "\t"])
def test_blank_quality_falls_through_to_typography(blank_quality):
    marker = detect_marker("WALNUT", blank_quality)
    assert marker.marker_key == MARKER_UPPERCASE


# ---------------------------------------------------------------------------
# Typographic markers
# ---------------------------------------------------------------------------

def test_asterisk_uppercase():
    marker = detect_marker("*CARAMEL", None)
    assert marker.marker_key == MARKER_ASTERISK_UPPERCASE
    assert marker.strength_marker_raw == MARKER_RAW_ASTERISK_UPPERCASE
    assert marker.ambiguous is False


def test_multiple_leading_asterisks_still_asterisk_uppercase():
    assert detect_marker("**CARAMEL", None).marker_key == MARKER_ASTERISK_UPPERCASE


def test_surrounding_whitespace_ignored_for_asterisk_uppercase():
    assert detect_marker("  *CARAMEL  ", None).marker_key == MARKER_ASTERISK_UPPERCASE


def test_uppercase():
    marker = detect_marker("WALNUT", None)
    assert marker.marker_key == MARKER_UPPERCASE
    assert marker.strength_marker_raw == MARKER_RAW_UPPERCASE
    assert marker.ambiguous is False


def test_uppercase_multiword_with_digits_and_punctuation():
    assert detect_marker("WALNUT OIL, TOASTED 100%", None).marker_key == MARKER_UPPERCASE


@pytest.mark.parametrize("entry", ["cinnamon", "Cinnamon", "cinnamon Sugar", "100", "-", ""])
def test_plain_for_lowercase_mixed_uncased_and_empty(entry):
    marker = detect_marker(entry, None)
    assert marker.marker_key == MARKER_PLAIN
    assert marker.strength_marker_raw is None
    assert marker.ambiguous is False


# ---------------------------------------------------------------------------
# Ambiguous typography (approved decision: plain bucket + visible for review)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", ["*Caramel", "*caramel", "*", "**"])
def test_asterisk_without_uppercase_is_plain_and_ambiguous(entry):
    marker = detect_marker(entry, None)
    assert marker.marker_key == MARKER_PLAIN
    assert marker.strength_marker_raw is None
    assert marker.ambiguous is True


# ---------------------------------------------------------------------------
# Closed-set property
# ---------------------------------------------------------------------------

def test_detector_emits_only_the_closed_marker_key_set():
    entries = [
        "", " ", "cinnamon", "Cinnamon", "WALNUT", "*CARAMEL", "**CARAMEL",
        "*Caramel", "*", "a + b", "Season: autumn", "e.g. berries",
        "berries, esp. strawberries", "100%", "CRÈME FRAÎCHE", "œuf",
        "*ŒUF", "x: y: z", "  MIXED case  ", "\tTAB\t",
    ]
    qualities = [None, "", "  ", "heaven", "Heaven", "REC", "new label", " x "]
    for entry in entries:
        for quality in qualities:
            marker = detect_marker(entry, quality)
            assert _is_in_closed_set(marker.marker_key), (
                f"detect_marker({entry!r}, {quality!r}) emitted marker_key "
                f"{marker.marker_key!r} outside the closed set"
            )
