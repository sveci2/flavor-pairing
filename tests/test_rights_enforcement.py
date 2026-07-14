"""CP3B tests: rights/path enforcement (docs/DATA_FOUNDATION_PLAN.md §4, §16
test_rights_enforcement.py).

All ledger roots here are fake, tmp_path-based paths — never the real
data/ledger/ or data/imports_private/ledger/. This suite never reads or
writes data/imports_private/.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from conftest import DEFAULT_CONFIG
from flavor_pairing.config.loaders import load_config
from flavor_pairing.ingest.raw_ingest import ingest_file
from flavor_pairing.ingest.rights import RightsError, is_public_safe, resolve_ledger_root
from flavor_pairing.store import db


def _write_csv(path: Path, header, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
    return path


@pytest.fixture
def conn():
    connection = db.open_database(":memory:")
    yield connection
    connection.close()


def _config_with_rights_status(build_config, conn, rights_status: str):
    sources = [dict(DEFAULT_CONFIG["sources.csv"][0])]
    sources[0]["rights_status"] = rights_status
    config_dir = build_config(overrides={"sources.csv": sources})
    conn.execute(
        "INSERT INTO sources (source_id, source_name, source_format, rights_status) "
        "VALUES (?, ?, ?, ?)",
        (sources[0]["source_id"], sources[0]["source_name"], sources[0]["source_format"], rights_status),
    )
    conn.commit()
    return load_config(config_dir)


# ---------------------------------------------------------------------------
# is_public_safe / resolve_ledger_root — unit level
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rights_status", ["project_owned_demo", "project_owned"])
def test_safe_rights_statuses_are_public_safe(rights_status):
    assert is_public_safe(rights_status) is True


@pytest.mark.parametrize(
    "rights_status",
    ["restricted", "unverified", "unverified_no_repository_licence_recorded", "unknown", "totally_new_status"],
)
def test_unsafe_rights_statuses_fail_closed(rights_status):
    assert is_public_safe(rights_status) is False


def test_resolve_ledger_root_refuses_when_roots_coincide(tmp_path):
    same_root = tmp_path / "only_root"
    with pytest.raises(RightsError):
        resolve_ledger_root("restricted", private_root=same_root, public_root=same_root)


def test_resolve_ledger_root_picks_public_for_safe_status(tmp_path):
    public_root = tmp_path / "public"
    private_root = tmp_path / "private"
    result = resolve_ledger_root("project_owned_demo", private_root=private_root, public_root=public_root)
    assert result == public_root


def test_resolve_ledger_root_picks_private_for_unsafe_status(tmp_path):
    public_root = tmp_path / "public"
    private_root = tmp_path / "private"
    result = resolve_ledger_root("restricted", private_root=private_root, public_root=public_root)
    assert result == private_root


# ---------------------------------------------------------------------------
# End-to-end via ingest_file
# ---------------------------------------------------------------------------

def test_safe_source_writes_to_public_root_only(build_config, conn, tmp_path):
    config = _config_with_rights_status(build_config, conn, "project_owned_demo")
    public_root = tmp_path / "public_ledger"
    private_root = tmp_path / "private_ledger"
    csv_path = _write_csv(tmp_path / "v1.csv", ["col_subject", "col_entry"], [["APPLE", "cinnamon"]])

    ingest_file(
        config, "src_alpha", csv_path, conn,
        private_ledger_root=private_root, public_ledger_root=public_root,
    )

    assert (public_root / "src_alpha" / "import_runs.csv").is_file()
    assert not private_root.exists()


@pytest.mark.parametrize(
    "rights_status", ["restricted", "unverified", "unverified_no_repository_licence_recorded", "unknown"]
)
def test_unsafe_source_writes_to_private_root_not_public(build_config, conn, tmp_path, rights_status):
    config = _config_with_rights_status(build_config, conn, rights_status)
    public_root = tmp_path / "public_ledger"
    private_root = tmp_path / "private_ledger"
    csv_path = _write_csv(tmp_path / "v1.csv", ["col_subject", "col_entry"], [["APPLE", "cinnamon"]])

    ingest_file(
        config, "src_alpha", csv_path, conn,
        private_ledger_root=private_root, public_ledger_root=public_root,
    )

    assert (private_root / "src_alpha" / "import_runs.csv").is_file()
    assert not public_root.exists()


def test_no_default_private_root_inside_flavor_pairing_package():
    """Neither ingest_file nor resolve_ledger_root default private_ledger_root/
    private_root — callers in the package must always supply it explicitly.
    """
    import inspect

    ingest_signature = inspect.signature(ingest_file)
    assert ingest_signature.parameters["private_ledger_root"].default is inspect.Parameter.empty

    resolve_signature = inspect.signature(resolve_ledger_root)
    assert resolve_signature.parameters["private_root"].default is inspect.Parameter.empty
