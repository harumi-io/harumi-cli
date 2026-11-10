"""`harumi keys`: list and revoke the user's API keys, through harumi-api.

Create and rotate are deliberately absent (the API wants a fresh authenticator code for them), so
these tests pin what the CLI sends and prints for the two it has, against a fake harumi-api.
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


def _key(key_id: str, name: str, **extra) -> dict:
    return {
        "id": key_id, "name": name, "api_key": "pk_abcd****", "scopes": ["outputs:read"], "project_ids": ["p1", "p2"],
        "created_at": "2026-10-01T10:00:00Z", "expires_at": "2099-01-01T00:00:00Z", "last_used_at": None, "revoked_at": None, **extra,
    }


class Api:
    def __init__(self):
        self.keys = [
            _key("aaaa1111-0000-0000-0000-000000000001", "moldagem", scopes=["outputs:read", "runs:write"], last_used_at="2026-11-09T10:00:00Z"),
            _key("aaaa2222-0000-0000-0000-000000000002", "old", revoked_at="2026-10-05T10:00:00Z"),
            _key("bbbb3333-0000-0000-0000-000000000003", "stale", expires_at="2026-01-01T00:00:00Z"),
        ]
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/keys":
            return httpx.Response(200, json=self.keys)
        if request.method == "DELETE" and request.url.path.startswith("/api/keys/"):
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "not routed " + request.url.path})


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch, isolate_harumi_home):
    isolate_harumi_home(tmp_path / "home")
    monkeypatch.setattr(cli.console, "_width", 140)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler)))
    return a


def run(*args: str):
    return runner.invoke(cli.app, ["keys", *args])


def test_list_shows_each_key_with_its_state(api):
    result = run("list")
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "moldagem" in out and "outputs:read, runs:write" in out
    assert "revoked" in out and "expired" in out and "active" in out
    assert "pk_abcd" not in out


def test_list_json_has_named_fields_and_no_secret(api):
    data = json.loads(run("list", "--json").stdout)
    assert [k["state"] for k in data] == ["active", "revoked", "expired"]
    assert all("api_key" not in k for k in data)
    assert data[0]["scopes"] == ["outputs:read", "runs:write"]


def test_list_with_no_keys_points_to_the_web_app(api):
    api.keys = []
    assert "web app" in run("list").stdout


def test_rm_revokes_by_name_after_confirming(api):
    result = run("rm", "moldagem", "--yes")
    assert result.exit_code == 0, result.output
    assert ("DELETE", "/api/keys/aaaa1111-0000-0000-0000-000000000001") in api.calls
    assert "Revoked" in result.stdout


def test_rm_matches_the_start_of_an_id(api):
    run("rm", "bbbb", "--yes")
    assert ("DELETE", "/api/keys/bbbb3333-0000-0000-0000-000000000003") in api.calls


def test_rm_declined_changes_nothing(api):
    result = runner.invoke(cli.app, ["keys", "rm", "moldagem"], input="n\n")
    assert not [c for c in api.calls if c[0] == "DELETE"]
    assert result.exit_code != 0 or "Revoked" not in result.stdout


def test_rm_an_ambiguous_prefix_is_refused(api):
    result = run("rm", "aaaa", "--yes")
    assert result.exit_code == 1
    assert not [c for c in api.calls if c[0] == "DELETE"]


def test_rm_an_unknown_key_says_where_to_look(api):
    result = run("rm", "nope", "--yes")
    assert result.exit_code == 1
    assert "harumi keys list" in result.output


def test_rm_an_already_revoked_key_is_a_no_op(api):
    result = run("rm", "old", "--yes")
    assert result.exit_code == 0 and "already revoked" in result.stdout
    assert not [c for c in api.calls if c[0] == "DELETE"]
