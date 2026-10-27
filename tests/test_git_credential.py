"""`harumi git-credential`: git's credential helper for Harumi's git server (issue #126).

The token used to be written into the `harumi` remote's URL in `.git/config`, where `git remote -v`, IDEs and screen
shares show it. It is now handed to git by this helper, only when git asks, and only for Harumi's host.
"""

from __future__ import annotations

import io
import json

import pytest

from harumi import config, git_credential

PROD = "https://git.harumi.io"
STAGING = "https://git.dev.harumi.io"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "home"


def log_in(environment="production", *, token="tok-prod", username="u-abc", git_url=None):
    path = config.credentials_path(environment)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"git_token": token, "git_username": username}
    if git_url:
        data["git_url"] = git_url
    path.write_text(json.dumps(data))


def ask(action, *lines):
    out = io.StringIO()
    git_credential.respond(action, io.StringIO("\n".join(lines) + "\n\n"), out)
    return out.getvalue()


def test_it_answers_with_the_username_and_token_for_harumis_host():
    log_in()

    assert ask("get", "protocol=https", "host=git.harumi.io") == "username=u-abc\npassword=tok-prod\n"


def test_it_says_nothing_for_any_other_host():
    """A helper is handed to whatever git is talking to. The token must not go to GitHub because someone ran git there."""
    log_in()

    for host in ("github.com", "git.harumi.io.evil.example", "evilgit.harumi.io", "git.dev.harumi.io"):
        assert ask("get", "protocol=https", f"host={host}") == "", host


def test_it_says_nothing_over_plain_http():
    log_in()

    assert ask("get", "protocol=http", "host=git.harumi.io") == ""


def test_it_says_nothing_when_nobody_is_logged_in_or_there_is_no_token():
    assert ask("get", "protocol=https", "host=git.harumi.io") == ""
    log_in(token="", username="u-abc")
    assert ask("get", "protocol=https", "host=git.harumi.io") == ""


def test_it_answers_for_the_environment_whose_host_was_asked_about():
    """Logged in to both: staging's token for staging's host, production's for production's, never the other way."""
    log_in("production", token="tok-prod", username="u-prod")
    log_in("staging", token="tok-stag", username="u-stag")

    assert "tok-prod" in ask("get", "protocol=https", "host=git.harumi.io")
    assert "tok-stag" in ask("get", "protocol=https", "host=git.dev.harumi.io")
    assert "tok-stag" not in ask("get", "protocol=https", "host=git.harumi.io")


def test_it_answers_for_a_git_server_the_login_recorded():
    """`HARUMI_GIT_URL` or a self-hosted server: the host saved with the token is the one it belongs to."""
    log_in(git_url="https://git.internal.example:8443")

    assert "tok-prod" in ask("get", "protocol=https", "host=git.internal.example:8443")
    assert ask("get", "protocol=https", "host=git.harumi.io") == ""


@pytest.mark.parametrize("action", ["store", "erase"])
def test_it_never_stores_or_erases(action, home):
    """Git calls `store` after a good push and `erase` after a bad one. Neither may touch the token we keep."""
    log_in()
    before = config.credentials_path("production").read_text()

    out = ask(action, "protocol=https", "host=git.harumi.io", "username=u-abc", "password=something-else")

    assert out == "" and config.credentials_path("production").read_text() == before


def test_it_never_raises_whatever_it_is_given(monkeypatch):
    log_in()
    out = io.StringIO()

    git_credential.respond("get", io.StringIO("not a protocol line\n\x00\xff garbage"), out)
    monkeypatch.setattr(git_credential, "credentials_for", lambda request: 1 / 0)
    git_credential.respond("get", io.StringIO("protocol=https\nhost=git.harumi.io\n\n"), out)

    assert out.getvalue() == ""


def test_the_answer_is_only_the_two_protocol_lines():
    log_in(token="tok/with+reserved=chars", username="a@b.example")

    assert ask("get", "protocol=https", "host=git.harumi.io") == "username=a@b.example\npassword=tok/with+reserved=chars\n"
