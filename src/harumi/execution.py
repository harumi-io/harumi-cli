"""Execution logic: interactive (SSE) runs and async queued job runs.

Two distinct semantics mirroring the two paths harumi-api already exposes
(see harumi-api/src/api/sandbox/router.py and
harumi-api/src/api/notebooks/router.py:execute_notebook):

- ``run_interactive`` sends the *actual local code text* to the live sandbox
  kernel (POST /sandbox/{notebook_id}/execute) and streams back stdout,
  results, and errors in real time. This is the way to run your local file's
  code as-is.

- ``run_job`` queues the notebook's *currently configured* live version on
  the async Hatchet job queue (POST /notebooks/{notebook_id}/execute) for a
  long/heavy run, then (optionally) polls persisted outputs and downloads
  them. It does NOT push your local file's code into the notebook — the
  notebook-execute endpoint has no code parameter, it re-runs whatever is
  already stored as the notebook's live version. `run_job` uploads your
  local path to the notebook's project first, so any data files/modules the
  configured notebook reads are kept in sync; use `run_interactive` for
  "run exactly this local file" semantics.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from harumi.errors import HarumiError
from harumi.models import ExecutionOutput, InteractiveResult, NotebookExecuteResponse
from harumi.sse import SSEEvent, SSEStreamBuffer, aggregate_events

if TYPE_CHECKING:
    from harumi.client import ApiClient

_DEFAULT_INTERACTIVE_TIMEOUT = 600.0  # 10 minutes
_DEFAULT_POLL_INTERVAL = 5.0
_TERMINAL_OUTPUT_STATUSES = {"finished", "completed", "failed", "timeout", "cancelled"}


def run_interactive(
    api: "ApiClient",
    code: str,
    notebook_id: str,
    kernel_spec: Optional[str] = None,
    on_event: Optional[Callable[[SSEEvent], None]] = None,
    timeout: Optional[float] = None,
) -> InteractiveResult:
    """Run `code` in the notebook's live sandbox kernel and stream results.

    `on_event` (if given) is invoked for every SSE event as it arrives, so
    callers (e.g. the CLI) can print output live while this function also
    returns the aggregated InteractiveResult once the stream ends.
    """
    body: dict[str, Any] = {"code": code}
    if kernel_spec:
        body["kernel_spec"] = kernel_spec

    buffer = SSEStreamBuffer()
    events: list[SSEEvent] = []

    with api.stream(
        "POST",
        f"/sandbox/{notebook_id}/execute",
        json=body,
        timeout=timeout or _DEFAULT_INTERACTIVE_TIMEOUT,
    ) as response:
        for chunk in response.iter_text():
            events.extend(buffer.feed(chunk))

    return aggregate_events(events, on_event=on_event)


def resolve_project_id(api: "ApiClient", notebook_id: str) -> str:
    """Look up the project a notebook belongs to via GET /projects/by-notebook/{id}."""
    response = api.request("GET", f"/projects/by-notebook/{notebook_id}")
    projects = response.json()
    if not projects:
        raise HarumiError(
            f"Notebook {notebook_id} is not linked to any project; "
            "pass --project explicitly."
        )
    return projects[0]["id"]


def run_job(
    api: "ApiClient",
    path: Optional[Path],
    notebook_id: str,
    project_id: Optional[str] = None,
    kernel_spec: Optional[str] = None,
    scenario_id: Optional[str] = None,
    scenario_name: Optional[str] = None,
    output_format: Optional[str] = None,
    email_to: Optional[str] = None,
    watch: bool = False,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: Optional[float] = None,
    output_dir: Optional[Path] = None,
) -> NotebookExecuteResponse:
    """Queue an async run of `notebook_id` on the infra's job queue.

    If `path` is given, it is uploaded to the notebook's project first (see
    module docstring for what job mode does and does not do with that code).
    If `watch` is True, blocks polling outputs until the run reaches a
    terminal status, downloading artifacts to `output_dir` if given.
    """
    if path is not None:
        resolved_project_id = project_id or resolve_project_id(api, notebook_id)
        from harumi.files import upload_path

        upload_path(api, resolved_project_id, path)

    body: dict[str, Any] = {}
    if scenario_id:
        body["scenario_id"] = scenario_id
    if scenario_name:
        body["scenario_name"] = scenario_name
    if output_format:
        body["output_format"] = output_format
    if email_to:
        body["email_to"] = email_to
    if kernel_spec:
        body["kernel_spec"] = kernel_spec

    response = api.request("POST", f"/notebooks/{notebook_id}/execute", json=body)
    result = NotebookExecuteResponse.model_validate(response.json())

    if watch and result.output_id:
        output = wait_for_output(
            api, notebook_id, result.output_id, poll_interval=poll_interval, timeout=timeout
        )
        if output_dir and output.succeeded:
            download_output(api, notebook_id, output.id, output_dir)

    return result


def list_outputs(api: "ApiClient", notebook_id: str) -> list[ExecutionOutput]:
    response = api.request("GET", f"/notebooks/{notebook_id}/outputs")
    return [ExecutionOutput.model_validate(o) for o in response.json()]


def get_output(api: "ApiClient", notebook_id: str, output_id: str) -> ExecutionOutput:
    response = api.request("GET", f"/notebooks/{notebook_id}/outputs/{output_id}")
    return ExecutionOutput.model_validate(response.json())


def get_latest_output(api: "ApiClient", notebook_id: str) -> Optional[ExecutionOutput]:
    outputs = list_outputs(api, notebook_id)
    if not outputs:
        return None

    from datetime import datetime, timezone

    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def _started_at(output: ExecutionOutput) -> datetime:
        if output.started is None:
            return epoch
        return output.started if output.started.tzinfo else output.started.replace(tzinfo=timezone.utc)

    return max(outputs, key=_started_at)


def wait_for_output(
    api: "ApiClient",
    notebook_id: str,
    output_id: str,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: Optional[float] = None,
    on_poll: Optional[Callable[[ExecutionOutput], None]] = None,
) -> ExecutionOutput:
    """Block, polling GET /notebooks/{id}/outputs/{output_id}, until the run
    reaches a terminal status (finished/completed/failed/timeout/cancelled).
    """
    deadline = time.monotonic() + timeout if timeout else None

    while True:
        output = get_output(api, notebook_id, output_id)
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
    api: "ApiClient", notebook_id: str, output_id: str, dest_dir: Path
) -> Path:
    """Download an output's files as a zip into `dest_dir`. Returns the zip path."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"output_{output_id}.zip"

    with api.stream(
        "GET", f"/notebooks/{notebook_id}/outputs/{output_id}/download", timeout=300.0
    ) as response:
        with open(dest_path, "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)

    return dest_path
