"""Behavior tests for the Typer command layer.

Every command in cli.py builds its Client through the single `_get_client`
chokepoint, so patching that one function routes the whole CLI at an
httpx.MockTransport — no network, no real credentials, no backend. What these
tests cover that tests/test_client.py cannot: argument parsing, the
.harumi-binding fallback in `_resolve_project`, the `_handle_errors` exit
codes, and what actually gets rendered to the terminal.
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

import harumi.cli as cli
from harumi.client import Client

runner = CliRunner()


def test_every_command_builds():
    """Guards the dependency floor, and pins the command tree to the
    committed cli-surface.json contract.

    Typer builds each command's click Options at import time, so a typer/click
    pairing that rejects an Option signature takes down the entire CLI —
    `harumi --version` included — with a TypeError before any command body
    runs. `compileall` cannot see it. This walks the whole tree so the failure
    surfaces here instead of on a user's machine.

    The contract comparison catches the other failure mode: a command added,
    removed, renamed, or re-flagged without regenerating cli-surface.json —
    which is the file harumi-docs' weekly drift check reads to find commands
    and flags its CLI reference hasn't documented yet. An unregenerated
    contract would let that check silently pass on stale data.
    """
    from typer.main import get_command

    def leaves(command, path):
        subcommands = getattr(command, "commands", None)
        if subcommands:
            for name, sub in subcommands.items():
                yield from leaves(sub, path + [name])
        else:
            yield " ".join(path)

    names = list(leaves(get_command(cli.app), []))

    # Every leaf must also expose --help, which forces full param construction.
    for name in names:
        result = runner.invoke(cli.app, name.split() + ["--help"])
        assert result.exit_code == 0, f"`harumi {name} --help` failed:\n{result.output}"

    # Sanity floor: the tree should not silently shrink to nothing. Not an
    # exact count (new commands land in other branches/PRs) — just enough
    # margin below the actual count (68 as of this commit) to catch a large
    # accidental deletion of the command tree, e.g. a bad merge or a
    # sub-Typer losing its `add_typer` registration.
    assert len(names) > 50

    import json
    from pathlib import Path

    from scripts.emit_cli_surface import build_surface

    contract_path = Path(__file__).parent.parent / "cli-surface.json"
    committed = json.loads(contract_path.read_text())
    current = build_surface()
    # cli_version is expected to differ on every release; the command tree is
    # what this test protects.
    committed_commands = committed["commands"]
    current_commands = current["commands"]
    assert current_commands == committed_commands, (
        "cli-surface.json is stale — regenerate it with:\n"
        "    python scripts/emit_cli_surface.py > cli-surface.json"
    )


def test_cli_surface_normalizes_click_builtin_type_names():
    """Typer >=0.27's vendored click names STRING/INT 'str'/'int'; every real
    click release (8.1-8.4, which is what Python 3.9's typer 0.23.x uses) names
    them 'text'/'integer'. The committed contract uses the vendored spelling,
    so the emitter must collapse both or compat (3.9) fails
    test_every_command_builds on a label rather than a real flag change.

    `integer` is the case that actually broke CI: the original normalizer only
    handled `text`, so the 7 int-typed params (e.g. `--port`) mismatched.
    """
    from types import SimpleNamespace

    from scripts.emit_cli_surface import _param_info

    def type_of(name):
        return _param_info(
            SimpleNamespace(
                opts=["--x"],
                type=SimpleNamespace(name=name),
                required=False,
                default=None,
                help=None,
            )
        )["type"]

    # Real click spellings collapse onto the vendored ones...
    assert type_of("text") == "str"
    assert type_of("integer") == "int"
    # ...the vendored spellings pass through unchanged (idempotent)...
    assert type_of("str") == "str"
    assert type_of("int") == "int"
    # ...and labels that agree across both flavours are left alone.
    for shared in ("boolean", "path", "float", "uuid"):
        assert type_of(shared) == shared



@pytest.fixture(autouse=True)
def isolated_harumi_home(tmp_path, monkeypatch):
    """Redirect ~/.harumi to a temp dir and pre-store a session, so commands
    get past the auth check without touching the real machine.
    """
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    # Rich truncates table cells to the terminal width; widen it so assertions
    # can match full ids instead of ellipsized ones.
    monkeypatch.setenv("COLUMNS", "200")

    from harumi.config import save_credentials

    save_credentials(access_token="token-1", refresh_token="refresh-1")
    yield


class FakeApi:
    """Records requests and replays registered responses."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.requests: list[httpx.Request] = []

    def route(self, method: str, path: str, payload: object = None, status: int = 200) -> None:
        self._routes[(method, path)] = (status, payload)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def body_for(self, method: str, path: str) -> object:
        """Parsed JSON body of the last request to (method, path)."""
        import json

        for request in reversed(self.requests):
            if request.method == method and request.url.path == path:
                return json.loads(request.content)
        raise AssertionError(f"no {method} {path} request was made; saw {self.paths()}")

    def params_for(self, method: str, path: str) -> dict[str, str]:
        for request in reversed(self.requests):
            if request.method == method and request.url.path == path:
                return dict(request.url.params)
        raise AssertionError(f"no {method} {path} request was made; saw {self.paths()}")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self._routes.get((request.method, request.url.path))
        if route is None:
            # Surfaces as a failed command, so an unexpected path is loud
            # rather than silently mocked.
            return httpx.Response(502, json={"detail": f"unrouted: {request.method} {request.url.path}"})
        status, payload = route
        return httpx.Response(status, json=payload)


@pytest.fixture
def api(monkeypatch) -> FakeApi:
    fake = FakeApi()
    transport = httpx.MockTransport(fake._handle)

    def _get_client(api_url=None, git_url=None, org=None) -> Client:
        return Client(
            api_url="https://harumi-api.test/api",
            git_url=git_url,
            org_id=org,
            transport=transport,
        )

    monkeypatch.setattr(cli, "_get_client", _get_client)
    return fake


@pytest.fixture
def bound_dir(tmp_path, monkeypatch):
    """A cwd containing a .harumi/config.json binding, as `harumi init` writes."""
    import json

    harumi_dir = tmp_path / "work" / ".harumi"
    harumi_dir.mkdir(parents=True)
    (harumi_dir / "config.json").write_text(
        json.dumps(
            {
                "project_id": "proj-bound",
                "repo": {
                    "owner": "acme",
                    "name": "solver",
                    "clone_url": "https://git.harumi.test/acme/solver.git",
                    "default_branch": "main",
                },
            }
        )
    )
    monkeypatch.chdir(tmp_path / "work")
    return tmp_path / "work"


def test_projects_list_renders_each_project(api):
    api.route(
        "GET",
        "/api/projects",
        {"projects": [{"id": "p1", "name": "Routing", "notebook_ids": []}], "total_count": 1},
    )

    result = runner.invoke(cli.app, ["projects", "list"])

    assert result.exit_code == 0, result.output
    assert "p1" in result.output
    assert "Routing" in result.output


def test_projects_list_reports_empty_instead_of_an_empty_table(api):
    api.route("GET", "/api/projects", {"projects": [], "total_count": 0})

    result = runner.invoke(cli.app, ["projects", "list"])

    assert result.exit_code == 0, result.output
    assert "No projects found." in result.output


def test_projects_create_rejects_personal_with_customer_id(api):
    """The two flags name different workspaces, so passing both must be refused.

    Without this the guard could be dropped in a refactor and one flag would
    silently win, putting the project somewhere the user didn't ask for.
    """
    result = runner.invoke(
        cli.app, ["projects", "create", "Scoped", "--personal", "--customer-id", "org-other"]
    )

    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
    # Refused during argument validation, before any project was created.
    assert api.requests == []


def test_api_error_exits_nonzero_with_a_message(api):
    api.route("GET", "/api/projects", {"detail": "boom"}, status=500)

    result = runner.invoke(cli.app, ["projects", "list"])

    assert result.exit_code == 1
    assert "Error" in result.output


def test_secrets_list_prints_names_but_never_values(api):
    api.route("GET", "/api/projects/proj-1/secrets", [{"name": "API_KEY", "value": "super-secret"}])

    result = runner.invoke(cli.app, ["secrets", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "API_KEY" in result.output
    assert "super-secret" not in result.output


def test_command_needing_a_project_fails_cleanly_with_no_binding(api, tmp_path, monkeypatch):
    # chdir rather than runner.isolated_filesystem(): Typer dropped its click
    # dependency in 0.27, and the click-era CliRunner helpers went with it.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["secrets", "list"])

    assert result.exit_code == 1
    assert "--project" in result.output


def test_projects_get_requests_the_documented_route(api):
    api.route("GET", "/api/projects/p1", {"id": "p1", "name": "Routing", "notebook_ids": []})

    result = runner.invoke(cli.app, ["projects", "get", "p1"])

    assert result.exit_code == 0, result.output
    # Pins the path the CLI depends on in harumi-api.
    assert api.paths() == ["/api/projects/p1"]


def test_unknown_command_is_rejected():
    result = runner.invoke(cli.app, ["projects", "frobnicate"])

    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# harumi repo
# ---------------------------------------------------------------------------

FILE_CONTENT_B64 = "cHJpbnQoJ2hpJyk="  # print('hi')


def test_repo_ls_lists_files_and_forwards_ref(api):
    api.route(
        "GET",
        "/api/projects/proj-1/repo/files",
        [{"name": "main.py", "path": "main.py", "type": "file", "size": 10}],
    )

    result = runner.invoke(cli.app, ["repo", "ls", "--project", "proj-1", "--ref", "dev"])

    assert result.exit_code == 0, result.output
    assert "main.py" in result.output
    assert api.params_for("GET", "/api/projects/proj-1/repo/files")["ref"] == "dev"


def test_repo_cat_decodes_base64_to_stdout(api):
    api.route(
        "GET",
        "/api/projects/proj-1/repo/file-content",
        {"path": "main.py", "sha": "abc", "encoding": "base64", "content": FILE_CONTENT_B64},
    )

    result = runner.invoke(cli.app, ["repo", "cat", "main.py", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "print('hi')" in result.output


def test_repo_cat_output_flag_writes_bytes_to_disk(api, tmp_path):
    api.route(
        "GET",
        "/api/projects/proj-1/repo/file-content",
        {"path": "main.py", "sha": "abc", "encoding": "base64", "content": FILE_CONTENT_B64},
    )
    dest = tmp_path / "nested" / "main.py"

    result = runner.invoke(
        cli.app, ["repo", "cat", "main.py", "--project", "proj-1", "-o", str(dest)]
    )

    assert result.exit_code == 0, result.output
    # Also proves the command creates missing parent dirs.
    assert dest.read_bytes() == b"print('hi')"


def test_repo_cat_rejects_non_utf8_without_output(api):
    api.route(
        "GET",
        "/api/projects/proj-1/repo/file-content",
        {"path": "a.bin", "sha": "abc", "encoding": "base64", "content": "/w=="},  # 0xFF
    )

    result = runner.invoke(cli.app, ["repo", "cat", "a.bin", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "--output" in result.output


def test_repo_put_sends_create_when_file_is_absent(api, tmp_path):
    local = tmp_path / "new.py"
    local.write_text("x = 1")
    api.route("GET", "/api/projects/proj-1/repo/file-content", {"detail": "nope"}, status=404)
    api.route("POST", "/api/projects/proj-1/repo/changes", {"commit_sha": "cafe", "changed": 1})

    result = runner.invoke(
        cli.app, ["repo", "put", str(local), "new.py", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    assert "Created" in result.output
    ops = api.body_for("POST", "/api/projects/proj-1/repo/changes")["operations"]
    assert ops[0]["action"] == "create"


def test_repo_put_sends_update_when_file_exists(api, tmp_path):
    local = tmp_path / "main.py"
    local.write_text("x = 2")
    api.route(
        "GET",
        "/api/projects/proj-1/repo/file-content",
        {"path": "main.py", "sha": "abc", "encoding": "base64", "content": FILE_CONTENT_B64},
    )
    api.route("POST", "/api/projects/proj-1/repo/changes", {"commit_sha": "cafe", "changed": 1})

    result = runner.invoke(
        cli.app, ["repo", "put", str(local), "main.py", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    assert "Updated" in result.output
    ops = api.body_for("POST", "/api/projects/proj-1/repo/changes")["operations"]
    assert ops[0]["action"] == "update"


def test_repo_put_rejects_a_missing_local_file(api, tmp_path):
    result = runner.invoke(
        cli.app, ["repo", "put", str(tmp_path / "ghost.py"), "x.py", "--project", "proj-1"]
    )

    assert result.exit_code != 0
    assert api.requests == []


def test_repo_rm_aborts_without_confirmation(api):
    api.route("POST", "/api/projects/proj-1/repo/changes", {"commit_sha": "cafe", "changed": 1})

    result = runner.invoke(cli.app, ["repo", "rm", "main.py", "--project", "proj-1"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Aborted." in result.output
    # The safety property that matters: a declined prompt sends nothing.
    assert api.requests == []


def test_repo_rm_deletes_when_confirmed(api):
    api.route("POST", "/api/projects/proj-1/repo/changes", {"commit_sha": "cafe", "changed": 1})

    result = runner.invoke(cli.app, ["repo", "rm", "main.py", "--project", "proj-1"], input="y\n")

    assert result.exit_code == 0, result.output
    ops = api.body_for("POST", "/api/projects/proj-1/repo/changes")["operations"]
    assert ops == [{"action": "delete", "path": "main.py"}]


def test_repo_rm_yes_flag_skips_the_prompt(api):
    api.route("POST", "/api/projects/proj-1/repo/changes", {"commit_sha": "cafe", "changed": 2})

    result = runner.invoke(cli.app, ["repo", "rm", "out/", "--project", "proj-1", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output


def test_repo_mv_sends_a_move_operation(api):
    api.route("POST", "/api/projects/proj-1/repo/changes", {"commit_sha": "cafe", "changed": 1})

    result = runner.invoke(
        cli.app, ["repo", "mv", "old.py", "new.py", "--project", "proj-1", "-m", "rename"]
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("POST", "/api/projects/proj-1/repo/changes")
    assert body["operations"] == [
        {"action": "move", "from_path": "old.py", "path": "new.py"}
    ]
    assert body["message"] == "rename"


def test_repo_branches_flags_the_live_branch(api):
    api.route(
        "GET",
        "/api/projects/proj-1/repo/branches",
        [
            {"name": "main", "commit_sha": "abcdef1234", "is_live": True},
            {"name": "feature-x", "commit_sha": "deadbeef99", "is_live": False},
        ],
    )

    result = runner.invoke(cli.app, ["repo", "branches", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "main" in result.output
    assert "feature-x" in result.output
    # Commit shas are truncated to 8 chars for display.
    assert "abcdef12" in result.output
    assert "abcdef1234" not in result.output


def test_repo_branch_rm_aborts_without_confirmation(api):
    result = runner.invoke(
        cli.app, ["repo", "branch-rm", "feature-x", "--project", "proj-1"], input="n\n"
    )

    assert result.exit_code == 0, result.output
    assert "Aborted." in result.output
    assert api.requests == []


# ---------------------------------------------------------------------------
# harumi schedules
# ---------------------------------------------------------------------------

SCHEDULE = {
    "id": "sched-1",
    "project_id": "proj-1",
    "cron": "0 9 * * *",
    "start_at": "2026-01-22T09:00:00Z",
    "git_branch": "main",
    "kernel_spec": "or_python_small",
    "last_executed_at": None,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


def test_schedules_list_renders_the_cron_and_branch(api):
    api.route("GET", "/api/projects/proj-1/schedules", {"schedules": [SCHEDULE]})

    result = runner.invoke(cli.app, ["schedules", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "sched-1" in result.output
    assert "0 9 * * *" in result.output
    assert "main" in result.output


def test_schedules_list_reports_empty(api):
    api.route("GET", "/api/projects/proj-1/schedules", {"schedules": []})

    result = runner.invoke(cli.app, ["schedules", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "No schedules found." in result.output


def test_schedules_add_posts_the_cron_and_defaults_branch_to_main(api):
    api.route("POST", "/api/projects/proj-1/schedules", SCHEDULE, status=201)

    result = runner.invoke(
        cli.app, ["schedules", "add", "--cron", "0 9 * * *", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("POST", "/api/projects/proj-1/schedules")
    assert body["cron"] == "0 9 * * *"
    assert body["git_branch"] == "main"


def test_schedules_add_forwards_optional_overrides(api):
    api.route("POST", "/api/projects/proj-1/schedules", SCHEDULE, status=201)

    result = runner.invoke(
        cli.app,
        [
            "schedules", "add",
            "--cron", "*/5 * * * *",
            "--git-branch", "dev",
            "--command", "python solve.py",
            "--kernel", "or_python_large",
            "--email-to", "everyone",
            "--project", "proj-1",
        ],
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("POST", "/api/projects/proj-1/schedules")
    assert body["cron"] == "*/5 * * * *"
    assert body["git_branch"] == "dev"
    assert body["command"] == "python solve.py"
    assert body["kernel_spec"] == "or_python_large"
    assert body["email_to"] == "everyone"


def test_schedules_remove_aborts_without_confirmation(api):
    result = runner.invoke(
        cli.app, ["schedules", "remove", "sched-1", "--project", "proj-1"], input="n\n"
    )

    assert result.exit_code == 0, result.output
    assert "Aborted." in result.output
    assert api.requests == []


def test_schedules_remove_deletes_when_confirmed(api):
    api.route("DELETE", "/api/projects/proj-1/schedules/sched-1", SCHEDULE)

    result = runner.invoke(
        cli.app, ["schedules", "remove", "sched-1", "--project", "proj-1", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output
    assert api.paths() == ["/api/projects/proj-1/schedules/sched-1"]


def test_schedules_get_surfaces_a_404_as_a_clean_error(api):
    api.route("GET", "/api/projects/proj-1/schedules/nope", {"detail": "Not Found"}, status=404)

    result = runner.invoke(cli.app, ["schedules", "get", "nope", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "Error" in result.output


# ---------------------------------------------------------------------------
# harumi share
# ---------------------------------------------------------------------------

SHARE_LINK = {
    "id": "link-1",
    "project_id": "proj-1",
    "token": "tok-abc123",
    "label": "Client dashboard",
    "enabled": True,
    "chat_enabled": False,
    "run_history_enabled": False,
    "run_control_enabled": False,
    "io_control_enabled": False,
    "password_set": False,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


def test_share_list_renders_the_links_and_their_flags(api):
    api.route("GET", "/api/projects/proj-1/share-links", {"links": [SHARE_LINK]})

    result = runner.invoke(cli.app, ["share", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "link-1" in result.output
    assert "tok-abc123" in result.output


def test_share_list_reports_empty(api):
    api.route("GET", "/api/projects/proj-1/share-links", {"links": []})

    result = runner.invoke(cli.app, ["share", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "No share links found." in result.output


def test_share_get_shows_the_link_url_and_permissions(api):
    api.route("GET", "/api/projects/proj-1/share-links", {"links": [SHARE_LINK]})

    result = runner.invoke(cli.app, ["share", "get", "link-1", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "tok-abc123" in result.output
    assert "Client dashboard" in result.output


def test_share_get_unknown_link_id_fails_cleanly(api):
    api.route("GET", "/api/projects/proj-1/share-links", {"links": [SHARE_LINK]})

    result = runner.invoke(cli.app, ["share", "get", "nope", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "No share link" in result.output


def test_share_add_defaults_every_flag_to_false(api):
    api.route("POST", "/api/projects/proj-1/share-links", SHARE_LINK, status=201)

    result = runner.invoke(cli.app, ["share", "add", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    body = api.body_for("POST", "/api/projects/proj-1/share-links")
    assert body == {
        "chat_enabled": False,
        "run_history_enabled": False,
        "run_control_enabled": False,
        "io_control_enabled": False,
    }


def test_share_add_forwards_label_and_permission_flags(api):
    api.route("POST", "/api/projects/proj-1/share-links", SHARE_LINK, status=201)

    result = runner.invoke(
        cli.app,
        [
            "share", "add",
            "--label", "Internal",
            "--chat",
            "--run-history",
            "--run-control",
            "--project", "proj-1",
        ],
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("POST", "/api/projects/proj-1/share-links")
    assert body["label"] == "Internal"
    assert body["chat_enabled"] is True
    assert body["run_history_enabled"] is True
    assert body["run_control_enabled"] is True
    assert body["io_control_enabled"] is False


def test_share_update_only_sends_provided_fields(api):
    api.route("PATCH", "/api/projects/proj-1/share-links/link-1", SHARE_LINK)

    result = runner.invoke(
        cli.app,
        ["share", "update", "link-1", "--run-control", "--project", "proj-1"],
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("PATCH", "/api/projects/proj-1/share-links/link-1")
    assert body == {"run_control_enabled": True}


def test_share_update_with_no_flags_fails_without_a_request(api):
    result = runner.invoke(cli.app, ["share", "update", "link-1", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "No fields to update" in result.output
    assert api.requests == []


def test_share_remove_aborts_without_confirmation(api):
    result = runner.invoke(
        cli.app, ["share", "remove", "link-1", "--project", "proj-1"], input="n\n"
    )

    assert result.exit_code == 0, result.output
    assert "Aborted." in result.output
    assert api.requests == []


def test_share_remove_deletes_when_confirmed(api):
    api.route("DELETE", "/api/projects/proj-1/share-links/link-1", None, status=204)

    result = runner.invoke(
        cli.app, ["share", "remove", "link-1", "--project", "proj-1", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output
    assert api.paths() == ["/api/projects/proj-1/share-links/link-1"]


def test_share_rotate_mints_a_new_token(api):
    rotated = {**SHARE_LINK, "token": "tok-new456"}
    api.route("POST", "/api/projects/proj-1/share-links/link-1/rotate", rotated)

    result = runner.invoke(
        cli.app, ["share", "rotate", "link-1", "--project", "proj-1", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "tok-new456" in result.output


def test_share_set_password_prompts_and_never_echoes_it(api):
    api.route(
        "PUT", "/api/projects/proj-1/share-links/link-1/password", {**SHARE_LINK, "password_set": True}
    )

    result = runner.invoke(
        cli.app,
        ["share", "set-password", "link-1", "--project", "proj-1"],
        input="correcthorse\n",
    )

    assert result.exit_code == 0, result.output
    assert "correcthorse" not in result.output
    body = api.body_for("PUT", "/api/projects/proj-1/share-links/link-1/password")
    assert body == {"password": "correcthorse"}


def test_share_rm_password_removes_it(api):
    api.route("DELETE", "/api/projects/proj-1/share-links/link-1/password", SHARE_LINK)

    result = runner.invoke(
        cli.app, ["share", "rm-password", "link-1", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    assert "Removed" in result.output


# ---------------------------------------------------------------------------
# harumi datasources
# ---------------------------------------------------------------------------

DATASOURCE = {
    "id": "ds-1",
    "project_id": "proj-1",
    "name": "sales_db",
    "type": "postgresql",
    "host": "db.internal",
    "port": 5432,
    "database": "sales",
    "username": "reader",
    "use_proxy": False,
    "ssm_parameter_name": "/harumi/projects/proj-1/datasources/sales_db",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


def test_datasources_list_renders_the_datasource(api):
    api.route("GET", "/api/datasources/proj-1", {"datasources": [DATASOURCE], "total_count": 1})

    result = runner.invoke(cli.app, ["datasources", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "sales_db" in result.output
    assert "postgresql" in result.output


def test_datasources_add_prompts_for_credentials_and_never_echoes_them(api):
    api.route("POST", "/api/datasources/proj-1", DATASOURCE, status=201)

    result = runner.invoke(
        cli.app,
        [
            "datasources", "add", "sales_db",
            "--type", "postgresql",
            "--host", "db.internal",
            "--project", "proj-1",
        ],
        input="hunter2\nhunter2\n",
    )

    assert result.exit_code == 0, result.output
    # The secret must reach the API...
    assert api.body_for("POST", "/api/datasources/proj-1")["credentials"] == "hunter2"
    # ...but must never be echoed back to the terminal.
    assert "hunter2" not in result.output


def _write_proxy_certs(tmp_path):
    ca = tmp_path / "ca.pem"
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    ca.write_text("-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----\n")
    cert.write_text("-----BEGIN CERTIFICATE-----\nclient\n-----END CERTIFICATE-----\n")
    key.write_text("-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n")
    return ca, cert, key


def test_datasources_add_sends_the_proxy_mtls_pem_contents(api, tmp_path):
    """--use-proxy must ship all three PEMs, or the API rejects the datasource.

    The flags previously stopped at --proxy-host/--proxy-port/--proxy-server-name,
    so a proxied datasource could not be created at all: the API requires the
    certificate material whenever use_proxy is on.
    """
    api.route("POST", "/api/datasources/proj-1", DATASOURCE, status=201)
    ca, cert, key = _write_proxy_certs(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "datasources", "add", "sales_db",
            "--type", "postgresql",
            "--host", "db.internal",
            "--use-proxy",
            "--proxy-host", "vpnproxy.harumi.io",
            "--proxy-port", "1433",
            "--proxy-tls-ca-cert", str(ca),
            "--proxy-tls-client-cert", str(cert),
            "--proxy-tls-client-key", str(key),
            "--project", "proj-1",
        ],
        input="hunter2\nhunter2\n",
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("POST", "/api/datasources/proj-1")
    assert body["use_proxy"] is True
    assert body["proxy_host"] == "vpnproxy.harumi.io"
    assert body["proxy_port"] == 1433
    # The file contents travel, not the paths — the API stores these in SSM.
    assert "ca" in body["proxy_tls_ca_cert"]
    assert "BEGIN CERTIFICATE" in body["proxy_tls_ca_cert"]
    assert "client" in body["proxy_tls_client_cert"]
    assert "BEGIN PRIVATE KEY" in body["proxy_tls_client_key"]
    assert str(ca) not in str(body)
    # The private key must not be echoed to the terminal.
    assert "BEGIN PRIVATE KEY" not in result.output


def test_datasources_add_rejects_use_proxy_without_certs_before_prompting(api, tmp_path):
    """A half-specified --use-proxy fails locally, naming every missing flag.

    Worth doing client-side: the check runs before the credentials prompt, so
    the user isn't asked for a password only to have the request rejected.
    """
    api.route("POST", "/api/datasources/proj-1", DATASOURCE, status=201)

    result = runner.invoke(
        cli.app,
        [
            "datasources", "add", "sales_db",
            "--type", "postgresql",
            "--host", "db.internal",
            "--use-proxy",
            "--proxy-host", "vpnproxy.harumi.io",
            "--project", "proj-1",
        ],
    )

    assert result.exit_code == 1
    assert "--proxy-port" in result.output
    assert "--proxy-tls-ca-cert" in result.output
    assert "--proxy-tls-client-cert" in result.output
    assert "--proxy-tls-client-key" in result.output
    # Nothing should have been sent, and no password should have been asked for.
    assert api.paths() == []


def test_datasources_update_rotates_one_cert_without_resending_the_others(api, tmp_path):
    """Certificates rotate individually; the API keeps the rest of the bundle."""
    api.route("PUT", "/api/datasources/proj-1/sales_db", DATASOURCE)
    _, cert, _ = _write_proxy_certs(tmp_path)

    result = runner.invoke(
        cli.app,
        [
            "datasources", "update", "sales_db",
            "--proxy-tls-client-cert", str(cert),
            "--project", "proj-1",
        ],
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("PUT", "/api/datasources/proj-1/sales_db")
    assert "client" in body["proxy_tls_client_cert"]
    # The untouched certs must be absent, not null — the API treats a present
    # null as "clear this" and would wipe the stored CA and key.
    assert "proxy_tls_ca_cert" not in body
    assert "proxy_tls_client_key" not in body


def test_datasources_add_reports_an_unreadable_cert_path(api, tmp_path):
    api.route("POST", "/api/datasources/proj-1", DATASOURCE, status=201)

    result = runner.invoke(
        cli.app,
        [
            "datasources", "add", "sales_db",
            "--type", "postgresql",
            "--use-proxy",
            "--proxy-host", "vpnproxy.harumi.io",
            "--proxy-port", "1433",
            "--proxy-tls-ca-cert", str(tmp_path / "nope.pem"),
            "--project", "proj-1",
        ],
    )

    assert result.exit_code == 1
    assert "proxy_tls_ca_cert" in result.output
    assert "nope.pem" in result.output


def test_datasources_query_renders_rows_and_the_count(api):
    api.route(
        "POST",
        "/api/datasources/proj-1/sales_db/execute",
        {
            "columns": ["id", "total"],
            "data": [[1, 100], [2, 250]],
            "rowCount": 2,
            "wasLimited": False,
            "maxRows": 10000,
        },
    )

    result = runner.invoke(
        cli.app,
        ["datasources", "query", "sales_db", "--sql", "SELECT * FROM orders", "--project", "proj-1"],
    )

    assert result.exit_code == 0, result.output
    assert "250" in result.output
    assert "2 row(s) returned." in result.output
    body = api.body_for("POST", "/api/datasources/proj-1/sales_db/execute")
    assert body["query"] == "SELECT * FROM orders"
    assert body["limit"] == 10000


def test_datasources_query_warns_when_the_server_truncated_results(api):
    api.route(
        "POST",
        "/api/datasources/proj-1/sales_db/execute",
        {
            "columns": ["id"],
            "data": [[1]],
            "rowCount": 1,
            "wasLimited": True,
            "maxRows": 100000,
        },
    )

    result = runner.invoke(
        cli.app,
        ["datasources", "query", "sales_db", "--sql", "SELECT 1", "--project", "proj-1"],
    )

    assert result.exit_code == 0, result.output
    assert "truncated" in result.output


def test_datasources_query_csv_flag_writes_a_header_and_rows(api, tmp_path):
    api.route(
        "POST",
        "/api/datasources/proj-1/sales_db/execute",
        {
            "columns": ["id", "total"],
            "data": [[1, 100], [2, 250]],
            "rowCount": 2,
            "wasLimited": False,
            "maxRows": 10000,
        },
    )
    dest = tmp_path / "out.csv"

    result = runner.invoke(
        cli.app,
        [
            "datasources", "query", "sales_db",
            "--sql", "SELECT 1",
            "--csv", str(dest),
            "--project", "proj-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert dest.read_text().splitlines() == ["id,total", "1,100", "2,250"]


def test_datasources_query_surfaces_the_read_only_rejection(api):
    api.route(
        "POST",
        "/api/datasources/proj-1/sales_db/execute",
        {"detail": "Only SELECT queries are allowed."},
        status=403,
    )

    result = runner.invoke(
        cli.app,
        ["datasources", "query", "sales_db", "--sql", "DELETE FROM orders", "--project", "proj-1"],
    )

    assert result.exit_code == 1
    assert "Only SELECT queries are allowed" in result.output


# ---------------------------------------------------------------------------
# harumi runs
# ---------------------------------------------------------------------------

RUN = {
    "id": "run-1",
    "project_id": "proj-1",
    "status": "completed",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:05:00Z",
}


def test_runs_list_renders_each_run(api):
    api.route("GET", "/api/projects/proj-1/runs", {"runs": [RUN]})

    result = runner.invoke(cli.app, ["runs", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "run-1" in result.output
    assert "completed" in result.output


def test_runs_get_prints_captured_stdout_and_error(api):
    api.route(
        "GET",
        "/api/projects/proj-1/runs/run-1",
        {**RUN, "status": "failed", "stdout": "solving...\n", "stderr": "", "error": "infeasible"},
    )

    result = runner.invoke(cli.app, ["runs", "get", "run-1", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "solving..." in result.output
    assert "infeasible" in result.output


def test_runs_cancel_posts_to_the_cancel_endpoint(api):
    api.route("POST", "/api/projects/proj-1/runs/run-1/cancel", {**RUN, "status": "cancelled"})

    result = runner.invoke(cli.app, ["runs", "cancel", "run-1", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "cancelled" in result.output
    assert api.paths() == ["/api/projects/proj-1/runs/run-1/cancel"]


# ---------------------------------------------------------------------------
# harumi run
#
# The only command that touches git, so the git helpers are stubbed to model
# each working-tree state the command branches on.
# ---------------------------------------------------------------------------

QUEUED = {
    "execution_log_id": "log-1",
    "status": "queued",
    "workflow_run_id": "wf-1",
    "project_run_id": "run-1",
}


@pytest.fixture
def git(monkeypatch):
    """Stub the git helpers `run` imports, defaulting to a clean, pushed tree."""

    state = {"dirty": False, "unpushed": False, "branch": "main", "deleted": []}

    monkeypatch.setattr(cli, "is_dirty", lambda: state["dirty"])
    monkeypatch.setattr(cli, "has_unpushed_commits", lambda: state["unpushed"])
    monkeypatch.setattr(cli, "current_branch", lambda: state["branch"])
    monkeypatch.setattr(
        cli, "push_scratch", lambda username, token: ("harumi-scratch-abc", "sha123")
    )
    monkeypatch.setattr(
        cli, "delete_remote_scratch", lambda name: state["deleted"].append(name)
    )
    return state


def test_run_without_a_binding_points_at_init(api, git, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["run"])

    assert result.exit_code == 1
    assert "harumi init" in result.output
    assert api.requests == []


def test_run_uses_the_current_branch_when_the_tree_is_clean(api, git, bound_dir):
    api.route("POST", "/api/projects/proj-bound/execute", QUEUED, status=202)

    result = runner.invoke(cli.app, ["run"])

    assert result.exit_code == 0, result.output
    assert api.body_for("POST", "/api/projects/proj-bound/execute")["branch"] == "main"
    # Nothing was pushed, so nothing needs cleaning up.
    assert git["deleted"] == []


def test_run_explicit_branch_skips_git_inspection(api, git, bound_dir):
    api.route("POST", "/api/projects/proj-bound/execute", QUEUED, status=202)
    # Even with a dirty tree, an explicit ref must not trigger a scratch push.
    git["dirty"] = True

    result = runner.invoke(cli.app, ["run", "--branch", "release"])

    assert result.exit_code == 0, result.output
    assert api.body_for("POST", "/api/projects/proj-bound/execute")["branch"] == "release"
    assert git["deleted"] == []


def test_run_pushes_and_cleans_up_a_scratch_branch_when_dirty(api, git, bound_dir):
    from harumi.config import save_git_token

    save_git_token("gitea-token", username="dev@harumi.test")
    api.route("POST", "/api/projects/proj-bound/execute", QUEUED, status=202)
    git["dirty"] = True

    result = runner.invoke(cli.app, ["run"])

    assert result.exit_code == 0, result.output
    assert api.body_for("POST", "/api/projects/proj-bound/execute")["branch"] == "harumi-scratch-abc"
    # The scratch branch must always be torn down.
    assert git["deleted"] == ["harumi-scratch-abc"]


def test_run_cleans_up_the_scratch_branch_even_when_execute_fails(api, git, bound_dir):
    from harumi.config import save_git_token

    save_git_token("gitea-token", username="dev@harumi.test")
    api.route("POST", "/api/projects/proj-bound/execute", {"detail": "boom"}, status=500)
    git["dirty"] = True

    result = runner.invoke(cli.app, ["run"])

    assert result.exit_code == 1
    # The `finally` block is the whole point: a failed run must not leak a branch.
    assert git["deleted"] == ["harumi-scratch-abc"]


def test_run_requires_a_git_token_before_pushing_a_scratch_branch(api, git, bound_dir):
    git["dirty"] = True

    result = runner.invoke(cli.app, ["run"])

    assert result.exit_code == 1
    assert "harumi login" in result.output
    assert api.requests == []


# ---------------------------------------------------------------------------
# harumi dashboard validate
#
# A project renders one dashboard per spec — every `dashboard/*.toml` plus the
# legacy root `dashboard.toml` — so validate has to cover all of them, not just
# the root file it originally hardcoded.
# ---------------------------------------------------------------------------

VALID_SPEC = """
[[widgets]]
type = "metric"
id = "objective"
title = "Objective"
value_key = "objective"
"""

# `valueKey` instead of `value_key` — the typo the platform silently drops.
BROKEN_SPEC = """
[[widgets]]
type = "metric"
id = "revenue"
title = "Revenue"
valueKey = "totals.revenue"
"""


def _b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode()).decode()


def test_dashboard_validate_checks_every_local_folder_spec(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard" / "costs.toml").write_text(VALID_SPEC)
    (tmp_path / "dashboard" / "schedule.toml").write_text(VALID_SPEC)

    result = runner.invoke(cli.app, ["dashboard", "validate"])

    assert result.exit_code == 0, result.output
    assert "dashboard/costs.toml" in result.output
    assert "dashboard/schedule.toml" in result.output


def test_dashboard_validate_fails_when_any_spec_drops_a_widget(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard" / "ok.toml").write_text(VALID_SPEC)
    (tmp_path / "dashboard" / "broken.toml").write_text(BROKEN_SPEC)

    result = runner.invoke(cli.app, ["dashboard", "validate"])

    assert result.exit_code == 1
    assert "dropped" in result.output
    # The good spec is still reported, so a viewer sees which one is at fault.
    assert "dashboard/ok.toml" in result.output


def test_dashboard_validate_still_finds_the_legacy_root_file(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dashboard.toml").write_text(VALID_SPEC)

    result = runner.invoke(cli.app, ["dashboard", "validate"])

    assert result.exit_code == 0, result.output
    assert "objective" in result.output


def test_dashboard_validate_reports_when_nothing_is_committed(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["dashboard", "validate"])

    assert result.exit_code == 1
    assert "No dashboard specs found." in result.output


def test_dashboard_validate_ref_lists_the_repo_then_reads_each_spec(api):
    api.route(
        "GET",
        "/api/projects/proj-1/repo/files",
        [
            {"name": "main.py", "path": "main.py", "type": "file"},
            {"name": "schedule.toml", "path": "dashboard/schedule.toml", "type": "file"},
            {"name": "dashboard.toml", "path": "dashboard.toml", "type": "file"},
        ],
    )
    api.route(
        "GET",
        "/api/projects/proj-1/repo/file-content",
        {"path": "x", "sha": "abc", "encoding": "base64", "content": _b64(VALID_SPEC)},
    )

    result = runner.invoke(
        cli.app, ["dashboard", "validate", "--ref", "main", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    assert "dashboard/schedule.toml" in result.output
    assert "dashboard.toml" in result.output
    # One listing plus one read per spec — never a guessed path.
    assert api.paths().count("/api/projects/proj-1/repo/file-content") == 2


def test_dashboard_validate_ref_reports_a_repo_with_no_specs(api):
    api.route(
        "GET",
        "/api/projects/proj-1/repo/files",
        [{"name": "main.py", "path": "main.py", "type": "file"}],
    )

    result = runner.invoke(
        cli.app, ["dashboard", "validate", "--ref", "main", "--project", "proj-1"]
    )

    assert result.exit_code == 1
    assert "No dashboard specs" in result.output


def test_dashboard_validate_explicit_path_checks_only_that_file(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard" / "broken.toml").write_text(BROKEN_SPEC)
    only = tmp_path / "one.toml"
    only.write_text(VALID_SPEC)

    result = runner.invoke(cli.app, ["dashboard", "validate", str(only)])

    assert result.exit_code == 0, result.output
    assert "dropped" not in result.output





