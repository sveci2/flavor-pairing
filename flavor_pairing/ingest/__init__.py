"""Ingest layer: source-record identity, import-run tracking, and
mapping-driven raw ingestion (CP3A + CP3B scope).

Public API re-exported from :mod:`flavor_pairing.ingest.identity`,
:mod:`flavor_pairing.ingest.runs`, :mod:`flavor_pairing.ingest.raw_ingest`,
and :mod:`flavor_pairing.ingest.rights`. Source adapters for non-tabular
formats and the CLI wrapper's real private-path default remain outside this
package — see ``docs/DATA_FOUNDATION_PLAN.md`` §4/§10/§20-21.
"""

from flavor_pairing.ingest.identity import (
    RawRowContent,
    assign_source_record_ids,
    content_hash16,
)
from flavor_pairing.ingest.raw_ingest import IngestError, ingest_file, read_mapped_csv
from flavor_pairing.ingest.rights import (
    SAFE_RIGHTS_STATUSES,
    RightsError,
    is_public_safe,
    resolve_ledger_root,
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
    "IngestError",
    "ingest_file",
    "read_mapped_csv",
    "SAFE_RIGHTS_STATUSES",
    "RightsError",
    "is_public_safe",
    "resolve_ledger_root",
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
