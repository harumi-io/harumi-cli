"""Regression tests for embedding credentials into a Gitea clone URL.

An email-like username (containing `@`) or a token with URL-reserved chars
must not re-split the URL's authority section — see the incident this
guards against: `https://user@harumi.io:token@host/...` was parsed by
git/curl as host=`harumi.io`, port=`token@host` (not numeric), failing with
"URL rejected: Port number was not a decimal number between 0 and 65535".
"""

from __future__ import annotations

import subprocess

import pytest

from harumi.git import GitError, _authenticated_url, _run, push_folder
from urllib.parse import quote


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


def test_run_redacts_a_token_containing_url_reserved_chars(monkeypatch, tmp_path):
    """A failing git command must never leak a token in `GitError`, whether
    it appears in its raw form or (as `_authenticated_url` embeds it) its
    percent-encoded form — see the redaction bug this guards against: a
    token like `ab+cd/ef=12` never matches its own literal text once quoted
    to `ab%2Bcd%2Fef%3D12`, so a naive `str.replace(raw_token, "***")` is a
    silent no-op and the (trivially reversible) encoded token leaks.
    """
    token = "ab+cd/ef=12"
    authed_url = _authenticated_url(
        "https://git.dev.harumi.io/o/repo.git", username="u-abc123", token=token
    )

    def fake_run(full_args, **_kwargs):
        raise subprocess.CalledProcessError(
            128, full_args, output="", stderr=f"fatal: could not clone {authed_url}"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(GitError) as exc_info:
        _run(["clone", authed_url, str(tmp_path / "dest")], redact=token)

    message = str(exc_info.value)
    assert token not in message
    assert quote(token, safe="") not in message
    assert "***" in message


def test_push_folder_redacts_the_token_on_a_failed_push(monkeypatch, tmp_path):
    """`push_folder`'s final `git push` is the one call site `ensure_remote`
    already redacts for (via the `harumi` remote URL written to
    `.git/config`) but that this diff's own `push` call originally skipped —
    a failed HTTPS push commonly echoes the remote URL, credentials and
    all, in git's stderr."""
    token = "ab+cd/ef=12"
    clone_url = "https://git.dev.harumi.io/o/repo.git"
    authed_url = _authenticated_url(clone_url, username="u-abc123", token=token)
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "main.py").write_text("print('hi')\n")

    def fake_run(full_args, **_kwargs):
        if full_args[1:3] == ["push", "--force"]:
            raise subprocess.CalledProcessError(
                128, full_args, output="", stderr=f"fatal: unable to access '{authed_url}'"
            )
        return subprocess.CompletedProcess(full_args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(GitError) as exc_info:
        push_folder(folder, clone_url, "u-abc123", token)

    message = str(exc_info.value)
    assert token not in message
    assert quote(token, safe="") not in message
