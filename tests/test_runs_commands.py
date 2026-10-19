"""runs list / get / cancel / download and check: readable, and no id needed where one can be inferred."""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.client import Client

runner = CliRunner()
P = "proj-1"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


def run_row(id_, status="completed", **extra):
    return {"id": id_, "project_id": P, "status": status, "created_at": "2026-10-15T12:00:00Z", **extra}


class Server:
    def __init__(self, runs=None, readiness=None):
        self.runs = runs or []
        self.readiness = readiness
        self.cancelled: list[str] = []
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path.endswith("/readiness"):
            return httpx.Response(200, json=self.readiness)
        if path.endswith("/cancel"):
            rid = path.split("/")[-2]
            self.cancelled.append(rid)
            row = next(r for r in self.runs if r["id"] == rid)
            return httpx.Response(200, json={**row, "status": "cancelled"} if row["status"] in ("queued", "running") else row)
        if path.endswith("/runs"):
            return httpx.Response(200, json={"runs": self.runs})
        if "/runs/" in path:
            rid = path.split("/")[-1]
            row = next((r for r in self.runs if r["id"] == rid), None)
            return httpx.Response(200, json=row) if row else httpx.Response(404, json={"detail": "no such run"})
        return httpx.Response(404, json={"detail": "not routed"})


@pytest.fixture
def server(monkeypatch):
    holder = {}

    def make(**kw):
        holder["s"] = Server(**kw)
        monkeypatch.setattr(
            cli,
            "_get_client",
            lambda *a, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(holder["s"].handler)),
        )
        return holder["s"]

    return make


def invoke(*args):
    return runner.invoke(cli.app, [*args, "--project", P])


# -- runs list -----------------------------------------------------------------------


def test_runs_list_shows_how_long_each_run_took_and_when_it_started(server):
    server(runs=[run_row("run-1", started="2026-10-15T12:00:00Z", ended="2026-10-15T12:01:12Z")])

    result = invoke("runs", "list")

    assert result.exit_code == 0, result.output
    assert "1m 12s" in result.output
    assert "took" in result.output and "started" in result.output
    assert "2026-10-15T12" not in result.output  # no raw timestamps


def test_a_run_with_no_end_time_shows_no_duration_rather_than_nonsense(server):
    server(runs=[run_row("run-1", "running", started="2026-10-15T12:00:00Z")])
    assert invoke("runs", "list").exit_code == 0


# -- runs get -------------------------------------------------------------------------


def test_runs_get_with_no_id_shows_the_latest_run(server):
    s = server(runs=[run_row("newest", exit_code=0), run_row("older")])

    result = invoke("runs", "get")

    assert result.exit_code == 0, result.output
    assert "newest" in result.output and "older" not in result.output
    assert s.requests == [f"GET /api/projects/{P}/runs"]


def test_runs_get_with_no_runs_says_how_to_start_one(server):
    server(runs=[])

    result = invoke("runs", "get")

    assert result.exit_code == 1
    assert "no runs yet" in result.output and "harumi run" in result.output


def test_runs_get_is_a_short_summary_then_the_output(server):
    server(
        runs=[
            run_row(
                "run-1",
                "failed",
                exit_code=1,
                git_branch="main",
                git_commit="abcdef1234567",
                command="python main.py",
                kernel_spec="python_small",
                started="2026-10-15T12:00:00Z",
                ended="2026-10-15T12:00:45Z",
                stdout="solving...\n",
                stderr="ValueError: infeasible\n",
                error="solver crashed",
            )
        ]
    )

    out = invoke("runs", "get", "run-1").output

    for expected in ("failed (exit code 1)", "45s", "main @ abcdef12", "python main.py", "python_small", "solving...", "ValueError: infeasible", "solver crashed"):
        assert expected in out, expected
    assert "output_url" not in out and "triggered_by" not in out  # no internal field names
    assert out.index("failed") < out.index("solving...")  # facts first, output after


def test_output_that_looks_like_rich_markup_is_printed_literally(server):
    server(runs=[run_row("run-1", "failed", stdout="a [bold]b[/bold] c", error="x [/red] y")])

    out = invoke("runs", "get", "run-1").output

    assert "a [bold]b[/bold] c" in out and "x [/red] y" in out


def test_a_run_still_going_offers_cancel_and_a_finished_one_with_output_offers_download(server):
    server(runs=[run_row("going", "running"), run_row("done", output_url="s3://x")])

    assert "harumi runs cancel going" in invoke("runs", "get", "going").output
    assert "harumi runs download done" in invoke("runs", "get", "done").output


# -- runs cancel -------------------------------------------------------------------------


def test_cancel_with_an_id_cancels_that_run_with_no_questions(server):
    s = server(runs=[run_row("run-1", "running")])

    result = invoke("runs", "cancel", "run-1")

    assert result.exit_code == 0, result.output
    assert s.cancelled == ["run-1"]
    assert "cancelled" in result.output


def test_cancel_without_an_id_picks_from_the_runs_still_going(server, monkeypatch):
    s = server(runs=[run_row("done"), run_row("a-running", "running"), run_row("b-queued", "queued")])
    seen = {}

    def select(message, options, *, flag=None, default=None):
        seen["values"] = [o.value for o in options]
        seen["flag"] = flag
        return "b-queued"

    monkeypatch.setattr(interactive, "select", select)

    result = invoke("runs", "cancel")

    assert result.exit_code == 0, result.output
    assert seen["values"] == ["a-running", "b-queued"]  # finished runs are not offered
    assert seen["flag"] == "the RUN_ID argument"
    assert s.cancelled == ["b-queued"]


