"""`harumi compare`: choose the runs from a list, and say what to do when it cannot go ahead.

Before this, `compare` needed 2 to 4 run UUIDs copied out of `runs list`, failed with "Compare
2 to 4 distinct runs" and left you to work out where to find them, and its --help had lost the
`[[output.kpi]]` table name to Rich's markup.
"""

from __future__ import annotations

import copy
import json

import httpx
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.client import Client

runner = CliRunner()
P = "proj-1"

COMPARISON = {
    "columns": [
        {"run_id": "run-1", "branch": "main", "commit": "aaaaaaa1", "status": "completed", "runtime_s": 90.0},
        {"run_id": "run-2", "branch": "v2", "commit": "bbbbbbb2", "status": "completed", "runtime_s": 125.0},
    ],
    "kpis": [
        {"key": "cost", "label": "Total cost", "unit": "USD", "direction": "min", "primary": True,
         "values": [100, 80], "deltas": [None, -20]},
    ],
    "winner_run_id": "run-2",
    "no_winner_reason": None,
    "warnings": [],
    "fairness": {"verdict": "same", "files": []},
}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A throwaway home: these tests must not read, or depend on, the real ~/.harumi."""
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


def run_row(id_, status="completed", branch="main", when="2026-10-15T12:00:00Z"):
    return {"id": id_, "project_id": P, "status": status, "git_branch": branch, "created_at": when}


class Server:
    def __init__(self):
        self.runs = [
            run_row("run-3", branch="v3", when="2026-10-17T12:00:00Z"),
            run_row("run-2", branch="v2", when="2026-10-16T12:00:00Z"),
            run_row("run-1", branch="main", when="2026-10-15T12:00:00Z"),
            run_row("run-0", status="running", branch="wip", when="2026-10-18T12:00:00Z"),
            run_row("run-f", status="failed", branch="broken", when="2026-10-18T13:00:00Z"),
        ]
        self.comparison = COMPARISON
        self.compared: list[str] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/compare"):
            self.compared.append(request.url.params["runs"])
            return httpx.Response(self.status, json=self.comparison if self.status == 200 else {"detail": "no such run"})
        if path.endswith("/runs"):
            return httpx.Response(200, json={"runs": self.runs})
        return httpx.Response(404, json={"detail": "not routed"})


@pytest.fixture
def server(monkeypatch):
    s = Server()
    monkeypatch.setattr(
        cli, "_get_client", lambda *a, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(s.handler))
    )
    return s


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


@pytest.fixture
def at_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)


def plain(text: str) -> str:
    return " ".join(text.split())


def choose(monkeypatch, baseline, others):
    """Stand in for the two menus; record what each offered."""
    offered = {}

    def select(message, options, *, flag=None, default=None):
        offered["baseline"] = [o.value for o in options]
        return baseline

    def select_many(message, options, *, at_least=1, at_most=None, flag=None):
        offered["others"] = [o.value for o in options]
        offered["limits"] = (at_least, at_most)
        return others

    monkeypatch.setattr(interactive, "select", select)
    monkeypatch.setattr(interactive, "select_many", select_many)
    return offered


# -- choosing the runs --------------------------------------------------------------------------------------


def test_with_no_ids_at_a_terminal_it_asks_for_a_baseline_then_the_others(server, at_terminal, monkeypatch):
    offered = choose(monkeypatch, "run-1", ["run-2", "run-3"])

    result = runner.invoke(cli.app, ["compare", "--project", P])

    assert result.exit_code == 0, result.output
    assert server.compared == ["run-1,run-2,run-3"]  # the baseline first, as the API wants
    assert offered["baseline"] == ["run-3", "run-2", "run-1"]  # succeeded runs, newest first
    assert offered["others"] == ["run-3", "run-2"]  # everything but the baseline
    assert offered["limits"] == (1, 3)  # baseline + 1 to 3 = the API's 2 to 4


def test_only_runs_with_something_to_compare_are_offered(server, at_terminal, monkeypatch):
    """A run still going has no output yet, and a failed one never will."""
    offered = choose(monkeypatch, "run-1", ["run-2"])

    runner.invoke(cli.app, ["compare", "--project", P])

    assert "run-0" not in offered["baseline"] and "run-f" not in offered["baseline"]


def test_only_the_latest_25_are_offered_like_runs_list(server, at_terminal, monkeypatch):
    # The API returns newest first, and so does the picker.
    server.runs = [run_row(f"run-{i:02d}", when=f"2026-09-{i + 1:02d}T12:00:00Z") for i in reversed(range(30))]
    offered = choose(monkeypatch, "run-29", ["run-28"])

    runner.invoke(cli.app, ["compare", "--project", P])

    assert len(offered["baseline"]) == 25 and offered["baseline"][0] == "run-29"


def test_backing_out_of_either_menu_compares_nothing(server, at_terminal, monkeypatch):
    choose(monkeypatch, None, None)
    assert runner.invoke(cli.app, ["compare", "--project", P]).exit_code == 0
    choose(monkeypatch, "run-1", None)
    assert runner.invoke(cli.app, ["compare", "--project", P]).exit_code == 0
    assert server.compared == []


def test_with_fewer_than_two_succeeded_runs_it_says_so(server, at_terminal, monkeypatch):
    server.runs = [run_row("run-1"), run_row("run-f", status="failed")]
    choose(monkeypatch, "run-1", ["x"])

    result = runner.invoke(cli.app, ["compare", "--project", P])

    out = plain(result.output)
    assert result.exit_code == 1 and "at least two runs that succeeded" in out and "harumi run" in out
    assert server.compared == []


def test_without_a_terminal_it_lists_the_runs_and_how_to_pass_them(server, no_terminal):
    result = runner.invoke(cli.app, ["compare", "--project", P])

    out = plain(result.output)
    assert result.exit_code == 1
    assert "run-3" in out and "run-2" in out and "run-1" in out
    assert "RUN_ID" in out and "first is the baseline" in out
    assert server.compared == []


def test_ids_on_the_command_line_never_open_a_menu(server, at_terminal, monkeypatch):
    choose_called = []
    monkeypatch.setattr(interactive, "select", lambda *a, **k: choose_called.append("select"))
    monkeypatch.setattr(interactive, "select_many", lambda *a, **k: choose_called.append("many"))

    result = runner.invoke(cli.app, ["compare", "run-1", "run-2", "--project", P])

    assert result.exit_code == 0 and choose_called == [] and server.compared == ["run-1,run-2"]


# -- when it cannot go ahead --------------------------------------------------------------------------------


def test_one_id_says_where_ids_come_from(server, no_terminal):
    result = runner.invoke(cli.app, ["compare", "run-1", "--project", P])

    out = plain(result.output)
    assert result.exit_code == 1
    assert "2 to 4" in out and "harumi runs list" in out and "pick" in out


def test_a_repeated_id_says_so_and_where_ids_come_from(server, no_terminal):
    out = plain(runner.invoke(cli.app, ["compare", "run-1", "run-1", "--project", P]).output)

    assert "more than once" in out and "harumi runs list" in out


def test_too_many_ids_says_the_limit(server, no_terminal):
    out = plain(runner.invoke(cli.app, ["compare", "a", "b", "c", "d", "e", "--project", P]).output)

    assert "at most 4" in out and "harumi runs list" in out


def test_an_unknown_run_says_which_command_lists_the_real_ones(server):
    server.status = 404

    result = runner.invoke(cli.app, ["compare", "run-1", "run-9", "--project", P])

    out = plain(result.output)
    assert result.exit_code == 1 and "harumi runs list" in out


# -- what it prints -----------------------------------------------------------------------------------------


def test_the_help_shows_the_kpi_table_name_that_rich_used_to_swallow():
    out = plain(runner.invoke(cli.app, ["compare", "--help"]).output)

    assert "[[output.kpi]]" in out and "declarations in harumi.toml" in out


def test_no_kpis_says_how_to_declare_them(server):
    bare = copy.deepcopy(COMPARISON)
    bare.update(kpis=[], winner_run_id=None, no_winner_reason="no KPI is marked primary")
    server.comparison = bare

    out = plain(runner.invoke(cli.app, ["compare", "run-1", "run-2", "--project", P]).output)

    assert "[[output.kpi]]" in out and "harumi.toml" in out
    assert "no KPI is marked primary" not in out  # the table is empty; that reason would mislead


def test_a_winner_names_the_command_that_gets_its_files(server):
    out = plain(runner.invoke(cli.app, ["compare", "run-1", "run-2", "--project", P]).output)

    assert "Winner: v2" in out and "harumi runs download run-2" in out


def test_json_output_is_untouched_by_all_of_this(server, no_terminal):
    result = runner.invoke(cli.app, ["compare", "run-1", "run-2", "--project", P, "--json"])

    assert json.loads(result.output) == COMPARISON
