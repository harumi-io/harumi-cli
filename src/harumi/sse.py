"""Parser for harumi-api's sandbox execution SSE stream.

The wire format (see harumi-api/src/api/sandbox/utils.py:sse_event and
harumi-api/src/api/sandbox/router.py:execute_code) is:

    event: <event_type>\\n
    data: <json>\\n
    \\n

i.e. the event *type* lives on the ``event:`` line and ``data:`` carries the
payload directly (NOT wrapped in a ``{"type": ..., "data": ...}`` envelope).
This mirrors frontend/src/services/sandbox.ts:parseSSEFrame, which is the
production-verified implementation.

Event payloads observed from the backend:
  - status              {execution_state: "busy"|"idle", kernel_id?}
  - stream               {name: "stdout"|"stderr", text: str}
  - result               {data: {<mime-type>: value, ...}, execution_count}
  - error                {ename, evalue, traceback: list[str]}
  - execution_complete   {execution_time_ms: int}
  - metrics              {cpu_count, cpu_used_percentage, memory_total_mib, memory_used_mib}
  - fs_change            {event_type: str, path: str}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Optional

from harumi.models import InteractiveResult

# Cap how much stdout/stderr we buffer, mirroring ai-solver's
# code_executor.py::_MAX_OUTPUT_CHARS so runaway print-heavy code can't
# blow up memory/output.
_MAX_OUTPUT_CHARS = 20_000


@dataclass
class SSEEvent:
    type: str
    data: dict[str, Any]


def parse_sse_frame(frame: str) -> Optional[SSEEvent]:
    """Parse a single ``event: ...\\ndata: ...`` frame into an SSEEvent."""
    event_type = ""
    data_line = ""
    for line in frame.split("\n"):
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_line = line[len("data:") :].strip()

    if not event_type or not data_line:
        return None

    try:
        data = json.loads(data_line)
    except (json.JSONDecodeError, ValueError):
        return None

    return SSEEvent(type=event_type, data=data)


def iter_sse_events(raw: str) -> Iterator[SSEEvent]:
    """Parse a complete SSE response body (non-streaming) into events."""
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = parse_sse_frame(block)
        if event is not None:
            yield event


class SSEStreamBuffer:
    """Incremental frame splitter for a live streaming response.

    Feed raw text chunks as they arrive over the wire via ``feed()``; it
    yields complete SSEEvents as soon as a full ``\\n\\n``-terminated frame
    is available, without waiting for the whole response.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[SSEEvent]:
        self._buffer += chunk
        events: list[SSEEvent] = []
        while "\n\n" in self._buffer:
            frame, self._buffer = self._buffer.split("\n\n", 1)
            frame = frame.strip()
            if not frame:
                continue
            event = parse_sse_frame(frame)
            if event is not None:
                events.append(event)
        return events


def _truncate(text: str, max_chars: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    marker = f"\n…[{omitted} chars omitted]…\n"
    half = max((max_chars - len(marker)) // 2, 0)
    return text[:half] + marker + text[len(text) - half :]


def aggregate_events(
    events: Iterable[SSEEvent],
    on_event: Optional[Callable[[SSEEvent], None]] = None,
) -> InteractiveResult:
    """Fold a sequence of SSEEvents into a single InteractiveResult.

    If ``on_event`` is given, it is called for every event as it is
    consumed (used by the CLI to print output live while it also builds up
    the final aggregate for the return value / exit code).
    """
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    results: list[dict[str, Any]] = []
    fs_changes: list[dict[str, Any]] = []
    error: Optional[dict[str, Any]] = None
    execution_time_ms: Optional[int] = None

    for event in events:
        if on_event is not None:
            on_event(event)

        if event.type == "stream":
            text = event.data.get("text", "")
            if event.data.get("name") == "stderr":
                stderr_parts.append(text)
            else:
                stdout_parts.append(text)

        elif event.type == "result":
            result_data = event.data.get("data", {})
            results.append(result_data)
            text_plain = result_data.get("text/plain", "")
            if text_plain:
                stdout_parts.append(text_plain)

        elif event.type == "error":
            error = {
                "ename": event.data.get("ename", ""),
                "evalue": event.data.get("evalue", ""),
                "traceback": event.data.get("traceback", []),
            }

        elif event.type == "execution_complete":
            execution_time_ms = event.data.get("execution_time_ms")

        elif event.type == "fs_change":
            fs_changes.append(event.data)

    return InteractiveResult(
        status="error" if error else "success",
        stdout=_truncate("".join(stdout_parts)),
        stderr=_truncate("".join(stderr_parts)),
        results=results,
        error=error,
        execution_time_ms=execution_time_ms,
        fs_changes=fs_changes,
    )