def test_cancel_picker_backed_out_cancels_nothing(server, monkeypatch):
    s = server(runs=[run_row("a", "running")])
    monkeypatch.setattr(interactive, "select", lambda *a, **k: None)

    result = invoke("runs", "cancel")

    assert result.exit_code == 0
    assert s.cancelled == []


def test_cancel_without_an_id_or_a_terminal_lists_the_runs_and_the_argument(server, monkeypatch):
    s = server(runs=[run_row("aaaaaaaa-1111", "running")])
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)

    result = invoke("runs", "cancel")

    assert result.exit_code == 1
    assert "aaaaaaaa" in result.output and "RUN_ID" in result.output
    assert s.cancelled == []


def test_cancel_with_nothing_running_says_so(server):
    s = server(runs=[run_row("done")])

    result = invoke("runs", "cancel")

    assert result.exit_code == 0
    assert "Nothing to cancel" in result.output
    assert s.cancelled == []


def test_cancelling_a_run_that_already_finished_is_not_reported_as_a_cancellation(server):
    server(runs=[run_row("run-1", "completed")])

    result = invoke("runs", "cancel", "run-1")

    assert result.exit_code == 0
    assert "had already completed" in result.output
    assert "now cancelled" not in result.output


# -- runs download -------------------------------------------------------------------------


def test_download_refuses_a_run_that_has_not_finished_and_says_to_wait(server):
    server(runs=[run_row("run-1", "running")])

    result = invoke("runs", "download", "run-1")

    assert result.exit_code == 1
    assert "still running" in result.output and "harumi runs get run-1" in result.output


def test_download_of_a_finished_run_with_no_output_says_what_to_look_at_instead(server):
    server(runs=[run_row("run-1", "completed")])

    result = invoke("runs", "download", "run-1")

    assert result.exit_code == 1
    assert "without leaving any output" in " ".join(result.output.split())


def test_download_reports_the_path_and_the_size(server, tmp_path, monkeypatch):
    server(runs=[run_row("run-1", output_url="s3://x")])

    def fake_download(_api, _project, run, dest):
        path = dest / f"run_{run.id}_output.zip"
        path.write_bytes(b"x" * 2048)
        return path

    monkeypatch.setattr("harumi.execution.download_run_output", fake_download)

    result = invoke("runs", "download", "run-1", "-o", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert "run_run-1_output.zip" in result.output and "2.0 KB" in result.output


# -- check -------------------------------------------------------------------------------------


def readiness(ready, checks, ref="main"):
    return {"project_id": P, "ref": ref, "ready": ready, "checks": checks}


def test_check_uses_readable_names_and_points_at_run_when_ready(server):
    server(readiness=readiness(True, [{"id": "repo", "ok": True}, {"id": "manifest_valid", "ok": True}]))

    result = runner.invoke(cli.app, ["check", "--project", P])

    assert result.exit_code == 0, result.output
    assert "Repository exists" in result.output and "harumi.toml is valid" in result.output
    assert "repo " not in result.output.replace("Repository", "")
    assert "Ready to run" in result.output and "harumi run --watch" in result.output


def test_check_exits_one_and_counts_the_problems_so_a_script_can_stop(server):
    server(
        readiness=readiness(
            False,
            [
                {"id": "repo", "ok": True},
                {"id": "manifest_valid", "ok": False, "detail": "missing entrypoint"},
                {"id": "kernel", "ok": False, "detail": "kernel not allowed [gurobi]"},
            ],
        )
    )

    result = runner.invoke(cli.app, ["check", "--project", P])

    assert result.exit_code == 1
    assert "2 problems" in result.output
    assert "missing entrypoint" in result.output
    assert "[gurobi]" in result.output  # server text is not read as markup


def test_not_ready_with_no_failing_check_does_not_claim_zero_problems(server):
    """The server can say not-ready without a check failing (a state the checks do not
    cover). "0 problems" would be a contradiction a person cannot act on."""
    server(readiness=readiness(False, [{"id": "repo", "ok": True}]))

    result = runner.invoke(cli.app, ["check", "--project", P])

    out = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "0 problem" not in out
    assert "Not ready" in out and "did not say why" in out


def test_the_old_repo_readiness_command_still_exits_zero_for_the_scripts_that_use_it(server):
    """`harumi check` now exits 1 when blocked. `harumi repo readiness` never did, and
    scripts that call it and ignore the exit code must not start failing."""
    server(readiness=readiness(False, [{"id": "manifest_valid", "ok": False, "detail": "missing entrypoint"}]))

    old = runner.invoke(cli.app, ["repo", "readiness", "--project", P])
    new = runner.invoke(cli.app, ["check", "--project", P])

    assert old.exit_code == 0, old.output
    assert "missing entrypoint" in old.output and "Not ready" in " ".join(old.output.split())
    assert new.exit_code == 1


def test_a_check_the_cli_has_no_name_for_still_shows_its_id(server):
    server(readiness=readiness(False, [{"id": "quota", "ok": False, "detail": "over quota"}]))

    result = runner.invoke(cli.app, ["check", "--project", P])

    assert "quota" in result.output and "over quota" in result.output


def test_a_long_download_path_is_never_broken_across_lines(server, tmp_path, monkeypatch):
    """A path someone will copy must come out whole whatever the terminal width.
    CI's long temp directory split it mid-filename."""
    server(runs=[run_row("run-1", output_url="s3://x")])
    deep = tmp_path / ("nested-directory-with-a-long-name-" * 3)
    deep.mkdir()

    def fake_download(_api, _project, run, dest):
        path = dest / f"run_{run.id}_output.zip"
        path.write_bytes(b"x")
        return path

    monkeypatch.setattr("harumi.execution.download_run_output", fake_download)

    result = invoke("runs", "download", "run-1", "-o", str(deep))

    assert result.exit_code == 0, result.output
    assert str(deep / "run_run-1_output.zip") in result.output
