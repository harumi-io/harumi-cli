"""`harumi login`: Harumi staff choose an environment; nobody else is asked.

Uses the real login command with only the network (`auth.*`, the profile
lookup) and the menu replaced, so the flow itself is what is under test.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.config import is_internal_email
from harumi.models import LoggedUser

runner = CliRunner()


def saved_default() -> str:
    """The environment `harumi login` left as the default, read from disk.

    Not `saved_default()`: login sets HARUMI_ENV in the process (as `--env`
    does) for the rest of the run, and that would mask what was actually saved.
    """
    import json

    path = config.CONFIG_PATH
    return json.loads(path.read_text()).get("environment", "production") if path.exists() else "production"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    # After login: org lookup and Gitea provisioning are not what is tested here.
    monkeypatch.setattr(cli, "_resolve_and_store_org", lambda config: None)
    monkeypatch.setattr(cli, "_provision_git_token", lambda config: None)
    # The next-steps menu at the end of login has its own tests (test_login_workspace.py).
    monkeypatch.setattr(cli, "_quickstart", lambda **k: None)
    yield tmp_path
    import os

    os.environ.pop("HARUMI_ENV", None)  # set by the code under test, not by monkeypatch


class Backend:
    """Stands in for harumi-api's login endpoints."""

    def __init__(self) -> None:
        self.otp_requests: list[tuple[str, str]] = []  # (environment, email)
        self.fail_otp = False

    def install(self, monkeypatch) -> None:
        def request_otp(cfg, email, sign_up=False, transport=None):
            self.otp_requests.append((cfg.environment, email))
            if self.fail_otp:
                from harumi.errors import ApiUnreachableError

                raise ApiUnreachableError("Could not reach harumi-api: timed out")

        def verify_otp(cfg, email, token, transport=None):
            config.save_credentials(access_token="a", refresh_token="r", email=email)
            return LoggedUser(id="u1", email=email, access_token="a", refresh_token="r")

        monkeypatch.setattr(cli.auth, "request_otp", request_otp)
        monkeypatch.setattr(cli.auth, "verify_otp", verify_otp)


@pytest.fixture
def backend(monkeypatch):
    b = Backend()
    b.install(monkeypatch)
    # Default: no live session, so the code flow always runs.
    monkeypatch.setattr(cli, "_session_still_works", lambda cfg, email: False)
    return b


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


def _menu(monkeypatch, answer):
    asked = []

    def select(message, options, *, flag=None, default=None):
        asked.append({"message": message, "values": [o.value for o in options], "default": default})
        return answer

    monkeypatch.setattr(interactive, "select", select)
    return asked


# -- who gets asked ---------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("andre@harumi.io", True),
        ("Andre@Harumi.IO", True),
        (" andre@harumi.io ", True),
        ("ana@example.com", False),
        ("ana@notharumi.io", False),
        ("ana@harumi.io.evil.com", False),
        ("ana@sub.harumi.io", False),
        ("harumi.io", False),
        ("@harumi.io", False),
        ("", False),
    ],
)
def test_only_exact_harumi_addresses_count_as_staff(email, expected):
    assert is_internal_email(email) is expected


def test_a_client_is_never_asked_which_environment(backend, terminal, monkeypatch):
    asked = _menu(monkeypatch, "staging")

    result = runner.invoke(cli.app, ["login", "--email", "ana@example.com"], input="123456\n")

    assert result.exit_code == 0, result.output
    assert asked == []
    assert backend.otp_requests == [("production", "ana@example.com")]
    assert "staging" not in result.output


def test_staff_are_asked_before_any_code_is_sent(backend, terminal, monkeypatch):
    asked = _menu(monkeypatch, "staging")

    result = runner.invoke(cli.app, ["login", "--email", "andre@harumi.io"], input="123456\n")

    assert result.exit_code == 0, result.output
    assert len(asked) == 1
    assert asked[0]["values"] == ["production", "staging"]
    assert asked[0]["default"] == "production"
    # The code was requested from the environment they picked, not production.
    assert backend.otp_requests == [("staging", "andre@harumi.io")]


def test_the_pick_becomes_the_saved_default(backend, terminal, monkeypatch):
    _menu(monkeypatch, "staging")

    runner.invoke(cli.app, ["login", "--email", "andre@harumi.io"], input="123456\n")

    assert saved_default() == "staging"


def test_picking_production_switches_a_staging_default_back(backend, terminal, monkeypatch):
    config.save_environment("staging")
    asked = _menu(monkeypatch, "production")

    result = runner.invoke(cli.app, ["login", "--email", "andre@harumi.io"], input="123456\n")

    assert result.exit_code == 0, result.output
    assert asked[0]["default"] == "staging"  # the menu starts on where they were
    assert saved_default() == "production"
    assert backend.otp_requests == [("production", "andre@harumi.io")]


