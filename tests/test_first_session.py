"""The first session: a mistyped login code, `harumi help`, `start` and `logout`."""

from __future__ import annotations

import os
import re

import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

from harumi import auth, cli, config, interactive
from harumi.errors import ApiError
from harumi.models import LoggedUser

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def plain_lines(text: str) -> str:
    """Styling removed but line structure kept, for tests that read a table row by row."""
    return _ANSI.sub("", text)


def plain(text: str) -> str:
    """Output as a person reads it: styling codes removed, whitespace collapsed.

    Whether rich emits colour depends on the environment (CI sets GITHUB_ACTIONS; a
    developer's shell may set NO_COLOR), so assertions about wording must not see it.
    """
    return " ".join(_ANSI.sub("", text).split())


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    real_home = os.path.expanduser("~/.harumi")
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
    assert not str(config.credentials_path()).startswith(real_home)
    yield tmp_path
    assert not str(config.credentials_path()).startswith(real_home), "a test escaped to the real HARUMI_HOME"


class Mail:
    """The login endpoints: a code that works once, and everything else is refused."""

    def __init__(self, good="123456", refuse_with=422):
        self.good = good
        self.refuse_with = refuse_with
        self.tried: list[str] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(auth, "request_otp", lambda cfg, email, sign_up=False, transport=None: None)

        def verify(cfg, email, token, transport=None):
            self.tried.append(token)
            if token != self.good:
                raise ApiError(self.refuse_with, "Token has expired or is invalid")
            config.save_credentials(access_token="a", refresh_token="r", email=email, replace=True)
            return LoggedUser(id="u1", email=email, access_token="a", refresh_token="r")

        monkeypatch.setattr(auth, "verify_otp", verify)


@pytest.fixture
def mail(monkeypatch):
    m = Mail()
    m.install(monkeypatch)
    return m


@pytest.fixture
def at_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


def login(*inputs):
    return runner.invoke(cli.app, ["login", "--email", "ana@example.com"], input="".join(f"{i}\n" for i in inputs))


# -- a wrong code ------------------------------------------------------------------------------


def test_a_wrong_code_at_a_terminal_can_be_retyped(mail, at_terminal):
    result = login("999999", "123456")

    assert result.exit_code == 0, result.output
    assert mail.tried == ["999999", "123456"]
    assert "That code was not accepted" in result.output
    assert "Logged in" in result.output
    assert "HTTP" not in result.output and "422" not in result.output


def test_three_wrong_codes_end_with_how_to_get_a_new_one(mail, at_terminal):
    result = login("1", "2", "3")

    assert result.exit_code == 1
    assert mail.tried == ["1", "2", "3"]
    assert "harumi login" in result.output and "new one" in result.output
    assert "HTTP" not in result.output


def test_without_a_terminal_a_wrong_code_fails_once_and_does_not_eat_piped_input(mail, no_terminal):
    """Retrying would read further lines of whatever was piped in."""
    result = login("999999", "123456")  # the second line must NOT be tried

    assert result.exit_code == 1
    assert mail.tried == ["999999"]
    assert "HTTP" not in result.output


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_every_way_the_server_refuses_a_code_reads_the_same(monkeypatch, at_terminal, status):
    Mail(refuse_with=status).install(monkeypatch)
    result = login("1", "2", "3")
    assert "That code was not accepted" in result.output and "HTTP" not in result.output


def test_too_many_attempts_says_to_wait_and_stops_asking(monkeypatch, at_terminal):
    m = Mail(refuse_with=429)
    m.install(monkeypatch)

    result = login("1", "2", "3")

    assert result.exit_code == 1
    assert m.tried == ["1"], "kept hammering a rate-limited endpoint"
    assert "Wait a minute" in result.output


def test_an_outage_while_checking_the_code_is_not_called_a_wrong_code(monkeypatch, at_terminal):
    m = Mail(refuse_with=503)
    m.install(monkeypatch)
    result = login("000000", "000000", "000000")  # none of these is the good code, so the 503 is what answers
    assert result.exit_code == 1
    assert m.tried == ["000000"], "retried through an outage"
    assert "not accepted" not in result.output


def test_spaces_in_a_pasted_code_are_ignored(mail, at_terminal):
    result = login("123 456")
    assert result.exit_code == 0, result.output
    assert mail.tried == ["123456"]


# -- an email with no account -------------------------------------------------------------------


def test_an_unknown_email_says_so_in_words_and_offers_signup(monkeypatch):
    def refused(cfg, email, sign_up=False, transport=None):
        raise ApiError(422, "Signups not allowed for otp")

    monkeypatch.setattr(auth, "request_otp", refused)

    result = login()

    assert result.exit_code == 1
    out = plain(result.output)
    assert "no Harumi account for ana@example.com" in out and "harumi login --signup" in out
    assert "HTTP" not in out and "Signups not allowed" not in out


