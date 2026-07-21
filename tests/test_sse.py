"""Offline unit tests for the SSE frame parser and event aggregator."""

from __future__ import annotations

from harumi.sse import SSEStreamBuffer, aggregate_events, iter_sse_events, parse_sse_frame


def _frame(event_type: str, data: dict) -> str:
    import json

    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def test_parse_sse_frame_basic():
    event = parse_sse_frame('event: stream\ndata: {"name": "stdout", "text": "hi"}')
    assert event is not None
    assert event.type == "stream"
    assert event.data == {"name": "stdout", "text": "hi"}


def test_parse_sse_frame_missing_parts_returns_none():
    assert parse_sse_frame("event: stream") is None
    assert parse_sse_frame('data: {"a": 1}') is None
    assert parse_sse_frame("") is None


def test_parse_sse_frame_invalid_json_returns_none():
    assert parse_sse_frame("event: stream\ndata: not-json") is None


def test_iter_sse_events_multiple_frames():
    raw = (
        _frame("status", {"execution_state": "busy"})
        + _frame("stream", {"name": "stdout", "text": "hello\n"})
        + _frame("execution_complete", {"execution_time_ms": 42})
    )
    events = list(iter_sse_events(raw))
    assert [e.type for e in events] == ["status", "stream", "execution_complete"]
    assert events[1].data["text"] == "hello\n"
    assert events[2].data["execution_time_ms"] == 42


def test_sse_stream_buffer_handles_partial_chunks():
    buffer = SSEStreamBuffer()
    full = _frame("stream", {"name": "stdout", "text": "chunked"})

    # Feed byte-by-byte-ish to simulate a slow network stream.
    mid = len(full) // 2
    events_first = buffer.feed(full[:mid])
    assert events_first == []  # incomplete frame, nothing yielded yet

    events_second = buffer.feed(full[mid:])
    assert len(events_second) == 1
    assert events_second[0].type == "stream"
    assert events_second[0].data["text"] == "chunked"


def test_sse_stream_buffer_across_many_small_chunks():
    buffer = SSEStreamBuffer()
    full = _frame("stream", {"name": "stdout", "text": "a"}) + _frame(
        "execution_complete", {"execution_time_ms": 5}
    )

    collected = []
    for i in range(0, len(full), 3):
        collected.extend(buffer.feed(full[i : i + 3]))

    assert [e.type for e in collected] == ["stream", "execution_complete"]


def test_aggregate_events_success():
    events = list(
        iter_sse_events(
            _frame("stream", {"name": "stdout", "text": "hello "})
            + _frame("stream", {"name": "stdout", "text": "world"})
            + _frame("stream", {"name": "stderr", "text": "warn!"})
            + _frame("execution_complete", {"execution_time_ms": 100})
        )
    )
    result = aggregate_events(events)

    assert result.ok
    assert result.status == "success"
    assert result.stdout == "hello world"
    assert result.stderr == "warn!"
    assert result.execution_time_ms == 100
    assert result.error is None


def test_aggregate_events_error():
    events = list(
        iter_sse_events(
            _frame("stream", {"name": "stdout", "text": "before crash\n"})
            + _frame(
                "error",
                {
                    "ename": "ValueError",
                    "evalue": "boom",
                    "traceback": ["line1", "line2"],
                },
            )
        )
    )
    result = aggregate_events(events)

    assert not result.ok
    assert result.status == "error"
    assert result.error == {
        "ename": "ValueError",
        "evalue": "boom",
        "traceback": ["line1", "line2"],
    }


def test_aggregate_events_result_and_fs_change():
    events = list(
        iter_sse_events(
            _frame("result", {"data": {"text/plain": "42", "image/png": "abc123"}})
            + _frame("fs_change", {"event_type": "created", "path": "out.csv"})
        )
    )
    result = aggregate_events(events)

    assert result.results == [{"text/plain": "42", "image/png": "abc123"}]
    assert "42" in result.stdout
    assert result.fs_changes == [{"event_type": "created", "path": "out.csv"}]


def test_aggregate_events_on_event_callback_invoked_for_each_event():
    events = list(
        iter_sse_events(
            _frame("stream", {"name": "stdout", "text": "one"})
            + _frame("stream", {"name": "stdout", "text": "two"})
        )
    )
    seen = []
    aggregate_events(events, on_event=lambda e: seen.append(e.type))
    assert seen == ["stream", "stream"]


def test_aggregate_events_truncates_long_output():
    long_text = "x" * 50_000
    events = list(iter_sse_events(_frame("stream", {"name": "stdout", "text": long_text})))
    result = aggregate_events(events)
    assert len(result.stdout) < len(long_text)
    assert "chars omitted" in result.stdout
