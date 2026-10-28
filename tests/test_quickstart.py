"""Bare `harumi`: the quick start, in a terminal and out of one."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from harumi import cli, interactive, quickstart
from harumi.quickstart import State

runner = CliRunner()

LOGGED_OUT = State(logged_in=False, environment="production")
NO_PROJECT = State(logged_in=True, environment="production", email="ana@example.com")
IN_PROJECT = State(logged_in=True, environment="production", email="ana@example.com", project_id="proj-1")


def _keys(state):
    return [a.key for a in quickstart.actions_for(state)]


def test_logged_out_user_is_pointed_at_login_first():
    assert _keys(LOGGED_OUT) == [quickstart.LOGIN, quickstart.SIGNUP]
    assert "harumi login" in quickstart.status_line(LOGGED_OUT)


def test_logged_in_user_without_a_project_gets_the_three_onboarding_journeys():
    assert _keys(NO_PROJECT) == [quickstart.NEW, quickstart.IMPORT, quickstart.CLONE]


def test_user_inside_a_project_gets_the_run_loop():
    assert _keys(IN_PROJECT) == [quickstart.RUN, quickstart.CHECK, quickstart.RUNS]
    assert "proj-1" in quickstart.status_line(IN_PROJECT)


@pytest.mark.parametrize("state", [LOGGED_OUT, NO_PROJECT, IN_PROJECT])
def test_plain_text_lists_every_action_command(state):
    text = "\n".join(quickstart.plain_text(state))
    for action in quickstart.actions_for(state):
        assert action.command in text
    assert "harumi --help" in text


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


def _state(monkeypatch, state):
    monkeypatch.setattr(cli, "_quickstart_state", lambda: state)


def test_bare_harumi_without_a_terminal_prints_text_and_exits_zero(monkeypatch, no_terminal):
    _state(monkeypatch, NO_PROJECT)
    monkeypatch.setattr(
        cli.interactive, "select", lambda *a, **k: pytest.fail("must not prompt without a terminal")
    )

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0
    assert "harumi new" in result.output
    assert "ana@example.com" in result.output


def test_a_real_subcommand_does_not_trigger_the_quick_start(monkeypatch):
    monkeypatch.setattr(cli, "_quickstart", lambda: pytest.fail("quick start ran for a subcommand"))
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0


@pytest.fixture
def at_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)


@pytest.mark.parametrize(
    ("state", "pick", "expected_argv"),
    [
        (LOGGED_OUT, quickstart.LOGIN, ["login"]),
        (LOGGED_OUT, quickstart.SIGNUP, ["login", "--signup"]),
        (NO_PROJECT, quickstart.IMPORT, ["import"]),
        (IN_PROJECT, quickstart.RUN, ["run", "--watch"]),
        (IN_PROJECT, quickstart.CHECK, ["check"]),
        (IN_PROJECT, quickstart.RUNS, ["runs", "list"]),
        (IN_PROJECT, quickstart.HELP, ["--help"]),
    ],
)
def test_menu_choice_runs_the_same_command_a_user_would_type(
    monkeypatch, at_terminal, state, pick, expected_argv
):
    _state(monkeypatch, state)
    monkeypatch.setattr(cli.interactive, "select", lambda *a, **k: pick)
    ran = []
    monkeypatch.setattr(cli, "_run_cli", ran.append)

    runner.invoke(cli.app, [])

    assert ran == [expected_argv]


def test_menu_new_project_asks_for_a_name_then_runs_new(monkeypatch, at_terminal):
    _state(monkeypatch, NO_PROJECT)
    monkeypatch.setattr(cli.interactive, "select", lambda *a, **k: quickstart.NEW)
    ran = []
    monkeypatch.setattr(cli, "_run_cli", ran.append)

    runner.invoke(cli.app, [], input="Routing demo\n")

    assert ran == [["new", "Routing demo"]]


@pytest.mark.parametrize("pick", [None, quickstart.QUIT])
def test_quitting_the_menu_runs_nothing_and_exits_zero(monkeypatch, at_terminal, pick):
    _state(monkeypatch, LOGGED_OUT)
    monkeypatch.setattr(cli.interactive, "select", lambda *a, **k: pick)
    monkeypatch.setattr(cli, "_run_cli", lambda argv: pytest.fail(f"ran {argv}"))

    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0
