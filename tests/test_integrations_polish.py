"""datasources, schedules and memories: show what is stored, accept the ids the tables print.

Driven against a fake API with the real payload shapes, rendered at 80 columns (a default terminal).
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.client import Client

runner = CliRunner()
P = "proj-1"
S1 = "a1b2c3d4-1111-4222-8333-444455556666"
S2 = "b2c3d4e5-2222-4333-8444-555566667777"
M1 = "e1e1e1e1-1111-4222-8333-444455556666"
M3 = "e3e3e3e3-3333-4444-8555-666677778888"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli.console, "_width", 80)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


class Api:
    def __init__(self):
        self.query = {"columns": ["note"], "data": [["done [/oops] ok"]], "rowCount": 1, "wasLimited": False}
        self.schedules = [
            {"id": S1, "project_id": P, "cron": "0 9 * * 1-5", "git_branch": "main", "command": "python main.py", "kernel_spec": "or_python_small"},
            {"id": S2, "project_id": P, "cron": "*/15 * * * *", "git_branch": "main", "kernel_spec": "or_python_small",
             "command": "pytest tests/test_model.py::test_x[case-1] --tag [nightly]"},
        ]
        self.memories = [
            {"id": M1, "scope": "project", "project_id": P, "content": "Tag unreviewed numbers with [draft].", "retrieval_mode": "always"},
            {"id": M3, "scope": "organization", "content": "Fiscal year starts in February.", "retrieval_mode": "always"},
        ]
        self.deleted: list[str] = []
        self.posted: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if p.endswith("/execute"):
            return httpx.Response(200, json=self.query)
        if p.endswith("/test-connection"):
            self.posted.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "message": "ok"})
        if p.endswith(f"/datasources/{P}") and m == "POST":
            self.posted.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "d", "project_id": P, **json.loads(request.content)})
        if p.endswith("/schedules") and m == "GET":
            return httpx.Response(200, json={"schedules": self.schedules})
        if "/schedules/" in p:
            row = next((s for s in self.schedules if s["id"] == p.rsplit("/", 1)[-1]), None)
            if not row:
                return httpx.Response(404, json={"detail": "Schedule not found"})
            if m == "DELETE":
                self.deleted.append(row["id"])
            return httpx.Response(200, json=row)
        if p.endswith("/memories") and m == "GET":
            return httpx.Response(200, json={"memories": self.memories})
        if "/memories/" in p:
            row = next((x for x in self.memories if x["id"] == p.rsplit("/", 1)[-1] and x["scope"] == "project"), None)
            if not row:
                return httpx.Response(404, json={"detail": "Memory not found"})
            if m == "DELETE":
                self.deleted.append(row["id"])
            return httpx.Response(200, json=row)
        return httpx.Response(404, json={"detail": "not routed " + p})


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler)))
    monkeypatch.setattr(cli, "_resolve_project", lambda p: p or P)
    return a


def plain(text: str) -> str:
    return " ".join(text.split())


# -- text is shown as stored --------------------------------------------------------------------------------


def test_a_query_result_with_a_stray_closing_tag_is_shown_not_a_traceback(api):
    result = runner.invoke(cli.app, ["datasources", "query", "wh", "--sql", "select 1"])

    assert result.exit_code == 0 and "done [/oops] ok" in plain(result.output)


def test_a_query_result_keeps_bracketed_text_and_shows_null_as_null(api):
    api.query = {"columns": ["[bold]x[/bold]", "n"], "data": [["[nightly] run", None]], "rowCount": 1, "wasLimited": False}

    out = plain(runner.invoke(cli.app, ["datasources", "query", "wh", "--sql", "select 1"]).output)

    assert "[bold]x[/bold]" in out and "[nightly] run" in out and "NULL" in out and "None" not in out


def test_a_query_result_cannot_send_escape_sequences_to_the_terminal(api):
    api.query = {"columns": ["a"], "data": [["x\x1b[2Jy\u202ez"]], "rowCount": 1, "wasLimited": False}

    out = runner.invoke(cli.app, ["datasources", "query", "wh", "--sql", "select 1"]).output

    # The escape byte and the direction override are gone; what is left of the sequence is inert text.
    assert "\x1b" not in out and "\u202e" not in out and "x[2Jyz" in plain(out)


def test_the_schedule_command_is_shown_with_its_brackets(api, monkeypatch):
    monkeypatch.setattr(cli.console, "_width", 200)  # wide, so the command is not folded between border characters
    out = plain(runner.invoke(cli.app, ["schedules", "get", S2]).output)

    assert "test_x[case-1] --tag [nightly]" in out


def test_memories_are_shown_with_their_brackets(api):
    assert "[draft]" in plain(runner.invoke(cli.app, ["memories", "list"]).output)


# -- the ids the tables print are accepted -------------------------------------------------------------------


def test_schedules_list_prints_ids_that_can_be_pasted_back(api):
    out = runner.invoke(cli.app, ["schedules", "list"]).output

    assert "a1b2c3d4" in out and S1 not in out and "…" not in out.split("cron")[0]
    assert runner.invoke(cli.app, ["schedules", "get", "a1b2c3d4"]).exit_code == 0


def test_memories_list_prints_the_start_of_each_id_which_edit_and_rm_accept(api):
    out = runner.invoke(cli.app, ["memories", "list"]).output

    assert "e1e1e1e1" in out and M1 not in out
    assert runner.invoke(cli.app, ["memories", "edit", "e1e1e1e1", "--content", "x"]).exit_code == 0


def test_a_schedule_is_deleted_by_the_start_of_its_id(api):
    result = runner.invoke(cli.app, ["schedules", "rm", "b2c3d4e5", "--yes"])

    assert result.exit_code == 0 and api.deleted == [S2]


def test_a_memory_is_deleted_by_the_start_of_its_id(api):
    result = runner.invoke(cli.app, ["memories", "rm", "e1e1e1e1", "--yes"])

    assert result.exit_code == 0 and api.deleted == [M1]


def test_an_id_start_that_matches_two_is_refused_and_deletes_nothing(api):
    api.schedules[1]["id"] = "a1b2c3d4-9999-4222-8333-444455556666"

    result = runner.invoke(cli.app, ["schedules", "rm", "a1b2c3d4", "--yes"])

    assert result.exit_code == 1 and "more than one" in plain(result.output) and api.deleted == []


@pytest.mark.parametrize("typed", ["zzzz", "99999999-9999-4999-8999-999999999999"])
def test_an_unknown_schedule_points_at_the_list(api, typed):
    result = runner.invoke(cli.app, ["schedules", "rm", typed, "--yes"])  # the second is a full id: the API's 404

    assert result.exit_code == 1 and "harumi schedules list" in plain(result.output)


def test_an_unknown_memory_points_at_the_list(api):
    result = runner.invoke(cli.app, ["memories", "rm", "99999999-9999-4999-8999-999999999999", "--yes"])

    assert result.exit_code == 1 and "harumi memories list" in plain(result.output)


# -- no id: pick, or say how --------------------------------------------------------------------------------


def test_rm_without_an_id_picks_one_at_a_terminal(api, monkeypatch):
    monkeypatch.setattr(interactive, "select", lambda message, options, **k: S2)

    result = runner.invoke(cli.app, ["schedules", "rm"], input="y\n")

    assert result.exit_code == 0 and api.deleted == [S2]


def test_rm_without_an_id_and_without_a_terminal_lists_the_choices(api, monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)

    result = runner.invoke(cli.app, ["memories", "rm"])

    out = plain(result.output)
    assert result.exit_code == 1 and "no interactive terminal" in out and "e1e1e1e1" in out and api.deleted == []


@pytest.mark.parametrize("group", ["schedules", "memories"])
def test_yes_without_an_id_is_refused(api, group):
    result = runner.invoke(cli.app, [group, "rm", "--yes"])

    assert result.exit_code == 1 and "Say which" in plain(result.output) and api.deleted == []


# -- inherited memories -------------------------------------------------------------------------------------


def test_removing_a_workspace_memory_says_where_it_lives_instead_of_not_found(api):
    result = runner.invoke(cli.app, ["memories", "rm", M3, "--yes"])

    out = plain(result.output)
    assert result.exit_code == 1 and "organization" in out and "not found" not in out.lower() and api.deleted == []


def test_editing_a_workspace_memory_says_where_it_lives(api):
    out = plain(runner.invoke(cli.app, ["memories", "edit", M3, "--content", "x"]).output)

    assert "organization" in out and "not found" not in out.lower()


# -- fail before the prompt ---------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["add", "test"])
def test_an_unsupported_database_type_is_refused_before_asking_for_a_password(api, command):
    args = ["datasources", command, "--type", "postgres", "--host", "h", "--port", "5432", "--database", "d", "--username", "u"]
    if command == "add":
        args.insert(2, "dw")

    result = runner.invoke(cli.app, args)  # no input: a password prompt would abort

    out = plain(result.output)
    assert result.exit_code == 1 and "postgresql" in out and "hidden" not in out and api.posted == []


def test_a_supported_type_still_goes_through(api):
    result = runner.invoke(
        cli.app,
        ["datasources", "add", "dw", "--type", "postgresql", "--host", "h", "--port", "5432"],
        input="pw\n",
    )

    assert result.exit_code == 0 and api.posted and api.posted[0]["type"] == "postgresql"


def test_adding_a_memory_with_no_text_and_no_terminal_says_to_pass_it(api, monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)

    result = runner.invoke(cli.app, ["memories", "add"])

    out = plain(result.output)
    assert result.exit_code == 1 and "Memory:" not in out and "pass" in out.lower()


def test_schedules_add_help_states_the_minimum_interval():
    assert "5 minutes" in plain(runner.invoke(cli.app, ["schedules", "add", "--help"]).output)


# -- secrets ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["my-key", "9lives", "a/b", "has space", "../x", ""])
def test_a_secret_name_that_cannot_be_an_environment_variable_is_refused_before_the_prompt(api, name):
    result = runner.invoke(cli.app, ["secrets", "set", name])  # no input: a value prompt would abort

    out = plain(result.output)
    assert result.exit_code == 1 and "environment variable" in out and "hidden" not in out


def test_a_good_secret_name_asks_for_the_value(api, monkeypatch):
    sent = []
    monkeypatch.setattr(Client, "create_secret", lambda self, pid, name, value: sent.append((name, value)))

    result = runner.invoke(cli.app, ["secrets", "set", "API_KEY_2"], input="s3cret\n")

    assert result.exit_code == 0 and sent == [("API_KEY_2", "s3cret")]


@pytest.mark.parametrize("args", [["get"], ["rm", "--yes"], ["query", "--sql", "select 1"]])
def test_an_unknown_datasource_points_at_the_list(monkeypatch, api, args):
    api.handler_orig = api.handler
    monkeypatch.setattr(
        api, "handler", lambda r: httpx.Response(404, json={"detail": "Datasource not found"}) if "/datasources/" in r.url.path else api.handler_orig(r)
    )

    result = runner.invoke(cli.app, ["datasources", args[0], "nope", *args[1:]])

    assert result.exit_code == 1 and "harumi datasources list" in plain(result.output)
