"""Destructive commands ask first, one way, and say so when they cannot.

One rule for every delete: `--yes` goes ahead; at a terminal you are asked (and declining exits 1);
with no terminal and no `--yes` the command refuses at once and names the flag, as the menus do. It
never prints a prompt into a log and waits on stdin. Protection is in proportion: a delete that
cannot be undone and takes other things with it (an organization) asks you to type its name.
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
LINK = "11111111-aaaa-4bbb-8ccc-000000000011"
SCHED = "5c4e1b2a-0000-4000-8000-000000000001"
MEM = "3d7a9f10-0000-4000-8000-000000000001"
ORG = "0a1b2c3d-1111-4222-8333-444455556666"
USER = "11111111-aaaa-4bbb-8ccc-000000000001"
BO = "22222222-aaaa-4bbb-8ccc-000000000002"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG", "CI"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli.console, "_width", 100)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


class Api:
    """Serves the lookups a delete does first, and records every request that changes something."""

    def __init__(self):
        self.changes: list[str] = []
        self.secrets = [{"name": "API_KEY", "value": "CIPHERTEXT"}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        if m != "GET":
            self.changes.append(f"{m} {p}" + (f"?{request.url.query.decode()}" if request.url.query else ""))
        if p.endswith("/users/organizations") and m == "GET":
            return httpx.Response(200, json=[{"id": ORG, "business_name": "Acme [Labs]", "role": "owner"}])
        if p.endswith(f"/organizations/{ORG}/users") and m == "GET":
            return httpx.Response(200, json=[
                {"user_id": USER, "role": "owner", "email": "ana@example.com"},
                {"user_id": BO, "role": "admin", "email": "bo@example.com"},
            ])
        if p == f"/api/projects/{P}":
            return httpx.Response(200, json={"id": P, "name": "Demo", "created_at": "2026-10-01T00:00:00Z"})
        if p.endswith("/files") and m == "DELETE":
            return httpx.Response(200, json={"deleted": 1})  # harumi-api answers 1 for any path
        if p.endswith("/files") and m == "GET":
            return httpx.Response(200, json={"files": [{"name": "inputs/a.csv", "key": "k", "size": 1}], "is_truncated": False})
        if p.endswith("/share-links") and m == "GET":
            return httpx.Response(200, json={"links": [{"id": LINK, "project_id": P, "token": "t", "label": "Client", "enabled": True, "password_set": True,
                                                        "app_enabled": False, "chat_enabled": False, "run_history_enabled": False, "run_control_enabled": False}]})
        if p.endswith("/share-links/" + LINK) and m == "PATCH":
            return httpx.Response(200, json={"id": LINK, "project_id": P, "token": "t", "label": "Client", "enabled": True, "password_set": True,
                                             "app_enabled": False, "chat_enabled": False, "run_history_enabled": False, "run_control_enabled": False})
        if p.endswith("/password") and m == "DELETE":  # the real API answers with the link
            return httpx.Response(200, json={"id": LINK, "project_id": P, "token": "t", "enabled": True, "password_set": False})
        if p.endswith("/rotate"):
            return httpx.Response(200, json={"id": LINK, "project_id": P, "token": "t2", "enabled": True, "password_set": False})
        if p.endswith(f"/schedules/{SCHED}") and m == "GET":
            return httpx.Response(200, json={"id": SCHED, "project_id": P, "cron": "0 9 * * *", "git_branch": "release", "command": "python main.py"})
        if f"/schedules/{SCHED}" in p:
            return httpx.Response(200, json={"id": SCHED, "project_id": P, "cron": "0 9 * * *", "git_branch": "release"})
        if p.endswith("/memories") and m == "GET":
            return httpx.Response(200, json={"memories": [{"id": MEM, "scope": "project", "content": "Always report cost in USD.", "retrieval_mode": "always"}]})
        if f"/memories/{MEM}" in p:
            return httpx.Response(200, json={"id": MEM, "scope": "project", "content": "Always report cost in USD."})
        if p.endswith("/secrets") and m == "GET":
            return httpx.Response(200, json=self.secrets)
        if p.endswith("/secrets") and m == "POST":
            return httpx.Response(200, json={"name": json.loads(request.content)["name"], "value": "x"})
        if "/datasources/" in p:
            return httpx.Response(200, json={"id": "d", "project_id": P, "name": "wh", "type": "postgresql"})
        if m == "DELETE":
            return httpx.Response(200 if f"/projects/{P}" == p.split("/api", 1)[-1] else 204, json={"id": P, "name": "Demo"} if p.endswith(f"/projects/{P}") else None)
        return httpx.Response(404, json={"detail": "not routed " + m + " " + p})


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler)))
    monkeypatch.setattr(cli, "_resolve_project", lambda p, *a, **k: p or P)
    return a


def terminal(monkeypatch, on=True):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: on)


def plain(text: str) -> str:
    return " ".join(text.split())


# Each destructive command: its arguments, and the one request that does the damage.
DELETES = [
    pytest.param(["projects", "rm", P], f"DELETE /api/projects/{P}", id="projects rm"),
    pytest.param(["files", "rm", "inputs/a.csv"], "DELETE /api/projects/proj-1/files?path=inputs%2Fa.csv", id="files rm"),
    pytest.param(["share", "rm", LINK], f"DELETE /api/projects/proj-1/share-links/{LINK}", id="share rm"),
    pytest.param(["share", "rotate", LINK], f"POST /api/projects/proj-1/share-links/{LINK}/rotate", id="share rotate"),
    pytest.param(["datasources", "rm", "wh"], "DELETE /api/datasources/proj-1/wh", id="datasources rm"),
    pytest.param(["schedules", "rm", SCHED], f"DELETE /api/projects/proj-1/schedules/{SCHED}", id="schedules rm"),
    pytest.param(["secrets", "rm", "API_KEY"], "DELETE /api/projects/proj-1/secrets/API_KEY", id="secrets rm"),
    pytest.param(["memories", "rm", MEM], f"DELETE /api/projects/proj-1/memories/{MEM}", id="memories rm"),
    pytest.param(["org", "rm", "Acme [Labs]"], f"DELETE /api/users/organizations/{ORG}", id="org rm"),
    pytest.param(["org", "rm-member", "Acme [Labs]", "bo@example.com"], f"DELETE /api/users/organizations/{ORG}/users/{BO}", id="org rm-member"),
]


def answer_for(args):
    """What a person types at the prompt to say yes: the name, for the commands that ask for it."""
    return {"projects": "Demo\n", "org": "Acme [Labs]\n" if args[1] == "rm" else "y\n"}.get(args[0], "y\n")


@pytest.mark.parametrize("args, change", DELETES)
def test_with_no_terminal_and_no_yes_it_refuses_at_once_and_names_the_flag(api, args, change):
    result = runner.invoke(cli.app, args, input="y\n")  # even a "y" waiting on stdin is not consent

    out = plain(result.stderr + result.stdout)
    assert result.exit_code == 1 and "--yes" in out and "no interactive terminal" in out
    assert api.changes == []
    assert "[y/N]" not in out  # it never printed a prompt that nobody could answer


@pytest.mark.parametrize("args, change", DELETES)
def test_yes_goes_ahead(api, args, change):
    result = runner.invoke(cli.app, [*args, "--yes"])

    assert result.exit_code == 0, result.stderr
    assert api.changes == [change]


@pytest.mark.parametrize("args, change", DELETES)
def test_at_a_terminal_a_yes_goes_ahead(api, monkeypatch, args, change):
    terminal(monkeypatch)

    result = runner.invoke(cli.app, args, input=answer_for(args))

    assert result.exit_code == 0, result.stderr
    assert api.changes == [change]


@pytest.mark.parametrize("args, change", DELETES)
def test_at_a_terminal_declining_stops_and_exits_one_so_a_script_does_not_carry_on(api, monkeypatch, args, change):
    terminal(monkeypatch)

    result = runner.invoke(cli.app, args, input="n\n")

    assert result.exit_code == 1 and "Aborted" in plain(result.stderr + result.stdout)
    assert api.changes == []


@pytest.mark.parametrize("args, change", DELETES)
def test_just_pressing_enter_is_a_no(api, monkeypatch, args, change):
    terminal(monkeypatch)

    result = runner.invoke(cli.app, args, input="\n")

    assert result.exit_code == 1 and api.changes == []


# -- proportion: the permanent, far-reaching one asks for its name ----------------------------------------------


def test_deleting_an_organization_asks_you_to_type_its_name_not_just_y(api, monkeypatch):
    terminal(monkeypatch)

    result = runner.invoke(cli.app, ["org", "rm", "Acme [Labs]"], input="y\n")

    out = plain(result.stderr + result.stdout)
    assert result.exit_code == 1 and "Type the organization name" in out and "Acme [Labs]" in out
    assert api.changes == []


@pytest.mark.parametrize("typed", ["acme [labs]", "Acme", "Acme [Labs] ", ""])
def test_a_name_that_does_not_match_exactly_deletes_nothing(api, monkeypatch, typed):
    terminal(monkeypatch)

    result = runner.invoke(cli.app, ["org", "rm", "Acme [Labs]"], input=typed + "\n")

    assert result.exit_code == 1 and "did not match" in plain(result.stderr + result.stdout) and api.changes == []


def test_the_name_prompt_shows_what_goes_with_the_organization(api, monkeypatch):
    terminal(monkeypatch)

    result = runner.invoke(cli.app, ["org", "rm", "Acme [Labs]"], input="no\n")  # one run; both streams from it

    out = plain(result.stdout + result.stderr)
    assert "Every project in it is deleted" in out and "cannot be undone" in out


def test_a_name_with_a_rich_tag_in_it_is_shown_as_typed_so_it_can_be_typed_back(api, monkeypatch):
    """`[red]` is a Rich style, not text: unescaped, "Q3 [red]" is shown as "Q3" and the prompt asks for a
    name nobody can type. (`[Labs]` is not a known style, so it survives either way; this one does not.)"""
    terminal(monkeypatch)
    orig = api.handler
    monkeypatch.setattr(
        api, "handler",
        lambda r: httpx.Response(200, json=[{"id": ORG, "business_name": "Q3 [red]", "role": "owner"}]) if r.url.path.endswith("/users/organizations") else orig(r),
    )

    result = runner.invoke(cli.app, ["org", "rm", "Q3 [red]"], input="no\n")

    out = plain(result.stdout + result.stderr)
    assert "Q3 [red]" in out and "Delete organization 'Q3 [red]'" in out


def test_a_project_name_that_does_not_match_exits_one(api, monkeypatch):
    terminal(monkeypatch)

    result = runner.invoke(cli.app, ["projects", "rm", P], input="demo\n")

    assert result.exit_code == 1 and api.changes == []


def test_a_name_that_looks_like_markup_cannot_break_the_refusal(api, monkeypatch):
    """The refusal prints the question through Rich, and a name is someone else's text: `[/x]` is a crash."""
    orig = api.handler
    monkeypatch.setattr(
        api, "handler",
        lambda r: httpx.Response(200, json=[{"id": ORG, "business_name": "Lab [/x]", "role": "owner"}]) if r.url.path.endswith("/users/organizations") else orig(r),
    )

    result = runner.invoke(cli.app, ["org", "rm", "Lab [/x]"])  # no terminal: it refuses, printing the question

    assert result.exit_code == 1 and isinstance(result.exception, SystemExit) and "Lab [/x]" in plain(result.stderr)


