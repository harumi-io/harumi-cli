"""What an ordinary client sees: nothing about environments, staging or staff.

Almost everyone uses production and cannot reach staging at all. Showing them
"Environment: production", "Logged in on production", or a help paragraph about
Harumi staff suggests a choice they do not have. The environment is named only
when it is not production, so a staff member on staging is still told where.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from harumi import auth, cli, config, interactive
from harumi.models import LoggedUser

runner = CliRunner()

# Words a client has no use for. ("environment variables" is real, about secrets.)
STAFF_ONLY = re.compile(r"\bstaging\b|\bstaff\b|\bVPN\b|\bHARUMI_ENV\b|active environment|per environment|\benv:", re.I)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_resolve_and_store_org", lambda c: None)
    monkeypatch.setattr(cli, "_provision_git_token", lambda c: None)
    monkeypatch.setattr(cli, "_quickstart", lambda **k: None)
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


def log_in_flow(monkeypatch):
    monkeypatch.setattr(auth, "request_otp", lambda cfg, email, sign_up=False, transport=None: None)

    def verify(cfg, email, token, transport=None):
        config.save_credentials(access_token="a", refresh_token="r", email=email, replace=True)
        return LoggedUser(id="u1", email=email, access_token="a", refresh_token="r")

    monkeypatch.setattr(auth, "verify_otp", verify)


def leaf_commands(command, path=()):
    subs = getattr(command, "commands", None)
    if subs:
        for name, sub in subs.items():
            if not getattr(sub, "hidden", False):
                yield from leaf_commands(sub, (*path, name))
    else:
        yield path


def test_no_visible_help_text_mentions_staff_staging_or_environments():
    """Every command's --help, read as a client reads it."""
    from typer.main import get_command

    offenders = []
    for path in [(), *leaf_commands(get_command(cli.app))]:
        out = runner.invoke(cli.app, [*path, "--help"]).output
        # Box-drawing characters and line wraps are layout, not words: "saved per" and
        # "environment" on two lines is still the phrase "saved per environment".
        text = " ".join(re.sub(r"[│╭╮╰╯─┃┏┓┗┛━]", " ", out).split())
        for hit in STAFF_ONLY.finditer(text):
            offenders.append(f"harumi {' '.join(path)} --help: ...{text[max(0, hit.start() - 30):hit.end() + 30]}...")
    assert offenders == []


def test_logging_in_to_production_never_says_production(monkeypatch):
    log_in_flow(monkeypatch)

    result = runner.invoke(cli.app, ["login", "--email", "ana@example.com"], input="123456\n")

    assert result.exit_code == 0, result.output
    assert "Logged in as ana@example.com." in " ".join(result.output.split())
    assert not re.search(r"production|staging|Environment:|VPN", result.output), result.output


def test_logging_in_again_never_says_production(monkeypatch):
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com", replace=True)
    monkeypatch.setattr(cli, "_session_still_works", lambda cfg, email: True)

    result = runner.invoke(cli.app, ["login", "--email", "ana@example.com"])

    assert "Already logged in as ana@example.com." in " ".join(result.output.split())
    assert "production" not in result.output


def test_logout_never_says_production(monkeypatch):
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com", replace=True)

    assert "production" not in runner.invoke(cli.app, ["logout"]).output


def test_the_quick_start_never_says_production():
    from harumi.quickstart import State, plain_text, status_line

    state = State(logged_in=True, environment="production", email="ana@example.com")
    assert "production" not in status_line(state)
    assert not any("production" in line for line in plain_text(state))


# -- and whoever is somewhere else is still told ---------------------------------------------------------


def test_staging_is_named_so_a_staff_member_knows_where_they_are(monkeypatch):
    log_in_flow(monkeypatch)
    monkeypatch.setenv("HARUMI_ENV", "staging")

    result = runner.invoke(cli.app, ["login", "--email", "andre@harumi.io"], input="123456\n")

    out = " ".join(result.output.split())
    assert "Environment: staging" in out and "VPN" in out
    assert "Logged in as andre@harumi.io on staging." in out


def test_a_developers_own_api_is_named_even_on_production(monkeypatch):
    log_in_flow(monkeypatch)

    result = runner.invoke(cli.app, ["login", "--email", "dev@example.com", "--api-url", "http://localhost:8000/api"], input="123456\n")

    assert "http://localhost:8000/api" in result.output  # not the ordinary setup: say where


def test_the_quick_start_names_staging(monkeypatch):
    from harumi.quickstart import State, status_line

    assert "staging" in status_line(State(logged_in=True, environment="staging", email="andre@harumi.io"))


def test_logout_names_staging(monkeypatch):
    monkeypatch.setenv("HARUMI_ENV", "staging")
    config.Config.load()
    config.save_credentials(access_token="a", refresh_token="r", email="andre@harumi.io", replace=True)

    assert "staging" in runner.invoke(cli.app, ["logout"]).output