def test_other_refusals_of_the_email_are_not_mistaken_for_a_missing_account(monkeypatch):
    def refused(cfg, email, sign_up=False, transport=None):
        raise ApiError(422, "Unable to validate email address: invalid format")

    monkeypatch.setattr(auth, "request_otp", refused)

    result = login()

    assert result.exit_code == 1
    assert "invalid format" in plain(result.output)
    assert "no Harumi account" not in result.output


def test_login_help_does_not_quote_the_servers_error_string():
    out = plain(runner.invoke(cli.app, ["login", "--help"]).output)
    assert "--signup" in out and "Signups not allowed" not in out


# -- help ---------------------------------------------------------------------------------------


def test_help_is_the_same_as_dash_dash_help():
    assert plain(runner.invoke(cli.app, ["help"]).output) == plain(runner.invoke(cli.app, ["--help"]).output)


def test_help_for_a_command_is_that_commands_help():
    got = runner.invoke(cli.app, ["help", "run"])
    assert got.exit_code == 0 and plain(got.output) == plain(runner.invoke(cli.app, ["run", "--help"]).output)


def test_help_for_a_nested_command_works():
    got = runner.invoke(cli.app, ["help", "runs", "cancel"])
    assert got.exit_code == 0 and plain(got.output) == plain(runner.invoke(cli.app, ["runs", "cancel", "--help"]).output)


def test_help_for_something_that_does_not_exist_says_so():
    got = runner.invoke(cli.app, ["help", "frobnicate"])
    assert got.exit_code != 0 and "frobnicate" in got.output


def test_help_is_a_real_command_that_the_command_list_does_not_advertise():
    """Asserting on the rendered list can pass for the wrong reason (wrapped text may simply
    not contain the substring), so ask the command tree itself."""
    commands = get_command(cli.app).commands

    assert "help" in commands and commands["help"].hidden is True
    listed = re.findall(r"^[│|] *(\w[\w-]*) ", plain_lines(runner.invoke(cli.app, ["--help"]).output), re.M)
    assert "help" not in listed and "login" in listed  # the parse found the table, and help is not in it


# -- start --------------------------------------------------------------------------------------


def test_start_without_a_login_fails_before_asking_anything(monkeypatch):
    asked = []
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: asked.append(a) or "1")

    result = runner.invoke(cli.app, ["start"])

    assert result.exit_code == 1
    assert "harumi login" in result.output
    assert asked == [], "asked a question before checking that the answers could be used"
    assert "What do you have" not in result.output


def test_start_with_a_login_still_asks(monkeypatch):
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com", replace=True)
    asked = []

    def stop_at_the_first_question(text, *a, **k):
        asked.append(text)
        raise typer.Abort()

    # Not `input="\n"` and hoping the input runs out: click 8.4 treats that as an abort, but
    # click 8.1 (the Python 3.9 job) returns an empty answer forever, and `start` re-asks
    # until it gets 1, 2 or 3, so the test hung CI for twelve minutes.
    monkeypatch.setattr(cli.typer, "prompt", stop_at_the_first_question)

    result = runner.invoke(cli.app, ["start"])

    assert "What do you have" in result.output
    assert asked == ["Choice (1/2/3)"]  # it got as far as asking
    assert "harumi login" not in result.output  # the login guard did not fire for someone who is logged in


# -- logout -------------------------------------------------------------------------------------


def test_logout_says_it_logged_you_out_and_how_to_come_back():
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com", replace=True)

    result = runner.invoke(cli.app, ["logout"])

    assert result.exit_code == 0
    assert "Logged out" in result.output and "harumi login" in result.output
    assert config.load_credentials() is None


def test_logout_when_not_logged_in_does_not_pretend_otherwise():
    result = runner.invoke(cli.app, ["logout"])

    assert result.exit_code == 0
    assert "not logged in" in result.output and "Logged out" not in result.output


def test_an_email_with_brackets_is_printed_whole_after_login(monkeypatch):
    """Rich reads [..] as markup; an address (or a server-supplied one) must not vanish into it."""
    monkeypatch.setattr(auth, "request_otp", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_resolve_and_store_org", lambda c: None)
    monkeypatch.setattr(cli, "_provision_git_token", lambda c: None)
    monkeypatch.setattr(cli, "_quickstart", lambda **k: None)
    monkeypatch.setattr(
        auth, "verify_otp", lambda cfg, email, code, transport=None: LoggedUser(id="u", email="a[bold]b@example.com", access_token="a", refresh_token="r")
    )

    result = runner.invoke(cli.app, ["login", "--email", "x@example.com"], input="123456\n")

    assert "a[bold]b@example.com" in plain(result.output)


def test_the_help_note_in_the_skill_reference_is_not_filed_under_an_unrelated_command():
    """A note about `harumi help` that sits at the end of the `link` or `logout` section reads
    as part of that command, and an agent looking up `help` will not find it."""
    import pathlib

    ref = (pathlib.Path(__file__).resolve().parents[1] / ".agents/skills/harumi-cli/references/commands.md").read_text(encoding="utf-8")
    note = "`harumi help <command>` is `harumi <command> --help`"
    first_heading = ref.index("\n## ")

    assert note in ref
    assert ref.index(note) < first_heading, "the note is under a command's section instead of the overview at the top"