def test_a_file_name_that_looks_like_markup_cannot_break_the_plain_refusal_either(api, monkeypatch):
    orig = api.handler
    monkeypatch.setattr(
        api, "handler",
        lambda r: httpx.Response(200, json={"files": [{"name": "a[/x].csv", "key": "k", "size": 1}], "is_truncated": False})
        if r.url.path.endswith("/files") and r.method == "GET" else orig(r),
    )

    result = runner.invoke(cli.app, ["files", "rm", "a[/x].csv"])

    assert result.exit_code == 1 and isinstance(result.exception, SystemExit) and "a[/x].csv" in plain(result.stderr)


def test_a_rich_tag_in_a_file_name_is_shown_as_typed_in_the_plain_refusal(api, monkeypatch):
    """The crash case above needs `[/x]`; a name like `[bold]x[/bold].csv` does not crash, it is silently rewritten."""
    orig = api.handler
    monkeypatch.setattr(
        api, "handler",
        lambda r: httpx.Response(200, json={"files": [{"name": "[red]q3[/red].csv", "key": "k", "size": 1}], "is_truncated": False})
        if r.url.path.endswith("/files") and r.method == "GET" else orig(r),
    )

    out = plain(runner.invoke(cli.app, ["files", "rm", "[red]q3[/red].csv"]).stderr)

    assert "[red]q3[/red].csv" in out


