"""--json: one JSON document on stdout, nothing else, and only fields we mean to publish.

A script cannot parse the tables. Every read command takes --json and prints a single document
(an array, or an object for one item) on stdout. Errors stay on stderr with exit 1 and an empty
stdout. The fields are listed by name, not dumped from the API model, so an internal key, a signed
URL or a secret never becomes part of a contract by accident.
"""

from __future__ import annotations

import json
import re

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
RUN1, RUN2 = "r1111111-aaaa-4bbb-8ccc-000000000001", "r2222222-aaaa-4bbb-8ccc-000000000002"
LINK = "11111111-aaaa-4bbb-8ccc-000000000011"
SCHED = "5c4e1b2a-0000-4000-8000-000000000001"
MEM = "3d7a9f10-0000-4000-8000-000000000001"
ORG = "0a1b2c3d-1111-4222-8333-444455556666"
USER = "11111111-aaaa-4bbb-8ccc-000000000001"
ANSI = re.compile(r"\x1b\[")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli.console, "_width", 80)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


class Api:
    def __init__(self):
        self.runs = [
            {"id": RUN1, "project_id": P, "status": "completed", "exit_code": 0, "git_branch": "main", "git_commit": "abc1234def",
             "created_at": "2026-10-15T12:00:00Z", "started": "2026-10-15T12:00:05Z", "ended": "2026-10-15T12:01:05Z",
             "kernel_spec": "or_python_small", "output_url": "https://s3.example/signed?X-Amz-Signature=SECRET", "stdout": "hello [/x]", "stderr": ""},
            {"id": RUN2, "project_id": P, "status": "failed", "exit_code": 1, "git_branch": "feat/[x]", "created_at": "2026-10-16T12:00:00Z",
             "stderr": "Traceback\nValueError", "error": "exit 1"},
        ]
        self.files = [{"name": "inputs/a.csv", "key": f"{P}/inputs/a.csv", "etag": "e1", "size": 2048, "last_modified": "2026-10-10T10:00:00Z"}]
        self.truncated = False
        self.projects_total = None
        self.fail: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if self.fail:
            return httpx.Response(self.fail, json={"detail": "boom"})
        if p.endswith("/users/profile"):
            return httpx.Response(200, json={"id": USER, "email": "ana@example.com", "first_name": "Ana", "last_name": "Souza", "bio": "[bio]"})
        if p.endswith("/users/organizations"):
            return httpx.Response(200, json=[{"id": ORG, "business_name": "Acme [Labs]", "role": "owner"}])
        if p.endswith(f"/organizations/{ORG}/users"):
            return httpx.Response(200, json=[{"user_id": USER, "role": "owner", "email": "ana@example.com", "first_name": "Ana", "pending": False}])
        if p.endswith("/sandbox/specs"):
            return httpx.Response(200, json=[{"name": "or_python_small", "display_name": "Small", "language": "python", "description": "d",
                                              "size": {"name": "s", "cpu": 1.0, "memory": "2Gi", "gpu": False}, "subscription_required": False}])
        if p.endswith("/blueprints"):
            return httpx.Response(200, json={"blueprints": [{"slug": "getting-started", "name": "Getting started", "description": "d"}], "total_count": 1})
        if p.endswith("/billing/usage"):
            return httpx.Response(200, json={"billing_account_id": "b", "plan_code": "pro", "tier": "pro", "balance_credits": 10, "included_credits": 100,
                                             "period_start": "2026-10-01", "period_end": "2026-11-01", "weekly_used_pct": 12.5, "monthly_used_pct": 40.0,
                                             "weekly_resets_at": "2026-10-27T00:00:00Z", "monthly_resets_at": "2026-11-01T00:00:00Z", "overage_enabled": False})
        if p.endswith("/billing/entitlement/me"):
            return httpx.Response(200, json={"paywall_enabled": False, "personal": {"active": True, "tier": "pro", "name": "Personal"}, "organizations": []})
        if p == "/api/projects":
            rows = [{"id": P, "name": "Demo [x]", "customer_id": None, "kernel_spec": "or_python_small", "role_name": "owner",
                     "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-20T00:00:00Z"}]
            return httpx.Response(200, json={"projects": rows, "total_count": self.projects_total or len(rows)})
        if p == f"/api/projects/{P}":
            return httpx.Response(200, json={"id": P, "name": "Demo [x]", "kernel_spec": "or_python_small", "role_name": "owner", "created_at": "2026-10-01T00:00:00Z"})
        if p.endswith(f"/projects/{P}/runs"):
            return httpx.Response(200, json={"runs": self.runs})
        if "/runs/" in p:
            row = next((r for r in self.runs if r["id"] == p.rsplit("/", 1)[-1]), None)
            return httpx.Response(200, json=row) if row else httpx.Response(404, json={"detail": "no such run"})
        if p.endswith("/files"):
            return httpx.Response(200, json={"files": self.files, "is_truncated": self.truncated})
        if p.endswith("/share-links"):
            return httpx.Response(200, json={"links": [{"id": LINK, "project_id": P, "token": "tok_SECRET", "label": "Client [x]", "enabled": True,
                                                        "password_set": False, "app_enabled": True, "chat_enabled": False, "run_history_enabled": False, "run_control_enabled": False}]})
        if p.endswith(f"/datasources/{P}"):
            return httpx.Response(200, json={"datasources": [{"id": "d1", "project_id": P, "name": "wh", "type": "postgresql", "host": "h", "port": 5432,
                                                              "database": "db", "username": "u", "use_proxy": False}], "total_count": 1})
        if p.endswith(f"/datasources/{P}/wh"):
            return httpx.Response(200, json={"id": "d1", "project_id": P, "name": "wh", "type": "postgresql", "host": "h", "port": 5432, "database": "db", "username": "u", "use_proxy": False})
        if p.endswith(f"/projects/{P}/schedules"):
            return httpx.Response(200, json={"schedules": [{"id": SCHED, "project_id": P, "cron": "0 9 * * *", "git_branch": "main", "command": "pytest [nightly]", "kernel_spec": "or_python_small"}]})
        if f"/schedules/{SCHED}" in p:
            return httpx.Response(200, json={"id": SCHED, "project_id": P, "cron": "0 9 * * *", "git_branch": "main", "command": "pytest [nightly]", "kernel_spec": "or_python_small"})
        if p.endswith("/secrets"):
            return httpx.Response(200, json=[{"name": "API_KEY", "value": "CIPHERTEXT-DO-NOT-PRINT"}])
        if p.endswith("/memories"):
            return httpx.Response(200, json={"memories": [{"id": MEM, "scope": "project", "content": "Use [draft] tag", "retrieval_mode": "always", "source": "chat", "updated_at": "2026-10-19T00:00:00Z"}]})
        return httpx.Response(404, json={"detail": "not routed " + p})


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler)))
    monkeypatch.setattr(cli, "_resolve_project", lambda p, *a, **k: p or P)
    return a


