"""Regression tests for embedding credentials into a Gitea clone URL.

An email-like username (containing `@`) or a token with URL-reserved chars
must not re-split the URL's authority section — see the incident this
guards against: `https://user@harumi.io:token@host/...` was parsed by
git/curl as host=`harumi.io`, port=`token@host` (not numeric), failing with
"URL rejected: Port number was not a decimal number between 0 and 65535".
"""

from __future__ import annotations

from harumi.git import _authenticated_url


def test_percent_encodes_email_like_username():
    url = _authenticated_url(
        "https://git.dev.harumi.io/u-abc123/afm_scheduling.git",
        username="andre.koga@harumi.io",
        token="tok123",
    )
    # Exactly one unescaped '@' — the credentials/host separator.
    assert url.count("@") == 1
    assert url == "https://andre.koga%40harumi.io:tok123@git.dev.harumi.io/u-abc123/afm_scheduling.git"


def test_percent_encodes_token_with_reserved_chars():
    url = _authenticated_url(
        "https://git.dev.harumi.io/o/repo.git",
        username="u-abc123",
        token="tok/with:reserved@chars",
    )
    assert url.count("@") == 1
    assert url == "https://u-abc123:tok%2Fwith%3Areserved%40chars@git.dev.harumi.io/o/repo.git"


def test_plain_username_and_token_round_trip():
    url = _authenticated_url(
        "https://git.dev.harumi.io/o/repo.git", username="u-abc123", token="tok123"
    )
    assert url == "https://u-abc123:tok123@git.dev.harumi.io/o/repo.git"


def test_non_https_url_passed_through_unchanged():
    assert _authenticated_url("git@github.com:org/repo.git", "u", "t") == "git@github.com:org/repo.git"


def test_strips_existing_credentials_before_re_embedding():
    url = _authenticated_url(
        "https://old-user:old-tok@git.dev.harumi.io/o/repo.git",
        username="u-abc123",
        token="new-tok",
    )
    assert url == "https://u-abc123:new-tok@git.dev.harumi.io/o/repo.git"
