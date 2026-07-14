"""CP7 tests: structural checks on the GitHub Actions workflow.

GitHub is never invoked; the stdlib has no YAML parser and adding one for
this is unjustified, so these are plain-text structural assertions on the
committed workflow file (approved CP7 testing requirement F).
"""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"


def workflow_text() -> str:
    assert WORKFLOW.is_file(), "CI workflow file is missing"
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_triggers():
    text = workflow_text()
    assert "pull_request:" in text
    assert "- main" in text
    assert "- feature/data-foundation" in text


def test_workflow_pins_major_action_versions_and_python():
    text = workflow_text()
    assert "actions/checkout@v4" in text
    assert "actions/setup-python@v5" in text
    assert 'python-version: "3.12"' in text


def test_workflow_runs_the_required_command_sequence_in_order():
    text = workflow_text()
    commands = [
        "python -m compileall -q flavor_pairing scripts",
        "python -m pytest",
        "python scripts/regenerate_sample.py --check",
        "python scripts/validate_sample.py",
        "git diff --check",
    ]
    positions = [text.find(command) for command in commands]
    assert all(position != -1 for position in positions), positions
    assert positions == sorted(positions), "steps out of order"


def test_workflow_uses_no_secrets_or_private_paths():
    text = workflow_text()
    assert "secrets." not in text
    assert "imports_private" not in text