# -- the prompt says what, not which UUID ------------------------------------------------------------------------


def test_the_schedule_prompt_describes_the_schedule(api, monkeypatch):
    terminal(monkeypatch)

    out = plain(runner.invoke(cli.app, ["schedules", "rm", SCHED], input="n\n").stdout)

    assert "0 9 * * *" in out and "release" in out and "python main.py" in out


def test_the_memory_prompt_quotes_the_memory(api, monkeypatch):
    terminal(monkeypatch)

    out = plain(runner.invoke(cli.app, ["memories", "rm", MEM], input="n\n").stdout)

    assert "Always report cost in USD." in out


def test_with_yes_the_describing_lookup_is_skipped(api):
    runner.invoke(cli.app, ["schedules", "rm", SCHED, "--yes"])
    runner.invoke(cli.app, ["memories", "rm", MEM, "--yes"])

    assert api.changes == [f"DELETE /api/projects/proj-1/schedules/{SCHED}", f"DELETE /api/projects/proj-1/memories/{MEM}"]


# -- removing a share password exposes the dashboard -------------------------------------------------------------


def test_removing_a_share_password_asks_first(api, monkeypatch):
    terminal(monkeypatch)

    result = runner.invoke(cli.app, ["share", "update", LINK, "--no-password"], input="n\n")

    out = plain(result.stderr + result.stdout)
    assert result.exit_code == 1 and "anyone with the link" in out.lower() and api.changes == []


