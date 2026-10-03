"""Exceptions shared across the harumi package."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import httpx


class HarumiError(Exception):
    """Base class for all harumi SDK/CLI errors."""


class NotAuthenticatedError(HarumiError):
    """Raised when an operation requires login but no valid session exists."""

    def __init__(self, message: str = "Not logged in. Run `harumi login` first.") -> None:
        super().__init__(message)


class ApiError(HarumiError):
    """Raised when harumi-api returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"harumi-api returned HTTP {status_code}: {detail}")


class ApiUnreachableError(ApiError):
    """harumi-api could not be reached at all (status 0). Distinct from other `ApiError(0, ...)`
    (e.g. a failed presigned S3 transfer) so the CLI only points at the Harumi status page
    when the Harumi API itself is the thing that is down."""

    def __init__(self, detail: str) -> None:
        super().__init__(0, detail)


@contextmanager
def transport_errors_as_api_error() -> Iterator[None]:
    """Report a request that never got an answer (refused connection, DNS failure,
    timeout) as `ApiUnreachableError`.

    Without this a bare `httpx` exception escapes `_handle_errors` as a raw
    traceback. Status 0 means "no HTTP response", which is what tells the CLI to
    point the user at the status page.
    """
    try:
        yield
    except httpx.HTTPError as exc:
        raise ApiUnreachableError(f"Could not reach harumi-api: {exc}") from exc


class ExecutionError(HarumiError):
    """Raised when a run fails (interactive error event or failed job)."""
