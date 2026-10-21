"""Execution helpers: polling runs and downloading run output artifacts.

A run is queued via `Client.execute_project` (git-ref based). This module
handles the lifecycle *after* queuing: polling a run until it reaches a
terminal status, reading its structured output, and downloading its output
artifacts as a zip.

The former `run_interactive` / `run_job` / S3-upload path has been removed
as part of the git-first pivot. If you need those, check git history.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from harumi.errors import ApiError, ApiUnreachableError, HarumiError
from harumi.models import ProjectRun

if TYPE_CHECKING:
    from harumi.client import ApiClient

_DEFAULT_POLL_INTERVAL = 5.0

# A watch can last an hour. Over that time one poll in a few thousand meets a dropped connection or a
# gateway hiccup, and that says nothing about the run, which goes on without us. Only a failure of the
# kind that passes (no answer, or a 5xx) is ridden out, and only for a few polls in a row.
DEFAULT_MAX_POLL_FAILURES = 4


class WatchLostError(HarumiError):
    """Polling a run failed several times in a row. The run itself is not known to have failed."""

    def __init__(self, run_id: str, last: Exception) -> None:
        super().__init__(f"Lost touch with harumi-api while waiting for run {run_id}.")
        self.run_id = run_id
        self.last = last


def _is_transient(exc: ApiError) -> bool:
    return isinstance(exc, ApiUnreachableError) or exc.status_code in (502, 503, 504)


def list_runs(api: "ApiClient", project_id: str) -> list[ProjectRun]:
    response = api.request("GET", f"/projects/{project_id}/runs")
    return [ProjectRun.model_validate(r) for r in response.json().get("runs", [])]


def get_run(api: "ApiClient", project_id: str, run_id: str) -> ProjectRun:
    response = api.request("GET", f"/projects/{project_id}/runs/{run_id}")
    return ProjectRun.model_validate(response.json())


def cancel_run(api: "ApiClient", project_id: str, run_id: str) -> ProjectRun:
    response = api.request("POST", f"/projects/{project_id}/runs/{run_id}/cancel")
    return ProjectRun.model_validate(response.json())


def get_latest_run(api: "ApiClient", project_id: str) -> Optional[ProjectRun]:
    """Most recent run, or None. The list endpoint is already newest-first."""
    runs = list_runs(api, project_id)
    return runs[0] if runs else None


def wait_for_run(
    api: "ApiClient",
    project_id: str,
    run_id: str,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: Optional[float] = None,
    on_poll: Optional[Callable[[ProjectRun], None]] = None,
    max_poll_failures: int = 0,
    on_poll_failure: Optional[Callable[[ApiError], None]] = None,
) -> ProjectRun:
    """Block, polling until the run reaches a terminal status.

    With `max_poll_failures` > 0, that many transient poll failures in a row are ridden out
    (`on_poll_failure` hears each one); the next raises `WatchLostError`. Any other error, and the
    default of 0, raise at once as before.
    """
    deadline = time.monotonic() + timeout if timeout else None
    failures = 0

    while True:
        try:
            run = get_run(api, project_id, run_id)
        except ApiError as exc:
            if not max_poll_failures or not _is_transient(exc):
                raise
            failures += 1
            if on_poll_failure is not None:
                on_poll_failure(exc)
            if failures > max_poll_failures:
                raise WatchLostError(run_id, exc) from exc
            time.sleep(poll_interval)
            continue
        failures = 0
        if on_poll is not None:
            on_poll(run)
        if run.finished:
            return run
        if deadline is not None and time.monotonic() >= deadline:
            raise HarumiError(
                f"Timed out after {timeout}s waiting for run {run_id} "
                f"(last status: {run.status!r})"
            )
        time.sleep(poll_interval)


def get_run_output(api: "ApiClient", project_id: str, run_id: str) -> dict[str, Any]:
    """The run's parsed structured output (`GET .../runs/{run_id}/output`).

    Reads from S3 (current runs) or Gitea (runs recorded before the S3
    migration) — the API resolves that transparently. Raises `HarumiError` on
    a 404 (no run, no output pointer, or no `output.json`), so callers don't
    need to special-case the status code themselves.
    """
    from harumi.errors import ApiError

    try:
        response = api.request("GET", f"/projects/{project_id}/runs/{run_id}/output")
    except ApiError as exc:
        if exc.status_code == 404:
            raise HarumiError(f"Run {run_id!r} has no output to read.") from exc
        raise
    return response.json()


def download_run_output(
    api: "ApiClient", project_id: str, run: ProjectRun, dest_dir: Path
) -> Path:
    """Download every artifact a run wrote to `[output].dir` as one zip into `dest_dir`.

    Proxied through the API (`GET .../runs/{run_id}/output/archive`), which
    reads from S3 (current runs) or Gitea (runs recorded before the S3
    migration) transparently.
    """
    if not run.output_url:
        raise HarumiError(f"Run {run.id!r} has no output to download.")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"run_{run.id}_output.zip"

    with api.stream(
        "GET",
        f"/projects/{project_id}/runs/{run.id}/output/archive",
        timeout=300.0,
    ) as response:
        with open(dest_path, "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)

    return dest_path
