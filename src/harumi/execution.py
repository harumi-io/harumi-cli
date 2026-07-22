"""Execution helpers: polling outputs and downloading artifacts.

The run is queued via `Client.execute_project` (git-ref based). This module
handles the async lifecycle *after* queuing: polling until a terminal status
is reached, listing outputs, and downloading zip artifacts.

The former `run_interactive` / `run_job` / S3-upload path has been removed
as part of the git-first pivot. If you need those, check git history.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from harumi.errors import HarumiError
from harumi.models import ExecutionOutput

if TYPE_CHECKING:
    from harumi.client import ApiClient

_DEFAULT_POLL_INTERVAL = 5.0
_TERMINAL_OUTPUT_STATUSES = {"finished", "completed", "failed", "timeout", "cancelled"}


def list_outputs(api: "ApiClient", project_id: str) -> list[ExecutionOutput]:
    response = api.request("GET", f"/notebooks/{project_id}/outputs")
    return [ExecutionOutput.model_validate(o) for o in response.json()]


def get_output(api: "ApiClient", project_id: str, output_id: str) -> ExecutionOutput:
    response = api.request("GET", f"/notebooks/{project_id}/outputs/{output_id}")
    return ExecutionOutput.model_validate(response.json())


def get_latest_output(api: "ApiClient", project_id: str) -> Optional[ExecutionOutput]:
    all_outputs = list_outputs(api, project_id)
    if not all_outputs:
        return None

    from datetime import datetime, timezone

    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def _started_at(output: ExecutionOutput) -> datetime:
        if output.started is None:
            return epoch
        return output.started if output.started.tzinfo else output.started.replace(tzinfo=timezone.utc)

    return max(all_outputs, key=_started_at)


def wait_for_output(
    api: "ApiClient",
    project_id: str,
    output_id: str,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: Optional[float] = None,
    on_poll: Optional[Callable[[ExecutionOutput], None]] = None,
) -> ExecutionOutput:
    """Block, polling until the run reaches a terminal status."""
    deadline = time.monotonic() + timeout if timeout else None

    while True:
        output = get_output(api, project_id, output_id)
        if on_poll is not None:
            on_poll(output)
        if output.finished:
            return output
        if deadline is not None and time.monotonic() >= deadline:
            raise HarumiError(
                f"Timed out after {timeout}s waiting for output {output_id} "
                f"(last status: {output.status!r})"
            )
        time.sleep(poll_interval)


def download_output(
    api: "ApiClient", project_id: str, output_id: str, dest_dir: Path
) -> Path:
    """Download an output's files as a zip into `dest_dir`. Returns the zip path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"output_{output_id}.zip"

    with api.stream(
        "GET", f"/notebooks/{project_id}/outputs/{output_id}/download", timeout=300.0
    ) as response:
        with open(dest_path, "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)

    return dest_path