def test_the_menu_is_asked_for_the_email_the_user_typed_not_one_passed_as_a_flag_only(
    backend, terminal, monkeypatch
):
    """Prompted email must drive the decision too."""
    asked = _menu(monkeypatch, "production")

    runner.invoke(cli.app, ["login"], input="andre@harumi.io\n123456\n")

    assert len(asked) == 1


# -- when nobody can be asked ----------------------------------------------


def test_without_a_terminal_staff_get_no_prompt_and_keep_their_current_environment(
    backend, no_terminal, monkeypatch
):
    monkeypatch.setattr(
        interactive, "select", lambda *a, **k: pytest.fail("must not prompt without a terminal")
    )

    result = runner.invoke(cli.app, ["login", "--email", "andre@harumi.io"], input="123456\n")

    assert result.exit_code == 0, result.output
    assert backend.otp_requests == [("production", "andre@harumi.io")]


def test_an_explicit_env_wins_and_is_not_saved(backend, terminal, monkeypatch):
    monkeypatch.setattr(
        interactive, "select", lambda *a, **k: pytest.fail("--env already answered the question")
    )

    result = runner.invoke(
        cli.app, ["--env", "staging", "login", "--email", "andre@harumi.io"], input="123456\n"
    )

    assert result.exit_code == 0, result.output
    assert backend.otp_requests == [("staging", "andre@harumi.io")]
    assert saved_default() == "production"  # a one-off, not a new default


# -- failure and cancel ------------------------------------------------------


def test_a_failed_login_does_not_change_the_saved_default(backend, terminal, monkeypatch):
    """VPN off: the staging code request fails, and every later command must
    still talk to the environment that worked before."""
    _menu(monkeypatch, "staging")
    backend.fail_otp = True

    result = runner.invoke(cli.app, ["login", "--email", "andre@harumi.io"])

    assert result.exit_code == 1
    assert saved_default() == "production"


def test_ctrl_c_in_the_environment_menu_cancels_without_sending_a_code(backend, terminal, monkeypatch):
    _menu(monkeypatch, None)

    result = runner.invoke(cli.app, ["login", "--email", "andre@harumi.io"])

    assert result.exit_code == 1
    assert backend.otp_requests == []
    assert saved_default() == "production"


# -- switching without a new code -------------------------------------------


def test_switching_to_an_environment_with_a_live_session_sends_no_new_code(terminal, monkeypatch):
    backend = Backend()
    backend.install(monkeypatch)
    monkeypatch.setattr(cli, "_session_still_works", lambda cfg, email: cfg.environment == "staging")
    _menu(monkeypatch, "staging")

    result = runner.invoke(cli.app, ["login", "--email", "andre@harumi.io"])

    assert result.exit_code == 0, result.output
    assert backend.otp_requests == []
    assert "Already logged in" in result.output
    assert saved_default() == "staging"


def test_session_check_rejects_another_email_or_a_missing_session(monkeypatch):
    from harumi.config import Config

    cfg = Config.load()
    assert cli._session_still_works(cfg, "a@b.com") is False  # nothing stored

    config.save_credentials(access_token="a", refresh_token="r", email="someone@else.com")
    assert cli._session_still_works(cfg, "a@b.com") is False  # a different person


# -- the old command ----------------------------------------------------------


@pytest.mark.parametrize("argv", [["env"], ["env", "use", "staging"], ["env", "list", "--all"]])
def test_env_is_gone_and_says_where_to_go(argv):
    result = runner.invoke(cli.app, argv)

    assert result.exit_code == 1
    assert "harumi login" in result.output
    assert "--env" in result.output


def test_env_is_not_advertised():
    result = runner.invoke(cli.app, ["--help"])
    assert " env " not in result.output
    assert "--env" not in result.output


# -- review findings -----------------------------------------------------------------------------------


def test_the_env_stub_tells_scripts_how_to_get_the_old_behaviour_without_a_terminal():
    result = runner.invoke(cli.app, ["env", "use", "staging"])

    out = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "HARUMI_ENV=staging" in out and "--env staging" in out
    assert "harumi login" in out


def _write_session(env: str, email: str) -> None:
    path = config.credentials_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "email": email}))


def test_the_email_default_comes_from_the_active_environment_first():
    _write_session("production", "ana@example.com")
    _write_session("staging", "ana.staging@harumi.io")

    assert cli._stored_email() == "ana@example.com"  # production is the active environment


def test_the_email_default_falls_back_to_another_environments_session():
    """First login to staging from someone already signed in to production."""
    _write_session("production", "andre@harumi.io")

    import os

    os.environ["HARUMI_ENV"] = "staging"
    try:
        config.Config.load()
        assert cli._stored_email() == "andre@harumi.io"
    finally:
        os.environ.pop("HARUMI_ENV", None)


def test_no_session_anywhere_means_no_default():
    assert cli._stored_email() is None


def test_an_unreadable_session_file_is_skipped_not_fatal():
    path = config.credentials_path("staging")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    _write_session("production", "ana@example.com")

    assert cli._stored_email() == "ana@example.com"
