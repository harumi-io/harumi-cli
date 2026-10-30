"""`harumi run` says which uploaded files the run will find at `inputs/` (issue #132).

Files from `harumi files put` stay in the project's storage and appear at `inputs/` in every later run until someone
runs `harumi files rm`. A stale `config.json` once silently switched a solver to a different objective, and the output
still looked plausible. The run now lists them before it starts, so a surprise input is visible in the log.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, execution
from harumi.client import Client

runner = CliRunner()
try:
    split_runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]  # click < 8.2 mixes the two streams by default
except TypeError:
    split_runner = CliRunner()  # click >= 8.2 always keeps them apart
PROJECT = "proj-bound"
NOW = datetime.now(timezone.utc)


def iso(**delta):
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def f(name, size, **ago):
    return {"name": name, "key": f"{PROJECT}/{name}", "size": size, "last_modified": iso(**ago)}


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
    monkeypatch.setattr(execution.time, "sleep", lambda s: None)


class Backend:
    def __init__(self):
        self.files = [f("scenario.json", 12 * 1024, hours=2), f("config.json", 1024, days=3)]
        self.truncated = False
        self.files_status = 200
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        path = request.url.path
        if path.endswith("/execute"):
            return httpx.Response(200, json={"execution_log_id": "l", "project_run_id": "run-1", "status": "queued"})
        if path.endswith("/files"):
            if self.files_status != 200:
                return httpx.Response(self.files_status, json={"detail": "storage is down"})
            return httpx.Response(200, json={"files": self.files, "is_truncated": self.truncated})
        return httpx.Response(404, json={"detail": "not routed"})

    def order(self):
        return [r for r in self.requests if r.endswith("/files") or r.endswith("/execute")]


@pytest.fixture
def backend(monkeypatch):
    b = Backend()
    monkeypatch.setattr(
        cli, "_get_client", lambda *a, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(b.handler))
    )
    return b


def run(*args):
    return runner.invoke(cli.app, ["run", *args])


def said(result):
    return " ".join(result.output.split())


# -- the list -------------------------------------------------------------------------------------------------------


def test_the_run_lists_what_it_will_find_in_inputs_with_size_and_age(backend):
    result = run()

    out = said(result)
    assert result.exit_code == 0, result.output
    assert "scenario.json" in out and "config.json" in out
    assert "12.0 KB" in out and "1.0 KB" in out  # the same figures `harumi files ls` prints
    assert "2 hours ago" in out and "3 days ago" in out  # hours, not seconds: a slow machine must not tip "just now" into "1 minute ago"
    assert "inputs/" in out


def test_the_list_comes_before_the_run_starts(backend):
    """The point is to see it before the run reads it, not after."""
    run()

    assert backend.order() == ["GET /api/projects/proj-bound/files", "POST /api/projects/proj-bound/execute"]


def test_the_list_is_in_the_output_before_the_queued_line(backend):
    out = run().output

    assert out.index("scenario.json") < out.index("Queued run")


def test_with_no_uploads_it_says_so_in_one_line(backend):
    backend.files = []

    result = run()

    assert result.exit_code == 0 and "no uploaded files" in said(result).lower()
    assert "inputs/" in said(result)


def test_newest_uploads_are_first_because_the_stale_one_is_the_surprise(backend):
    """The old file is the one you forgot; listing by age puts what you just uploaded where you expect it."""
    out = run().output

    assert out.index("scenario.json") < out.index("config.json")


def test_a_long_list_is_cut_with_the_count_and_the_command_for_the_rest(backend):
    backend.files = [f(f"data_{i:02d}.csv", 100, days=i + 1) for i in range(25)]

    out = said(run())

    assert out.count(".csv") <= 12, "a 25-line wall before every run would be the thing people stop reading"
    assert "25" in out and "harumi files ls" in out


def test_a_truncated_listing_says_it_is_a_minimum(backend):
    backend.truncated = True

    assert "at least" in said(run()).lower()


# -- never in the way ------------------------------------------------------------------------------------------------


def test_quiet_hides_the_list_and_does_not_even_ask(backend):
    result = run("--quiet")

    assert result.exit_code == 0
    assert "scenario.json" not in result.output
    assert not any(r.endswith("/files") for r in backend.requests), "no request for a list nobody will see"


def test_short_flag_does_the_same(backend):
    assert "scenario.json" not in run("-q").output


def test_a_failing_listing_does_not_stop_the_run(backend):
    backend.files_status = 503

    result = run()

    assert result.exit_code == 0, result.output
    assert "Queued run run-1" in result.output
    assert "Traceback" not in result.output and "storage is down" not in result.output


def test_a_file_name_cannot_break_the_screen(backend):
    backend.files = [f("[/x]report.csv", 10, days=1), f("evil\x1b[31m.csv", 10, days=2)]

    result = run()

    assert result.exit_code == 0
    assert "[/x]report.csv" in result.output, "a bracket in a name is shown as typed"
    assert "\x1b" not in result.output, "a terminal escape in a name is not printed"


# -- scripts ---------------------------------------------------------------------------------------------------------


def test_json_keeps_stdout_a_single_document_and_the_list_goes_to_stderr(backend):
    result = split_runner.invoke(cli.app, ["run", "--json"])

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["run_id"] == "run-1"
    assert "scenario.json" in result.stderr and "scenario.json" not in result.stdout


def test_the_help_lists_the_flag():
    out = runner.invoke(cli.app, ["run", "--help"]).output

    assert "--quiet" in out and "-q" in out
