"""Supabase OTP login flow against harumi-api's /users endpoints.

Mirrors the flow harumi-api itself exposes (src/api/users/router.py):
  POST /users/sign_up      {email}          -> creates the account, then sends a code
  POST /users/otp          {email}          -> sends a one-time code by email
                                                (existing accounts only)
  POST /users/otp/verify   {email, token}   -> LoggedUser (access + refresh token)
  POST /users/refresh      {refresh_token}  -> LoggedUser (new tokens)

`/users/otp` calls Supabase's sign_in_with_otp with `should_create_user:
False`, so it 422s with "Signups not allowed for otp" for an email with no
existing Supabase account — use `request_otp(..., sign_up=True)` (`/users/sign_up`)
to create the account first.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx

from harumi.config import Config, clear_credentials, credentials_lock, load_credentials, save_credentials
from harumi.errors import (
    ApiError,
    NotAuthenticatedError,
    SessionRefreshError,
    guard_json,
    raise_for_status as _raise_for_status,
    transport_errors_as_api_error,
)
from harumi.models import LoggedUser

# Refresh proactively if the access token expires within this many seconds,
# so we don't get a surprise 401 mid-upload/mid-stream.
_EXPIRY_SKEW_SECONDS = 60


def request_otp(
    config: Config,
    email: str,
    sign_up: bool = False,
    transport: Optional[httpx.BaseTransport] = None,
) -> None:
    """Ask harumi-api to email a one-time login code to `email`.

    If `sign_up` is True, hits `/users/sign_up` instead, which creates the
    Supabase account first (needed the first time a new email logs in).
    """
    path = "/users/sign_up" if sign_up else "/users/otp"
    with transport_errors_as_api_error(), httpx.Client(transport=transport, timeout=30.0) as client:
        response = client.post(f"{config.api_url}{path}", json={"email": email})
    _raise_for_status(response)


def verify_otp(
    config: Config, email: str, token: str, transport: Optional[httpx.BaseTransport] = None
) -> LoggedUser:
    """Exchange an emailed OTP code for a session, and persist it."""
    with transport_errors_as_api_error(), httpx.Client(transport=transport, timeout=30.0) as client:
        response = client.post(
            f"{config.api_url}/users/otp/verify", json={"email": email, "token": token}
        )
    _raise_for_status(response)
    user = LoggedUser.model_validate(guard_json(response).json())
    _persist(user, replace=True)
    return user


def refresh_session(
    config: Config, refresh_token: str, transport: Optional[httpx.BaseTransport] = None
) -> LoggedUser:
    """Exchange a refresh token for a new access token, and persist it.

    A 4xx here means harumi-api looked at the token and refused it (harumi-api
    answers 422 "Refresh token is not valid"): the session is over, and the user
    needs to log in again. That is reported as `NotAuthenticatedError`, not as
    the raw HTTP error. A server error or a timeout says nothing about the
    session, so those stay `ApiError`.
    """
    with transport_errors_as_api_error(), httpx.Client(transport=transport, timeout=30.0) as client:
        response = client.post(
            f"{config.api_url}/users/refresh", json={"refresh_token": refresh_token}
        )
    if _session_refused(response.status_code):
        raise NotAuthenticatedError(expired=True, environment=config.environment)
    try:
        _raise_for_status(response)
    except ApiError as exc:
        raise SessionRefreshError(exc.status_code, exc.detail) from exc
    user = LoggedUser.model_validate(guard_json(response).json())
    _persist(user)
    return user


def renew_session(
    config: Config, stale_refresh_token: str, transport: Optional[httpx.BaseTransport] = None
) -> str:
    """Get a working access token, refreshing at most once however many processes ask.

    Refresh tokens can be used once. Several commands that find the access token
    expired at the same moment (an agent running a few in parallel) would each spend
    the same refresh token: the first wins and the rest are told it was already used,
    which used to log them out of a session that was perfectly valid.

    So: take the cross-process lock, then look at the file again. If the refresh token
    on disk is no longer `stale_refresh_token`, someone else already renewed while we
    waited, and their tokens are used as they are. Only otherwise is the refresh sent.
    A refusal is checked once more the same way, in case another process got in first
    without taking the lock.
    """
    with credentials_lock():
        creds = load_credentials() or {}
        current = creds.get("refresh_token")
        if current and current != stale_refresh_token and creds.get("access_token"):
            return creds["access_token"]
        try:
            user = refresh_session(config, current or stale_refresh_token, transport=transport)
            return user.access_token or creds.get("access_token", "")
        except NotAuthenticatedError:
            after = load_credentials() or {}
            moved_on = after.get("refresh_token") and after["refresh_token"] != (current or stale_refresh_token)
            if moved_on and after.get("access_token"):
                return after["access_token"]
            raise


def logout() -> None:
    clear_credentials()


def get_valid_access_token(
    config: Config,
    *,
    allow_refresh: bool = True,
    transport: Optional[httpx.BaseTransport] = None,
) -> str:
    """Return the stored access token, proactively refreshing only if it is
    at or near expiry. Reactive refresh-on-401 is handled by ApiClient; this
    just avoids a guaranteed-to-fail request when we already know we're
    expired. Raises NotAuthenticatedError if there is no session on file.

    `transport` is the client's own, so a refresh goes where the request goes (a test's
    fake server, a proxy); without it a refresh would use a default transport.
    """
    creds = load_credentials()
    if not creds or not creds.get("access_token"):
        raise NotAuthenticatedError(environment=config.environment)

    expires_at = creds.get("expires_at")
    is_near_expiry = expires_at is not None and expires_at <= time.time() + _EXPIRY_SKEW_SECONDS

    if allow_refresh and is_near_expiry and creds.get("refresh_token"):
        try:
            return renew_session(config, creds["refresh_token"], transport=transport) or creds["access_token"]
        except NotAuthenticatedError:
            # The refresh token was refused. If the access token is already past
            # its expiry nothing can work, so say so now; if it has a few seconds
            # left, let the request try and report the 401 itself.
            if expires_at <= time.time():
                raise
            return creds["access_token"]
        except ApiError:
            # Fall back to the (possibly still valid) stored token; a 401
            # from the real request will trigger ApiClient's retry path.
            return creds["access_token"]

    return creds["access_token"]


def current_credentials() -> Optional[dict]:
    return load_credentials()


def _persist(user: LoggedUser, *, replace: bool = False) -> None:
    if not user.access_token or not user.refresh_token:
        raise ApiError(500, "harumi-api did not return a full session (missing tokens)")
    save_credentials(
        access_token=user.access_token,
        refresh_token=user.refresh_token,
        replace=replace,
        user_id=user.id,
        email=user.email,
        expires_at=user.expires_at,
    )


def _session_refused(status_code: int) -> bool:
    """Whether a refresh response means "this session is finished".

    harumi-api answers a refresh token it rejects with 422 (it turns every auth
    failure into a validation error); 400 and 401 are the other ways an API says "that
    token is no good". Nothing else is taken to mean it. A 403 or 404 can come from a
    proxy or firewall in front of the API, and telling someone their session expired
    then sends them into a login loop that cannot fix anything: those stay API errors,
    with the response's own detail.
    """
    return status_code in (400, 401, 422)


def has_session() -> bool:
    """Whether a login is on file at all. Says nothing about it still working."""
    creds = load_credentials()
    return bool(creds and creds.get("access_token"))
