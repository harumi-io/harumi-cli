"""`harumi projects rm` moves a project to the Trash; it does not erase it.

harumi-api's DELETE /projects/{id} is a soft delete (src/api/projects/services/projects.py
`delete`): the project is frozen at once, share links are revoked for good, and it can be
restored (POST /projects/{id}/restore) for `project_trash_retention_days`, 7 by default,
after which a daily sweep removes it permanently. The web app has a Trash page for it.
The CLI used to say "This cannot be undone", which sent people away from the one thing that
could save their work. Organization deletion, by contrast, really is permanent (the API says it
is deliberately not soft-deleted), so that wording stays.
"""

from __future__ import annotations

import ast
import pathlib
import re

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config
from harumi.client import Client

runner = CliRunner()
SOURCE = pathlib.Path(cli.__file__)


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


PROJECT = {"id": "proj-1", "name": "Routing [v2]", "notebook_ids": [], "role_name": "owner"}


class Api:
    def __init__(self):
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            self.deleted.append(request.url.path)
            return httpx.Response(200, json={**PROJECT, "deleted_at": "2026-10-20T16:00:00Z"})
        return httpx.Response(200, json=PROJECT)


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(
        cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler))
    )
    return a


def plain(text: str) -> str:
    return " ".join(text.split())


PERMANENT = re.compile(r"cannot be undone|can't be undone|irreversible|permanently|for good|forever", re.I)


# -- what projects rm tells you ---------------------------------------------------------------------------


def test_the_help_says_trash_and_the_window_not_that_it_is_final():
    out = plain(runner.invoke(cli.app, ["projects", "rm", "--help"]).output)

    assert out.count("7 days") == 2, "the window should be stated at the top and where the restore is explained"
    assert "Restore it from the Trash in the web app" in out and "deleted for good" in out
    assert "cannot be undone" not in out and "irreversible" not in out.lower()


def test_the_help_has_no_stray_short_lines_from_hard_wrapped_text(monkeypatch):
    """A docstring broken by hand renders as ragged lines ("down", "in the") on a narrow terminal.
    The suite pins the terminal at 200 columns, where nothing wraps, so render at 80."""
    monkeypatch.setenv("COLUMNS", "80")

    body = [l.strip() for l in runner.invoke(cli.app, ["projects", "rm", "--help"]).output.splitlines()]
    prose = [l for l in body[: body.index(next(l for l in body if l.startswith("╭")))] if l and not l.startswith("Usage")]

    assert prose and max(len(l) for l in prose) <= 80, "not rendered at 80 columns, so this test proves nothing"
    assert all(len(l) > 12 for l in prose), prose


def test_the_help_says_what_stops_at_once_including_the_share_links_that_do_not_come_back():
    """Restoring does not republish: the API revokes share links on trash and deliberately does not restore them."""
    out = plain(runner.invoke(cli.app, ["projects", "rm", "--help"]).output)

    assert "schedules" in out and "share links" in out
    assert "restoring does not bring them back" in out


def test_the_confirmation_prompt_says_trash_and_shows_the_consequences(api, monkeypatch):
    monkeypatch.setattr(cli.interactive, "is_interactive", lambda *a, **k: True)
    result = runner.invoke(cli.app, ["projects", "rm", "proj-1"], input="Routing [v2]\n")

    out = plain(result.output)
    shown_before_prompt = out.split("Type the project name")[0]
    assert "Routing [v2] goes to the Trash" in shown_before_prompt and "within 7 days" in shown_before_prompt
    assert "schedules cancelled" in shown_before_prompt and "share links are removed" in shown_before_prompt
    assert "Type the project name ('Routing [v2]') to move it to the Trash" in out
    assert not re.search(r"cannot be undone|irreversible", out, re.I)
    assert "confirm deletion" not in out  # it is not a deletion


def test_the_result_says_it_was_moved_and_how_to_get_it_back(api):
    result = runner.invoke(cli.app, ["projects", "rm", "proj-1", "--yes"])

    out = plain(result.output)
    assert result.exit_code == 0 and api.deleted == ["/api/projects/proj-1"]
    assert "Moved project Routing [v2] to the Trash" in out  # the name's brackets survive Rich
    assert "Restore it" in out and "7 days" in out and "web app" in out
    assert "Deleted project" not in out


def test_the_result_names_the_place_to_restore_from(api):
    out = plain(runner.invoke(cli.app, ["projects", "rm", "proj-1", "--yes"]).output)

    assert config.active_platform_url() in out and "Trash" in out


