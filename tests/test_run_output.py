"""`harumi run` and `run --watch`: what is printed, and what the exit code says."""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, execution
from harumi.client import Client

runner = CliRunner()
PROJECT = "proj-bound"


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
    config.save_git_token("gitea", username="u-1")
    workdir = tmp_path / "work"
    (workdir / ".harumi").mkdir(parents=True)
    (workdir / ".harumi" / "config.json").write_text(
        json.dumps(
            {
                "project_id": PROJECT,
                "repo": {"owner": "acme", "name": "s", "clone_url": "https://g/acme/s.git", "default_branch": "main"},
            }
        )
    )
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(cli, "is_dirty", lambda: False)
    monkeypatch.setattr(cli, "has_unpushed_commits", lambda: False)
    monkeypatch.setattr(cli, "current_branch", lambda: "main")
    monkeypatch.setattr(execution.time, "sleep", lambda s: None)  # never really wait


class Backend:
    """harmumi-api's run endpoints, replaying a scripted sequence of run states."""

    def __init__(self, states, *, execute=None):
        self.states = list(states)
        self.execute = execute or (200, {"execution_log_id": "log-1", "project_run_id": "run-1", "status": "queued"})
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/execute"):
            return httpx.Response(self.execute[0], json=self.execute[1])
        if "/runs/run-1" in request.url.path:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return httpx.Response(200, json={"id": "run-1", "project_id": PROJECT, **state})
        return httpx.Response(404, json={"detail": "not routed"})


@pytest.fixture
def backend(monkeypatch):
    holder = {}

    def make(states, **kw):
        holder["b"] = Backend(states, **kw)
        monkeypatch.setattr(
            cli,
            "_get_client",
            lambda *a, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(holder["b"].handler)),
        )
        return holder["b"]

    return make


def _run(*args):
    return runner.invoke(cli.app, ["run", *args])


# -- starting ------------------------------------------------------------------


def test_run_prints_one_clear_start_and_the_run_id_without_internal_ids(backend):
    backend([{"status": "queued"}])

    result = _run()

    assert result.exit_code == 0, result.output
    assert "Running project" in result.output and "on branch main" in result.output
    assert "Queued run run-1" in result.output
    assert "execution_log_id" not in result.output and "log-1" not in result.output
    assert "harumi runs get run-1" in result.output and "--watch" in result.output


def test_a_dirty_tree_is_one_plain_sentence_not_scratch_branch_jargon(backend, monkeypatch):
    backend([{"status": "queued"}])
    monkeypatch.setattr(cli, "is_dirty", lambda: True)
    deleted = []
    monkeypatch.setattr(cli, "push_scratch", lambda username, token: ("harumi-scratch-xyz", "sha"))
    monkeypatch.setattr(cli, "delete_remote_scratch", deleted.append)

    result = _run()

    assert "uncommitted changes" in result.output
    assert "your branches are not touched" in result.output
    assert "scratch" not in result.output.lower()
    assert "harumi-scratch-xyz" not in result.output
    assert deleted == ["harumi-scratch-xyz"]  # still cleaned up, just not announced


# -- being told "check" when the server refuses ---------------------------------


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_a_refused_run_points_at_harumi_check(backend, status):
    backend([{"status": "queued"}], execute=(status, {"detail": "harumi.toml is invalid"}))

    result = _run()

    assert result.exit_code == 1
    assert "harumi.toml is invalid" in " ".join(result.output.split())
    assert "harumi check" in result.output


@pytest.mark.parametrize("status", [401, 403, 500])
def test_other_failures_are_not_blamed_on_the_project(backend, status):
    backend([{"status": "queued"}], execute=(status, {"detail": "nope"}))

    result = _run()

    assert result.exit_code == 1
    assert "harumi check" not in result.output


# -- watching ---------------------------------------------------------------------


def test_watch_logs_only_changes_of_status_when_not_at_a_terminal(backend):
    backend(
        [
            {"status": "queued"},
            {"status": "queued"},
            {"status": "running"},
            {"status": "running"},
            {"status": "running"},
            {"status": "completed", "exit_code": 0, "output_url": "s3://x"},
        ]
    )

    result = _run("--watch")

    assert result.exit_code == 0, result.output
    assert result.output.count("running") == 1  # once, not once per poll
    assert result.output.count("queued") <= 2  # the "Queued run" line, no repeats per poll
    assert "Run completed" in result.output
    assert "harumi runs download run-1" in result.output


