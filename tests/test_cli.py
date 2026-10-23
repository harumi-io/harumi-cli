"""Behavior tests for the Typer command layer.

Every command in cli.py builds its Client through the single `_get_client`
chokepoint, so patching that one function routes the whole CLI at an
httpx.MockTransport — no network, no real credentials, no backend. What these
tests cover that tests/test_client.py cannot: argument parsing, the
.harumi-binding fallback in `_resolve_project`, the `_handle_errors` exit
codes, and what actually gets rendered to the terminal.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pathlib import Path
from typer.testing import CliRunner

import harumi.cli as cli
from harumi.client import Client
from harumi.errors import ApiError, ApiUnreachableError

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


@pytest.mark.parametrize(
    "argv",
    [[], ["--help"]],
)
def test_banner_shown_for_bare_and_help(argv, monkeypatch, capsys):
    """The banner is an entry-point decoration (see `main()`), not a Click
    callback, so it's checked against raw argv rather than through
    `CliRunner` — this pins that argv match directly."""
    import harumi.skills as skills_mod

    monkeypatch.setattr(skills_mod, "agents_missing_skill", lambda: [])
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: True)
    monkeypatch.setattr("sys.argv", ["harumi"] + argv)
    cli._print_banner_if_bare_entrypoint()
    assert capsys.readouterr().out.strip() != ""


@pytest.mark.parametrize("argv", [[], ["--help"]])
def test_banner_and_agent_nudge_hidden_when_output_is_not_a_terminal(argv, monkeypatch, capsys):
    """Piped or logged output (and a coding agent reading stdout) gets no art
    and no 'install the skill' nudge: both are decoration for a person."""
    import harumi.skills as skills_mod

    monkeypatch.setattr(
        skills_mod,
        "agents_missing_skill",
        lambda: [skills_mod.Agent("cursor", "Cursor", Path("/tmp/does-not-matter"))],
    )
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: False)
    monkeypatch.setattr("sys.argv", ["harumi"] + argv)
    cli._print_banner_if_bare_entrypoint()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "argv",
    [["--version"], ["login"], ["login", "--help"], ["whoami"], ["--env", "staging"]],
)
def test_banner_not_shown_for_version_and_real_commands(argv, monkeypatch, capsys):
    """`--version` is deliberately excluded: harumi-cli-setup/SKILL.md
    documents its output as an exact-match `harumi <x.y.z>` contract used to
    verify installs and disambiguate a shadowing same-named binary — banner
    art would break that machine-parsed shape."""
    import harumi.skills as skills_mod

    monkeypatch.setattr(skills_mod, "agents_missing_skill", lambda: [])
    monkeypatch.setattr("sys.argv", ["harumi"] + argv)
    cli._print_banner_if_bare_entrypoint()
    assert capsys.readouterr().out == ""


def test_skill_install_hint_shown_when_agent_missing_skill(monkeypatch, capsys):
    import harumi.skills as skills_mod

    monkeypatch.setattr(
        skills_mod,
        "agents_missing_skill",
        lambda: [skills_mod.Agent("cursor", "Cursor", Path("/tmp/does-not-matter"))],
    )
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: True)
    monkeypatch.setattr("sys.argv", ["harumi"])
    cli._print_banner_if_bare_entrypoint()
    out = capsys.readouterr().out
    assert "harumi agent-skills install" in out
    assert "Cursor" in out


def test_skill_install_hint_hidden_when_nothing_missing(monkeypatch, capsys):
    import harumi.skills as skills_mod

    monkeypatch.setattr(skills_mod, "agents_missing_skill", lambda: [])
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: True)
    monkeypatch.setattr("sys.argv", ["harumi"])
    cli._print_banner_if_bare_entrypoint()
    assert "harumi agent-skills install" not in capsys.readouterr().out


def test_skill_install_hint_swallows_filesystem_errors(monkeypatch, capsys):
    """A decorative nudge must never crash the CLI's most common invocation
    over a filesystem hiccup (odd container, permission error, symlink loop)
    while probing ~/.cursor, ~/.claude, ~/.codex."""
    import harumi.skills as skills_mod

    def _boom():
        raise PermissionError("nope")

    monkeypatch.setattr(skills_mod, "agents_missing_skill", _boom)
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: True)
    monkeypatch.setattr("sys.argv", ["harumi"])
    cli._print_banner_if_bare_entrypoint()  # must not raise
    out = capsys.readouterr().out
    assert "harumi agent-skills install" not in out
    assert out.strip() != ""  # banner itself still printed


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
                secondary_opts=[],
                param_type_name="option",
                type=SimpleNamespace(name=name),
                required=False,
                multiple=False,
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
    # ...and so do ranges (`min=1`), which broke 3.9 CI for `--limit`.
    assert type_of("integer range") == "int range"
    assert type_of("int range") == "int range"
    assert type_of("float range") == "float range"
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
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG", "HARUMI_STATUS_URL"):
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
def at_a_terminal(monkeypatch):
    """A person at a keyboard. Confirmations only ask where there is one; elsewhere they refuse and name --yes."""
    monkeypatch.setattr(cli.interactive, "is_interactive", lambda *a, **k: True)


@pytest.fixture
def api(monkeypatch) -> FakeApi:
    fake = FakeApi()
    transport = httpx.MockTransport(fake._handle)

    def _get_client(api_url=None, git_url=None, org=None) -> Client:
        # The stock API host, on purpose: the "check the status page" hint is only shown
        # for it (see `config.status_hint_url`). A made-up host would read as a
        # self-hosted stack and silently drop the hint the tests below assert on. The
        # MockTransport routes on the path only, so nothing leaves the process.
        return Client(
            api_url="https://api.harumi.io/api",
            git_url=git_url,
            org_id=org,
            transport=transport,
        )

    monkeypatch.setattr(cli, "_get_client", _get_client)
    return fake


@pytest.fixture
def bound_dir(tmp_path, monkeypatch):
    """A cwd containing a .harumi/config.json binding, as `harumi link` writes."""
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


def test_new_rejects_personal_with_customer_id(api):
    """The two flags name different workspaces, so passing both must be refused.

    Without this the guard could be dropped in a refactor and one flag would
    silently win, putting the project somewhere the user didn't ask for.
    """
    result = runner.invoke(
        cli.app, ["new", "Scoped", "--no-clone", "--personal", "--customer-id", "org-other"]
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


def test_a_402_from_a_run_points_at_usage_and_the_billing_settings_page(api, bound_dir):
    # A billing denial ("harumi-api returned HTTP 402: ...") is technically
    # correct but doesn't tell the user what to do about it — this pins the
    # friendlier message _format_api_error swaps in for exactly this status.
    api.route(
        "POST",
        "/api/projects/proj-bound/execute",
        {"detail": "Included credits exhausted for this period."},
        status=402,
    )

    result = runner.invoke(cli.app, ["run", "--branch", "main"])

    assert result.exit_code == 1
    assert "Included credits exhausted for this period." in result.output
    assert "harumi usage" in result.output
    assert "settings?tab=billing" in result.output


@pytest.mark.parametrize(
    ("code", "hint"),
    [
        ("no_subscription", "Subscribe at"),
        ("org_not_subscribed", "harumi.io/contact"),
        ("weekly_limit_reached", "turn on overage"),
        ("member_monthly_limit_reached", "Ask your organization admin"),
        ("kernel_size_not_in_plan", "smaller kernel"),
        # The 7-day trial and the other plan limits (harumi-api#462).
        ("concurrent_runs_limit_reached", "Wait for a run to finish"),
        ("live_kernel_limit_reached", "Stop another kernel"),
        ("storage_quota_reached", "harumi files rm"),
        ("project_limit_reached", "Delete a project"),
        ("app_limit_reached", "Stop another app"),
        ("app_time_exhausted", "keep your app running"),
        ("schedule_limit_reached", "harumi schedules rm"),
        ("schedule_too_frequent", "longer interval"),
        ("insight_limit_reached", "resets tomorrow"),
        ("byok_not_in_plan", "bring your own key"),
    ],
)
def test_a_coded_402_gets_the_hint_for_its_reason(api, bound_dir, code, hint):
    api.route(
        "POST",
        "/api/projects/proj-bound/execute",
        {"detail": "Denied by billing.", "code": code},
        status=402,
    )

    result = runner.invoke(cli.app, ["run", "--branch", "main"])

    assert result.exit_code == 1
    assert "Denied by billing." in result.output
    assert hint in result.output


def test_new_stops_before_creating_when_personal_has_no_plan(api, git_ops, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api.route("GET", "/api/billing/entitlement/me", _entitlement_body(personal_active=False))
    api.route("POST", "/api/projects", {"id": "proj-9", "name": "Widget", "notebook_ids": []})

    result = runner.invoke(cli.app, ["new", "Widget", "--personal", "--no-clone"])

    assert result.exit_code == 1
    assert "personal workspace doesn't have an active plan" in result.output
    assert "Subscribe at" in result.output
    assert not any(r.method == "POST" for r in api.requests)


def test_new_stops_when_the_target_org_has_no_plan(api, git_ops, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    org = {"customer_id": "org-1", "name": "Acme", "active": False, "tier": "none", "reason": "org_not_subscribed"}
    api.route("GET", "/api/billing/entitlement/me", _entitlement_body(orgs=[org]))

    result = runner.invoke(cli.app, ["new", "Widget", "--customer-id", "org-1", "--no-clone"])

    assert result.exit_code == 1
    assert "Acme" in result.output
    assert "harumi.io/contact" in result.output


def test_new_proceeds_when_the_paywall_is_off(api, git_ops, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api.route("GET", "/api/billing/entitlement/me", _entitlement_body(personal_active=False, paywall=False))
    api.route("POST", "/api/projects", {"id": "proj-9", "name": "Widget", "notebook_ids": []})
    _route_project_repo(api, "proj-9", "widget")

    result = runner.invoke(cli.app, ["new", "Widget", "--personal", "--no-clone"])

    assert result.exit_code == 0, result.output


def test_start_stops_before_asking_for_a_name_with_no_entitled_workspace(api):
    api.route("GET", "/api/billing/entitlement/me", _entitlement_body(personal_active=False))

    result = runner.invoke(cli.app, ["start"], input="1\nWidget\n")

    assert result.exit_code == 1
    assert "Project name" not in result.output
    assert "Subscribe at" in result.output


def test_a_5xx_points_at_the_status_page(api, bound_dir):
    api.route("POST", "/api/projects/proj-bound/execute", {"detail": "boom"}, status=503)

    result = runner.invoke(cli.app, ["run", "--branch", "main"])

    assert result.exit_code == 1
    assert "https://status.harumi.io" in result.output
    assert "harumi status" in result.output


def test_a_4xx_does_not_point_at_the_status_page(api, bound_dir):
    api.route("POST", "/api/projects/proj-bound/execute", {"detail": "nope"}, status=422)

    result = runner.invoke(cli.app, ["run", "--branch", "main"])

    assert result.exit_code == 1
    assert "status.harumi.io" not in result.output


def test_a_connection_failure_is_an_error_message_not_a_traceback(monkeypatch, bound_dir):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        cli,
        "_get_client",
        # Stock host so the status hint is shown; see the `api` fixture.
        lambda api_url=None, git_url=None, org=None: Client(
            api_url="https://api.harumi.io/api", transport=httpx.MockTransport(refuse)
        ),
    )

    result = runner.invoke(cli.app, ["run", "--branch", "main"])

    assert result.exit_code == 1
    assert "Couldn't connect to Harumi" in result.output
    assert "https://status.harumi.io" in result.output
    assert not isinstance(result.exception, httpx.HTTPError)


def _statuses(monkeypatch, payload, status=200):
    seen = []

    def fake_get(url, **_):
        seen.append(url)
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(cli.httpx, "get", fake_get)
    return seen


def test_status_lists_services_and_exits_zero_when_all_are_up(monkeypatch):
    seen = _statuses(
        monkeypatch,
        [
            {"name": "API", "group": "platform", "results": [{"success": False}, {"success": True}]},
            {"name": "Database", "group": "infra", "results": [{"success": True}]},
        ],
    )

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0
    assert seen == ["https://status.harumi.io/api/v1/endpoints/statuses"]
    assert "API" in result.output and "Database" in result.output
    assert "down" not in result.output


def test_status_exits_nonzero_and_names_the_service_that_is_down(monkeypatch):
    _statuses(
        monkeypatch,
        [
            {"name": "API", "group": "platform", "results": [{"success": True}]},
            {"name": "Git", "group": "platform", "results": [{"success": True}, {"success": False}]},
        ],
    )

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 1
    assert "Git" in result.output and "down" in result.output


@pytest.mark.parametrize(
    "payload",
    [{"error": "bad gateway"}, [], None, ["not-an-object"], "<html>captive portal</html>"],
)
def test_status_rejects_a_payload_that_is_not_a_list_of_monitors(monkeypatch, payload):
    """A proxy/captive-portal reply or an empty list must not be reported as "all up"."""
    _statuses(monkeypatch, payload)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 1
    # An empty body (payload=None) fails earlier, as unparseable JSON; every other shape is
    # "unexpected". Either way: a clean error, never a traceback or a false "all up".
    assert "the status page" in result.output
    assert not isinstance(result.exception, (AttributeError, TypeError))


def test_status_treats_a_malformed_latest_result_as_unknown(monkeypatch):
    _statuses(monkeypatch, [{"name": "API", "group": "platform", "results": ["oops"]}])

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0
    assert "unknown" in result.output


@pytest.mark.parametrize(
    "error",
    [
        ApiUnreachableError("Could not reach harumi-api: bad path [/tmp/x] [red]y"),
        ApiError(404, "no such file [/data/x.csv]"),
        ApiError(422, "field [/body/name] is required"),
        ApiError(402, "credits exhausted for [/plan/free]"),
        ApiError(503, "upstream [/db] unavailable"),
    ],
)
def test_server_text_cannot_break_the_error_printer(error):
    """Exception and server text is Rich markup once it reaches `_fail`. A stray `[/x]` used to
    crash the printer with MarkupError instead of showing the error."""
    cli.err_console.print(cli._format_api_error(error))  # must not raise


def test_a_failed_s3_transfer_does_not_blame_the_harumi_platform():
    """`ApiError(0, ...)` is also used for presigned S3 upload/download failures, which say
    nothing about whether Harumi itself is up."""
    assert "status.harumi.io" not in cli._format_api_error(ApiError(0, "Upload failed: S3 timed out"))
    assert "status.harumi.io" in cli._format_api_error(ApiUnreachableError("Could not reach harumi-api: x"))


def test_no_status_hint_when_the_api_url_is_overridden(monkeypatch):
    """The stock status page says nothing about a self-hosted or local stack."""
    err = ApiUnreachableError("Could not reach harumi-api: x")
    monkeypatch.setenv("HARUMI_API_URL", "http://localhost:8000/api")
    assert "status.harumi.io" not in cli._format_api_error(err)
    assert "harumi status" not in cli._format_api_error(err)


def test_status_hint_returns_with_an_explicit_status_url_override(monkeypatch):
    err = ApiUnreachableError("Could not reach harumi-api: x")
    monkeypatch.setenv("HARUMI_API_URL", "http://localhost:8000/api")
    monkeypatch.setenv("HARUMI_STATUS_URL", "http://localhost:8080/")
    assert "Check http://localhost:8080 for platform status" in cli._format_api_error(err)


def test_no_status_hint_when_the_api_url_came_from_the_flag():
    """`--api-url` is resolved in Config.load, not via the environment, so the hint has to
    look at the URL the client actually used."""
    from harumi.config import Config

    Config.load(api_url="http://localhost:8000/api")
    assert "status.harumi.io" not in cli._format_api_error(ApiUnreachableError("Could not reach harumi-api: x"))


def test_a_stock_api_url_passed_as_the_flag_still_gets_the_hint():
    from harumi.config import Config

    Config.load(api_url="https://api.harumi.io/api/")
    assert "status.harumi.io" in cli._format_api_error(ApiUnreachableError("Could not reach harumi-api: x"))


def test_a_stock_api_url_passed_explicitly_still_gets_the_hint(monkeypatch):
    monkeypatch.setenv("HARUMI_API_URL", "https://api.harumi.io/api/")
    assert "status.harumi.io" in cli._format_api_error(ApiUnreachableError("Could not reach harumi-api: x"))


def test_a_non_string_status_or_api_url_in_config_json_does_not_crash_the_error_path(tmp_path):
    """A hand-edited config.json is the one input here nobody validates."""
    import json

    from harumi.config import env_config_path

    path = env_config_path("production")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status_url": 5, "api_url": ["x"], "platform_url": 7, "git_url": {}, "org_id": 3}))

    err = ApiUnreachableError("Could not reach harumi-api: x")
    assert "status.harumi.io" in cli._format_api_error(err)
    # Config.load and the platform URL read the same file and must not crash either.
    from harumi.config import Config, active_platform_url

    config = Config.load()
    assert config.api_url == "https://api.harumi.io/api"
    assert config.org_id is None
    assert active_platform_url() == "https://app.harumi.io"


@pytest.mark.parametrize("value", [["staging"], {"a": 1}, 5, True])
def test_a_non_string_environment_in_the_global_config_is_a_clear_error_not_a_crash(value):
    """An unknown *name* already raises a readable ValueError. A hand-edited non-string
    (a list is unhashable) must get the same, not a TypeError -- and must not silently
    fall back to production when the user meant something else."""
    import json

    from harumi import config

    config.CONFIG_PATH.write_text(json.dumps({"environment": value}))
    with pytest.raises(ValueError, match="Unknown environment"):
        config.resolve_environment()


def test_status_command_honors_a_status_url_override(monkeypatch):
    seen = []

    def fake_get(url, **_):
        seen.append(url)
        raise httpx.ConnectError("stop here")

    monkeypatch.setenv("HARUMI_STATUS_URL", "http://status.internal/")
    monkeypatch.setattr(cli.httpx, "get", fake_get)
    runner.invoke(cli.app, ["status"])
    assert seen == ["http://status.internal/api/v1/endpoints/statuses"]


def test_status_reports_an_unreachable_status_page(monkeypatch):
    def fail(url, **_):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(cli.httpx, "get", fail)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 1
    assert "Could not read the status page" in result.output


def test_status_targets_the_staging_page_for_the_staging_env(monkeypatch):
    seen = _statuses(monkeypatch, [])

    runner.invoke(cli.app, ["--env", "staging", "status"])

    assert seen == ["https://status.dev.harumi.io/api/v1/endpoints/statuses"]


def _usage_body(**overrides):
    body = {
        "billing_account_id": "acct-1",
        "plan_code": "pro",
        "tier": "pro",
        "balance_credits": 200_000,
        "included_credits": 396_000,
        "period_start": "2026-10-01T00:00:00+00:00",
        "period_end": "2026-11-01T00:00:00+00:00",
        "weekly_used_pct": 48.2,
        "monthly_used_pct": 16.0,
        "weekly_resets_at": "2026-10-20T15:00:00+00:00",
        "monthly_resets_at": "2026-11-01T00:00:00+00:00",
        "overage_allowed": True,
        "overage_enabled": False,
        "overage_cap_cents": 0,
        "overage_spent_cents": 0,
        "overage_cap_credits": 0,
        "entries": [],
    }
    body.update(overrides)
    return body


def _entitlement_body(personal_active=True, orgs=(), paywall=True):
    return {
        "paywall_enabled": paywall,
        "is_harumi": False,
        "personal": {
            "customer_id": None,
            "active": personal_active,
            "tier": "pro" if personal_active else "free",
            "reason": None if personal_active else "no_subscription",
        },
        "organizations": list(orgs),
        "any_active": personal_active or any(o["active"] for o in orgs),
    }


def test_usage_shows_the_plan_and_both_meters_as_percentages(api):
    api.route("GET", "/api/billing/usage", _usage_body())
    api.route("GET", "/api/billing/entitlement/me", _entitlement_body())

    result = runner.invoke(cli.app, ["usage"])

    assert result.exit_code == 0, result.output
    assert "Pro" in result.output
    assert "Weekly: 48% used" in result.output
    assert "Monthly: 16% used" in result.output
    # Percentages only: no raw credit counts.
    assert "396,000" not in result.output
    assert "credits" not in result.output
    assert "Overage: off" in result.output


def test_usage_reports_overage_in_dollars(api):
    api.route(
        "GET",
        "/api/billing/usage",
        _usage_body(overage_enabled=True, overage_cap_cents=1000, overage_spent_cents=320, weekly_used_pct=104.0),
    )

    result = runner.invoke(cli.app, ["usage"])

    assert result.exit_code == 0, result.output
    assert "$3.20 of $10.00 used" in result.output
    # Overage is on, so reaching the weekly limit isn't a stop.
    assert "reached a limit" not in result.output


def test_usage_flags_a_reached_limit_with_no_overage(api):
    api.route("GET", "/api/billing/usage", _usage_body(weekly_used_pct=100.0))

    result = runner.invoke(cli.app, ["usage"])

    assert result.exit_code == 0, result.output
    assert "reached a limit" in result.output
    assert "settings?tab=billing" in result.output


def test_usage_says_overage_is_not_available_on_free(api):
    api.route(
        "GET", "/api/billing/usage", _usage_body(plan_code="free", tier="free", overage_allowed=False)
    )

    result = runner.invoke(cli.app, ["usage"])

    assert "Free" in result.output
    assert "not available on this plan" in result.output


def test_usage_shows_the_harumi_io_tier_as_unlimited(api):
    api.route(
        "GET",
        "/api/billing/usage",
        _usage_body(plan_code="harumi_io", tier="harumi_io", unlimited=True, weekly_used_pct=None, monthly_used_pct=None),
    )

    result = runner.invoke(cli.app, ["usage"])

    assert result.exit_code == 0, result.output
    assert "harumi-io" in result.output
    assert "unlimited" in result.output


def test_import_stops_before_touching_the_folder_when_personal_has_no_plan(api, git_ops, tmp_path):
    api.route("GET", "/api/billing/entitlement/me", _entitlement_body(personal_active=False))
    folder = tmp_path / "code"
    folder.mkdir()
    (folder / "solve.py").write_text("print(1)\n")

    result = runner.invoke(cli.app, ["import", str(folder), "--personal"])

    assert result.exit_code == 1
    assert "personal workspace doesn't have an active plan" in result.output
    assert not any(r.method == "POST" for r in api.requests)
    # No harumi.toml scaffolded: the folder is left exactly as it was.
    assert sorted(p.name for p in folder.iterdir()) == ["solve.py"]


def test_start_checks_the_configured_workspace_not_just_any(api, monkeypatch):
    """An entitled org elsewhere doesn't let `start` past when the project
    would land in an unentitled Personal; the hint names the org instead."""
    org = {"customer_id": "org-1", "name": "Acme", "active": True, "tier": "enterprise", "reason": None}
    api.route("GET", "/api/billing/entitlement/me", _entitlement_body(personal_active=False, orgs=[org]))

    result = runner.invoke(cli.app, ["start"], input="1\nWidget\n")

    assert result.exit_code == 1
    assert "Project name" not in result.output
    assert "harumi org use org-1" in result.output


def test_usage_with_no_limits_on_a_new_server_derives_nothing(api):
    """A new server sends the pct fields as null for a plan with no limits;
    that is not an older server, so no percentage is made up from credits."""
    api.route(
        "GET",
        "/api/billing/usage",
        {
            "billing_account_id": "acct-1",
            "plan_code": "enterprise",
            "tier": "enterprise",
            "balance_credits": 500,
            "included_credits": 2000,
            "period_start": "2026-09-01T00:00:00Z",
            "period_end": "2026-10-01T00:00:00Z",
            "weekly_used_pct": None,
            "monthly_used_pct": None,
            "overage_enabled": False,
            "overage_cap_credits": 0,
            "entries": [],
        },
    )

    result = runner.invoke(cli.app, ["usage"])

    assert result.exit_code == 0, result.output
    assert "75%" not in result.output
    assert "Monthly: no limit" in result.output


def test_usage_for_an_unentitled_personal_workspace_points_at_subscribing(api):
    api.route("GET", "/api/billing/entitlement/me", _entitlement_body(personal_active=False))

    result = runner.invoke(cli.app, ["usage"])

    assert result.exit_code == 0, result.output
    assert "no active plan" in result.output
    assert "Subscribe at" in result.output


def test_usage_falls_back_to_a_monthly_percentage_on_an_older_server(api):
    api.route(
        "GET",
        "/api/billing/usage",
        {
            "billing_account_id": "acct-1",
            "plan_code": "individual",
            "balance_credits": 500,
            "included_credits": 2000,
            "period_start": "2026-09-01T00:00:00Z",
            "period_end": "2026-10-01T00:00:00Z",
            "overage_enabled": False,
            "overage_cap_credits": 0,
            "entries": [],
        },
    )

    result = runner.invoke(cli.app, ["usage"])

    assert result.exit_code == 0, result.output
    assert "Monthly: 75% used" in result.output


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


def test_new_no_clone_does_not_touch_local_directory_or_git(api, tmp_path, monkeypatch):
    """`new --no-clone` is the pure API primitive — binding/cloning is plain
    `new`/`import`/`clone`/`link`. This pins that it stays that way."""
    monkeypatch.chdir(tmp_path)
    clone_calls = []
    remote_calls = []
    monkeypatch.setattr(cli, "clone_repo", lambda **kw: clone_calls.append(kw))
    monkeypatch.setattr(cli, "ensure_remote", lambda **kw: remote_calls.append(kw))
    api.route("POST", "/api/projects", {"id": "proj-1", "name": "Widget", "notebook_ids": []})
    api.route(
        "GET",
        "/api/projects/proj-1/repo",
        {
            "owner": "acme",
            "name": "widget",
            "clone_url": "https://git.harumi.test/acme/widget.git",
            "default_branch": "main",
        },
    )

    result = runner.invoke(cli.app, ["new", "Widget", "--no-clone"])

    assert result.exit_code == 0, result.output
    assert "harumi clone proj-1" in result.output
    assert not (tmp_path / ".harumi").exists()
    assert clone_calls == []
    assert remote_calls == []


# ---------------------------------------------------------------------------
# harumi new / import / clone / link / start
#
# Each of these ends by binding a directory (writing .harumi/config.json)
# and, unless the directory isn't a real git repo yet, configuring the
# `harumi` remote — so the git subprocess helpers are stubbed the same way
# the `git` fixture stubs them for `run`, below.
# ---------------------------------------------------------------------------


@pytest.fixture
def git_ops(monkeypatch):
    """Stub the git helpers `new`/`push`/`clone`/`link` call, so these tests
    never shell out to real git. `clone_repo` creates the destination
    directory (mirroring what a real `git clone` would leave behind) so the
    binding step that follows has somewhere to write `.harumi/config.json`.
    """
    calls: dict[str, list[dict]] = {"clone": [], "ensure_remote": [], "push_folder": []}

    def fake_clone_repo(*, clone_url, username, token, dest, remote="harumi"):
        calls["clone"].append({"clone_url": clone_url, "dest": dest})
        Path(dest).mkdir(parents=True, exist_ok=True)

    def fake_ensure_remote(*, clone_url, username, token, cwd):
        calls["ensure_remote"].append({"clone_url": clone_url, "cwd": cwd})

    def fake_push_folder(folder, *, clone_url, username, token, branch, message, exclude=()):
        calls["push_folder"].append(
            {"folder": folder, "clone_url": clone_url, "branch": branch, "exclude": list(exclude)}
        )

    monkeypatch.setattr(cli, "clone_repo", fake_clone_repo)
    monkeypatch.setattr(cli, "ensure_remote", fake_ensure_remote)
    monkeypatch.setattr(cli, "push_folder", fake_push_folder)
    # Every directory these tests touch is a fresh tmp_path, never a real git
    # checkout — treat it as one so the binding step doesn't bail out early.
    monkeypatch.setattr(cli, "repo_root", lambda cwd=None: cwd or Path.cwd())
    return calls


def _route_project_repo(api, project_id: str, name: str) -> None:
    api.route(
        "GET",
        f"/api/projects/{project_id}/repo",
        {
            "owner": "acme",
            "name": name,
            "clone_url": f"https://git.harumi.test/acme/{name}.git",
            "default_branch": "main",
        },
    )


def test_new_creates_project_clones_and_binds(api, git_ops, tmp_path, monkeypatch):
    from harumi.config import save_git_token

    monkeypatch.chdir(tmp_path)
    save_git_token("gitea-token", username="dev@harumi.test")
    api.route("POST", "/api/projects", {"id": "proj-new", "name": "Widget", "notebook_ids": []})
    _route_project_repo(api, "proj-new", "widget")

    result = runner.invoke(cli.app, ["new", "Widget"])

    assert result.exit_code == 0, result.output
    dest = tmp_path / "widget"
    assert (dest / ".harumi" / "config.json").exists()
    assert git_ops["clone"][0]["clone_url"] == "https://git.harumi.test/acme/widget.git"
    assert git_ops["ensure_remote"][0]["cwd"] == dest


def test_new_reports_a_clear_error_when_dir_is_an_existing_file(api, git_ops, tmp_path):
    """`--dir` pointing at a regular file makes `dest.iterdir()` raise
    NotADirectoryError — must give the same clear message as a non-empty
    directory, not a raw OSError."""
    from harumi.config import save_git_token

    save_git_token("gitea-token", username="dev@harumi.test")
    dest_file = tmp_path / "notes.txt"
    dest_file.write_text("existing content")
    api.route("POST", "/api/projects", {"id": "proj-new", "name": "Widget", "notebook_ids": []})
    _route_project_repo(api, "proj-new", "widget")

    result = runner.invoke(cli.app, ["new", "Widget", "--dir", str(dest_file)])

    assert result.exit_code != 0
    assert "already exists and isn't empty" in result.output
    assert git_ops["clone"] == []


def test_import_writes_a_minimal_manifest_when_the_folder_has_none(api, git_ops, tmp_path):
    from harumi.config import save_git_token

    save_git_token("gitea-token", username="dev@harumi.test")
    folder = tmp_path / "myfolder"
    folder.mkdir()
    (folder / "main.py").write_text("print('hi')\n")
    api.route("POST", "/api/projects", {"id": "proj-push", "name": "myfolder", "notebook_ids": []})
    _route_project_repo(api, "proj-push", "myfolder")

    result = runner.invoke(cli.app, ["import", str(folder)])

    assert result.exit_code == 0, result.output
    assert "or_python_small" in (folder / "harumi.toml").read_text()
    assert git_ops["push_folder"][0]["folder"] == folder
    assert (folder / ".harumi" / "config.json").exists()


def test_import_never_overwrites_an_existing_manifest(api, git_ops, tmp_path):
    from harumi.config import save_git_token

    save_git_token("gitea-token", username="dev@harumi.test")
    folder = tmp_path / "myfolder"
    folder.mkdir()
    (folder / "harumi.toml").write_text('[run]\ncommand = "python custom.py"\n')
    api.route("POST", "/api/projects", {"id": "proj-push", "name": "myfolder", "notebook_ids": []})
    _route_project_repo(api, "proj-push", "myfolder")

    result = runner.invoke(cli.app, ["import", str(folder)])

    assert result.exit_code == 0, result.output
    assert "python custom.py" in (folder / "harumi.toml").read_text()


def test_import_shows_a_folder_name_with_brackets_as_typed(api, git_ops, tmp_path):
    """`Creating project route [v2]...` lost its `[v2]`: rich read it as a tag. (A `[/x]` closing tag cannot be in
    a folder name, since `/` separates path parts; that case is covered where the name is plain text, in
    `test_examples.py`.)"""
    from harumi.config import save_git_token

    save_git_token("gitea-token", username="dev@harumi.test")
    folder = tmp_path / "route [v2] [bold]x[bold]"
    folder.mkdir()
    (folder / "main.py").write_text("print(1)\n")
    api.route("POST", "/api/projects", {"id": "proj-b", "name": "route [v2] [bold]x[bold]", "notebook_ids": []})
    _route_project_repo(api, "proj-b", "route")

    result = runner.invoke(cli.app, ["import", str(folder), "--yes"])

    assert result.exit_code == 0, result.output
    flat = result.output.replace("\n", " ")
    assert "Creating project route [v2] [bold]x[bold]" in flat
    assert "Created project route [v2] [bold]x[bold]" in flat


def test_clone_fetches_an_existing_project_and_binds(api, git_ops, tmp_path, monkeypatch):
    from harumi.config import save_git_token

    monkeypatch.chdir(tmp_path)
    save_git_token("gitea-token", username="dev@harumi.test")
    api.route("GET", "/api/projects/proj-1", {"id": "proj-1", "name": "Solver", "notebook_ids": []})
    _route_project_repo(api, "proj-1", "solver")

    result = runner.invoke(cli.app, ["clone", "proj-1"])

    assert result.exit_code == 0, result.output
    dest = tmp_path / "solver"
    assert (dest / ".harumi" / "config.json").exists()
    assert git_ops["clone"][0]["clone_url"] == "https://git.harumi.test/acme/solver.git"


def test_clone_reports_a_clear_error_when_dir_is_an_existing_file(api, git_ops, tmp_path):
    """Same fix as `new --dir`: a file at `--dir` must give the "already
    exists and isn't empty" message, not a raw NotADirectoryError."""
    from harumi.config import save_git_token

    save_git_token("gitea-token", username="dev@harumi.test")
    dest_file = tmp_path / "notes.txt"
    dest_file.write_text("existing content")
    api.route("GET", "/api/projects/proj-1", {"id": "proj-1", "name": "Solver", "notebook_ids": []})
    _route_project_repo(api, "proj-1", "solver")

    result = runner.invoke(cli.app, ["clone", "proj-1", "--dir", str(dest_file)])

    assert result.exit_code != 0
    assert "already exists and isn't empty" in result.output
    assert git_ops["clone"] == []


