"""A command that needs a project and has none offers the project menu, instead of a dead end.

`clone`, `link` and `projects get|rename|rm` already gave an arrow-key list of your projects. The 38 commands that take
the project from the folder (`runs`, `files`, `share`, `schedules`, `secrets`, ...) did not: run from anywhere else
without `--project`, they failed with "Provide --project or run from a directory bound to a project".
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.client import Client

runner = CliRunner()


def project(id_, name, updated="2026-10-20T12:00:00Z", customer="org-1"):
    return {"id": id_, "name": name, "customer_id": customer, "created_at": updated, "updated_at": updated, "notebook_ids": []}


class Server:
    def __init__(self):
        self.projects = [
            project("proj-old", "Old model", "2026-09-01T12:00:00Z"),
            project("proj-new", "Demand planning", "2026-10-25T12:00:00Z"),
        ]
        self.requests: list[str] = []
        self.runs_for: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path.endswith("/projects"):
            return httpx.Response(200, json={"projects": self.projects, "total_count": len(self.projects)})
        if path.endswith("/runs"):
            self.runs_for.append(path.split("/")[-2])
            return httpx.Response(200, json={"runs": []})
        return httpx.Response(404, json={"detail": f"not routed: {path}"})


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")
    monkeypatch.chdir(tmp_path)  # a folder with no .harumi binding


@pytest.fixture
def server(monkeypatch):
    s = Server()
    s.clients = []

    def make(*a, **k):
        s.clients.append(k)
        return Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(s.handler))

    monkeypatch.setattr(cli, "_get_client", make)
    return s


@pytest.fixture
def at_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


def choose(monkeypatch, pick):
    """Stand in for the arrow-key menu: record what it was shown, answer with `pick` (a value, or None to back out)."""
    shown = {}

    def fake(question, options, **kw):
        shown.update(question=question, options=options, kw=kw)
        return pick

    monkeypatch.setattr(interactive, "select", fake)
    return shown


def bind(folder, project_id="proj-bound"):
    (folder / ".harumi").mkdir()
    (folder / ".harumi" / "config.json").write_text(
        json.dumps({"project_id": project_id, "repo": {"owner": "o", "name": "n", "clone_url": "https://git.harumi.io/o/n.git", "default_branch": "main"}})
    )


# -- the menu -------------------------------------------------------------------------------------------------------


def test_with_no_project_at_a_terminal_it_offers_the_menu_and_uses_the_choice(server, at_terminal, monkeypatch):
    shown = choose(monkeypatch, "proj-new")

    result = runner.invoke(cli.app, ["runs", "list"])

    assert result.exit_code == 0, result.output
    assert "Which project" in shown["question"]
    assert [o.value for o in shown["options"]][0] == "proj-new"  # most recently updated first, like every other project list
    assert server.runs_for == ["proj-new"]


def test_backing_out_of_the_menu_does_nothing_and_exits_quietly(server, at_terminal, monkeypatch):
    choose(monkeypatch, None)

    result = runner.invoke(cli.app, ["runs", "list"])

    assert result.exit_code == 0
    assert server.runs_for == [] and "Error" not in result.output


def test_the_menu_lists_the_projects_of_the_organization_asked_for(server, at_terminal, monkeypatch):
    """`--org` and `--api-url` change whose projects there are, so they have to reach the client the menu uses."""
    choose(monkeypatch, "proj-new")

    runner.invoke(cli.app, ["runs", "list", "--org", "org-9", "--api-url", "https://api.example/api"])

    assert any(k.get("org") == "org-9" and k.get("api_url") == "https://api.example/api" for k in server.clients)


# -- nothing changes when it is not needed ---------------------------------------------------------------------------


def test_project_flag_wins_with_no_menu_and_no_extra_request(server, at_terminal, monkeypatch):
    shown = choose(monkeypatch, "proj-new")

    runner.invoke(cli.app, ["runs", "list", "--project", "proj-flag"])

    assert shown == {} and server.runs_for == ["proj-flag"]
    assert not any(r.endswith("/projects") for r in server.requests), "listing projects for nothing"


def test_a_bound_folder_wins_with_no_menu_and_no_extra_request(server, at_terminal, monkeypatch, tmp_path):
    bind(tmp_path)
    shown = choose(monkeypatch, "proj-new")

    runner.invoke(cli.app, ["runs", "list"])

    assert shown == {} and server.runs_for == ["proj-bound"]
    assert not any(r.endswith("/projects") for r in server.requests)


def test_without_a_terminal_the_error_is_what_it_always_was(server, no_terminal, monkeypatch):
    shown = choose(monkeypatch, "proj-new")

    result = runner.invoke(cli.app, ["runs", "list"])

    assert result.exit_code == 1
    assert "Provide --project or run from a directory bound to a project" in " ".join(result.output.split())
    assert shown == {} and server.requests == [], "a script must not start making requests it did not make before"


def test_you_with_no_projects_are_told_how_to_make_one(server, at_terminal, monkeypatch):
    server.projects = []
    shown = choose(monkeypatch, "x")

    result = runner.invoke(cli.app, ["runs", "list"])

    assert result.exit_code == 1 and "harumi new" in result.output and shown == {}


# -- every command that takes a project ------------------------------------------------------------------------------

PROJECT_COMMANDS = [
    ["runs", "list"], ["runs", "get"], ["tags", "list"], ["files", "ls"], ["share", "list"], ["datasources", "list"],
    ["schedules", "list"], ["secrets", "list"], ["memories", "list"], ["check"],
]


@pytest.mark.parametrize("argv", PROJECT_COMMANDS, ids=lambda a: " ".join(a))
def test_each_command_offers_the_menu_when_it_has_no_project(argv, server, at_terminal, monkeypatch):
    shown = choose(monkeypatch, None)

    runner.invoke(cli.app, argv)

    assert "Which project" in shown.get("question", ""), f"{' '.join(argv)} did not offer the menu"


def test_every_resolve_project_call_passes_the_organization_through():
    """Calling it without `api_url` and `org` would list the wrong organization's projects with no error."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cli))
    bare = [
        c.lineno
        for c in ast.walk(tree)
        if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_resolve_project" and len(c.args) + len(c.keywords) < 3
    ]

    assert bare == []