def test_a_run_that_succeeds_exits_zero_and_reports_how_long_it_took(backend):
    backend(
        [
            {
                "status": "completed",
                "exit_code": 0,
                "started": "2026-10-15T12:00:00Z",
                "ended": "2026-10-15T12:01:12Z",
            }
        ]
    )

    result = _run("--watch")

    assert result.exit_code == 0
    assert "1m 12s" in result.output


# -- failing ------------------------------------------------------------------------


def _failed(**extra):
    return {
        "status": "failed",
        "exit_code": 1,
        "started": "2026-10-15T12:00:00Z",
        "ended": "2026-10-15T12:00:45Z",
        **extra,
    }


def test_a_failed_run_exits_one_and_names_the_cause_first(backend):
    backend([_failed(error="solver crashed", stderr="Traceback...\nValueError: infeasible model")])

    result = _run("--watch")

    assert result.exit_code == 1
    assert "Run failed after 45s (exit code 1)." in result.output
    assert "solver crashed" in result.output
    assert "ValueError: infeasible model" in result.output
    assert "harumi runs get run-1" in result.output


def test_a_long_stderr_is_cut_to_the_last_lines_and_says_so(backend):
    stderr = "\n".join(f"line {i}" for i in range(1, 101))
    backend([_failed(stderr=stderr)])

    result = _run("--watch")

    assert "line 100" in result.output and "line 81" in result.output
    assert "line 80" not in result.output and "line 1\n" not in result.output
    assert "80 earlier lines not shown" in result.output


def test_a_failure_with_no_output_says_so_instead_of_printing_nothing(backend):
    backend([_failed()])

    result = _run("--watch")

    assert result.exit_code == 1
    assert "No error output was captured" in result.output


def test_a_run_with_brackets_in_its_output_is_printed_literally(backend):
    backend([_failed(error="bad [/red] markup", stderr="oops [bold]x[/bold]")])

    result = _run("--watch")

    assert "bad [/red] markup" in result.output
    assert "oops [bold]x[/bold]" in result.output


@pytest.mark.parametrize("status", ["cancelled", "timeout"])
def test_cancelled_and_timed_out_runs_also_exit_one(backend, status):
    backend([{"status": status}])

    result = _run("--watch")

    assert result.exit_code == 1
    assert f"Run {status}" in result.output


# -- Ctrl-C -------------------------------------------------------------------------


def test_ctrl_c_while_watching_leaves_the_run_alone_and_says_how_to_cancel(backend, monkeypatch):
    b = backend([{"status": "running"}])

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(execution, "wait_for_run", interrupted)

    result = _run("--watch")

    assert result.exit_code == 130
    assert "The run is still going" in result.output
    assert "harumi runs cancel run-1" in result.output
    assert not any("cancel" in r for r in b.requests), "stopping the watch must not cancel the run"


def test_the_temporary_branch_is_still_cleaned_up_after_ctrl_c(backend, monkeypatch):
    backend([{"status": "running"}])
    monkeypatch.setattr(cli, "is_dirty", lambda: True)
    deleted = []
    monkeypatch.setattr(cli, "push_scratch", lambda username, token: ("harumi-scratch-xyz", "sha"))
    monkeypatch.setattr(cli, "delete_remote_scratch", deleted.append)
    monkeypatch.setattr(execution, "wait_for_run", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))

    _run("--watch")

    assert deleted == ["harumi-scratch-xyz"]


# -- helpers ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (45, "45s"), (59, "59s"), (60, "1m 00s"), (72, "1m 12s"), (3599, "59m 59s"), (3600, "1h 00m"), (7380, "2h 03m")],
)
def test_durations_read_naturally(seconds, text):
    assert cli._duration(seconds) == text


def test_no_duration_means_nothing_to_show():
    assert cli._duration(None) == ""
    assert cli._duration(-5) == ""


def test_tail_keeps_the_end_and_counts_what_was_dropped():
    assert cli._tail_lines("a\nb\nc\nd", 2) == ("c\nd", 2)
    assert cli._tail_lines("a\nb", 5) == ("a\nb", 0)
    assert cli._tail_lines("a\nb\n\n\n", 5) == ("a\nb", 0)  # trailing blank lines are not content


