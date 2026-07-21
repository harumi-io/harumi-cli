"""Exceptions shared across the harumi package."""

from __future__ import annotations


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


class ExecutionError(HarumiError):
    """Raised when a run fails (interactive error event or failed job)."""
