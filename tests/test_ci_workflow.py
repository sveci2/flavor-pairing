"""CP7/CP9 tests: structural checks on the GitHub Actions workflow.

GitHub is never invoked; the stdlib has no YAML parser and adding one for
this is unjustified, so these are plain-text structural assertions on the
committed workflow file (approved CP7 testing requirement F, extended for
the CP9 Python matrix and API layer).
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
    # Pushes build only main; feature branches are covered by pull_request
    # (a push trigger for them would duplicate CI runs on open PRs).
    assert "feature/" not in text


def test_workflow_pins_major_action_versions_and_python_matrix():
    text = workflow_text()
    assert "actions/checkout@v4" in text
    assert "actions/setup-python@v5" in text
    assert "python-version: ${{ matrix.python-version }}" in text
    assert '- "3.9"' in text  # the supported floor
    assert '- "3.12"' in text


def test_workflow_caches_pip_on_both_dependency_files():
    text = workflow_text()
    assert "cache: pip" in text
    assert "cache-dependency-path: |" in text
    assert "requirements-dev.txt" in text
    assert "requirements-api.txt" in text


def test_workflow_runs_the_required_command_sequence_in_order():
    text = workflow_text()
    commands = [
        "python -m pip install --upgrade pip",
        "python -m pip install -r requirements-dev.txt",
        "python -m compileall -q flavor_pairing scripts flavor_pairing_api",
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