def test_removing_a_share_password_with_yes_or_after_agreeing_goes_ahead(api, monkeypatch):
    assert runner.invoke(cli.app, ["share", "update", LINK, "--no-password", "--yes"]).exit_code == 0
    terminal(monkeypatch)
    assert runner.invoke(cli.app, ["share", "update", LINK, "--no-password"], input="y\n").exit_code == 0
    assert [c for c in api.changes if "password" in c] == [f"DELETE /api/projects/proj-1/share-links/{LINK}/password"] * 2


def test_removing_a_share_password_with_no_terminal_refuses(api):
    result = runner.invoke(cli.app, ["share", "update", LINK, "--no-password"])

    assert result.exit_code == 1 and "--yes" in plain(result.stderr + result.stdout) and api.changes == []


def test_other_share_changes_never_ask(api):
    assert runner.invoke(cli.app, ["share", "update", LINK, "--label", "New"]).exit_code == 0


# -- secrets set says whether it replaced something --------------------------------------------------------------


def test_setting_a_secret_that_exists_says_it_replaced_it(api):
    out = plain(runner.invoke(cli.app, ["secrets", "set", "API_KEY"], input="v\n").stdout)

    assert "Replaced" in out and "API_KEY" in out


def test_setting_a_new_secret_says_it_was_added(api):
    out = plain(runner.invoke(cli.app, ["secrets", "set", "NEW_ONE"], input="v\n").stdout)

    assert "Set" in out and "Replaced" not in out