# -- harumi start ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "picked, runs",
    [("1", "_new_impl"), ("2", "_push_impl"), ("3", "_clone_impl")],
    ids=["nothing yet -> new", "a folder -> import", "an existing project -> clone"],
)
def test_start_offers_an_arrow_menu_at_a_terminal_and_runs_what_was_picked(picked, runs, at_terminal, monkeypatch):
    shown = choose(monkeypatch, picked)
    monkeypatch.setattr(cli.auth, "has_session", lambda: True)
    called = []
    for name in ("_new_impl", "_push_impl", "_clone_impl"):
        monkeypatch.setattr(cli, name, lambda *a, _n=name, **k: called.append(_n))
    monkeypatch.setattr(cli, "_get_client", lambda *a, **k: type("C", (), {"config": type("K", (), {"org_id": None})()})())
    monkeypatch.setattr(cli, "_require_entitled_workspace", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_project_name", lambda default: "Demo")
    monkeypatch.setattr(cli.typer, "prompt", lambda text, **k: "." if "Path" in text else pytest.fail("typed a number although a menu was shown"))

    runner.invoke(cli.app, ["start"])

    assert "What do you have?" in shown["question"]
    assert [o.value for o in shown["options"]] == ["1", "2", "3"] and all(o.hint for o in shown["options"])
    assert called == [runs]


def test_backing_out_of_the_start_menu_does_nothing(at_terminal, monkeypatch):
    choose(monkeypatch, None)
    monkeypatch.setattr(cli.auth, "has_session", lambda: True)
    monkeypatch.setattr(cli, "_get_client", lambda *a, **k: pytest.fail("did something after quitting"))

    result = runner.invoke(cli.app, ["start"])

    assert result.exit_code == 0


def test_start_without_a_terminal_still_takes_a_typed_number(no_terminal, monkeypatch):
    shown = choose(monkeypatch, "1")
    monkeypatch.setattr(cli.auth, "has_session", lambda: True)
    cloned = []
    monkeypatch.setattr(cli, "_clone_impl", lambda **kw: cloned.append(kw))

    # Two answers that are not a choice, then one that is. `start` asks again until it gets a valid one, so the input
    # must end with a valid one: a loop fed only invalid answers never ends on Python 3.9's click, and hung CI for 72 minutes.
    result = runner.invoke(cli.app, ["start"], input="9\nx\n3\n")

    assert shown == {}, "a menu with nobody to press a key"
    assert "Choice (1/2/3)" in result.output
    assert len(cloned) == 1, "the typed 3 should have gone on to clone"