def test_link_binds_an_already_checked_out_directory(api, git_ops, tmp_path, monkeypatch):
    from harumi.config import save_git_token

    monkeypatch.chdir(tmp_path)
    save_git_token("gitea-token", username="dev@harumi.test")
    _route_project_repo(api, "proj-1", "solver")

    result = runner.invoke(cli.app, ["link", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".harumi" / "config.json").exists()
    assert git_ops["ensure_remote"][0]["cwd"] == tmp_path
    # `link` never fetches code — only `new`/`push`/`clone` do.
    assert git_ops["clone"] == []


def test_new_without_a_gitea_token_warns_but_does_not_fail(api, git_ops, tmp_path, monkeypatch):
    """A missing Gitea token is a soft warning, not a hard failure — the
    project was already created server-side by the time this check runs
    (mirrors `push`'s convention for the same precondition, see
    `_clone_and_bind`)."""
    monkeypatch.chdir(tmp_path)
    api.route("POST", "/api/projects", {"id": "proj-new", "name": "Widget", "notebook_ids": []})
    _route_project_repo(api, "proj-new", "widget")

    result = runner.invoke(cli.app, ["new", "Widget"])

    assert result.exit_code == 0, result.output
    assert "Created" in result.output
    assert "can't clone" in result.output
    assert git_ops["clone"] == []
    assert not (tmp_path / "widget").exists()


def test_start_dispatches_to_new_when_the_user_has_nothing_yet(api, git_ops, tmp_path, monkeypatch):
    from harumi.config import save_git_token

    monkeypatch.chdir(tmp_path)
    save_git_token("gitea-token", username="dev@harumi.test")
    api.route("POST", "/api/projects", {"id": "proj-new", "name": "Widget", "notebook_ids": []})
    _route_project_repo(api, "proj-new", "widget")

    result = runner.invoke(cli.app, ["start"], input="1\nWidget\n")

    assert result.exit_code == 0, result.output
    assert (tmp_path / "widget" / ".harumi" / "config.json").exists()


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


def test_repo_commits_lists_history_and_forwards_params(api):
    api.route(
        "GET",
        "/api/projects/proj-1/repo/commits",
        {
            "ref": "dev",
            "path": "main.py",
            "page": 2,
            "per_page": 10,
            "total": 1,
            "commits": [
                {
                    "sha": "abcdef1234",
                    "message": "Fix the thing\n\nlonger body",
                    "author_name": "Ada",
                    "committed_at": "2026-01-01T00:00:00Z",
                }
            ],
        },
    )

    result = runner.invoke(
        cli.app,
        [
            "repo",
            "commits",
            "main.py",
            "--ref",
            "dev",
            "--page",
            "2",
            "--per-page",
            "10",
            "--project",
            "proj-1",
        ],
    )

    assert result.exit_code == 0, result.output
    # Sha truncated to 8 chars; only the message's first line is shown.
    assert "abcdef12" in result.output
    assert "abcdef1234" not in result.output
    assert "Fix the thing" in result.output
    assert "longer body" not in result.output
    params = api.params_for("GET", "/api/projects/proj-1/repo/commits")
    assert params == {"page": "2", "per_page": "10", "ref": "dev", "path": "main.py"}


def test_repo_commits_reports_empty_history(api):
    api.route(
        "GET",
        "/api/projects/proj-1/repo/commits",
        {"ref": "main", "page": 1, "per_page": 30, "commits": []},
    )

    result = runner.invoke(cli.app, ["repo", "commits", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "No commits on 'main'." in result.output


def test_check_reports_ready(api):
    api.route(
        "GET",
        "/api/projects/proj-1/readiness",
        {
            "project_id": "proj-1",
            "ref": "main",
            "ready": True,
            "checks": [{"id": "repo", "ok": True, "detail": None}],
        },
    )

    result = runner.invoke(cli.app, ["check", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "Ready to run" in result.output
    assert "Repository exists" in result.output  # a readable name, not the check id "repo"


def test_check_reports_not_ready_and_forwards_ref(api):
    api.route(
        "GET",
        "/api/projects/proj-1/readiness",
        {
            "project_id": "proj-1",
            "ref": "dev",
            "ready": False,
            "checks": [
                {"id": "manifest_valid", "ok": False, "detail": "missing entrypoint"}
            ],
        },
    )

    result = runner.invoke(
        cli.app, ["check", "--ref", "dev", "--project", "proj-1"]
    )

    assert result.exit_code == 1, result.output  # a script can stop before `harumi run`
    assert "Not ready" in result.output and "1 problem" in result.output
    assert "missing entrypoint" in result.output
    assert api.params_for("GET", "/api/projects/proj-1/readiness")["ref"] == "dev"


# ---------------------------------------------------------------------------
# harumi files
# ---------------------------------------------------------------------------

def test_files_ls_lists_uploaded_files(api):
    api.route(
        "GET",
        "/api/projects/proj-1/files",
        {
            "files": [
                {"name": "data.csv", "key": "proj-1/data.csv", "last_modified": "2026-01-01T00:00:00Z", "etag": "abc", "size": 42}
            ],
            "is_truncated": False,
        },
    )

    result = runner.invoke(cli.app, ["files", "ls", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "data.csv" in result.output


def test_files_ls_reports_no_files(api):
    api.route("GET", "/api/projects/proj-1/files", {"files": [], "is_truncated": False})

    result = runner.invoke(cli.app, ["files", "ls", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "No files uploaded yet" in result.output


def test_files_put_uploads_and_defaults_remote_path_to_local_name(api, tmp_path, monkeypatch):
    local = tmp_path / "data.csv"
    local.write_text("a,b\n1,2\n")
    # The project already holds data.csv, so this is a replacement and the listing after the upload shows it:
    # `put` checks that a file whose key looked final really is in the project.
    api.route("GET", "/api/projects/proj-1/files", {"files": [{"name": "data.csv", "key": "proj-1/data.csv", "size": 8}], "is_truncated": False})
    api.route("POST", "/api/projects/proj-1/files/upload-url", {"url": "https://s3.test/proj-1/data.csv", "key": "proj-1/data.csv", "expires_in": 900})

    put_calls = []

    def fake_upload(self, upload_url, path, content_type):
        put_calls.append((upload_url.url, path, content_type))

    monkeypatch.setattr(Client, "upload_file_to_presigned_url", fake_upload)

    result = runner.invoke(cli.app, ["files", "put", str(local), "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "Uploaded" in result.output
    assert put_calls == [("https://s3.test/proj-1/data.csv", local, "text/csv")]
    body = api.body_for("POST", "/api/projects/proj-1/files/upload-url")
    assert body["path"] == "data.csv"


def test_files_put_sends_the_files_exact_size_so_a_plan_with_a_storage_quota_accepts_it(
    api, tmp_path, monkeypatch
):
    local = tmp_path / "data.csv"
    local.write_text("a,b\n1,2\n")
    api.route("GET", "/api/projects/proj-1/files", {"files": [{"name": "data.csv", "key": "k", "size": 8}], "is_truncated": False})
    api.route("POST", "/api/projects/proj-1/files/upload-url", {"url": "https://s3.test/x", "key": "k", "expires_in": 900})
    monkeypatch.setattr(Client, "upload_file_to_presigned_url", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["files", "put", str(local), "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    body = api.body_for("POST", "/api/projects/proj-1/files/upload-url")
    assert body["size_bytes"] == local.stat().st_size
    assert body["size_bytes"] > 0


def test_files_put_on_a_full_project_says_what_to_do(api, tmp_path):
    local = tmp_path / "big.bin"
    local.write_bytes(b"x" * 10)
    api.route("GET", "/api/projects/proj-1/files", {"files": [], "is_truncated": False})
    api.route(
        "POST",
        "/api/projects/proj-1/files/upload-url",
        {"detail": "This project has used its plan's storage.", "code": "storage_quota_reached"},
        status=402,
    )

    result = runner.invoke(cli.app, ["files", "put", str(local), "--project", "proj-1"])

    assert result.exit_code == 1
    assert "used its plan's storage" in result.output
    assert "harumi files rm" in result.output


def test_files_put_rejects_a_missing_local_file(api, tmp_path):
    result = runner.invoke(
        cli.app, ["files", "put", str(tmp_path / "ghost.csv"), "--project", "proj-1"]
    )

    assert result.exit_code != 0
    assert api.requests == []


def test_files_put_refuses_when_the_batch_would_exceed_the_file_count_cap(api, tmp_path, monkeypatch):
    """Mirrors harumi-platform's upload-cap guard: refuse before signing a URL."""
    local = tmp_path / "one-more.csv"
    local.write_text("x")
    existing = [
        {"name": f"f{i}.csv", "key": f"proj-1/f{i}.csv", "last_modified": "2026-01-01T00:00:00Z", "etag": "e", "size": 1}
        for i in range(500)
    ]
    api.route("GET", "/api/projects/proj-1/files", {"files": existing, "is_truncated": False})

    result = runner.invoke(cli.app, ["files", "put", str(local), "--project", "proj-1"])

    assert result.exit_code != 0
    assert "500-file" in result.output.replace("\n", " ")
    # Refused before ever asking for a presigned URL.
    assert not any(r.url.path.endswith("/upload-url") for r in api.requests)


def test_files_put_does_not_count_the_file_it_replaces_against_the_cap(api, tmp_path, monkeypatch):
    """Re-uploading an existing path overwrites that object, so a project sitting
    exactly at the file cap can still replace one of its files."""
    local = tmp_path / "f0.csv"
    local.write_text("x")
    existing = [
        {"name": f"f{i}.csv", "key": f"proj-1/f{i}.csv", "last_modified": "2026-01-01T00:00:00Z", "etag": "e", "size": 1}
        for i in range(500)
    ]
    api.route("GET", "/api/projects/proj-1/files", {"files": existing, "is_truncated": False})
    api.route("POST", "/api/projects/proj-1/files/upload-url", {"url": "https://s3.test/proj-1/f0.csv", "key": "proj-1/f0.csv", "expires_in": 900})

    monkeypatch.setattr(Client, "upload_file_to_presigned_url", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["files", "put", str(local), "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "Uploaded" in result.output


def test_files_put_skips_the_cap_check_with_a_warning_when_the_listing_is_truncated(api, tmp_path, monkeypatch):
    """A truncated listing under-counts existing files/bytes, so a local
    'OK' would be a false negative worse than no check at all — skip the
    check and say so, rather than silently approving an upload the server
    may still reject."""
    local = tmp_path / "one-more.csv"
    local.write_text("x")
    api.route("GET", "/api/projects/proj-1/files", {"files": [], "is_truncated": True})
    api.route(
        "POST",
        "/api/projects/proj-1/files/upload-url",
        {"url": "https://s3.test/proj-1/one-more.csv", "key": "proj-1/one-more.csv", "expires_in": 900},
    )
    monkeypatch.setattr(Client, "upload_file_to_presigned_url", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["files", "put", str(local), "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "truncated" in result.output
    assert "Uploaded" in result.output


def test_files_get_downloads_to_the_remote_files_basename_by_default(api, tmp_path, monkeypatch):
    api.route("GET", "/api/projects/proj-1/files/download-url", {"url": "https://s3.test/proj-1/data.csv", "expires_in": 900})

    calls = []

    def fake_download(self, download_url, dest_path):
        calls.append((download_url.url, dest_path))
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"a,b\n1,2\n")

    monkeypatch.setattr(Client, "download_file_from_presigned_url", fake_download)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["files", "get", "data.csv", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert calls[0][1] == Path("data.csv")
    assert api.params_for("GET", "/api/projects/proj-1/files/download-url")["path"] == "data.csv"


def test_files_get_defaults_the_name_from_a_path_with_a_trailing_slash(api, tmp_path, monkeypatch):
    """`data/` still has a usable name once the slash is stripped. Without the
    strip, `rsplit("/")` leaves an empty basename and `Path("")` is `Path(".")`."""
    api.route("GET", "/api/projects/proj-1/files/download-url", {"url": "https://s3.test/proj-1/data.csv", "expires_in": 900})

    calls = []

    def fake_download(self, download_url, dest_path):
        calls.append(dest_path)
        dest_path.write_bytes(b"a,b\n")

    monkeypatch.setattr(Client, "download_file_from_presigned_url", fake_download)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["files", "get", "nested/data.csv/", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert calls[0] == Path("data.csv")


def test_files_get_refuses_a_remote_path_with_no_basename(api, tmp_path, monkeypatch):
    """A path that is nothing but separators has no name to default to."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["files", "get", "/", "--project", "proj-1"])

    assert result.exit_code != 0
    assert "no file name to save as" in result.output
    assert api.requests == []  # refused before signing a download URL


def test_files_get_reports_an_unwritable_output_path(api, tmp_path, monkeypatch):
    """An OSError from the write reaches `_handle_errors`, not the user's
    terminal as a traceback. `-o <dir>` raises IsADirectoryError."""
    api.route("GET", "/api/projects/proj-1/files/download-url", {"url": "https://s3.test/proj-1/data.csv", "expires_in": 900})

    def fake_download(self, download_url, dest_path):
        raise IsADirectoryError(21, "Is a directory", str(dest_path))

    monkeypatch.setattr(Client, "download_file_from_presigned_url", fake_download)

    result = runner.invoke(
        cli.app, ["files", "get", "data.csv", "-o", str(tmp_path), "--project", "proj-1"]
    )

    assert result.exit_code != 0
    assert "Is a directory" in result.output
    assert "Traceback" not in result.output


def test_files_rm_aborts_without_confirmation(api, at_a_terminal):
    api.route("GET", "/api/projects/proj-1/files", {"files": [{"name": "data.csv", "key": "proj-1/data.csv", "size": 10}], "is_truncated": False})

    result = runner.invoke(cli.app, ["files", "rm", "data.csv", "--project", "proj-1"], input="n\n")

    assert result.exit_code == 1, result.output  # declining is not success: `rm && deploy` must stop
    assert "Aborted." in result.output
    assert not any(r.method == "DELETE" for r in api.requests)  # the listing is read; nothing is deleted


def test_files_rm_deletes_when_confirmed(api, at_a_terminal):
    api.route("GET", "/api/projects/proj-1/files", {"files": [{"name": "data.csv", "key": "proj-1/data.csv", "size": 10}], "is_truncated": False})
    api.route("DELETE", "/api/projects/proj-1/files", {"deleted": 1})

    result = runner.invoke(cli.app, ["files", "rm", "data.csv", "--project", "proj-1"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output
    assert api.params_for("DELETE", "/api/projects/proj-1/files")["path"] == "data.csv"


# ---------------------------------------------------------------------------
# harumi schedules
# ---------------------------------------------------------------------------

SCHEDULE = {
    "id": "5c4e1b2a-0000-4000-8000-000000000001",
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
    assert "5c4e1b2a" in result.output
    assert "0 9 * * *" in result.output
    assert "main" in result.output


def test_schedules_list_reports_empty(api):
    api.route("GET", "/api/projects/proj-1/schedules", {"schedules": []})

    result = runner.invoke(cli.app, ["schedules", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "No schedules yet" in result.output and "harumi schedules add" in result.output


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


def test_schedules_remove_aborts_without_confirmation(api, at_a_terminal):
    api.route("GET", "/api/projects/proj-1/schedules/5c4e1b2a-0000-4000-8000-000000000001", SCHEDULE)  # read to describe it

    result = runner.invoke(
        cli.app, ["schedules", "rm", "5c4e1b2a-0000-4000-8000-000000000001", "--project", "proj-1"], input="n\n"
    )

    assert result.exit_code == 1, result.output
    assert "Aborted." in result.output
    assert [r.method for r in api.requests] == ["GET"]  # it looked, and changed nothing


def test_schedules_remove_deletes_when_confirmed(api):
    api.route("DELETE", "/api/projects/proj-1/schedules/5c4e1b2a-0000-4000-8000-000000000001", SCHEDULE)

    result = runner.invoke(
        cli.app, ["schedules", "rm", "5c4e1b2a-0000-4000-8000-000000000001", "--project", "proj-1", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output
    assert api.paths() == ["/api/projects/proj-1/schedules/5c4e1b2a-0000-4000-8000-000000000001"]


def test_schedules_get_surfaces_a_404_as_a_clean_error(api):
    api.route("GET", "/api/projects/proj-1/schedules/nope", {"detail": "Not Found"}, status=404)

    result = runner.invoke(cli.app, ["schedules", "get", "nope", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "Error" in result.output


# ---------------------------------------------------------------------------
# harumi share
# ---------------------------------------------------------------------------

SHARE_LINK = {
    "id": "0a1b2c3d-1111-4222-8333-444455556666",
    "project_id": "proj-1",
    "token": "tok-abc123",
    "label": "Client dashboard",
    "enabled": True,
    "chat_enabled": False,
    "run_history_enabled": False,
    "run_control_enabled": False,
    "password_set": False,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


def test_share_list_renders_the_links_and_their_flags(api):
    api.route("GET", "/api/projects/proj-1/share-links", {"links": [SHARE_LINK]})

    result = runner.invoke(cli.app, ["share", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "0a1b2c3d" in result.output  # the start of the id: every share command accepts it
    assert "Client dashboard" in result.output
    assert "tok-abc123" not in result.output  # the token is the secret in the public URL; `share get` shows the URL


def test_share_list_reports_empty(api):
    api.route("GET", "/api/projects/proj-1/share-links", {"links": []})

    result = runner.invoke(cli.app, ["share", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "No share links yet" in result.output and "harumi share add" in result.output


def test_share_get_shows_the_link_url_and_permissions(api):
    api.route("GET", "/api/projects/proj-1/share-links", {"links": [SHARE_LINK]})

    result = runner.invoke(cli.app, ["share", "get", "0a1b2c3d-1111-4222-8333-444455556666", "--project", "proj-1"])

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
        "app_enabled": False,
        "chat_enabled": False,
        "run_history_enabled": False,
        "comparisons_enabled": False,
        "run_control_enabled": False,
    }


def test_share_add_forwards_label_and_permission_flags(api):
    api.route("POST", "/api/projects/proj-1/share-links", SHARE_LINK, status=201)

    result = runner.invoke(
        cli.app,
        [
            "share", "add",
            "--label", "Internal",
            "--app",
            "--chat",
            "--run-history",
            "--comparisons",
            "--run-control",
            "--project", "proj-1",
        ],
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("POST", "/api/projects/proj-1/share-links")
    assert body["label"] == "Internal"
    assert body["app_enabled"] is True
    assert body["chat_enabled"] is True
    assert body["run_history_enabled"] is True
    assert body["comparisons_enabled"] is True
    assert body["run_control_enabled"] is True


def test_share_add_comparisons_needs_run_history(api):
    result = runner.invoke(
        cli.app, ["share", "add", "--comparisons", "--project", "proj-1"]
    )

    assert result.exit_code != 0
    assert "--run-history" in result.output
    assert not [r for r in api.requests if r.method == "POST"]


LINK_ID = "0a1b2c3d-1111-4222-8333-444455556666"
LINK_PATH = f"/api/projects/proj-1/share-links/{LINK_ID}"


def test_share_update_comparisons_needs_run_history_on_the_link(api):
    api.route("GET", "/api/projects/proj-1/share-links", {"links": [SHARE_LINK]})
    api.route("PATCH", LINK_PATH, SHARE_LINK)

    result = runner.invoke(cli.app, ["share", "update", LINK_ID[:8], "--comparisons", "--project", "proj-1"])

    assert result.exit_code != 0
    assert "run history" in result.output
    assert not [r for r in api.requests if r.method == "PATCH"]


def test_share_update_comparisons_goes_through_when_the_link_has_run_history(api):
    api.route("GET", "/api/projects/proj-1/share-links", {"links": [{**SHARE_LINK, "run_history_enabled": True}]})
    api.route("PATCH", LINK_PATH, SHARE_LINK)

    result = runner.invoke(cli.app, ["share", "update", LINK_ID[:8], "--comparisons", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert api.body_for("PATCH", LINK_PATH) == {"comparisons_enabled": True}


def test_share_update_comparisons_together_with_run_history_needs_no_lookup(api):
    api.route("PATCH", LINK_PATH, SHARE_LINK)

    result = runner.invoke(
        cli.app, ["share", "update", LINK_ID, "--run-history", "--comparisons", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    assert api.body_for("PATCH", LINK_PATH) == {"run_history_enabled": True, "comparisons_enabled": True}


def test_share_update_turning_run_history_off_turns_comparisons_off(api):
    api.route("PATCH", LINK_PATH, SHARE_LINK)

    result = runner.invoke(cli.app, ["share", "update", LINK_ID, "--no-run-history", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert api.body_for("PATCH", LINK_PATH) == {"run_history_enabled": False, "comparisons_enabled": False}


def test_share_update_refuses_comparisons_with_no_run_history(api):
    result = runner.invoke(
        cli.app, ["share", "update", LINK_ID, "--comparisons", "--no-run-history", "--project", "proj-1"]
    )

    assert result.exit_code != 0
    assert "--no-run-history" in result.output
    assert not api.requests


def test_share_update_only_sends_provided_fields(api):
    api.route("PATCH", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666", SHARE_LINK)

    result = runner.invoke(
        cli.app,
        ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--run-control", "--project", "proj-1"],
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("PATCH", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666")
    assert body == {"run_control_enabled": True}


def test_share_add_still_accepts_the_removed_io_control_flag_with_a_warning(api):
    """`--io-control` was dropped with the server permission. Scripts and CI jobs that
    still pass it must keep working (exit 0), not fail with a usage error (exit 2)."""
    api.route("POST", "/api/projects/proj-1/share-links", SHARE_LINK, status=201)

    result = runner.invoke(cli.app, ["share", "add", "--io-control", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "no longer does anything" in result.output
    body = api.body_for("POST", "/api/projects/proj-1/share-links")
    assert "io_control_enabled" not in body


def test_share_update_still_accepts_the_removed_io_control_flag_with_a_warning(api):
    api.route("PATCH", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666", SHARE_LINK)

    result = runner.invoke(cli.app, ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--no-io-control", "--chat", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "no longer does anything" in result.output
    assert api.body_for("PATCH", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666") == {"chat_enabled": True}


def test_the_removed_io_control_flag_is_hidden_from_help():
    result = runner.invoke(cli.app, ["share", "add", "--help"])
    assert "io-control" not in result.output


def test_share_update_forwards_app_flag(api):
    api.route("PATCH", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666", SHARE_LINK)

    result = runner.invoke(
        cli.app,
        ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--app", "--project", "proj-1"],
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("PATCH", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666")
    assert body == {"app_enabled": True}

    result = runner.invoke(
        cli.app,
        ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--no-app", "--project", "proj-1"],
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("PATCH", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666")
    assert body == {"app_enabled": False}


def test_share_update_with_no_flags_fails_without_a_request(api):
    result = runner.invoke(cli.app, ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "No fields to update" in result.output
    assert api.requests == []


def test_share_remove_aborts_without_confirmation(api, at_a_terminal):
    api.route("GET", "/api/projects/proj-1/share-links", {"links": [SHARE_LINK]})  # read to name the link in the question

    result = runner.invoke(
        cli.app, ["share", "rm", "0a1b2c3d-1111-4222-8333-444455556666", "--project", "proj-1"], input="n\n"
    )

    assert result.exit_code == 1, result.output
    assert "Aborted." in result.output
    assert "Client dashboard" in result.output  # the question names what is being deleted
    assert not any(r.method == "DELETE" for r in api.requests)  # and nothing was deleted


def test_share_remove_deletes_when_confirmed(api):
    api.route("DELETE", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666", None, status=204)

    result = runner.invoke(
        cli.app, ["share", "rm", "0a1b2c3d-1111-4222-8333-444455556666", "--project", "proj-1", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output
    assert api.paths() == ["/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666"]


def test_share_rotate_mints_a_new_token(api):
    rotated = {**SHARE_LINK, "token": "tok-new456"}
    api.route("POST", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666/rotate", rotated)

    result = runner.invoke(
        cli.app, ["share", "rotate", "0a1b2c3d-1111-4222-8333-444455556666", "--project", "proj-1", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "tok-new456" in result.output


def test_share_update_password_prompts_and_never_echoes_it(api):
    api.route(
        "PUT", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666/password", {**SHARE_LINK, "password_set": True}
    )

    result = runner.invoke(
        cli.app,
        ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--password", "--project", "proj-1"],
        input="correcthorse\ncorrecthorse\n",
    )

    assert result.exit_code == 0, result.output
    assert "correcthorse" not in result.output
    body = api.body_for("PUT", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666/password")
    assert body == {"password": "correcthorse"}


def test_share_update_no_password_removes_it(api):
    api.route("DELETE", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666/password", SHARE_LINK)

    result = runner.invoke(
        cli.app, ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--no-password", "--yes", "--project", "proj-1"]
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
    assert "--proxy-tls-ca-cert" in result.output
    assert "nope.pem" in result.output


def test_datasources_add_rejects_proxy_flags_given_without_use_proxy(api):
    """Without --use-proxy, --proxy-* flags would otherwise be silently
    dropped — the datasource gets created as a plain, non-proxied
    connection with no warning that the flags were ignored."""
    result = runner.invoke(
        cli.app,
        [
            "datasources", "add", "sales_db",
            "--type", "postgresql",
            "--proxy-host", "vpnproxy.harumi.io",
            "--project", "proj-1",
        ],
    )

    assert result.exit_code == 1
    assert "--use-proxy" in result.output
    assert api.requests == []


def test_datasources_add_reports_a_binary_cert_file_without_a_traceback(api, tmp_path):
    """A binary file where a PEM was meant is an honest mistake, so it gets a
    message rather than a UnicodeDecodeError traceback."""
    binary = tmp_path / "not-a-cert.png"
    binary.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")

    result = runner.invoke(
        cli.app,
        [
            "datasources", "add", "sales_db",
            "--type", "postgresql",
            "--use-proxy",
            "--proxy-host", "vpnproxy.harumi.io",
            "--proxy-port", "1433",
            "--proxy-tls-ca-cert", str(binary),
            "--project", "proj-1",
        ],
    )

    assert result.exit_code == 1
    assert "isn't UTF-8 text" in result.output
    assert "Traceback" not in result.output


def test_dashboard_validate_reports_a_binary_spec_without_a_traceback(api, tmp_path):
    """Same guard, reached from a different command — it lives in _handle_errors,
    not in the PEM reader."""
    binary = tmp_path / "dashboard.toml"
    binary.write_bytes(b"\x00\x81\xfe spec")

    result = runner.invoke(cli.app, ["dashboard", "validate", str(binary)])

    assert result.exit_code == 1
    assert "isn't UTF-8 text" in result.output
    assert "Traceback" not in result.output


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
    "id": "7f3a91c2-0000-4000-8000-000000000001",
    "project_id": "proj-1",
    "status": "completed",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:05:00Z",
}


def test_runs_list_renders_each_run(api):
    api.route("GET", "/api/projects/proj-1/runs", {"runs": [RUN]})

    result = runner.invoke(cli.app, ["runs", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "7f3a91c2" in result.output  # the first 8 characters, which every `runs` command accepts
    assert "completed" in result.output


def test_runs_get_prints_captured_stdout_and_error(api):
    api.route(
        "GET",
        "/api/projects/proj-1/runs/7f3a91c2-0000-4000-8000-000000000001",
        {**RUN, "status": "failed", "stdout": "solving...\n", "stderr": "", "error": "infeasible"},
    )

    result = runner.invoke(cli.app, ["runs", "get", "7f3a91c2-0000-4000-8000-000000000001", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "solving..." in result.output
    assert "infeasible" in result.output


def test_runs_cancel_posts_to_the_cancel_endpoint(api):
    api.route("POST", "/api/projects/proj-1/runs/7f3a91c2-0000-4000-8000-000000000001/cancel", {**RUN, "status": "cancelled"})

    result = runner.invoke(cli.app, ["runs", "cancel", "7f3a91c2-0000-4000-8000-000000000001", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "cancelled" in result.output
    assert api.paths() == ["/api/projects/proj-1/runs/7f3a91c2-0000-4000-8000-000000000001/cancel"]


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
    "project_run_id": "7f3a91c2-0000-4000-8000-000000000001",
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


def test_run_without_a_binding_points_at_onboarding_verbs(api, git, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["run"])

    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "harumi new" in output
    assert "harumi clone" in output
    assert "harumi link" in output
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







COMPARISON = {
    "columns": [
        {"run_id": "7f3a91c2-0000-4000-8000-000000000001", "branch": "main", "commit": "aaaaaaa1", "status": "completed", "runtime_s": 90.0},
        {"run_id": "7f3a91c2-0000-4000-8000-000000000002", "branch": "v2", "commit": "bbbbbbb2", "status": "completed", "runtime_s": 125.0},
        {"run_id": "7f3a91c2-0000-4000-8000-000000000003", "branch": "v3", "commit": "ccccccc3", "status": "failed", "runtime_s": None},
    ],
    "kpis": [
        {
            "key": "objective",
            "label": "Total cost",
            "unit": "USD",
            "direction": "min",
            "primary": True,
            "values": [100, 80, None],
            "deltas": [None, -20, None],
        },
        {
            "key": "solver_status",
            "label": "Solver status",
            "unit": None,
            "direction": "none",
            "primary": False,
            "values": ["OPTIMAL", "FEASIBLE", None],
            "deltas": [None, None, None],
        },
    ],
    "winner_run_id": "7f3a91c2-0000-4000-8000-000000000002",
    "no_winner_reason": None,
    "warnings": ["v3@ccccccc: no usable output (No output.json for this run)."],
    "fairness": {"verdict": "differs", "files": ["inputs/demand.csv"]},
}


def test_compare_prints_kpis_winner_and_the_inputs_verdict(api):
    api.route("GET", "/api/projects/proj-1/compare", COMPARISON)

    result = runner.invoke(
        cli.app, ["compare", "7f3a91c2-0000-4000-8000-000000000001", "7f3a91c2-0000-4000-8000-000000000002", "7f3a91c2-0000-4000-8000-000000000003", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    out = result.output
    assert "Total cost" in out and "OPTIMAL" in out
    assert "main" in out and "v2" in out
    assert "baseline" in out.lower()
    assert "winner" in out.lower() and "v2" in out
    # A difference in inputs is the thing to notice, so the files are named.
    assert "inputs/demand.csv" in out
    assert "no usable output" in out


def test_compare_sends_the_runs_in_order_as_one_comma_separated_param(api):
    api.route("GET", "/api/projects/proj-1/compare", COMPARISON)

    runner.invoke(cli.app, ["compare", "7f3a91c2-0000-4000-8000-000000000002", "7f3a91c2-0000-4000-8000-000000000001", "--project", "proj-1"])

    assert api.params_for("GET", "/api/projects/proj-1/compare") == {"runs": "7f3a91c2-0000-4000-8000-000000000002,7f3a91c2-0000-4000-8000-000000000001"}


def test_compare_json_prints_exactly_what_the_api_returned(api):
    api.route("GET", "/api/projects/proj-1/compare", COMPARISON)

    result = runner.invoke(
        cli.app, ["compare", "7f3a91c2-0000-4000-8000-000000000001", "7f3a91c2-0000-4000-8000-000000000002", "--project", "proj-1", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == COMPARISON


def test_compare_says_why_there_is_no_winner(api):
    body = {**COMPARISON, "winner_run_id": None, "no_winner_reason": "No primary KPI is declared."}
    api.route("GET", "/api/projects/proj-1/compare", body)

    result = runner.invoke(cli.app, ["compare", "7f3a91c2-0000-4000-8000-000000000001", "7f3a91c2-0000-4000-8000-000000000002", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "No primary KPI is declared." in result.output


def test_compare_older_api_without_a_verdict_still_prints(api):
    body = {k: v for k, v in COMPARISON.items() if k != "fairness"}
    api.route("GET", "/api/projects/proj-1/compare", body)

    result = runner.invoke(cli.app, ["compare", "7f3a91c2-0000-4000-8000-000000000001", "7f3a91c2-0000-4000-8000-000000000002", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "Total cost" in result.output


@pytest.mark.parametrize(
    "ids, said",
    [
        (["7f3a91c2-0000-4000-8000-000000000001"], "needs 2 to 4 runs"),
        (["a", "b", "c", "d", "e"], "at most 4 runs"),
        (["7f3a91c2-0000-4000-8000-000000000001", "7f3a91c2-0000-4000-8000-000000000001"], "more than once"),
    ],
)
def test_compare_needs_two_to_four_distinct_runs_before_calling_the_api(api, ids, said):
    result = runner.invoke(cli.app, ["compare", *ids, "--project", "proj-1"])

    assert result.exit_code != 0
    assert said in " ".join(result.output.split())  # each case says what is wrong with it
    assert "harumi runs list" in " ".join(result.output.split())  # and where ids come from
    assert api.paths() == []  # and nothing was sent


def test_compare_prints_branch_names_and_paths_that_look_like_rich_markup(api):
    """`[x]` is markup to rich, and an unmatched `[/x]` makes it raise after the request
    succeeded. Branch names, files and warnings are all user-controlled."""
    body = {
        **COMPARISON,
        "columns": [
            {**COMPARISON["columns"][0], "branch": "fix/[bold]x"},
            {**COMPARISON["columns"][1], "branch": "odd/[/nope]"},
        ],
        "kpis": [{**COMPARISON["kpis"][0], "label": "Cost [red]", "values": [1, 2], "deltas": [None, 1]}],
        "winner_run_id": "7f3a91c2-0000-4000-8000-000000000002",
        "fairness": {"verdict": "differs", "files": ["inputs/[2024].csv"]},
        "warnings": ["see [/oops]"],
    }
    api.route("GET", "/api/projects/proj-1/compare", body)

    result = runner.invoke(cli.app, ["compare", "7f3a91c2-0000-4000-8000-000000000001", "7f3a91c2-0000-4000-8000-000000000002", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    for text in ("fix/[bold]x", "odd/[/nope]", "Cost [red]", "inputs/[2024].csv", "see [/oops]"):
        assert text in result.output


def test_compare_tolerates_a_response_that_leaves_fields_out(api):
    api.route(
        "GET",
        "/api/projects/proj-1/compare",
        {
            "columns": [{"run_id": "7f3a91c2-0000-4000-8000-000000000001"}, {"run_id": "7f3a91c2-0000-4000-8000-000000000002", "branch": "v2"}],
            "kpis": [{"key": "objective", "values": [1]}],
        },
    )

    result = runner.invoke(cli.app, ["compare", "7f3a91c2-0000-4000-8000-000000000001", "7f3a91c2-0000-4000-8000-000000000002", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "objective" in result.output


def test_compare_does_not_misalign_columns_when_a_kpi_has_too_few_values(api):
    body = {**COMPARISON, "kpis": [{**COMPARISON["kpis"][0], "values": [100], "deltas": []}]}
    api.route("GET", "/api/projects/proj-1/compare", body)

    result = runner.invoke(cli.app, ["compare", "7f3a91c2-0000-4000-8000-000000000001", "7f3a91c2-0000-4000-8000-000000000002", "7f3a91c2-0000-4000-8000-000000000003", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert result.output.count("—") >= 2


def test_compare_names_the_primary_kpi_only_when_there_is_one(api):
    body = {**COMPARISON, "kpis": [{**COMPARISON["kpis"][0], "primary": False}]}
    api.route("GET", "/api/projects/proj-1/compare", body)

    result = runner.invoke(cli.app, ["compare", "7f3a91c2-0000-4000-8000-000000000001", "7f3a91c2-0000-4000-8000-000000000002", "--project", "proj-1"])

    assert "Winner" in result.output
    assert "marked *" not in result.output


def test_compare_reports_an_unknown_run_as_an_error(api):
    """A whole id the server does not know: its own words, plus where to find real ones."""
    api.route("GET", "/api/projects/proj-1/compare", {"detail": "Run not found"}, status=404)

    result = runner.invoke(
        cli.app,
        ["compare", "7f3a91c2-0000-4000-8000-000000000001", "9e9e9e9e-0000-4000-8000-000000000009", "--project", "proj-1"],
    )

    assert result.exit_code == 1
    assert "Run not found" in result.output and "harumi runs list" in result.output


def test_compare_with_something_that_is_no_run_id_says_so_and_points_at_the_list(api):
    """Not a whole id and not the start of one: said before asking the server, naming what was typed."""
    api.route("GET", "/api/projects/proj-1/runs", {"runs": [RUN]})

    result = runner.invoke(cli.app, ["compare", "7f3a91c2-0000-4000-8000-000000000001", "nope", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "nope" in result.output and "harumi runs list" in result.output


def test_compare_rejects_a_run_id_containing_a_comma(api):
    result = runner.invoke(cli.app, ["compare", "7f3a91c2-0000-4000-8000-000000000001", "a,b", "--project", "proj-1"])

    assert result.exit_code == 1
    assert api.paths() == []


# ---------------------------------------------------------------------------
# harumi memories
# ---------------------------------------------------------------------------

MEMORY = {
    "id": "3d7a9f10-0000-4000-8000-000000000001",
    "scope": "project",
    "project_id": "proj-1",
    "content": "Use metric units",
    "source": "cli",
    "retrieval_mode": "on_demand",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-02T00:00:00Z",
}

INHERITED_MEMORY = {
    **MEMORY,
    "id": "mem-org",
    "scope": "organization",
    "project_id": None,
    "content": "Org-wide naming rules",
    "retrieval_mode": "always",
}


def test_memories_list_shows_scope_mode_and_content(api):
    api.route(
        "GET", "/api/projects/proj-1/memories", {"memories": [MEMORY, INHERITED_MEMORY]}
    )

    result = runner.invoke(cli.app, ["memories", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "3d7a9f10" in result.output
    assert "Use metric units" in result.output
    assert "organization" in result.output
    assert "always" in result.output


def test_memories_list_reports_empty(api):
    api.route("GET", "/api/projects/proj-1/memories", {"memories": []})

    result = runner.invoke(cli.app, ["memories", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "No memories yet" in result.output and "harumi memories add" in result.output


def test_memories_list_forwards_query_and_mode_filters(api):
    api.route("GET", "/api/projects/proj-1/memories", {"memories": []})

    result = runner.invoke(
        cli.app,
        ["memories", "list", "--query", "units", "--mode", "always", "--project", "proj-1"],
    )

    assert result.exit_code == 0, result.output
    params = api.params_for("GET", "/api/projects/proj-1/memories")
    assert params == {"q": "units", "retrieval_mode": "always"}


def test_memories_list_rejects_an_unknown_mode(api):
    result = runner.invoke(
        cli.app, ["memories", "list", "--mode", "sometimes", "--project", "proj-1"]
    )

    assert result.exit_code != 0
    assert api.requests == []


def test_memories_add_posts_content_with_cli_source(api):
    api.route("POST", "/api/projects/proj-1/memories", MEMORY, status=201)

    result = runner.invoke(
        cli.app, ["memories", "add", "Use metric units", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    assert "3d7a9f10-0000-4000-8000-000000000001" in result.output
    body = api.body_for("POST", "/api/projects/proj-1/memories")
    assert body["content"] == "Use metric units"
    assert body["source"] == "cli"
    assert body["retrieval_mode"] == "on_demand"


def test_memories_add_forwards_mode(api):
    api.route("POST", "/api/projects/proj-1/memories", MEMORY, status=201)

    result = runner.invoke(
        cli.app, ["memories", "add", "Always cite", "--mode", "always", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    assert api.body_for("POST", "/api/projects/proj-1/memories")["retrieval_mode"] == "always"


def test_memories_add_prompts_for_content_when_omitted(api, monkeypatch):
    monkeypatch.setattr(cli.interactive, "is_interactive", lambda *a, **k: True)
    api.route("POST", "/api/projects/proj-1/memories", MEMORY, status=201)

    result = runner.invoke(
        cli.app, ["memories", "add", "--project", "proj-1"], input="Typed at the prompt\n"
    )

    assert result.exit_code == 0, result.output
    assert api.body_for("POST", "/api/projects/proj-1/memories")["content"] == "Typed at the prompt"


def test_memories_edit_with_no_flags_fails_without_calling_the_api(api):
    result = runner.invoke(cli.app, ["memories", "edit", "3d7a9f10-0000-4000-8000-000000000001", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "No fields to update" in result.output
    assert api.requests == []


def test_memories_edit_sends_only_the_given_fields(api):
    api.route("PATCH", "/api/projects/proj-1/memories/3d7a9f10-0000-4000-8000-000000000001", MEMORY)

    result = runner.invoke(
        cli.app, ["memories", "edit", "3d7a9f10-0000-4000-8000-000000000001", "--mode", "always", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    assert "Updated" in result.output
    assert api.body_for("PATCH", "/api/projects/proj-1/memories/3d7a9f10-0000-4000-8000-000000000001") == {
        "retrieval_mode": "always"
    }


def test_memories_edit_sends_content(api):
    api.route("PATCH", "/api/projects/proj-1/memories/3d7a9f10-0000-4000-8000-000000000001", MEMORY)

    result = runner.invoke(
        cli.app,
        ["memories", "edit", "3d7a9f10-0000-4000-8000-000000000001", "--content", "New text", "--project", "proj-1"],
    )

    assert result.exit_code == 0, result.output
    assert api.body_for("PATCH", "/api/projects/proj-1/memories/3d7a9f10-0000-4000-8000-000000000001") == {"content": "New text"}


def test_memories_rm_aborts_without_confirmation(api, at_a_terminal):
    api.route("GET", "/api/projects/proj-1/memories", {"memories": [MEMORY]})  # read to quote it

    result = runner.invoke(
        cli.app, ["memories", "rm", "3d7a9f10-0000-4000-8000-000000000001", "--project", "proj-1"], input="n\n"
    )

    assert result.exit_code == 1, result.output
    assert "Aborted." in result.output
    assert [r.method for r in api.requests] == ["GET"]  # it looked, and changed nothing


def test_memories_rm_deletes_when_confirmed(api):
    api.route("DELETE", "/api/projects/proj-1/memories/3d7a9f10-0000-4000-8000-000000000001", MEMORY)

    result = runner.invoke(
        cli.app, ["memories", "rm", "3d7a9f10-0000-4000-8000-000000000001", "--project", "proj-1", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output
    assert api.paths() == ["/api/projects/proj-1/memories/3d7a9f10-0000-4000-8000-000000000001"]


def test_memories_rm_surfaces_a_404_as_a_clean_error(api):
    api.route("DELETE", "/api/projects/proj-1/memories/nope", {"detail": "Not Found"}, status=404)

    result = runner.invoke(
        cli.app, ["memories", "rm", "nope", "--project", "proj-1", "--yes"]
    )

    assert result.exit_code == 1
    assert "Error" in result.output


# ---------------------------------------------------------------------------
# harumi skills
# ---------------------------------------------------------------------------


LIBRARY_SKILL = """---
name: Scheduling modeling
description: Model rosters.
category: modeling
tags: scheduling
version: 3
source: someone/else
---

# Body
"""


def _route_library(api):
    summary = {
        "slug": "scheduling-modeling",
        "name": "Scheduling modeling",
        "description": "Model rosters.",
        "category": "modeling",
        "tags": ["scheduling"],
        "version": 3,
    }
    api.route("GET", "/api/skill-library", {"skills": [summary, {**summary, "slug": "vrp-modeling", "name": "VRP", "description": "Routes.", "category": "debugging", "tags": ["vrp"]}]})
    api.route("GET", "/api/skill-library/scheduling-modeling", {**summary, "content": LIBRARY_SKILL})


def test_skills_without_a_subcommand_shows_help_and_makes_no_api_call(api):
    result = runner.invoke(cli.app, ["skills"])

    assert "library" in result.output
    assert "add" in result.output
    assert api.requests == []


def test_skills_library_lists_presets(api):
    _route_library(api)

    result = runner.invoke(cli.app, ["skills", "library"])

    assert result.exit_code == 0, result.output
    assert "scheduling-modeling" in result.output
    assert "vrp-modeling" in result.output
    assert api.paths() == ["/api/skill-library"]


def test_skills_library_filters_by_category_and_query(api):
    _route_library(api)

    by_category = runner.invoke(cli.app, ["skills", "library", "--category", "debugging"])
    by_query = runner.invoke(cli.app, ["skills", "library", "--query", "roster"])

    assert "vrp-modeling" in by_category.output
    assert "scheduling-modeling" not in by_category.output
    assert "scheduling-modeling" in by_query.output
    assert "vrp-modeling" not in by_query.output


def test_skills_add_writes_the_preset_with_provenance(api, bound_dir):
    _route_library(api)

    result = runner.invoke(cli.app, ["skills", "add", "scheduling-modeling"])

    assert result.exit_code == 0, result.output
    written = (bound_dir / "skills" / "scheduling-modeling" / "SKILL.md").read_text()
    assert "source: harumi/scheduling-modeling" in written
    assert "version: 3" in written
    # The preset's own `source:` is replaced, not duplicated, and the body is untouched.
    assert "someone/else" not in written
    assert written.count("source:") == 1
    assert written.endswith("# Body\n")
    assert "git push harumi" in result.output


def test_skills_add_refuses_to_overwrite_without_force(api, bound_dir):
    _route_library(api)
    runner.invoke(cli.app, ["skills", "add", "scheduling-modeling"])

    again = runner.invoke(cli.app, ["skills", "add", "scheduling-modeling"])
    forced = runner.invoke(cli.app, ["skills", "add", "scheduling-modeling", "--force"])

    assert again.exit_code == 1
    assert "already installed" in again.output
    assert forced.exit_code == 0, forced.output


def test_skills_add_does_not_clobber_a_users_own_skill_of_the_same_name(api, bound_dir):
    _route_library(api)
    mine = bound_dir / "skills" / "scheduling-modeling" / "SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("---\nname: mine\n---\nmy own\n")

    result = runner.invoke(cli.app, ["skills", "add", "scheduling-modeling"])

    assert result.exit_code == 1
    # The message starts with the file's path, so how it wraps depends on how long the
    # temp directory is; compare the words, not the line breaks.
    assert "not the library" in " ".join(result.output.split())
    assert mine.read_text().endswith("my own\n")
    # Refused before asking the API for anything.
    assert api.requests == []


def test_skills_add_outside_a_clone_is_a_clean_error(api, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["skills", "add", "scheduling-modeling"])

    assert result.exit_code == 1
    assert "harumi clone" in result.output
    assert api.requests == []


def test_skills_add_rejects_a_slug_with_a_path_separator(api, bound_dir):
    result = runner.invoke(cli.app, ["skills", "add", "../escape"])

    assert result.exit_code == 1
    assert api.requests == []
    assert not (bound_dir / "skills").exists()


def test_skills_add_unknown_slug_is_a_clean_error(api, bound_dir):
    api.route("GET", "/api/skill-library/nope", {"detail": "Unknown skill 'nope'"}, status=404)

    result = runner.invoke(cli.app, ["skills", "add", "nope"])

    assert result.exit_code == 1
    assert "Error" in result.output
    assert not (bound_dir / "skills").exists()


def test_agent_skills_install_dry_run_shows_source_and_destination(tmp_path):
    result = runner.invoke(
        cli.app, ["agent-skills", "install", "--project", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "->" in result.output  # the dry run keeps its source -> destination: it is how you check what is copied
    assert "harumi-cli" in result.output


@pytest.mark.parametrize("content", ["", "   ", "x" * 4001])
def test_memories_add_rejects_blank_or_oversized_content_without_calling_the_api(api, content):
    result = runner.invoke(
        cli.app, ["memories", "add", content, "--project", "proj-1"]
    )

    assert result.exit_code == 1
    assert "Error" in result.output
    assert api.requests == []


def test_memories_add_rejects_blank_prompt_answer(api):
    result = runner.invoke(
        cli.app, ["memories", "add", "--project", "proj-1"], input="   \n"
    )

    assert result.exit_code == 1
    assert api.requests == []


@pytest.mark.parametrize("content", ["", "   ", "x" * 4001])
def test_memories_edit_rejects_blank_or_oversized_content_without_calling_the_api(api, content):
    result = runner.invoke(
        cli.app, ["memories", "edit", "3d7a9f10-0000-4000-8000-000000000001", "--content", content, "--project", "proj-1"]
    )

    assert result.exit_code == 1
    assert "Error" in result.output
    assert api.requests == []


def test_memories_add_accepts_content_at_the_limit(api):
    api.route("POST", "/api/projects/proj-1/memories", MEMORY, status=201)

    result = runner.invoke(
        cli.app, ["memories", "add", "x" * 4000, "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# 0.11: renamed commands and the deprecated names that still work
# ---------------------------------------------------------------------------


def test_deprecated_names_are_hidden_from_help_and_the_surface():
    from typer.main import get_command

    from scripts.emit_cli_surface import build_surface

    # Ask the command tree rather than scraping --help: the rendered help is
    # colourised differently on CI.
    top = get_command(cli.app).commands
    for old in ("push", "repo", "outputs", "config", "skill"):
        assert top[old].hidden, f"{old} should be hidden"
    assert not top["import"].hidden

    paths = {command["path"] for command in build_surface()["commands"]}
    assert "push" not in paths and "outputs" not in paths
    assert not any(path.startswith(("repo ", "config ", "skill ")) for path in paths)
    assert not any(path.endswith((" remove", " delete", " set-password", " rm-password", " current")) for path in paths)


@pytest.mark.parametrize(
    "argv, method, route, use",
    [
        (["projects", "delete", "p1", "--yes"], "DELETE", "/api/projects/p1", "harumi projects rm"),
        (["share", "remove", "0a1b2c3d-1111-4222-8333-444455556666", "--yes", "-p", "proj-1"], "DELETE", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666", "harumi share rm"),
        (["schedules", "remove", "5c4e1b2a-0000-4000-8000-000000000001", "--yes", "-p", "proj-1"], "DELETE", "/api/projects/proj-1/schedules/5c4e1b2a-0000-4000-8000-000000000001", "harumi schedules rm"),
        (["datasources", "remove", "sales_db", "--yes", "-p", "proj-1"], "DELETE", "/api/datasources/proj-1/sales_db", "harumi datasources rm"),
        (["org", "delete", "6d1c0aa5-0000-4000-8000-0000000000a1", "--yes"], "DELETE", "/api/users/organizations/6d1c0aa5-0000-4000-8000-0000000000a1", "harumi org rm"),
        (["share", "rm-password", "0a1b2c3d-1111-4222-8333-444455556666", "-p", "proj-1"], "DELETE", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666/password", "share update <LINK_ID> --no-password"),
        (["repo", "ls", "-p", "proj-1"], "GET", "/api/projects/proj-1/repo/files", "git ls-files"),
    ],
)
def test_a_deprecated_name_still_runs_and_names_its_replacement(api, argv, method, route, use):
    api.route(method, route, [] if argv[0] == "repo" else {**SHARE_LINK, "id": "x", "name": "x"})
    api.route("GET", "/api/users/organizations", [{"id": "6d1c0aa5-0000-4000-8000-0000000000a1", "business_name": "Acme", "role": "owner"}])  # `org rm` names what it deletes

    result = runner.invoke(cli.app, argv)

    assert "deprecated" in result.output
    assert use in result.output
    assert "removed in 0.12.0" in result.output
    # It really ran: the request the new name makes was sent.
    assert api.requests and api.requests[-1].url.path == route


def test_org_remove_is_now_org_rm_member_and_org_delete_is_org_rm(api):
    api.route("GET", "/api/users/organizations", [{"id": "6d1c0aa5-0000-4000-8000-0000000000a1", "business_name": "Acme", "role": "owner"}])
    api.route("DELETE", "/api/users/organizations/6d1c0aa5-0000-4000-8000-0000000000a1", None)

    removed = runner.invoke(cli.app, ["org", "rm", "6d1c0aa5-0000-4000-8000-0000000000a1", "--yes"])
    stale = runner.invoke(cli.app, ["org", "rm", "6d1c0aa5-0000-4000-8000-0000000000a1", "user-1", "--yes"])

    assert removed.exit_code == 0, removed.output
    assert "deprecated" not in removed.output
    # `org rm` takes one argument; the old two-argument form is a usage error, not a silent org delete.
    assert stale.exit_code != 0
    # The organization is looked up first so the question can name it, then deleted: once.
    assert api.paths() == ["/api/users/organizations", "/api/users/organizations/6d1c0aa5-0000-4000-8000-0000000000a1"]


def test_org_use_persists_the_org_and_the_old_config_name_still_works(api):
    from harumi.config import Config

    # The id is checked against your organizations before it is saved, so they have to exist.
    api.route("GET", "/api/users/organizations", [
        {"id": "org-9", "business_name": "Nine", "role": "owner"},
        {"id": "org-10", "business_name": "Ten", "role": "owner"},
    ])
    new = runner.invoke(cli.app, ["org", "use", "org-9"])
    assert new.exit_code == 0, new.output
    assert Config.load().org_id == "org-9"

    old = runner.invoke(cli.app, ["config", "set-org", "org-10"])
    assert old.exit_code == 0, old.output
    assert "harumi org use" in old.output
    assert Config.load().org_id == "org-10"


def test_skill_path_and_profile_show_are_deprecated_not_gone(api):
    api.route("GET", "/api/users/profile", {"id": "u1", "email": "dev@harumi.test"})

    assert "agent-skills install --dry-run" in runner.invoke(cli.app, ["skill", "path"]).output
    shown = runner.invoke(cli.app, ["profile", "show"])
    assert "harumi whoami" in shown.output
    assert "dev@harumi.test" in shown.output


def test_whoami_shows_the_profile_and_org_but_not_the_environment_to_a_production_user(api):
    api.route(
        "GET",
        "/api/users/profile",
        {"id": "u1", "email": "dev@harumi.test", "first_name": "Ada", "last_name": "Lovelace", "bio": "Math"},
    )

    result = runner.invoke(cli.app, ["whoami", "--org", "org-7"])

    assert result.exit_code == 0, result.output
    for expected in ("dev@harumi.test", "u1", "Ada Lovelace", "Math", "org-7"):
        assert expected in result.output
    assert "env:" not in result.output  # production is the only thing most people ever use


def test_new_no_clone_creates_only_and_points_at_clone(api, git_ops, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api.route("POST", "/api/projects", {"id": "proj-9", "name": "Widget", "notebook_ids": []})
    _route_project_repo(api, "proj-9", "widget")

    result = runner.invoke(cli.app, ["new", "Widget", "--no-clone"])
    with_dir = runner.invoke(cli.app, ["new", "Widget", "--no-clone", "--dir", str(tmp_path / "x")])

    assert result.exit_code == 0, result.output
    assert "harumi clone proj-9" in result.output
    assert git_ops["clone"] == []
    assert with_dir.exit_code == 1
    assert "--dir" in with_dir.output


def test_projects_create_is_a_deprecated_new_no_clone(api, git_ops, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api.route("POST", "/api/projects", {"id": "proj-9", "name": "Widget", "notebook_ids": []})
    _route_project_repo(api, "proj-9", "widget")

    result = runner.invoke(cli.app, ["projects", "create", "Widget"])

    assert result.exit_code == 0, result.output
    assert "harumi new <NAME> --no-clone" in result.output
    assert "harumi clone proj-9" in result.output
    assert git_ops["clone"] == []


def test_push_is_a_deprecated_import(api, git_ops, tmp_path):
    from harumi.config import save_git_token

    save_git_token("gitea-token", username="dev@harumi.test")
    folder = tmp_path / "solver"
    folder.mkdir()
    (folder / "main.py").write_text("print(1)\n")
    api.route(
        "POST",
        "/api/projects",
        {"id": "proj-1", "name": "solver", "notebook_ids": [], "repo": {"owner": "a", "name": "s", "clone_url": "https://git.harumi.test/a/s.git", "default_branch": "main"}},
    )
    _route_project_repo(api, "proj-1", "solver")

    result = runner.invoke(cli.app, ["push", str(folder)])

    assert "harumi import" in result.output
    assert git_ops["push_folder"], result.output


def test_runs_download_defaults_to_the_latest_run(api, tmp_path, monkeypatch):
    downloaded = {}

    def fake_download(_api, project_id, run, dest):
        downloaded.update(project=project_id, run=run.id, dest=dest)
        return dest / f"{run.id}.zip"

    monkeypatch.setattr("harumi.execution.download_run_output", fake_download)
    api.route(
        "GET",
        "/api/projects/proj-1/runs",
        {"runs": [{**RUN, "id": "newest", "output_url": "s3://x"}, {**RUN, "id": "older", "output_url": "s3://y"}]},
    )

    result = runner.invoke(cli.app, ["runs", "download", "--project", "proj-1", "-o", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert downloaded == {"project": "proj-1", "run": "newest", "dest": tmp_path}
    assert "newest.zip" in result.output


def test_runs_download_takes_an_explicit_run_id(api, tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.execution.download_run_output", lambda _a, _p, run, dest: dest / f"{run.id}.zip")
    api.route("GET", "/api/projects/proj-1/runs/7f3a91c2-0000-4000-8000-000000000001", {**RUN, "output_url": "s3://x"})

    result = runner.invoke(cli.app, ["runs", "download", "7f3a91c2-0000-4000-8000-000000000001", "--project", "proj-1", "-o", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert api.paths() == ["/api/projects/proj-1/runs/7f3a91c2-0000-4000-8000-000000000001"]


def test_runs_download_with_no_runs_is_a_clean_error(api):
    api.route("GET", "/api/projects/proj-1/runs", {"runs": []})

    result = runner.invoke(cli.app, ["runs", "download", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "no runs" in result.output


def test_share_update_password_can_combine_with_other_flags_and_alone_needs_no_other_field(api):
    api.route("PUT", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666/password", {**SHARE_LINK, "password_set": True})
    api.route("PATCH", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666", {**SHARE_LINK, "label": "renamed"})

    alone = runner.invoke(
        cli.app, ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--password", "--project", "proj-1"], input="correcthorse\ncorrecthorse\n"
    )
    both = runner.invoke(
        cli.app,
        ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--label", "renamed", "--password", "--project", "proj-1"],
        input="correcthorse\ncorrecthorse\n",
    )
    nothing = runner.invoke(cli.app, ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--project", "proj-1"])

    assert alone.exit_code == 0, alone.output
    assert both.exit_code == 0, both.output
    assert "correcthorse" not in alone.output + both.output
    # The password goes last, so a rejected field change can't half-apply it.
    assert [r.method for r in api.requests] == ["PUT", "PATCH", "PUT"]
    assert nothing.exit_code == 1
    assert "--password" in nothing.output


def test_run_output_dir_is_deprecated_but_still_accepted(api, git, bound_dir):
    api.route("POST", "/api/projects/proj-bound/execute", {"execution_log_id": "e1", "project_run_id": "7f3a91c2-0000-4000-8000-000000000001", "status": "queued"})

    result = runner.invoke(cli.app, ["run", "--branch", "main", "-o", "out"])

    assert "harumi runs download" in result.output


def test_share_update_does_not_touch_the_password_when_the_other_fields_are_rejected(api):
    api.route("PATCH", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666", {"detail": "bad label"}, status=422)
    api.route("PUT", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666/password", SHARE_LINK)

    result = runner.invoke(
        cli.app,
        ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--label", "x", "--password", "--project", "proj-1"],
        input="correcthorse\ncorrecthorse\n",
    )

    assert result.exit_code == 1
    assert [r.method for r in api.requests] == ["PATCH"]


def test_share_update_says_so_when_only_the_password_part_fails(api):
    api.route("PATCH", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666", SHARE_LINK)
    api.route("PUT", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666/password", {"detail": "too short"}, status=422)

    result = runner.invoke(
        cli.app,
        ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--label", "x", "--password", "--project", "proj-1"],
        input="short\nshort\n",
    )

    assert result.exit_code == 1
    assert "already updated" in result.output


def test_share_update_password_prompt_must_be_confirmed(api):
    api.route("PUT", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666/password", {**SHARE_LINK, "password_set": True})

    result = runner.invoke(
        cli.app,
        ["share", "update", "0a1b2c3d-1111-4222-8333-444455556666", "--password", "--project", "proj-1"],
        input="correcthorse\ntypo-horse\ncorrecthorse\ncorrecthorse\n",
    )

    assert result.exit_code == 0, result.output
    # The mismatched pair was rejected and re-asked; only the confirmed value was sent.
    assert api.body_for("PUT", "/api/projects/proj-1/share-links/0a1b2c3d-1111-4222-8333-444455556666/password") == {"password": "correcthorse"}
    assert len([r for r in api.requests if r.method == "PUT"]) == 1


@pytest.mark.parametrize("slug", ["..\\..\\x", "a/b", "../x", "A-Upper", "-lead", "", "x..y"])
def test_skills_add_rejects_slugs_that_could_escape_the_clone(api, bound_dir, slug):
    result = runner.invoke(cli.app, ["skills", "add", slug])

    assert result.exit_code != 0
    assert api.requests == []
    assert not (bound_dir / "skills").exists()


def test_skills_library_category_filter_ignores_case_on_both_sides(api):
    api.route(
        "GET",
        "/api/skill-library",
        {"skills": [{"slug": "a-skill", "name": "A", "description": "d", "category": "Modeling", "tags": [], "version": 1}]},
    )

    result = runner.invoke(cli.app, ["skills", "library", "--category", "MODELING"])

    assert "a-skill" in result.output


# --- project picker: `harumi clone` with no id -------------------------------

_TWO_PROJECTS = {
    "projects": [
        {"id": "proj-1", "name": "Solver", "notebook_ids": []},
        {"id": "proj-2", "name": "Routing [beta]", "notebook_ids": []},
    ],
    "total_count": 2,
}


def test_clone_without_an_id_lets_you_pick_the_project(api, git_ops, tmp_path, monkeypatch):
    from harumi import interactive
    from harumi.config import save_git_token

    monkeypatch.chdir(tmp_path)
    save_git_token("gitea-token", username="dev@harumi.test")
    api.route("GET", "/api/projects", _TWO_PROJECTS)
    api.route("GET", "/api/projects/proj-2", {"id": "proj-2", "name": "Routing", "notebook_ids": []})
    _route_project_repo(api, "proj-2", "routing")
    offered = {}

    def pick(message, options, flag=None):
        offered["labels"] = [o.label for o in options]
        offered["flag"] = flag
        return "proj-2"

    monkeypatch.setattr(interactive, "select", pick)

    result = runner.invoke(cli.app, ["clone"])

    assert result.exit_code == 0, result.output
    assert offered["labels"] == ["Solver", "Routing [beta]"]
    assert offered["flag"] == "the PROJECT_ID argument"
    assert git_ops["clone"][0]["clone_url"] == "https://git.harumi.test/acme/routing.git"


def test_clone_without_an_id_and_without_a_terminal_lists_projects_and_never_prompts(
    api, git_ops, tmp_path, monkeypatch
):
    """The agent/CI path: fail fast, naming the projects and the argument to pass."""
    from harumi import interactive

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)
    api.route("GET", "/api/projects", _TWO_PROJECTS)

    result = runner.invoke(cli.app, ["clone"])

    assert result.exit_code == 1
    assert "Solver" in result.output and "proj-1" in result.output
    # Server-supplied text with brackets must survive Rich markup intact.
    assert "Routing [beta]" in result.output
    assert "PROJECT_ID" in result.output
    assert git_ops["clone"] == []


def test_clone_picker_quit_clones_nothing_and_exits_zero(api, git_ops, tmp_path, monkeypatch):
    from harumi import interactive

    monkeypatch.chdir(tmp_path)
    api.route("GET", "/api/projects", _TWO_PROJECTS)
    monkeypatch.setattr(interactive, "select", lambda *a, **k: None)

    result = runner.invoke(cli.app, ["clone"])

    assert result.exit_code == 0, result.output
    assert git_ops["clone"] == []


def test_clone_with_no_projects_points_at_new(api, git_ops, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api.route("GET", "/api/projects", {"projects": [], "total_count": 0})

    result = runner.invoke(cli.app, ["clone"])

    assert result.exit_code == 1
    assert "harumi new" in result.output


def test_memories_add_reads_standard_input_when_given_a_dash(api):
    api.route("POST", "/api/projects/proj-1/memories", MEMORY, status=201)

    result = runner.invoke(cli.app, ["memories", "add", "-", "--project", "proj-1"], input="From a pipe\n")

    assert result.exit_code == 0, result.output
    assert api.body_for("POST", "/api/projects/proj-1/memories")["content"] == "From a pipe"
