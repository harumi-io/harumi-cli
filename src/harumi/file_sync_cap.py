"""The run-time sync cap `harumi files put` guards against.

Mirrors harumi-platform's `project-file-sync-cap.ts` and harumi-api's
`MAX_SYNC_FILES` / `MAX_SYNC_TOTAL_BYTES` (`src/libs/project_files.py:31-32`
in harumi-api) — that file is the source of truth, keep these in step with
it. The backend raises over these limits when a run syncs a project's files
into `inputs/`, so checking here turns that into an upload-time refusal
instead of every subsequent run failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

MAX_PROJECT_SYNC_FILES = 500
MAX_PROJECT_SYNC_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB


@dataclass(frozen=True)
class SyncCapViolation:
    reason: Literal["file-count", "total-bytes"]
    would_be: int
    limit: int


def check_project_sync_cap(
    existing_sizes: Sequence[int], incoming_sizes: Sequence[int]
) -> Optional[SyncCapViolation]:
    """Checks an incoming upload batch against the files already stored —
    the cap applies to the whole project prefix, not just this batch.
    Returns the first violation, or `None` if the upload is within both
    ceilings.
    """
    would_be_file_count = len(existing_sizes) + len(incoming_sizes)
    if would_be_file_count > MAX_PROJECT_SYNC_FILES:
        return SyncCapViolation(
            reason="file-count",
            would_be=would_be_file_count,
            limit=MAX_PROJECT_SYNC_FILES,
        )

    would_be_total_bytes = sum(existing_sizes) + sum(incoming_sizes)
    if would_be_total_bytes > MAX_PROJECT_SYNC_TOTAL_BYTES:
        return SyncCapViolation(
            reason="total-bytes",
            would_be=would_be_total_bytes,
            limit=MAX_PROJECT_SYNC_TOTAL_BYTES,
        )

    return None