# Every read command, with the arguments that make it succeed.
COMMANDS = [
    ["projects", "list"],
    ["projects", "get", P],
    ["runs", "list"],
    ["runs", "get", RUN1],
    ["files", "ls"],
    ["share", "list"],
    ["share", "get", LINK],
    ["datasources", "list"],
    ["datasources", "get", "wh"],
    ["schedules", "list"],
    ["schedules", "get", SCHED],
    ["secrets", "list"],
    ["memories", "list"],
    ["org", "list"],
    ["org", "members", ORG],
    ["specs"],
    ["examples"],
    ["usage"],
    ["whoami"],
]


def ids(args):
    return " ".join(args)


@pytest.mark.parametrize("args", COMMANDS, ids=ids)
def test_json_is_one_document_on_stdout_and_nothing_else(api, args):
    result = runner.invoke(cli.app, [*args, "--json"])

    assert result.exit_code == 0, result.stderr or result.output
    json.loads(result.stdout)  # the whole of stdout, not a document with a hint after it
    assert result.stderr == "" and not ANSI.search(result.stdout)


@pytest.mark.parametrize("args", COMMANDS, ids=ids)
def test_json_fails_like_any_other_call_with_stdout_empty(api, args):
    api.fail = 500

    result = runner.invoke(cli.app, [*args, "--json"])

    assert result.exit_code == 1 and result.stdout == "" and "boom" in result.stderr