def test_a_forced_colour_environment_does_not_get_the_animated_line(backend, monkeypatch):
    """rich treats any FORCE_COLOR as a terminal, and CI sets it for colour. The
    animated line must follow the real TTY, or build logs fill with control codes."""
    backend([{"status": "queued"}, {"status": "running"}, {"status": "completed", "exit_code": 0}])
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr(cli.console, "_force_terminal", True)  # rich says "terminal"...
    assert cli.console.is_terminal is True
    assert cli._live_display_ok() is False  # ...but stdout here is not a TTY

    result = _run("--watch")

    assert result.exit_code == 0, result.output
    assert "Waiting for run run-1" in result.output  # the plain, log-friendly path
    assert "running" in result.output


# -- a login problem is never blamed on the project ------------------------------------------------------


@pytest.mark.parametrize("refresh_status", [400, 403, 404, 409, 422, 500])
def test_a_failed_token_refresh_is_never_blamed_on_the_project(monkeypatch, refresh_status):
    """The token refresh that runs before `execute` can fail with any status. If it came
    back 404 or 422, `run` used to read that as "something is wrong with the project" and
    send the user to `harumi check`, when it was their login (or a firewall in front of the
    API) all along."""
    from harumi import auth
    from harumi.errors import SessionRefreshError

    def refresh_fails(config_, token, transport=None):
        raise SessionRefreshError(refresh_status, "refresh went wrong")

    monkeypatch.setattr(auth, "refresh_session", refresh_fails)
    config.save_credentials(access_token="stale", refresh_token="r", email="ana@example.com", expires_at=1)
    monkeypatch.setattr(
        cli,
        "_get_client",
        lambda *a, **k: Client(
            api_url="https://api.harumi.io/api",
            transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"detail": "expired"})),
        ),
    )

    result = _run()

    out = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "Could not renew your login" in out
    assert "harumi check" not in out, "blamed the project for a login problem"


# -- --json: the result as data, for a pipeline ------------------------------------------------------------

try:  # click 8.1 mixes stderr into the output unless told not to; 8.2 removed the switch
    split_runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    split_runner = CliRunner()


def _run_json(*args):
    return split_runner.invoke(cli.app, ["run", "--json", *args])


def test_run_json_prints_the_run_id_as_data_and_the_talk_on_stderr(backend):
    backend([{"status": "queued"}])

    result = _run_json()

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "run_id": "run-1", "status": "queued", "exit_code": None, "succeeded": None, "took_seconds": None, "error": None, "watched": False,
    }
    assert "Queued run run-1" in result.stderr and "Queued" not in result.stdout


def test_a_watched_run_json_reports_the_verdict_and_exits_zero(backend):
    backend([{"status": "queued"}, {"status": "running"}, {"status": "completed", "exit_code": 0, "output_url": "s3://x"}])

    result = _run_json("--watch")

    done = json.loads(result.stdout)  # the whole of stdout: no progress line before or after it
    assert result.exit_code == 0 and done["succeeded"] is True and done["status"] == "completed" and done["exit_code"] == 0 and done["watched"] is True
    assert "Run completed" in result.stderr


def test_a_failed_watched_run_json_still_exits_one_and_says_why_as_data(backend):
    backend([{"status": "queued"}, {"status": "failed", "exit_code": 2, "error": "solver crashed"}])

    result = _run_json("--watch")

    done = json.loads(result.stdout)
    assert result.exit_code == 1 and done["succeeded"] is False and done["exit_code"] == 2 and done["error"] == "solver crashed"
    assert "Run failed" in result.stderr


def test_a_refused_run_json_prints_no_document(backend):
    backend([{"status": "queued"}], execute=(400, {"detail": "harumi.toml is invalid"}))

    result = _run_json()

    assert result.exit_code == 1 and result.stdout == "" and "harumi.toml is invalid" in " ".join(result.stderr.split())


def test_the_next_command_prints_to_stdout_again_after_a_json_run(backend):
    backend([{"status": "queued"}])
    _run_json()

    result = split_runner.invoke(cli.app, ["run"])

    assert "Queued run run-1" in result.stdout  # the redirect to stderr did not outlive the command


def test_a_json_run_that_dies_early_still_restores_stdout(backend, monkeypatch):
    backend([{"status": "queued"}])
    monkeypatch.setattr(cli, "is_dirty", lambda: (_ for _ in ()).throw(cli.GitError("git broke")))
    assert cli.console._file is None  # unset: it follows sys.stdout

    result = _run_json()

    assert result.exit_code == 1 and cli.console._file is None
