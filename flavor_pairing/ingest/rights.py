"""Rights/path enforcement for import-run ledger writes (CP3B).

Decides, per source, whether its ledger may land in the committed public
root or must be routed to a private root instead. This module makes the
decision; it never performs any I/O itself (that stays in
:mod:`flavor_pairing.store.ledger` and :mod:`flavor_pairing.ingest.raw_ingest`).

No default private root is defined anywhere in this package. Every caller
must supply ``private_root`` explicitly — the real on-disk gitignored
private-source ledger path documented in docs/SCHEMA.md §11 is defined only
in the CLI wrapper script, which lives outside this package. This module is
never imported with that real path in the test suite; tests always pass a
``tmp_path``-based fake.
"""

from __future__ import annotations

from pathlib import Path

from flavor_pairing.store import ledger

__all__ = [
    "SAFE_RIGHTS_STATUSES",
    "RightsError",
    "is_public_safe",
    "resolve_ledger_root",
]


class RightsError(Exception):
    """A source's rights_status forbids the requested/resolved ledger path."""


# Fails closed: only these two values route to the committed public ledger
# root. Every other value — including real sample data's 'unverified' and
# 'unverified_no_repository_licence_recorded', 'restricted', 'unknown', and
# any value not yet invented — is treated as not public-safe by default.
SAFE_RIGHTS_STATUSES = frozenset({"project_owned_demo", "project_owned"})


def is_public_safe(rights_status: str) -> bool:
    """Whether a source's rights_status permits writing to the public ledger root."""
    return rights_status in SAFE_RIGHTS_STATUSES


def resolve_ledger_root(
    rights_status: str,
    *,
    private_root: Path,
    public_root: Path = ledger.DEFAULT_LEDGER_ROOT,
) -> Path:
    """The ledger root permitted for a source with this ``rights_status``.

    Public-safe statuses (``SAFE_RIGHTS_STATUSES``) get ``public_root``;
    everything else gets ``private_root``. Refuses (``RightsError``) if the
    two roots would coincide for a not-safe status, since that would
    silently defeat the enforcement rather than actually separating the
    outputs.
    """
    if is_public_safe(rights_status):
        return public_root
    if Path(private_root) == Path(public_root):
        raise RightsError(
            f"rights_status {rights_status!r} is not public-safe, but private_root "
            f"equals public_root ({public_root}); a distinct private_root is required "
            f"so restricted output never lands in the committed public ledger"
        )
    return private_root