@pytest.mark.parametrize("args", COMMANDS, ids=ids)
def test_the_option_is_documented_in_help(args):
    assert "--json" in runner.invoke(cli.app, [*args, "--help"]).stdout


LISTS = [
    (["projects", "list"], "projects"), (["runs", "list"], "runs"), (["share", "list"], "links"), (["datasources", "list"], "datasources"),
    (["schedules", "list"], "schedules"), (["memories", "list"], "memories"), (["org", "list"], "orgs"), (["specs"], "specs"), (["examples"], "examples"),
    (["secrets", "list"], "secrets"),
]


def test_an_empty_list_is_an_empty_array_not_a_sentence(api, monkeypatch):
    empty = httpx.Response
    monkeypatch.setattr(
        api, "handler",
        lambda r: empty(200, json=[] if any(r.url.path.endswith(s) for s in ("/organizations", "/specs", "/secrets"))
                        else {"projects": [], "runs": [], "links": [], "datasources": [], "schedules": [], "memories": [], "blueprints": [], "total_count": 0}),
    )
    for args, _ in LISTS:
        result = runner.invoke(cli.app, [*args, "--json"])
        assert result.exit_code == 0 and json.loads(result.stdout) == [], args


# -- fields -------------------------------------------------------------------------------------------------


def j(api, *args):
    result = runner.invoke(cli.app, [*args, "--json"])
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


def test_ids_are_whole_and_text_is_exactly_as_stored(api):
    run = j(api, "runs", "list")[1]

    assert run["id"] == RUN2 and run["git_branch"] == "feat/[x]"
    assert j(api, "projects", "list")[0]["name"] == "Demo [x]"
    assert j(api, "org", "list")[0]["name"] == "Acme [Labs]"


def test_secrets_list_never_includes_a_value(api):
    out = runner.invoke(cli.app, ["secrets", "list", "--json"]).stdout

    assert json.loads(out) == [{"name": "API_KEY"}] and "CIPHERTEXT" not in out


def test_a_share_list_leaves_out_the_token_but_a_single_link_has_its_url(api):
    listed = runner.invoke(cli.app, ["share", "list", "--json"]).stdout
    one = j(api, "share", "get", LINK)

    assert "tok_SECRET" not in listed and json.loads(listed)[0]["id"] == LINK
    assert "tok_SECRET" in one["url"] and one["token"] == "tok_SECRET"


def test_a_run_does_not_publish_its_signed_output_url(api):
    out = runner.invoke(cli.app, ["runs", "get", RUN1, "--json"]).stdout
    run = json.loads(out)

    assert "SECRET" not in out and "s3.example" not in out
    assert run["has_output"] is True and run["stdout"] == "hello [/x]" and run["exit_code"] == 0


def test_runs_list_leaves_out_the_logs_but_get_has_them(api):
    assert "stdout" not in j(api, "runs", "list")[0]
    assert j(api, "runs", "get", RUN2)["stderr"].startswith("Traceback")


def test_a_file_listing_has_no_storage_key_and_says_when_it_is_cut(api):
    api.truncated = True
    listing = j(api, "files", "ls")

    assert listing["is_truncated"] is True and listing["files"] == [{"name": "inputs/a.csv", "size": 2048, "last_modified": "2026-10-10T10:00:00Z"}]


def test_runs_list_gives_every_run_unless_asked_for_fewer(api):
    api.runs = [{"id": f"r{i:07d}-aaaa-4bbb-8ccc-000000000000", "project_id": P, "status": "completed", "created_at": "2026-10-15T12:00:00Z"} for i in range(40)]

    assert len(j(api, "runs", "list")) == 40  # the table shows 25 to be readable; a script wants all
    result = runner.invoke(cli.app, ["runs", "list", "--json", "--limit", "3"])
    assert len(json.loads(result.stdout)) == 3


