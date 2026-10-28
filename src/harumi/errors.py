"""Exceptions shared across the harumi package."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator, Optional

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

    def __init__(
        self,
        status_code: int,
        detail: str,
        code: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        # harumi-api's denial reason when it sent one (402s carry it).
        self.code = code
        # Seconds the server asked us to wait (`Retry-After` on a 429), if it said.
        self.retry_after = retry_after
        super().__init__(f"harumi-api returned HTTP {status_code}: {detail}")


class SessionRefreshError(ApiError):
    """Renewing the login failed for a reason that does not mean "the session is over"
    (a firewall's 403, a server error, a rate limit).

    A separate type so callers can tell it from the error of whatever command was
    running: a refresh that fails while `harumi run` executes is not the project's fault.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(status_code, detail)
        self.args = (f"Could not renew your login: harumi-api returned HTTP {status_code}: {detail}",)

    def __str__(self) -> str:
        return self.args[0]


class ApiUnreachableError(ApiError):
    """harumi-api could not be reached at all (status 0). Distinct from other `ApiError(0, ...)`
    (e.g. a failed presigned S3 transfer) so the CLI only points at the Harumi status page
    when the Harumi API itself is the thing that is down.

    `kind` says what happened, because the advice differs: `refused`, `dns`, `timeout`, `tls`,
    `dropped`, or `other`. `may_have_succeeded` is True when the request changes something and
    got far enough to have been received, so repeating it could do it twice.
    """

    def __init__(self, detail: str, *, kind: str = "other", may_have_succeeded: bool = False) -> None:
        super().__init__(0, detail)
        self.kind = kind
        self.may_have_succeeded = may_have_succeeded


class UnexpectedReplyError(HarumiError, ValueError):
    """harumi-api (or something answering for it) replied with something this CLI cannot read:
    a web page where JSON was expected, or JSON of a shape it does not know. `html` is True for
    the first, where the usual cause is a login or filter page on the network.

    A `ValueError` as well, because the `JSONDecodeError` it replaces was one: code that already
    caught that (a listing that may lag, say) keeps working."""

    def __init__(self, message: str, *, html: bool = False) -> None:
        super().__init__(message)
        self.html = html


_DNS_MARKERS = (
    "nodename nor servname", "name or service not known", "temporary failure in name resolution",
    "getaddrinfo failed", "no address associated", "name resolution",
)
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def classify_transport_error(exc: httpx.HTTPError) -> tuple[str, bool]:
    """(kind, may_have_succeeded) for a request that never got an answer.

    httpx reports a refused connection, a DNS failure and a TLS failure all as `ConnectError`,
    so those are told apart by their text; anything not recognised is `other`, which the CLI
    reports with the original wording rather than guessing.
    """
    text = str(exc).lower()
    connecting = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
    if isinstance(exc, httpx.TimeoutException):
        kind = "timeout"
    elif isinstance(exc, httpx.ConnectError):
        if "certificate" in text or "ssl" in text or "tls" in text:
            kind = "tls"
        elif any(marker in text for marker in _DNS_MARKERS):
            kind = "dns"
        elif "refused" in text:
            kind = "refused"
        else:
            kind = "other"
    elif isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        kind = "dropped"
    else:
        kind = "other"
    try:
        method = exc.request.method
    except RuntimeError:  # httpx raises this when the exception has no request attached
        method = ""
    # A connection that never opened cannot have delivered the request; one that dropped might have.
    return kind, (method in _WRITE_METHODS and not connecting)


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
        kind, may_have = classify_transport_error(exc)
        raise ApiUnreachableError(f"Could not reach harumi-api: {exc}", kind=kind, may_have_succeeded=may_have) from exc


class ExecutionError(HarumiError):
    """Raised when a run fails (interactive error event or failed job)."""


# -- turning an HTTP reply into an error ---------------------------------------------------------------------

_HIDDEN_LOC = {"body", "query", "path", "header", "cookie"}


def _detail_text(detail: object) -> tuple[str, Optional[str]]:
    """The message harumi-api sent, as text, and its machine-readable `code` if it sent one.

    `detail` is a string for most errors, but a dict for a second-factor refusal
    (`{"code": "mfa_required", "message": ...}`) and a list for a validation error. Printing
    those as they come shows a Python repr, and the validation list echoes back the input that
    failed (`input`), which can be a password or a token. Only the field and the reason are kept.
    """
    if isinstance(detail, dict):
        code = detail.get("code") if isinstance(detail.get("code"), str) else None
        message = detail.get("message") or detail.get("detail")
        return (str(message) if message else str(code or "")), code
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            where = ".".join(str(x) for x in item.get("loc", []) if str(x) not in _HIDDEN_LOC)
            msg = str(item.get("msg", "invalid value"))
            parts.append(f"{where}: {msg}" if where else msg)
        return "; ".join(parts), None
    return str(detail), None


_MAX_DETAIL = 300  # characters of a server's error text shown; a long one is cut, not dumped
_HTML_START = re.compile(r"^\s*(<!doctype\s+html|<html|<head|<body|<\?xml)", re.I)


def _looks_like_html(response: httpx.Response, text: str) -> bool:
    return "html" in response.headers.get("content-type", "").lower() or bool(_HTML_START.match(text or ""))


def describe_non_api_reply(status_code: int) -> str:
    """What to say about a web page that came back instead of harumi-api's answer.

    Its text is not shown: it is a proxy's, a firewall's or a captive portal's, it can be pages
    long, and a person cannot act on markup. The status code is what identifies it.
    """
    return (
        f"Got a web page (HTTP {status_code}) instead of an answer from Harumi. "
        "Something between you and harumi-api replied: a proxy, a firewall, a login page for this network, "
        "or an outage page."
    )


def _retry_after(response: httpx.Response) -> Optional[int]:
    """The seconds a 429 asks us to wait, or None if it did not say (or said something unusable)."""
    value = response.headers.get("retry-after", "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


def raise_for_status(response: httpx.Response, streamed: bool = False) -> None:
    """Raise an `ApiError` for an error status, with a readable message; return for a success."""
    if response.status_code >= 400:
        detail: object = "" if streamed else response.text
        code = None
        is_page = False
        if not streamed:
            try:
                body = response.json()
                detail = body.get("detail", detail)
                # harumi-api sends a machine-readable reason beside every 402
                # (`no_subscription`, `weekly_limit_reached`, ...).
                if isinstance(body.get("code"), str):
                    code = body["code"]
            except Exception:
                is_page = _looks_like_html(response, str(detail))
        text, detail_code = _detail_text(detail)
        code = code or detail_code
        if is_page:
            text, code = describe_non_api_reply(response.status_code), "non_api_reply"
        elif len(text) > _MAX_DETAIL:
            text = text[:_MAX_DETAIL].rstrip() + "…"
        raise ApiError(
            response.status_code,
            text or response.reason_phrase,
            code=code,
            retry_after=_retry_after(response) if response.status_code == 429 else None,
        )


def guard_json(response: httpx.Response) -> httpx.Response:
    """Make `response.json()` fail with an `UnexpectedReplyError` instead of a JSONDecodeError.

    A 200 is not always harumi-api's: a captive portal or a filtering proxy answers 200 with a web
    page. There are dozens of `.json()` calls on API replies, so the response itself is guarded
    once, where it is received, rather than each call site.
    """
    original = response.json

    def json(**kwargs: Any) -> Any:
        try:
            return original(**kwargs)
        except ValueError:
            page = _looks_like_html(response, response.text)
            raise UnexpectedReplyError(
                "Got a web page instead of an answer from Harumi: something on this network replied for it "
                "(a login or filter page, or a proxy)."
                if page
                else "Harumi's reply isn't in the form this version of the CLI expects.",
                html=page,
            ) from None

    response.json = json  # type: ignore[method-assign]
    return response