def test_if_the_existing_names_cannot_be_read_it_still_sets_the_secret(api, monkeypatch):
    orig = api.handler
    monkeypatch.setattr(api, "handler", lambda r: httpx.Response(500, json={"detail": "x"}) if r.url.path.endswith("/secrets") and r.method == "GET" else orig(r))

    result = runner.invoke(cli.app, ["secrets", "set", "API_KEY"], input="v\n")

    assert result.exit_code == 0 and "POST /api/projects/proj-1/secrets" in api.changes[0]


# -- hidden prompts, read from a pipe -----------------------------------------------------------------------------


def test_a_piped_secret_is_read_without_a_library_warning(api):
    result = runner.invoke(cli.app, ["secrets", "set", "NEW_ONE"], input="S3CR3T\n")

    out = result.stderr + result.stdout
    assert result.exit_code == 0 and "echo" not in out.lower() and "getpass" not in out.lower() and "GetPassWarning" not in out
    assert "S3CR3T" not in out  # and never printed back


def test_a_piped_secret_with_nothing_on_stdin_fails_clearly(api):
    result = runner.invoke(cli.app, ["secrets", "set", "NEW_ONE"], input="")

    out = plain(result.stderr + result.stdout)
    assert result.exit_code == 1 and "No value" in out and api.changes == []


def test_at_a_terminal_the_value_is_asked_for_hidden(api, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: True)
    monkeypatch.setattr(cli.typer, "prompt", lambda text, **kw: seen.update(text=text, **kw) or "typed")

    assert runner.invoke(cli.app, ["secrets", "set", "NEW_ONE"]).exit_code == 0
    assert seen["hide_input"] is True and api.changes[0].startswith("POST")


# -- the reference must say it ----------------------------------------------------------------------------------


def _commands_with_yes():
    from typer.main import get_command

    found = []

    def walk(command, path):
        for name, sub in getattr(command, "commands", {}).items():
            if getattr(sub, "hidden", False):
                continue  # deprecated aliases are not documented
            if hasattr(sub, "commands"):  # a group; no import of click needed (typer vendors its own on new versions)
                walk(sub, path + [name])
            elif any("--yes" in getattr(p, "opts", []) for p in sub.params):
                found.append(" ".join(path + [name]))

    walk(get_command(cli.app), [])
    return sorted(found)


@pytest.mark.parametrize("command", _commands_with_yes())
def test_every_command_that_takes_yes_says_so_in_the_reference(command):
    """An agent reads the reference to learn how to call a command without a terminal. A `--yes` missing
    from a usage line is a command it will fail on (and be refused, correctly, with no hint of why)."""
    import pathlib
    import re

    ref = (pathlib.Path(cli.__file__).resolve().parents[2] / ".agents/skills/harumi-cli/references/commands.md").read_text(encoding="utf-8")
    # The usage block: the line that starts with the command and the indented lines that continue it.
    blocks = re.findall(r"^harumi " + re.escape(command) + r"\b.*(?:\n {5,}.*)*", ref, re.M)

    assert blocks, f"`harumi {command}` has no usage line in the reference"
    assert any("--yes" in b for b in blocks), f"`harumi {command}` takes --yes but its usage line does not show it"


def test_the_list_of_commands_that_confirm_is_the_one_the_reference_documents():
    documented = {"projects rm", "files rm", "share rm", "share rotate", "share update", "datasources rm", "tags rm", "schedules rm", "secrets rm", "memories rm", "org rm", "org rm-member"}

    assert documented <= set(_commands_with_yes())
