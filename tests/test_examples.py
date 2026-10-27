"""`harumi examples` and `new --example` (US-6): a wrong slug is caught before anything is created, and the list
says enough to choose from. They were `harumi blueprints` and `new --blueprint` until the platform renamed
Templates to Examples; the API still calls them blueprints (`GET /blueprints`, a `blueprint` field), so the fake
server below keeps the wire names, and only what a person types and reads says "example".

harumi-api validates the slug before it writes a project (a 400), so nothing is left behind; what the CLI did with
that 400 was print it as a bare `HTTP 400: ...`.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config
from harumi.client import Client

try:
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()

BLUEPRINTS = [
    {"slug": "getting-started", "name": "Getting started", "description": "A first project with a dashboard.", "tags": ["intro"], "audience": "new users", "key_results": None},
    {"slug": "vehicle-routing", "name": "Vehicle routing [VRP]", "description": "Route a fleet.", "tags": ["routing", "ortools"]},
    {"slug": "empty-desc", "name": "No description", "description": "", "tags": []},
]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr(cli.console, "_width", 100)
    monkeypatch.setattr(cli.console, "_height", 40)
    config.save_credentials(access_token="a", refresh_token="r")


@pytest.fixture
def server(monkeypatch):
    state = {"blueprints": [dict(b) for b in BLUEPRINTS], "created": [], "fail_list": None, "requests": []}

    def handler(request):
        state["requests"].append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/blueprints"):
            if state["fail_list"]:
                return httpx.Response(state["fail_list"], json={"detail": "boom"})
            return httpx.Response(200, json={"blueprints": state["blueprints"]})
        if path.endswith("/billing/entitlement/me"):
            return httpx.Response(200, json={"personal": {"active": True, "tier": "pro", "reason": None}, "organizations": [], "paywall_enabled": False})
        if request.method == "POST" and path.endswith("/projects"):
            body = json.loads(request.content)
            wanted = body.get("blueprint")
            if wanted and wanted not in {b["slug"] for b in state["blueprints"]}:
                return httpx.Response(400, json={"detail": f"Blueprint '{wanted}' not found"})
            state["created"].append(body)
            return httpx.Response(201, json={"id": "p9", "name": body.get("name") or "Demo", "customer_id": "c1"})
        return httpx.Response(404, json={"detail": f"unrouted {request.method} {path}"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli, "_get_client", lambda api_url=None, git_url=None, org=None: Client(api_url="https://api.harumi.io/api", transport=transport))
    return state


# -- the list ----------------------------------------------------------------------------------------------------------------


def test_the_list_says_how_to_use_an_example(server):
    result = runner.invoke(cli.app, ["examples"])

    assert result.exit_code == 0, result.stderr
    assert "getting-started" in result.stdout and "harumi new" in result.stdout and "--example" in result.stdout


def test_the_list_shows_names_as_stored(server):
    result = runner.invoke(cli.app, ["examples"])

    assert "Vehicle routing [VRP]" in result.stdout


def test_a_slug_is_never_cut_at_a_narrow_width(server, monkeypatch):
    monkeypatch.setattr(cli.console, "_width", 50)

    result = runner.invoke(cli.app, ["examples"])

    assert "getting-started" in result.stdout and "vehicle-routing" in result.stdout


def test_json_is_the_same_list(server):
    document = json.loads(runner.invoke(cli.app, ["examples", "--json"]).stdout)

    assert [b["slug"] for b in document] == ["getting-started", "vehicle-routing", "empty-desc"]


def test_no_examples_says_so_and_that_new_still_works(server):
    server["blueprints"] = []

    result = runner.invoke(cli.app, ["examples"])

    assert result.exit_code == 0 and "No examples" in result.stdout and "harumi new" in result.stdout


# -- new --example ----------------------------------------------------------------------------------------------------------


def test_an_example_that_exists_is_sent(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--example", "vehicle-routing", "--no-clone"])

    assert result.exit_code == 0, result.stderr
    assert server["created"][0]["blueprint"] == "vehicle-routing"


def test_a_misspelt_example_is_caught_before_anything_is_created(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--example", "vehicle-routin", "--no-clone"])

    assert result.exit_code == 1
    assert server["created"] == [] and ("POST", "/api/projects") not in server["requests"]
    assert "vehicle-routing" in result.stderr and "harumi examples" in result.stderr


def test_an_example_nothing_like_any_is_pointed_at_the_list(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--example", "zzzzzz", "--no-clone"])

    assert result.exit_code == 1 and "Did you mean" not in result.stderr and "harumi examples" in result.stderr


def test_the_slug_is_checked_with_the_case_the_list_uses(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--example", "Vehicle-Routing", "--no-clone"])

    assert result.exit_code == 0 and server["created"][0]["blueprint"] == "vehicle-routing"


def test_when_the_list_cannot_be_read_new_does_not_block_on_it(server, tmp_path, monkeypatch):
    """The check is advisory: the server decides. A list that fails to load must not stop a valid creation."""
    monkeypatch.chdir(tmp_path)
    server["fail_list"] = 500

    result = runner.invoke(cli.app, ["new", "Demo", "--example", "vehicle-routing", "--no-clone"])

    assert result.exit_code == 0 and server["created"][0]["blueprint"] == "vehicle-routing"


def test_with_no_example_the_list_is_never_fetched(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    runner.invoke(cli.app, ["new", "Demo", "--no-clone"])

    assert ("GET", "/api/blueprints") not in server["requests"]


def test_a_project_name_with_brackets_is_shown_as_typed(server, tmp_path, monkeypatch):
    """`Creating project Route [v2]...` printed as `Creating project Route...` because rich read `[v2]` as a tag, in
    the line before the request and in the one after it (which prints the name the server sent back)."""
    monkeypatch.chdir(tmp_path)
    for example in (None, "vehicle-routing"):
        args = ["new", "Route [v2] [/x]", "--no-clone"] + (["--example", example] if example else [])

        result = runner.invoke(cli.app, args)

        assert result.exit_code == 0, result.stderr
        assert result.stdout.count("Route [v2] [/x]") >= 1


# -- the old names ---------------------------------------------------------------------------------------------------------------
# `harumi blueprints` and `new --blueprint` shipped in 0.11.0, so they keep working for a release, hidden, with a
# line on stderr (never stdout: a script pipes it) saying what replaced them and when they go.


def test_the_old_command_still_lists_the_same_things_and_warns_on_stderr(server):
    old = runner.invoke(cli.app, ["blueprints", "--json"])
    new = runner.invoke(cli.app, ["examples", "--json"])

    assert old.exit_code == 0 and json.loads(old.stdout) == json.loads(new.stdout)
    assert "deprecated" in old.stderr and "harumi examples" in old.stderr and "1.1.0" in old.stderr
    assert "deprecated" not in old.stdout and new.stderr == ""


def test_the_old_option_still_creates_the_project_and_warns_on_stderr(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--blueprint", "vehicle-routing", "--no-clone"])

    assert result.exit_code == 0, result.stderr
    assert server["created"][0]["blueprint"] == "vehicle-routing"
    assert "deprecated" in result.stderr and "--example" in result.stderr and "1.1.0" in result.stderr
    assert "deprecated" not in result.stdout


def test_the_new_option_does_not_warn(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--example", "vehicle-routing", "--no-clone"])

    assert result.exit_code == 0 and "deprecated" not in result.stderr


def test_giving_both_options_is_refused_not_guessed(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--example", "getting-started", "--blueprint", "vehicle-routing", "--no-clone"])

    assert result.exit_code == 1 and server["created"] == []
    assert "--example" in result.stderr and "--blueprint" in result.stderr


def test_the_old_names_are_hidden_from_help_and_the_surface():
    from typer.main import get_command

    from scripts.emit_cli_surface import build_surface

    root = get_command(cli.app)
    paths = {c["path"] for c in build_surface()["commands"]}
    new_options = {o for c in build_surface()["commands"] if c["path"] == "new" for p in c["params"] for o in p["opts"]}

    assert root.commands["blueprints"].hidden and not root.commands["examples"].hidden
    assert "blueprints" not in paths and "examples" in paths
    assert "--example" in new_options and "--blueprint" not in new_options


def test_the_old_option_is_not_shown_in_new_help(server):
    out = " ".join(runner.invoke(cli.app, ["new", "--help"]).stdout.split())

    assert "--example" in out and "--blueprint" not in out


def test_examples_is_in_the_get_started_help_group_and_blueprints_is_not_listed():
    out = runner.invoke(cli.app, ["--help"]).stdout

    assert "examples" in out and "blueprints" not in out


def test_examples_sits_in_get_started_next_to_new_where_you_look_it_up():
    """It was `blueprints` under "Get started" and a rename that drops it from the group list leaves it under a bare
    "Commands" heading, which every other test of the grouping still passes."""
    out = runner.invoke(cli.app, ["--help"]).stdout
    start, end = out.index("Get started"), out.index("Run and results")

    assert "examples" in out[start:end]


def test_the_message_for_an_unknown_example_says_example_not_blueprint(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--example", "vehicle-routin", "--no-clone"])

    assert "There is no example called 'vehicle-routin'" in result.stderr
    assert "blueprint" not in result.stderr.lower()
