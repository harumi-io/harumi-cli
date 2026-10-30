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
    # Exact wording, row by row: 6.5 days in leaves 11 hours, 1.2 days in leaves 5 days.
    old_row = next(l for l in result.output.splitlines() if "p-old" in l)
    new_row = next(l for l in result.output.splitlines() if "p-new" in l)
    assert "11 hours left" in " ".join(old_row.split())
    assert "5 days left" in " ".join(new_row.split())
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

    after_name = out.split("Too late", 1)[1]
    assert "about to be deleted for good" in after_name
    assert "days left" not in after_name and "hours left" not in after_name, "must not promise time that is gone"


def test_a_project_with_no_deletion_time_sorts_last_and_does_not_break_the_list(api, no_terminal):
    """The `far` fallback in the sort: a row with `deleted_at` null must not raise or jump the queue."""
    api.trash = [{**row("p-none", "No date", 1), "deleted_at": None}, row("p-soon", "Soon", 6.5)]

    result = runner.invoke(cli.app, ["projects", "trash"])

    out = result.output
    assert result.exit_code == 0 and out.index("Soon") < out.index("No date")


@pytest.mark.parametrize("stamp", ["2026-10-30T10:00:00", "2026-10-30T10:00:00Z", "2026-10-30T10:00:00+00:00", "2026-10-30T06:00:00-04:00"])
def test_json_dates_are_always_utc_with_a_z(api, no_terminal, stamp):
    """The bot's finding: a time with no offset came out zone-less. All four spellings are the same instant."""
    api.trash = [{**row("p-1", "One", 1), "deleted_at": stamp}]

    data = json.loads(runner.invoke(cli.app, ["projects", "trash", "--json"]).stdout)

    assert data[0]["deleted_at"] == "2026-10-30T10:00:00Z"
    assert data[0]["deleted_for_good_at"] == "2026-11-06T10:00:00Z"


def test_the_trash_is_read_in_one_request(api, no_terminal):
    """The server's trash endpoint takes no paging, so one request is all of it (stated in the client's docstring)."""
    runner.invoke(cli.app, ["projects", "trash"])

    assert api.calls == ["GET /api/projects/trash"]


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


def test_two_matches_are_not_guessed_between_and_both_are_named(api, no_terminal):
    api.trash = [row("p-aaa1", "Same", 1), row("p-aaa2", "Other", 2)]

    result = runner.invoke(cli.app, ["projects", "restore", "p-aaa"])

    out = said(result)
    assert result.exit_code == 1 and "more than one" in out and posts(api) == []
    assert "p-aaa1" in out and "Same" in out and "p-aaa2" in out and "Other" in out, "saves a round trip to find which"


def test_an_exact_name_beats_the_start_of_another_projects_id(api, no_terminal):
    """The bot noted a name is matched before an id prefix. That is deliberate: what you type in full is what you mean."""
    api.trash = [row("p-old", "p-new", 1), row("p-new", "Routing", 2)]

    runner.invoke(cli.app, ["projects", "restore", "p-new"])

    assert posts(api) == ["POST /api/projects/p-new/restore"], "the exact id wins over the name of another project"


def test_two_projects_with_the_same_name_are_not_guessed_between(api, no_terminal):
    api.trash = [row("p-1", "Routing", 1), row("p-2", "Routing", 2)]

    result = runner.invoke(cli.app, ["projects", "restore", "routing"])

    assert result.exit_code == 1 and posts(api) == [] and "p-1" in said(result) and "p-2" in said(result)


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
