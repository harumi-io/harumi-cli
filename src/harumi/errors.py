"""Exceptions shared across the harumi package."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

import httpx


class HarumiError(Exception):
    """Base class for all harumi SDK/CLI errors."""


class NotAuthenticatedError(HarumiError):
    """There is no usable login: none on file, or the one on file has expired.

    The message is the whole point: it says what happened and the one command
    that fixes it, because this is what a person sees the first time they run
    anything, and again whenever a session lapses.
    """

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        expired: bool = False,
        environment: Optional[str] = None,
    ) -> None:
        self.expired = expired
        self.environment = environment
        super().__init__(message or self.default_message(expired=expired, environment=environment))

    @staticmethod
    def default_message(*, expired: bool, environment: Optional[str]) -> str:
        # Production is what nearly everyone uses, so only name an environment
        # when it is not that one.
        where = f" on {environment}" if environment and environment != "production" else ""
        if expired:
            return f"Your Harumi session{where} has expired. Run `harumi login` to sign in again."
        return (
            f"You're not logged in{where}. Run `harumi login` to sign in "
            "(new to Harumi? `harumi login --signup` creates your account)."
        )


class ApiError(HarumiError):
    """Raised when harumi-api returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str, code: str | None = None) -> None:
        self.status_code = status_code
        self.detail = detail
        # harumi-api's denial reason when it sent one (402s carry it).
        self.code = code
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
