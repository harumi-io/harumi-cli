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

from harumi.config import Config, clear_credentials, load_credentials, save_credentials
from harumi.errors import ApiError, NotAuthenticatedError
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
    with httpx.Client(transport=transport, timeout=30.0) as client:
        response = client.post(f"{config.api_url}{path}", json={"email": email})
    _raise_for_status(response)


def verify_otp(
    config: Config, email: str, token: str, transport: Optional[httpx.BaseTransport] = None
) -> LoggedUser:
    """Exchange an emailed OTP code for a session, and persist it."""
    with httpx.Client(transport=transport, timeout=30.0) as client:
        response = client.post(
            f"{config.api_url}/users/otp/verify", json={"email": email, "token": token}
        )
    _raise_for_status(response)
    user = LoggedUser.model_validate(response.json())
    _persist(user)
    return user


def refresh_session(
    config: Config, refresh_token: str, transport: Optional[httpx.BaseTransport] = None
) -> LoggedUser:
    """Exchange a refresh token for a new access token, and persist it."""
    with httpx.Client(transport=transport, timeout=30.0) as client:
        response = client.post(
            f"{config.api_url}/users/refresh", json={"refresh_token": refresh_token}
        )
    _raise_for_status(response)
    user = LoggedUser.model_validate(response.json())
    _persist(user)
    return user


def logout() -> None:
    clear_credentials()


def get_valid_access_token(config: Config, *, allow_refresh: bool = True) -> str:
    """Return the stored access token, proactively refreshing only if it is
    at or near expiry. Reactive refresh-on-401 is handled by ApiClient; this
    just avoids a guaranteed-to-fail request when we already know we're
    expired. Raises NotAuthenticatedError if there is no session on file.
    """
    creds = load_credentials()
    if not creds or not creds.get("access_token"):
        raise NotAuthenticatedError()

    expires_at = creds.get("expires_at")
    is_near_expiry = expires_at is not None and expires_at <= time.time() + _EXPIRY_SKEW_SECONDS

    if allow_refresh and is_near_expiry and creds.get("refresh_token"):
        try:
            user = refresh_session(config, creds["refresh_token"])
            return user.access_token or creds["access_token"]
        except ApiError:
            # Fall back to the (possibly still valid) stored token; a 401
            # from the real request will trigger ApiClient's retry path.
            return creds["access_token"]

    return creds["access_token"]


def current_credentials() -> Optional[dict]:
    return load_credentials()


def _persist(user: LoggedUser) -> None:
    if not user.access_token or not user.refresh_token:
        raise ApiError(500, "harumi-api did not return a full session (missing tokens)")
    save_credentials(
        access_token=user.access_token,
        refresh_token=user.refresh_token,
        user_id=user.id,
        email=user.email,
        expires_at=user.expires_at,
    )


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code >= 400:
        detail = response.text
        try:
            detail = response.json().get("detail", detail)
        except Exception:
            pass
        raise ApiError(response.status_code, str(detail))
