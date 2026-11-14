"""`harumi issues`: the project's issues from a terminal, through harumi-api.

The API owns the status-label scheme, so these tests pin what the CLI sends (a status, never a label
name for a column) and what it prints, against a fake harumi-api.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.client import Client

try:  # click 8.1 mixes stderr into the output unless told not to; 8.2 removed the switch
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()

P = "proj-1"
ANA = {"id": "u-1", "name": "Ana Souza"}


def _issue(number: int, title: str, status: str = "todo", **extra) -> dict:
    return {
        "number": number, "title": title, "body": extra.pop("body", ""), "status": status, "labels": extra.pop("labels", []),
        "assignees": extra.pop("assignees", []), "author": ANA, "comment_count": extra.pop("comment_count", 0),
        "by_assistant": extra.pop("by_assistant", False), "created_at": "2026-11-01T10:00:00Z", "updated_at": "2026-11-02T10:00:00Z", **extra,
    }


class Api:
    def __init__(self):
        self.issues = [
            _issue(1, "Costs ignore overtime", labels=[{"name": "bug", "color": "d73a4a"}], assignees=[ANA], comment_count=1, body="Line one\nLine [two]"),
            _issue(2, "Add a night shift", status="in_progress", by_assistant=True),
            _issue(3, "Old report", status="done"),
        ]
        self.calls: list[tuple[str, str, dict, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        params = {k: request.url.params.get_list(k) for k in request.url.params.keys()}
        self.calls.append((request.method, path, params, body))
        base = f"/api/projects/{P}/issues"
        if request.method == "GET" and path == base:
            status = params.get("status", ["open"])[0]
            wanted = {"open": ("todo", "in_progress"), "in_progress": ("in_progress",), "done": ("done",), "all": ("todo", "in_progress", "done")}[status]
            items = [i for i in self.issues if i["status"] in wanted]
            page, per_page = int(params.get("page", ["1"])[0]), int(params.get("per_page", ["30"])[0])
            return httpx.Response(200, json={"items": items[(page - 1) * per_page : page * per_page], "total": len(items), "page": page, "per_page": per_page})
        if path.startswith(base + "/"):
            rest = path[len(base) + 1:].split("/")
            issue = next((i for i in self.issues if str(i["number"]) == rest[0]), None)
            if issue is None:
                return httpx.Response(404, json={"detail": "Issue not found"})
            if len(rest) == 2 and rest[1] == "comments":
                if request.method == "POST":
                    return httpx.Response(201, json={"id": 9, "body": body["body"], "author": ANA, "by_assistant": False, "created_at": "2026-11-03T09:00:00Z"})
                return httpx.Response(200, json=[{"id": 5, "body": "On it", "author": ANA, "by_assistant": True, "created_at": "2026-11-02T11:30:00Z"}])
            if request.method == "PATCH":
                return httpx.Response(200, json={**issue, **{k: v for k, v in body.items() if k in ("title", "body", "status")}})
            return httpx.Response(200, json=issue)
        if request.method == "POST" and path == base:
            return httpx.Response(201, json=_issue(4, body["title"], body["status"], body=body["body"]))
        return httpx.Response(404, json={"detail": "not routed " + path})


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch, isolate_harumi_home):
    isolate_harumi_home(tmp_path / "home")
    monkeypatch.setattr(cli.console, "_width", 120)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler)))
    monkeypatch.setattr(cli, "_resolve_project", lambda p, *a_, **k: p or P)
    return a


def run(*args: str, input: str | None = None):
    return runner.invoke(cli.app, ["issues", *args], input=input)


def test_list_shows_open_issues_with_their_column(api):
    result = run("list")
    assert result.exit_code == 0, result.output
    assert "Costs ignore overtime" in result.stdout and "in progress" in result.stdout
    assert "Old report" not in result.stdout
    assert "(assistant)" in result.stdout


def test_list_todo_hides_in_progress_issues(api):
    result = run("list", "--state", "todo")
    assert "Costs ignore overtime" in result.stdout and "Add a night shift" not in result.stdout


def test_list_todo_pages_after_filtering(api):
    # Page 1 of the open list holds only in-progress issues; the to-do ones sit behind them.
    api.issues = [_issue(n, f"Busy {n}", status="in_progress") for n in range(1, 4)] + [_issue(n, f"Idea {n}") for n in range(4, 9)]
    first = json.loads(run("list", "--state", "todo", "--limit", "2", "--json").stdout)
    assert [i["number"] for i in first["items"]] == [4, 5] and first["total"] == 5
    last = json.loads(run("list", "--state", "todo", "--limit", "2", "--page", "3", "--json").stdout)
    assert [i["number"] for i in last["items"]] == [8]
    hint = run("list", "--state", "todo", "--limit", "2").stdout
    assert "Showing 2 of 5" in hint and "--page 2" in hint


def test_list_todo_walks_every_api_page(api):
    api.issues = [_issue(n, f"Busy {n}", status="in_progress") for n in range(1, 151)] + [_issue(151, "The only idea")]
    data = json.loads(run("list", "--state", "todo", "--json").stdout)
    assert [i["number"] for i in data["items"]] == [151] and data["total"] == 1


def test_create_rejects_an_empty_title_and_drops_empty_labels(api):
    assert run("create", "--title", "  ").exit_code == 1
    assert not [c for c in api.calls if c[0] == "POST"]
    run("create", "--title", "Real", "--label", "", "-s", "in-progress")
    _, _, _, body = api.calls[-1]
    assert "labels" not in body and body["status"] == "in_progress"


def test_list_sends_the_state_labels_and_search_to_the_api(api):
    run("list", "--state", "in-progress", "--label", "bug", "--label", "urgent", "-q", "shift", "-a", "u-1")
    _, _, params, _ = api.calls[-1]
    assert params["status"] == ["in_progress"] and params["label"] == ["bug", "urgent"]
    assert params["q"] == ["shift"] and params["assignee"] == ["u-1"]


def test_list_json_is_one_document(api):
    result = run("list", "--state", "all", "--json")
    data = json.loads(result.stdout)
    assert [i["number"] for i in data["items"]] == [1, 2, 3]
    assert data["items"][0]["labels"] == ["bug"] and data["items"][0]["assignees"] == [ANA]
    assert "body" not in data["items"][0]


def test_list_rejects_an_unknown_state(api):
    result = run("list", "--state", "blocked")
    assert result.exit_code == 1 and "not a state" in result.output


def test_view_shows_the_description_and_comments(api):
    result = run("view", "#1")
    assert result.exit_code == 0, result.output
    assert "#1 Costs ignore overtime" in result.stdout
    assert "Line [two]" in result.stdout  # brackets are text, not styling
    assert "On it" in result.stdout and "via the assistant" in result.stdout


def test_view_json_includes_body_and_comments(api):
    data = json.loads(run("view", "1", "--json").stdout)
    assert data["body"].startswith("Line one") and data["comments"][0]["by_assistant"] is True


def test_view_unknown_issue_points_at_the_list(api):
    result = run("view", "99")
    assert result.exit_code == 1 and "harumi issues list" in result.output


def test_issue_number_must_be_a_number(api):
    result = run("view", "abc")
    assert result.exit_code == 1 and "not an issue number" in result.output


def test_create_sends_title_body_labels_and_status(api):
    result = run("create", "--title", "Check the model", "--body", "Details", "-l", "bug", "-a", "u-1", "--status", "in-progress")
    assert result.exit_code == 0, result.output
    method, _, _, body = api.calls[-1]
    assert method == "POST"
    assert body == {"title": "Check the model", "body": "Details", "status": "in_progress", "labels": ["bug"], "assignees": ["u-1"]}
    assert "Filed" in result.stdout


def test_create_reads_the_body_from_stdin(api):
    run("create", "--title", "From a pipe", "--body-file", "-", input="piped text")
    assert api.calls[-1][3]["body"] == "piped text"


def test_create_refuses_two_ways_to_give_the_body(api):
    result = run("create", "--title", "x", "--body", "a", "--body-file", "-", input="b")
    assert result.exit_code == 1 and "one way only" in result.output


def test_edit_needs_a_terminal_for_the_editor(api):
    result = run("edit", "1", "--edit")
    assert result.exit_code == 1 and "needs a terminal" in result.output


def test_edit_sends_only_what_changed(api):
    run("edit", "1", "--title", "Costs ignore overtime pay")
    assert api.calls[-1][3] == {"title": "Costs ignore overtime pay"}


def test_edit_with_nothing_to_change_fails(api):
    result = run("edit", "1")
    assert result.exit_code == 1 and "Nothing to change" in result.output


def test_edit_empty_label_clears_them(api):
    run("edit", "1", "--label", "")
    assert api.calls[-1][3] == {"labels": []}


@pytest.mark.parametrize(
    ("args", "status"),
    [(("move", "2", "--status", "done"), "done"), (("move", "2", "--status", "in-progress"), "in_progress"), (("close", "1"), "done"), (("reopen", "3"), "todo")],
)
def test_moves_send_a_status_not_a_label(api, args, status):
    result = run(*args)
    assert result.exit_code == 0, result.output
    method, _, _, body = api.calls[-1]
    assert method == "PATCH" and body == {"status": status}


def test_move_rejects_an_unknown_status(api):
    result = run("move", "1", "--status", "blocked")
    assert result.exit_code == 1 and "not a status" in result.output


def test_comment_posts_the_text(api):
    result = run("comment", "1", "--body", "Fixed in the last push.")
    assert result.exit_code == 0, result.output
    assert api.calls[-1][3] == {"body": "Fixed in the last push."}


def test_comment_without_text_fails(api):
    result = run("comment", "1")
    assert result.exit_code == 1 and "Say what to comment" in result.output