def test_a_wrong_name_still_aborts_and_deletes_nothing(api, monkeypatch):
    monkeypatch.setattr(cli.interactive, "is_interactive", lambda *a, **k: True)
    result = runner.invoke(cli.app, ["projects", "rm", "proj-1"], input="nope\n")

    assert "Aborted" in result.output and api.deleted == [] and result.exit_code == 1


# -- the wording is only used where it is true -------------------------------------------------------------


def functions_saying(phrase: str) -> set[str]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and phrase in sub.value:
                    found.add(node.name)
    return found


# Verified against harumi-api: each of these removes the thing for real.
#   delete_organization  "Permanently delete an organization ... Deliberately *not* soft-deleted"
#   datasources / secrets / memories / share links / project files / schedules: hard deletes, no restore route
#   repo_branch_rm: a branch ref removed from the server's repo
HARD_DELETES = {
    "org_rm", "datasources_rm", "schedules_rm", "secrets_rm", "memories_rm", "share_rm",
    "files_rm", "repo_branch_rm",
}


def test_cannot_be_undone_is_only_said_where_the_api_really_cannot_undo_it():
    """A soft delete (projects) must never say it. If a new command needs the phrase, it belongs in
    HARD_DELETES only after checking harumi-api for a restore path."""
    said = functions_saying("cannot be undone")

    assert "projects_rm" not in said and "repo_rm" not in said
    assert said <= HARD_DELETES, f"new 'cannot be undone' in: {sorted(said - HARD_DELETES)} (check harumi-api for a soft delete first)"
    assert said, "the scan found nothing, so this test would prove nothing"


def test_organization_deletion_still_says_it_is_permanent(monkeypatch):
    """The counter-example: the API purges an organization's projects at once and does not soft-delete it."""
    assert "org_rm" in functions_saying("cannot be undone")
    asked, sent = [], []

    def handler(request):
        sent.append(f"{request.method} {request.url.path}")
        if request.method != "GET":
            raise AssertionError("deleted without asking")
        return httpx.Response(200, json=[{"id": "6d1c0aa5-0000-4000-8000-0000000000a1", "business_name": "Acme", "role": "owner"}])  # looked up to name it

    monkeypatch.setattr(cli, "_get_client", lambda *a, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(cli.interactive, "is_interactive", lambda *a, **k: True)
    # The organization is the one delete that asks for its name rather than a y/N; what it must do first
    # is say, in the words below, that this is permanent and what goes with it.
    monkeypatch.setattr(cli.typer, "prompt", lambda text, **k: asked.append(text) or "not the name")

    result = runner.invoke(cli.app, ["org", "rm", "6d1c0aa5-0000-4000-8000-0000000000a1"])

    shown = " ".join(result.output.split())
    assert asked and "organization" in asked[0] and "Acme" in asked[0]
    assert "cannot be undone" in shown and "Every project in it is deleted" in shown and "Acme" in shown  # said before it asks
    assert asked[0].startswith("Type the organization name")  # and what it asks for is the name, not a y/N
    assert sent == ["GET /api/users/organizations"] and result.exit_code == 1  # a wrong name deletes nothing


# -- repo rm is a commit, so it can be undone --------------------------------------------------------------


def test_removing_files_from_the_repo_says_it_is_a_commit_that_can_be_reverted(api, monkeypatch):
    prompts = []
    monkeypatch.setattr(cli.typer, "confirm", lambda text, **k: prompts.append(text) or False)

    runner.invoke(cli.app, ["repo", "rm", "old.py", "--project", "proj-1"])

    assert prompts and "cannot be undone" not in prompts[0]
    assert "commit" in prompts[0] and "git revert" in prompts[0]


# -- the skill reference says the same thing ---------------------------------------------------------------


def reference() -> str:
    return (SOURCE.parents[2] / ".agents/skills/harumi-cli/references/commands.md").read_text(encoding="utf-8")


def test_the_skill_reference_tells_an_agent_that_projects_rm_goes_to_the_trash():
    """An agent asked to undo a `projects rm` reads this. Saying 'deleted' sends it, and the user, the wrong way."""
    text = reference()
    section = text[text.index("- **`rm`**: `DELETE /projects/{id}`") :].split("\n")[0]

    assert "Trash" in section and "7 days" in section and "no restore command" in section
    assert "web app" in section and "share links" in section
    assert "cannot be undone" not in section


def test_the_skill_reference_still_calls_organization_deletion_permanent():
    text = reference()
    section = text[text.index("- **`rm`**: `DELETE /users/organizations/{id}`") :].split("\n")[0]

    assert "Permanent" in section
