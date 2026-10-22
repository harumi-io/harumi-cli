"""link, new and the projects commands: no id or name typed from memory, and rm stays careful."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.client import Client

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


PROJECTS = {
    "projects": [
        {"id": "proj-1", "name": "Solver", "notebook_ids": [], "updated_at": "2026-10-15T10:00:00Z", "role_name": "owner"},
        {"id": "proj-2", "name": "Routing", "notebook_ids": [], "updated_at": "2026-10-01T10:00:00Z", "role_name": "member"},
    ],
    "total_count": 2,
}

# Fetched by id only, so the pickers that list projects keep seeing the two above.
ORG_PROJECT = {"id": "proj-org", "name": "Fleet", "notebook_ids": [], "customer_id": "org-acme-0001", "updated_at": "2026-09-01T10:00:00Z", "role_name": "member"}


class Server:
    def __init__(self):
        self.calls: list[str] = []
        self.renamed: dict = {}
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        self.calls.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if request.method == "GET" and path.endswith("/users/organizations"):
            return httpx.Response(200, json=[{"id": "org-acme-0001", "business_name": "Acme Corp"}])
        if request.method == "GET" and path.endswith("/projects"):
            return httpx.Response(200, json=PROJECTS)
        if request.method == "GET" and "/projects/" in path and path.endswith("/repo"):
            return httpx.Response(200, json={"owner": "acme", "name": "s", "clone_url": "https://g/acme/s.git", "default_branch": "main"})
        pid = path.rstrip("/").split("/")[-1]
        row = next((p for p in [*PROJECTS["projects"], ORG_PROJECT] if p["id"] == pid), None)
        if request.method == "GET" and row:
            return httpx.Response(200, json=row)
        if request.method in ("PATCH", "PUT") and row:
            self.renamed = json.loads(request.content)
            return httpx.Response(200, json={**row, **self.renamed})
        if request.method == "DELETE" and row:
            self.deleted.append(pid)
            return httpx.Response(200, json=row)
        return httpx.Response(404, json={"detail": "not routed"})


@pytest.fixture
def server(monkeypatch):
    s = Server()
    monkeypatch.setattr(
        cli, "_get_client", lambda *a, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(s.handler))
    )
    return s


def pick(monkeypatch, value):
    seen = {}

    def select(message, options, *, flag=None, default=None):
        seen.update(message=message, values=[o.value for o in options], flag=flag)
        return value

    monkeypatch.setattr(interactive, "select", select)
    return seen


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


@pytest.fixture
def at_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)


# -- link ------------------------------------------------------------------------------------


@pytest.fixture
def git_stubs(monkeypatch, tmp_path):
    bound = []
    monkeypatch.setattr(cli, "_bind_and_configure_remote", lambda pid, repo, cwd=None: bound.append(pid) or True)
    monkeypatch.chdir(tmp_path)
    return bound


def test_link_without_a_project_offers_a_picker_instead_of_asking_for_a_typed_id(server, git_stubs, monkeypatch):
    seen = pick(monkeypatch, "proj-2")

    result = runner.invoke(cli.app, ["link"])

    assert result.exit_code == 0, result.output
    assert seen["values"] == ["proj-1", "proj-2"]
    assert seen["flag"] == "--project"
    assert git_stubs == ["proj-2"]
    assert "Project id to bind" not in result.output  # the old typed prompt is gone


def test_link_without_a_project_or_a_terminal_lists_the_projects_and_names_the_flag(server, git_stubs, no_terminal):
    result = runner.invoke(cli.app, ["link"])

    assert result.exit_code == 1
    assert "Solver" in result.output and "proj-1" in result.output and "--project" in result.output
    assert git_stubs == []


def test_link_picker_backed_out_binds_nothing(server, git_stubs, monkeypatch):
    pick(monkeypatch, None)
    result = runner.invoke(cli.app, ["link"])
    assert result.exit_code == 0 and git_stubs == []


def test_link_with_a_project_flag_is_unchanged(server, git_stubs, monkeypatch):
    monkeypatch.setattr(interactive, "select", lambda *a, **k: pytest.fail("no picker when the project is given"))
    assert runner.invoke(cli.app, ["link", "--project", "proj-1"]).exit_code == 0
    assert git_stubs == ["proj-1"]


# -- new ----------------------------------------------------------------------------------------


def test_new_without_a_name_asks_for_one_at_a_terminal(server, at_terminal, monkeypatch):
    got = {}
    monkeypatch.setattr(cli, "_new_impl", lambda **kw: got.update(kw))

    result = runner.invoke(cli.app, ["new"], input="Routing demo\n")

    assert result.exit_code == 0, result.output
    assert got["name"] == "Routing demo"


def test_new_without_a_name_and_without_a_terminal_says_how_to_give_one(server, no_terminal, monkeypatch):
    monkeypatch.setattr(cli, "_new_impl", lambda **kw: pytest.fail("must not run"))

    result = runner.invoke(cli.app, ["new"])

    assert result.exit_code == 1
    assert 'harumi new "My Project"' in result.output


def test_new_with_a_name_asks_nothing(server, at_terminal, monkeypatch):
    got = {}
    monkeypatch.setattr(cli, "_new_impl", lambda **kw: got.update(kw))
    runner.invoke(cli.app, ["new", "Given"])
    assert got["name"] == "Given"


# -- projects get / rename / rm ----------------------------------------------------------------


def test_projects_get_without_an_id_picks_from_the_list(server, monkeypatch):
    seen = pick(monkeypatch, "proj-1")

    result = runner.invoke(cli.app, ["projects", "get"])

    assert result.exit_code == 0, result.output
    assert seen["values"] == ["proj-1", "proj-2"]
    assert "Solver" in result.output


def test_projects_get_names_the_workspace_of_an_organization_project(server):
    """Every org-owned project used to crash here: the workspace lookup was called without
    its client, and a test stub with the same wrong signature hid it. The real lookup runs."""
    result = runner.invoke(cli.app, ["projects", "get", "proj-org"])

    assert result.exit_code == 0, result.output
    assert "Acme Corp" in result.output
    assert "GET /api/users/organizations" in server.calls


def test_projects_get_is_a_readable_summary_not_a_table_of_field_names(server):
    out = runner.invoke(cli.app, ["projects", "get", "proj-1"]).output

    assert "Solver" in out and "owner" in out and "ago" in out
    assert "/projects/proj-1" in out  # where to open it
    assert "customer_id" not in out and "notebook_ids" not in out and "field" not in out


def test_projects_get_without_an_id_or_a_terminal_lists_the_choices(server, no_terminal):
    result = runner.invoke(cli.app, ["projects", "get"])
    assert result.exit_code == 1 and "PROJECT_ID" in result.output


def test_rename_with_both_arguments_works_as_before(server, monkeypatch):
    monkeypatch.setattr(interactive, "select", lambda *a, **k: pytest.fail("no picker"))

    result = runner.invoke(cli.app, ["projects", "rename", "proj-1", "Better name"])

    assert result.exit_code == 0, result.output
    assert server.renamed == {"name": "Better name"}


def test_rename_without_an_id_picks_one_then_asks_for_the_name(server, at_terminal, monkeypatch):
    pick(monkeypatch, "proj-2")

    result = runner.invoke(cli.app, ["projects", "rename"], input="Fresh name\n")

    assert result.exit_code == 0, result.output
    assert server.renamed == {"name": "Fresh name"}
    assert "PATCH /api/projects/proj-2" in server.calls or "PUT /api/projects/proj-2" in server.calls


def test_rename_offers_the_current_name_as_the_default(server, at_terminal):
    runner.invoke(cli.app, ["projects", "rename", "proj-1"], input="\n")
    assert server.renamed == {"name": "Solver"}  # Enter keeps it


def test_rename_without_a_name_or_a_terminal_says_how_to_give_one(server, no_terminal):
    result = runner.invoke(cli.app, ["projects", "rename", "proj-1"])

    assert result.exit_code == 1
    assert "New name" in result.output or "projects rename" in result.output
    assert server.renamed == {}


def test_rm_with_an_id_still_requires_the_typed_name(server, at_terminal):
    result = runner.invoke(cli.app, ["projects", "rm", "proj-1"], input="wrong\n")

    assert "Aborted" in result.output and result.exit_code == 1
    assert server.deleted == []


def test_picking_a_project_to_delete_is_not_consent_the_typed_name_is_still_required(server, monkeypatch, at_terminal):
    """The safety property. A picker makes it easy to land on the wrong row, so
    choosing from a list must never replace typing the name."""
    pick(monkeypatch, "proj-2")

    declined = runner.invoke(cli.app, ["projects", "rm"], input="not the name\n")
    assert server.deleted == [], "deleted on a mismatched name"
    assert "Aborted" in declined.output

    accepted = runner.invoke(cli.app, ["projects", "rm"], input="Routing\n")
    assert accepted.exit_code == 0, accepted.output
    assert server.deleted == ["proj-2"]


def test_rm_picker_backed_out_deletes_nothing(server, monkeypatch):
    pick(monkeypatch, None)
    result = runner.invoke(cli.app, ["projects", "rm"])
    assert result.exit_code == 0 and server.deleted == []


def test_rm_yes_with_an_id_still_skips_the_question_for_scripts(server):
    result = runner.invoke(cli.app, ["projects", "rm", "proj-1", "--yes"])
    assert result.exit_code == 0 and server.deleted == ["proj-1"]


def test_rm_without_an_id_and_without_a_terminal_does_not_hang_or_delete(server, no_terminal):
    result = runner.invoke(cli.app, ["projects", "rm", "--yes"])
    assert result.exit_code == 1 and server.deleted == []


# -- what to type next -----------------------------------------------------------------------------------


def test_next_steps_use_a_relative_path_when_it_is_shorter(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli._next_steps(tmp_path / "my-project")
    out = capsys.readouterr().out
    assert "cd my-project" in out and str(tmp_path) not in out
    assert "harumi run --watch" in out


def test_next_steps_for_the_current_folder_skip_the_cd(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cli._next_steps(tmp_path)
    out = capsys.readouterr().out
    assert "cd" not in out and "harumi run --watch" in out


def test_next_steps_for_a_folder_elsewhere_use_the_full_path(tmp_path, monkeypatch, capsys):
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    cli._next_steps(elsewhere)
    assert f"cd {elsewhere}" in capsys.readouterr().out


def test_rm_with_yes_and_no_id_refuses_instead_of_deleting_whatever_the_picker_returns(server, monkeypatch):
    """--yes skips the typed-name check, so a mis-click in a list would delete a project.
    With --yes the id has to be written down."""
    pick(monkeypatch, "proj-1")

    result = runner.invoke(cli.app, ["projects", "rm", "--yes"])

    assert result.exit_code != 0
    assert "project id" in " ".join(result.output.split()).lower()
    assert server.deleted == []
