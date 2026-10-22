"""Readable output: nothing the user copies is cut, and nothing they stored is rewritten.

Tables are the most-read thing the CLI prints. Two things went wrong in them. Text that merely looks
like Rich markup was rewritten (`feat/[red]x[/red]` shown as `feat/x`) or crashed the command. And
the id column, the one value you copy into the next command, was cut by a table that had to shrink:
`projects list` at 80 columns, `runs list` at every width.

Widths are set through `at_width`, which sets the height as well. Rich ignores a width alone, and the
tests run with TERM=dumb, which pins every console to 80 columns whatever else is set.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config
from harumi.client import Client

try:  # click 8.1 mixes stderr into the output unless told not to; 8.2 removed the switch
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()

P = "proj-1"
PID = "0a1b2c3d-1111-4222-8333-444455556666"
R1 = "7f3a91c2-0000-4000-8000-000000000001"
R2 = "7f3a91c2-0000-4000-8000-000000000002"  # shares its first 13 characters with R1
R3 = "b8e4d5f6-0000-4000-8000-000000000003"
TAG = "[red]TAG[/red]"  # Rich shows this as "TAG", coloured, with the brackets gone


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
    at_width_value(monkeypatch, 80)


def at_width_value(monkeypatch, columns: int) -> None:
    monkeypatch.setattr(cli.console, "_width", columns)
    monkeypatch.setattr(cli.console, "_height", 40)  # without a height Rich ignores the width


@pytest.fixture
def at_width(monkeypatch):
    return lambda columns: at_width_value(monkeypatch, columns)


def run_row(id_, **extra):
    return {"id": id_, "project_id": PID, "status": "completed", "created_at": "2026-10-15T12:00:00Z", "git_branch": "main", **extra}


class Api:
    def __init__(self):
        self.runs = [run_row(R1), run_row(R2, status="failed", git_branch=f"feat/{TAG}"), run_row(R3, status="running")]
        self.calls: list[str] = []
        self.projects = [{"id": PID, "name": "Vehicle routing for the northern depots", "kernel_spec": "or_python_small", "role_name": "owner",
                          "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-20T00:00:00Z"}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        self.calls.append(f"{m} {p}")
        if p == "/api/projects":
            return httpx.Response(200, json={"projects": self.projects, "total_count": len(self.projects)})
        if p.endswith("/runs") and m == "GET":
            return httpx.Response(200, json={"runs": self.runs})
        if "/runs/" in p:
            rid = p.split("/runs/")[1].split("/")[0]
            row = next((r for r in self.runs if r["id"] == rid), None)
            if not row:
                return httpx.Response(404, json={"detail": "no such run"})
            if p.endswith("/cancel"):
                return httpx.Response(200, json={**row, "status": "cancelled"})
            return httpx.Response(200, json=row)
        if p.endswith("/blueprints"):
            return httpx.Response(200, json={"blueprints": [{"slug": "a", "name": f"Name {TAG}", "description": f"Does {TAG}"}], "total_count": 1})
        if p.endswith("/skill-library"):
            return httpx.Response(200, json={"skills": [{"slug": "s", "name": f"Skill {TAG}", "category": "x", "version": 1, "description": f"About {TAG}"}]})
        if p.endswith("/runs/compare") or p.endswith("/compare"):
            return httpx.Response(200, json={"columns": [], "rows": []})
        return httpx.Response(404, json={"detail": "not routed " + p})


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler)))
    monkeypatch.setattr(cli, "_resolve_project", lambda p: p or PID)
    return a


def plain(text: str) -> str:
    return " ".join(text.split())


# -- text is shown as stored --------------------------------------------------------------------------------


def test_the_width_helper_really_changes_the_width(at_width):
    at_width(50)
    assert cli.console.width == 50
    at_width(120)
    assert cli.console.width == 120  # if this fails, every width test below measures nothing


@pytest.mark.parametrize("args", [["runs", "list"], ["blueprints"], ["skills", "library"]])
def test_a_rich_tag_in_a_table_cell_is_shown_as_typed(api, args):
    out = plain(runner.invoke(cli.app, args).stdout)

    assert TAG in out


def test_a_project_name_with_a_rich_tag_is_shown_as_typed(api, at_width):
    at_width(160)  # wide enough that the name is on one line
    api.projects[0]["name"] = f"Q3 {TAG}"

    assert f"Q3 {TAG}" in plain(runner.invoke(cli.app, ["projects", "list"]).stdout)


@pytest.mark.parametrize("args", [["runs", "list"], ["blueprints"], ["skills", "library"], ["projects", "list"]])
def test_a_stray_closing_tag_cannot_crash_a_table(api, monkeypatch, args):
    for row in api.runs:
        row["git_branch"] = "bad[/oops]"
    api.projects[0]["name"] = "bad[/oops]"
    orig = api.handler

    def stray(request):
        response = orig(request)
        if request.url.path.endswith(("/blueprints", "/skill-library")):
            return httpx.Response(200, json=json.loads(response.content.decode().replace("[red]TAG[/red]", "bad[/oops]")))
        return response

    monkeypatch.setattr(api, "handler", stray)

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 0 and isinstance(result.exception, (type(None), SystemExit))
    assert "bad[/oops]" in plain(result.stdout)  # it printed the text, not just survived it


# -- the id you copy is whole, or is a start that the next command accepts ----------------------------------------


@pytest.mark.parametrize("columns", [30, 40, 60, 80, 100, 120, 200])
def test_the_project_id_is_whole_on_one_line_at_every_width(api, at_width, columns):
    """A table cannot hold the whole id below 87 columns without cutting it or crushing the other columns, so
    there each project is two lines. Either way the id is whole and unbroken, and the name is still there."""
    at_width(columns)

    out = runner.invoke(cli.app, ["projects", "list"]).stdout

    line = next(line for line in out.splitlines() if PID[:8] in line)
    assert PID in line and "…" not in line  # on one line, whole
    assert "Vehicle" in out  # and the row still says what the project is


def test_a_wide_terminal_still_gets_the_table(api, at_width):
    at_width(200)

    out = runner.invoke(cli.app, ["projects", "list"]).stdout

    assert "┃" in out and "updated" in out


def test_a_narrow_one_gets_two_lines_per_project_with_nothing_lost(api, at_width):
    at_width(80)
    api.projects.append({"id": "9f8e7d6c-2222-4333-8444-555566667777", "name": "Zeta", "kernel_spec": "gurobi_python_large", "role_name": "member",
                         "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"})

    out = runner.invoke(cli.app, ["projects", "list"]).stdout

    assert "┃" not in out
    for fact in (PID, "Vehicle routing for the northern depots", "updated:", "or_python_small", "owner", "9f8e7d6c-2222-4333-8444-555566667777", "Zeta", "gurobi_python_large", "member"):
        assert fact in plain(out), fact


def test_the_two_line_form_keeps_a_rich_tag_in_a_name(api, at_width):
    at_width(80)
    api.projects[0]["name"] = f"Q3 {TAG}"

    assert f"Q3 {TAG}" in plain(runner.invoke(cli.app, ["projects", "list"]).stdout)


@pytest.mark.parametrize("columns", [30, 40, 50, 60, 80])
def test_the_organization_id_is_whole_at_every_width(api, at_width, columns):
    at_width(columns)
    ORG = "0a1b2c3d-1111-4222-8333-444455556666"
    orig = api.handler
    api.handler = lambda r: httpx.Response(200, json=[{"id": ORG, "business_name": "Acme", "role": "owner"}]) if r.url.path.endswith("/users/organizations") else orig(r)  # type: ignore[method-assign]

    out = runner.invoke(cli.app, ["org", "list"]).stdout

    line = next(line for line in out.splitlines() if ORG[:8] in line)
    assert ORG in line and "Acme" in out


@pytest.mark.parametrize("columns", [30, 40, 50, 60, 80, 120])
def test_a_run_id_in_the_list_is_the_start_and_is_not_cut(api, at_width, columns):
    at_width(columns)

    out = runner.invoke(cli.app, ["runs", "list"]).stdout

    assert "7f3a91c2" in out and R1 not in out  # eight characters, whatever the width
    first_cell = [line for line in out.splitlines() if "7f3a91c2" in line][0].split("│")[1].strip()
    assert first_cell == "7f3a91c2"  # not "7f3a91c…"


@pytest.mark.parametrize("typed", ["b8e4d5f6", "b8e4"])
def test_runs_get_accepts_the_start_of_an_id(api, typed):
    result = runner.invoke(cli.app, ["runs", "get", typed])

    assert result.exit_code == 0 and R3 in plain(result.stdout)


def test_runs_get_still_accepts_a_whole_id_without_a_lookup(api):
    assert runner.invoke(cli.app, ["runs", "get", R3]).exit_code == 0
    assert not any(c.endswith("/runs") for c in api.calls)  # a whole id goes straight to the run


def test_a_start_that_matches_two_runs_is_refused_not_guessed(api):
    result = runner.invoke(cli.app, ["runs", "get", "7f3a91c2"])  # R1 and R2 both start with this

    assert result.exit_code == 1 and "more than one" in plain(result.stderr)


def test_a_start_that_matches_no_run_points_at_the_list(api):
    result = runner.invoke(cli.app, ["runs", "get", "deadbeef"])

    assert result.exit_code == 1 and "harumi runs list" in plain(result.stderr)


def test_cancel_accepts_the_start_of_an_id(api):
    result = runner.invoke(cli.app, ["runs", "cancel", "b8e4d5f6"])

    assert result.exit_code == 0 and f"/runs/{R3}/cancel" in " ".join(api.calls)


def test_download_resolves_the_start_of_an_id_before_asking_for_the_run(api):
    runner.invoke(cli.app, ["runs", "download", "b8e4d5f6"])

    runs_asked_for = [c.split("/runs/")[1] for c in api.calls if "/runs/" in c]
    assert runs_asked_for and all(r.startswith(R3) for r in runs_asked_for)  # the whole id, never the start


def test_compare_accepts_starts_of_ids_and_sends_whole_ones(api):
    seen = {}
    orig = api.handler

    def spy(request):
        if "compare" in request.url.path:
            seen["ids"] = request.url.params.get("runs")
        return orig(request)

    api.handler = spy  # type: ignore[method-assign]
    runner.invoke(cli.app, ["compare", "b8e4d5f6", R1])

    assert seen.get("ids") == f"{R3},{R1}"


def test_compare_notices_when_a_start_and_a_whole_id_are_the_same_run(api):
    result = runner.invoke(cli.app, ["compare", "b8e4d5f6", R3])

    assert result.exit_code == 1 and "once" in plain(result.stderr)


# -- tables that print what someone else wrote --------------------------------------------------------------


def test_a_dashboard_widget_title_with_a_rich_tag_is_shown_as_written_by_validate(tmp_path, monkeypatch):
    """The title is the dashboard author's own text, and `validate` is how they check it. `[red]` is a style
    to Rich, so "Cost [red]" was shown as "Cost"; `[/oops]` crashed the command after validating."""
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard" / "main.toml").write_text(
        '[[widgets]]\ntype = "metric"\nid = "objective"\ntitle = "Cost [red] [/oops]"\nvalue_key = "objective"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.console, "_width", 120)

    result = runner.invoke(cli.app, ["dashboard", "validate"])

    assert isinstance(result.exception, (type(None), SystemExit)) and "Cost [red] [/oops]" in plain(result.stdout)


def test_the_status_table_shows_service_names_from_a_page_we_do_not_control(monkeypatch):
    payload = [{"group": "g", "name": "API [/x] [red]", "results": [{"success": True}]}]
    monkeypatch.setattr(cli.httpx, "get", lambda url, **k: httpx.Response(200, json=payload, request=httpx.Request("GET", url)))

    result = runner.invoke(cli.app, ["status"])

    assert isinstance(result.exception, (type(None), SystemExit)) and "API [/x] [red]" in plain(result.stdout)


@pytest.mark.parametrize("columns", [80, 200])
def test_a_terminal_escape_in_any_project_field_cannot_reach_the_terminal(api, at_width, columns):
    """Project fields are other people's text, in the table and in the two-line form alike."""
    at_width(columns)
    api.projects[0].update(name="n\x1b[2Jame", kernel_spec="k\u202eernel", role_name="r\x07ole")

    out = runner.invoke(cli.app, ["projects", "list"]).stdout

    assert "\x1b" not in out and "\u202e" not in out and "\x07" not in out
    # Cleaned, not dropped: the control characters go, what was around them stays (the `[2J` left behind is
    # inert text once the escape byte is gone).
    assert "n[2Jame" in out and "kernel" in out and "role" in out
