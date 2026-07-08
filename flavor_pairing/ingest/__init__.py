"""Ingest layer: source-record identity and import-run tracking (CP3A scope).

Public API re-exported from :mod:`flavor_pairing.ingest.identity` and
:mod:`flavor_pairing.ingest.runs`. File-based raw ingestion from arbitrary
external files (``raw_ingest.py``), source adapters, rights enforcement, and
the ``import_to_raw.py`` CLI wrapper are out of scope for CP3A — see
``docs/DATA_FOUNDATION_PLAN.md`` §4/§10/§20-21.
"""

from flavor_pairing.ingest.identity import (
    RawRowContent,
    assign_source_record_ids,
    content_hash16,
)
from flavor_pairing.ingest.runs import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_IN_PROGRESS,
    RUN_STATUSES,
    CurrentVersion,
    RunOutcome,
    compute_input_file_hash,
    current_version,
    make_run_id,
    record_completed_run,
    record_failed_run,
    record_in_progress_run,
)

__all__ = [
    "RawRowContent",
    "assign_source_record_ids",
    "content_hash16",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_IN_PROGRESS",
    "RUN_STATUSES",
    "CurrentVersion",
    "RunOutcome",
    "compute_input_file_hash",
    "current_version",
    "make_run_id",
    "record_completed_run",
    "record_failed_run",
    "record_in_progress_run",
]
