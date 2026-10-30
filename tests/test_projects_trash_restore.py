"""`harumi projects trash` and `harumi projects restore`: bring a project back instead of sending people to the web app.

`projects rm` moves a project to the Trash for 7 days, and used to end by telling you to restore it from the web app
because the CLI could not. harumi-api has both `GET /projects/trash` and `POST /projects/{id}/restore`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.client import Client

runner = CliRunner()
NOW = datetime.now(timezone.utc)


def iso(days_ago=0.0):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def row(id_, name, deleted_days_ago, customer="org-1"):
    return {
        "id": id_, "name": name, "customer_id": customer, "kernel_spec": "or_python_small", "notebook_ids": [],
        "created_at": iso(30), "updated_at": iso(deleted_days_ago), "deleted_at": iso(deleted_days_ago),
    }


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli.console, "_width", 100)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")
    monkeypatch.chdir(tmp_path)


class Api:
    def __init__(self):
        self.trash = [row("p-old", "Old model [v1]", 6.5), row("p-new", "Routing", 1.2)]
        self.calls: list[str] = []
        self.fail: tuple[int, object] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        self.calls.append(f"{m} {p}")
        if m == "POST" and self.fail:
            return httpx.Response(self.fail[0], json=self.fail[1])
        if p.endswith("/projects/trash") and m == "GET":
            return httpx.Response(200, json={"projects": self.trash, "total_count": len(self.trash)})
        if p.endswith("/restore") and m == "POST":
            pid = p.split("/")[-2]
            found = next((r for r in self.trash if r["id"] == pid), None)
            if not found:
                return httpx.Response(404, json={"detail": "Project not found"})
            return httpx.Response(200, json={**found, "deleted_at": None})
        return httpx.Response(404, json={"detail": f"not routed: {m} {p}"})


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(
        cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler))
    )
    return a


@pytest.fixture
def at_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


def said(result):
    return " ".join((result.output or "").split())


def posts(api):
    return [c for c in api.calls if c.startswith("POST")]


# -- trash ----------------------------------------------------------------------------------------------------------


def test_trash_lists_what_was_deleted_and_how_long_is_left(api, no_terminal):
    result = runner.invoke(cli.app, ["projects", "trash"])

    out = said(result)
    assert result.exit_code == 0
    assert "Routing" in out and "Old model [v1]" in out, "a name with brackets is shown as typed"
    assert "p-old" in out and "p-new" in out, "the whole id, which restore takes"
    assert "6 days left" in out or "5 days left" in out or "less than a day" in out or "day" in out
    assert "restore" in out


def test_the_one_nearest_deletion_is_listed_first(api, no_terminal):
    """The project that is closest to being deleted for good is the one you most need to see."""
    out = runner.invoke(cli.app, ["projects", "trash"]).output

    assert out.index("Old model") < out.index("Routing")


def test_an_empty_trash_says_so(api, no_terminal):
    api.trash = []

    result = runner.invoke(cli.app, ["projects", "trash"])

    assert result.exit_code == 0 and "empty" in said(result).lower()


def test_trash_json_is_named_fields_with_a_deadline(api, no_terminal):
    api.trash[0]["internal_note"] = "not for scripts"

    result = runner.invoke(cli.app, ["projects", "trash", "--json"])

    data = json.loads(result.stdout)
    assert [d["id"] for d in data] == ["p-old", "p-new"]
    assert set(data[0]) == {"id", "name", "deleted_at", "deleted_for_good_at"}
    assert "internal_note" not in result.output
    deleted = datetime.fromisoformat(data[0]["deleted_at"].replace("Z", "+00:00"))
    gone = datetime.fromisoformat(data[0]["deleted_for_good_at"].replace("Z", "+00:00"))
    assert gone - deleted == timedelta(days=7)


def test_an_empty_json_trash_is_an_empty_array(api, no_terminal):
    api.trash = []

    assert json.loads(runner.invoke(cli.app, ["projects", "trash", "--json"]).stdout) == []


def test_a_project_past_the_window_is_not_promised_a_restore(api, no_terminal):
    api.trash = [row("p-late", "Too late", 7.5)]

    out = said(runner.invoke(cli.app, ["projects", "trash"]))

    assert "Too late" in out and "any moment" in out or "about to be deleted" in out or "deleted for good" in out


# -- restore --------------------------------------------------------------------------------------------------------


def test_restore_brings_a_project_back_and_says_what_is_not_restored(api, no_terminal):
    result = runner.invoke(cli.app, ["projects", "restore", "p-old"])

    out = said(result)
    assert result.exit_code == 0, result.output
    assert posts(api) == ["POST /api/projects/p-old/restore"]
    assert "Restored" in out and "Old model [v1]" in out
    assert "share links" in out, "removing a project removed its share links, and restoring does not bring them back"


def test_restore_never_asks(api, at_terminal, monkeypatch):
    """It undoes a delete; there is nothing to protect."""
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: pytest.fail("asked before restoring"))
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: pytest.fail("asked before restoring"))

    result = runner.invoke(cli.app, ["projects", "restore", "p-new"])

    assert result.exit_code == 0 and posts(api)


def test_restore_accepts_the_start_of_an_id(api, no_terminal):
    runner.invoke(cli.app, ["projects", "restore", "p-o"])

    assert posts(api) == ["POST /api/projects/p-old/restore"]


def test_restore_accepts_the_name(api, no_terminal):
    runner.invoke(cli.app, ["projects", "restore", "routing"])

    assert posts(api) == ["POST /api/projects/p-new/restore"]


def test_two_matches_are_not_guessed_between(api, no_terminal):
    api.trash = [row("p-aaa1", "Same", 1), row("p-aaa2", "Other", 2)]

    result = runner.invoke(cli.app, ["projects", "restore", "p-aaa"])

    assert result.exit_code == 1 and "more than one" in said(result) and posts(api) == []


def test_an_unknown_project_points_at_the_trash_list(api, no_terminal):
    result = runner.invoke(cli.app, ["projects", "restore", "nowhere"])

    assert result.exit_code == 1 and "harumi projects trash" in said(result) and posts(api) == []


def test_without_an_id_a_terminal_picks_from_the_trash(api, at_terminal, monkeypatch):
    shown = {}

    def pick(question, options, **kw):
        shown.update(question=question, values=[o.value for o in options])
        return "p-new"

    monkeypatch.setattr(interactive, "select", pick)

    runner.invoke(cli.app, ["projects", "restore"])

    assert "restore" in shown["question"].lower() and shown["values"] == ["p-old", "p-new"]
    assert posts(api) == ["POST /api/projects/p-new/restore"]


def test_without_an_id_and_no_terminal_it_lists_the_trash_and_restores_nothing(api, no_terminal):
    result = runner.invoke(cli.app, ["projects", "restore"])

    assert result.exit_code == 1 and "Routing" in said(result) and posts(api) == []


def test_restoring_from_an_empty_trash_says_there_is_nothing_to_restore(api, at_terminal):
    api.trash = []

    result = runner.invoke(cli.app, ["projects", "restore"])

    assert result.exit_code == 1 and "nothing" in said(result).lower()


def test_a_server_refusal_is_shown_not_a_traceback(api, no_terminal):
    api.fail = (410, {"detail": "This project was deleted for good."})

    result = runner.invoke(cli.app, ["projects", "restore", "p-old"])

    assert result.exit_code == 1 and "deleted for good" in said(result) and "Traceback" not in result.output


# -- where the old advice led ---------------------------------------------------------------------------------------


def test_rm_now_points_at_the_command_instead_of_the_web_app(monkeypatch):
    calls = []

    def handler(request):
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"id": "p1", "name": "Demo", "notebook_ids": [], "deleted_at": iso()})

    monkeypatch.setattr(
        cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)

    result = runner.invoke(cli.app, ["projects", "rm", "p1", "--yes"])

    out = said(result)
    assert result.exit_code == 0 and "harumi projects restore" in out and "web app" not in out


def test_the_help_lists_both():
    out = runner.invoke(cli.app, ["projects", "--help"]).output

    assert "trash" in out and "restore" in out
