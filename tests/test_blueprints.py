"""`harumi blueprints` and `new --blueprint` (US-6): a wrong slug is caught before anything is created, and the
list says enough to choose from.

harumi-api validates the slug before it writes a project (a 400, `Unknown blueprint`), so nothing is left behind;
what the CLI did with that 400 was print it as a bare `HTTP 400: ...`.
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


def test_the_list_says_how_to_use_a_blueprint(server):
    result = runner.invoke(cli.app, ["blueprints"])

    assert result.exit_code == 0, result.stderr
    assert "getting-started" in result.stdout and "harumi new" in result.stdout and "--blueprint" in result.stdout


def test_the_list_shows_names_as_stored(server):
    result = runner.invoke(cli.app, ["blueprints"])

    assert "Vehicle routing [VRP]" in result.stdout


def test_a_slug_is_never_cut_at_a_narrow_width(server, monkeypatch):
    monkeypatch.setattr(cli.console, "_width", 50)

    result = runner.invoke(cli.app, ["blueprints"])

    assert "getting-started" in result.stdout and "vehicle-routing" in result.stdout


def test_json_is_the_same_list(server):
    document = json.loads(runner.invoke(cli.app, ["blueprints", "--json"]).stdout)

    assert [b["slug"] for b in document] == ["getting-started", "vehicle-routing", "empty-desc"]


def test_no_blueprints_says_so_and_that_new_still_works(server):
    server["blueprints"] = []

    result = runner.invoke(cli.app, ["blueprints"])

    assert result.exit_code == 0 and "No blueprints" in result.stdout and "harumi new" in result.stdout


# -- new --blueprint ----------------------------------------------------------------------------------------------------------


def test_a_blueprint_that_exists_is_sent(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--blueprint", "vehicle-routing", "--no-clone"])

    assert result.exit_code == 0, result.stderr
    assert server["created"][0]["blueprint"] == "vehicle-routing"


def test_a_misspelt_blueprint_is_caught_before_anything_is_created(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--blueprint", "vehicle-routin", "--no-clone"])

    assert result.exit_code == 1
    assert server["created"] == [] and ("POST", "/api/projects") not in server["requests"]
    assert "vehicle-routing" in result.stderr and "harumi blueprints" in result.stderr


def test_a_blueprint_nothing_like_any_is_pointed_at_the_list(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--blueprint", "zzzzzz", "--no-clone"])

    assert result.exit_code == 1 and "Did you mean" not in result.stderr and "harumi blueprints" in result.stderr


def test_the_slug_is_checked_with_the_case_the_list_uses(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["new", "Demo", "--blueprint", "Vehicle-Routing", "--no-clone"])

    assert result.exit_code == 0 and server["created"][0]["blueprint"] == "vehicle-routing"


def test_when_the_list_cannot_be_read_new_does_not_block_on_it(server, tmp_path, monkeypatch):
    """The check is advisory: the server decides. A list that fails to load must not stop a valid creation."""
    monkeypatch.chdir(tmp_path)
    server["fail_list"] = 500

    result = runner.invoke(cli.app, ["new", "Demo", "--blueprint", "vehicle-routing", "--no-clone"])

    assert result.exit_code == 0 and server["created"][0]["blueprint"] == "vehicle-routing"


def test_with_no_blueprint_the_list_is_never_fetched(server, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    runner.invoke(cli.app, ["new", "Demo", "--no-clone"])

    assert ("GET", "/api/blueprints") not in server["requests"]


def test_a_project_name_with_brackets_is_shown_as_typed(server, tmp_path, monkeypatch):
    """`Creating project Route [v2]...` printed as `Creating project Route...` because rich read `[v2]` as a tag, in
    the line before the request and in the one after it (which prints the name the server sent back)."""
    monkeypatch.chdir(tmp_path)
    for blueprint in (None, "vehicle-routing"):
        args = ["new", "Route [v2] [/x]", "--no-clone"] + (["--blueprint", blueprint] if blueprint else [])

        result = runner.invoke(cli.app, args)

        assert result.exit_code == 0, result.stderr
        assert result.stdout.count("Route [v2] [/x]") >= 1
