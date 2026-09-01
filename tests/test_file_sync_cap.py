"""Unit tests for harumi.file_sync_cap.check_project_sync_cap — the pure
validator `harumi files put` uses before uploading."""

from harumi.file_sync_cap import (
    MAX_PROJECT_SYNC_FILES,
    MAX_PROJECT_SYNC_TOTAL_BYTES,
    check_project_sync_cap,
)


def test_allows_upload_well_within_both_ceilings():
    assert check_project_sync_cap([100], [200]) is None


def test_rejects_when_file_count_would_exceed_the_limit():
    existing = [1] * (MAX_PROJECT_SYNC_FILES - 1)
    violation = check_project_sync_cap(existing, [1, 1])
    assert violation is not None
    assert violation.reason == "file-count"
    assert violation.would_be == MAX_PROJECT_SYNC_FILES + 1
    assert violation.limit == MAX_PROJECT_SYNC_FILES


def test_rejects_when_total_bytes_would_exceed_the_limit():
    violation = check_project_sync_cap(
        [MAX_PROJECT_SYNC_TOTAL_BYTES - 10], [20]
    )
    assert violation is not None
    assert violation.reason == "total-bytes"
    assert violation.would_be == MAX_PROJECT_SYNC_TOTAL_BYTES + 10
    assert violation.limit == MAX_PROJECT_SYNC_TOTAL_BYTES


def test_allows_the_boundary_where_existing_files_exactly_fill_the_cap():
    assert check_project_sync_cap([MAX_PROJECT_SYNC_TOTAL_BYTES], []) is None
    assert check_project_sync_cap([1] * MAX_PROJECT_SYNC_FILES, []) is None


def test_rejects_a_single_incoming_file_that_alone_pushes_past_the_cap():
    existing = [1] * MAX_PROJECT_SYNC_FILES
    violation = check_project_sync_cap(existing, [1])
    assert violation is not None
    assert violation.reason == "file-count"
    assert violation.would_be == MAX_PROJECT_SYNC_FILES + 1