def test_a_project_list_that_is_cut_says_so_on_stderr_not_in_the_document(api):
    api.projects_total = 5

    result = runner.invoke(cli.app, ["projects", "list", "--json"])

    assert len(json.loads(result.stdout)) == 1 and "1 of 5" in result.stderr


def test_whoami_and_usage_are_objects(api):
    me = j(api, "whoami")
    use = j(api, "usage")

    assert me["email"] == "ana@example.com" and me["organization"] is None and me["id"] == USER
    assert me["environment"] is None  # production is not named, here or in the table
    assert use["weekly_used_pct"] == 12.5 and use["tier"] == "pro"


def test_whoami_names_the_organization_you_work_in(api):
    config.Config.load().save_org_id(ORG)

    assert j(api, "whoami")["organization"] == {"id": ORG, "name": "Acme [Labs]"}


def test_a_terminal_escape_in_a_value_cannot_reach_the_terminal(api):
    api.runs[1]["git_branch"] = "evil\x1b[2J\u202e"

    out = runner.invoke(cli.app, ["runs", "list", "--json"]).stdout

    assert "\x1b" not in out and "\u202e" not in out and json.loads(out)[1]["git_branch"] == "evil\x1b[2J\u202e"


# -- no menu in a pipe --------------------------------------------------------------------------------------


@pytest.mark.parametrize("args", [["projects", "get"], ["share", "get"], ["schedules", "get"]])
def test_json_without_an_id_never_opens_a_menu(api, monkeypatch, args):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)
    monkeypatch.setattr(interactive, "select", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a menu would corrupt the JSON")))

    result = runner.invoke(cli.app, [*args, "--json"])

    assert result.exit_code == 1 and result.stdout == "" and "id" in result.stderr.lower()


def test_whoami_names_an_environment_only_when_it_is_not_production(api, monkeypatch):
    monkeypatch.setenv("HARUMI_ENV", "staging")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")  # staging keeps its own login

    assert j(api, "whoami")["environment"] == "staging"


@pytest.mark.parametrize("args", [["org", "members"]])
def test_org_members_with_json_and_no_organization_never_opens_a_menu(api, monkeypatch, args):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)
    monkeypatch.setattr(interactive, "select", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a menu would corrupt the JSON")))

    result = runner.invoke(cli.app, [*args, "--json"])

    assert result.exit_code == 1 and result.stdout == "" and "organization" in result.stderr.lower()


def test_usage_without_a_plan_says_so_as_data(api, monkeypatch):
    orig = api.handler

    def no_plan(request):
        if request.url.path.endswith("/billing/entitlement/me"):
            return httpx.Response(200, json={"paywall_enabled": True, "personal": {"active": False, "tier": "none", "reason": "no_subscription"}, "organizations": []})
        return orig(request)

    monkeypatch.setattr(api, "handler", no_plan)

    assert j(api, "usage") == {"workspace": None, "active": False, "reason": "no_subscription"}


@pytest.mark.parametrize("org_id, expected", [(None, "no_subscription"), (ORG, "org_not_subscribed")])
def test_usage_without_a_plan_gives_the_cause_the_table_would_even_when_the_server_sends_none(api, monkeypatch, org_id, expected):
    orig = api.handler
    ent = {"paywall_enabled": True, "personal": {"active": False, "tier": "none"}, "organizations": [{"customer_id": ORG, "name": "Acme", "active": False, "tier": "none"}]}

    def no_plan(request):
        return httpx.Response(200, json=ent) if request.url.path.endswith("/billing/entitlement/me") else orig(request)

    monkeypatch.setattr(api, "handler", no_plan)
    if org_id:
        config.Config.load().save_org_id(org_id)

    assert j(api, "usage")["reason"] == expected
