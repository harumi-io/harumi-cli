"""Update notice + changelog. Pure: a fake clock, fake stream and fake fetch, so
nothing touches the network or the real ~/.harumi."""

from __future__ import annotations

import io

import httpx
import pytest
from typer.testing import CliRunner

import harumi.cli as cli
from harumi import changelog, update

DAY = 24 * 60 * 60


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv(update.DISABLE_ENV, raising=False)


def run_check(argv=("run",), *, fetch=lambda: "0.12.0", now=1_000_000.0, current="0.11.0", environ=None, stream=None):
    out = []
    check = update.UpdateCheck(
        list(argv),
        environ=environ or {},
        stream=stream or Tty(),
        fetch=fetch,
        clock=lambda: now,
        current=current,
        printer=out.append,
    )
    check.start()
    check.finish()
    return out


# -- versions -----------------------------------------------------------------


@pytest.mark.parametrize(
    "latest, current, expected",
    [("0.12.0", "0.11.0", True), ("0.11.0", "0.11.0", False), ("0.10.9", "0.11.0", False), ("1.0.0", "0.99.9", True),
     ("0.12.0rc1", "0.11.0", False), ("garbage", "0.11.0", False), ("0.11.0.1", "0.11.0", True)],
)
def test_is_newer(latest, current, expected):
    assert update.is_newer(latest, current) is expected


# -- when a notice is allowed at all -------------------------------------------


def test_a_newer_release_is_announced_with_an_upgrade_command():
    (message,) = run_check()
    assert "0.11.0 -> [bold]0.12.0" in message
    assert "harumi whats-new" in message
    assert update.upgrade_command() in message


@pytest.mark.parametrize(
    "kwargs",
    [
        {"environ": {update.DISABLE_ENV: "1"}},
        {"environ": {"CI": "true"}},
        {"argv": ("--version",)},
        {"argv": ("whats-new",)},
        {"stream": io.StringIO()},  # not a terminal: piped or redirected
    ],
)
def test_it_stays_silent_when_it_would_pollute_a_script_or_a_pipe(kwargs):
    assert run_check(**kwargs) == []


def test_up_to_date_prints_nothing():
    assert run_check(fetch=lambda: "0.11.0") == []


def test_a_failed_lookup_prints_nothing_and_is_not_retried_for_a_day():
    calls = []

    def fail():
        calls.append(1)
        return None

    assert run_check(fetch=fail) == []
    assert run_check(fetch=fail, now=1_000_000.0 + 60) == []
    assert len(calls) == 1
    run_check(fetch=fail, now=1_000_000.0 + DAY + 1)
    assert len(calls) == 2


def test_a_fetch_that_raises_never_breaks_the_command():
    def boom():
        raise RuntimeError("no network")

    assert run_check(fetch=boom) == []


# -- how often ----------------------------------------------------------------


def test_pypi_is_asked_at_most_once_a_day():
    calls = []

    def fetch():
        calls.append(1)
        return "0.12.0"

    run_check(fetch=fetch)
    run_check(fetch=fetch, now=1_000_000.0 + 3600)
    run_check(fetch=fetch, now=1_000_000.0 + DAY - 1)
    assert len(calls) == 1


def test_the_same_notice_repeats_at_most_once_a_day():
    assert len(run_check()) == 1
    assert run_check(now=1_000_000.0 + 3600) == []  # same day: quiet
    assert len(run_check(now=1_000_000.0 + DAY + 5)) == 1  # a day later: a reminder


def test_a_still_newer_release_is_announced_immediately():
    assert len(run_check(fetch=lambda: "0.12.0")) == 1
    # Next day PyPI has 0.13.0: announce right away, even inside the remind window.
    state = update.load_state()
    state["checked_at"] = 0  # force a refresh
    update.save_state(state)
    (message,) = run_check(fetch=lambda: "0.13.0", now=1_000_000.0 + 60)
    assert "0.13.0" in message


# -- just upgraded ------------------------------------------------------------


def test_first_ever_run_records_the_version_silently():
    assert run_check(fetch=lambda: "0.11.0") == []
    assert update.load_state()["last_seen_version"] == "0.11.0"


def test_after_an_upgrade_it_says_so_once_and_points_at_whats_new():
    update.save_state({"last_seen_version": "0.10.0", "checked_at": 1_000_000.0})
    (message,) = run_check(fetch=lambda: "0.11.0")
    assert "0.10.0 -> [bold]0.11.0" in message
    assert "harumi whats-new" in message
    assert run_check(fetch=lambda: "0.11.0") == []


# -- state-file robustness ----------------------------------------------------


def test_a_corrupt_state_file_is_ignored(tmp_path):
    (tmp_path / "update-check.json").write_text("{not json")
    assert len(run_check()) == 1


def test_an_unwritable_home_does_not_break_anything(monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", update.Path("/proc/definitely/not/writable"))
    run_check()  # must simply not raise


# -- PyPI lookup --------------------------------------------------------------


def test_fetch_latest_reads_the_version_from_pypi(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json={"info": {"version": "0.12.0"}}, request=httpx.Request("GET", "https://x")))
    assert update.fetch_latest() == "0.12.0"


@pytest.mark.parametrize("response", [httpx.Response(500, request=httpx.Request("GET", "https://x")),
                                      httpx.Response(200, json={"nope": 1}, request=httpx.Request("GET", "https://x")),
                                      httpx.Response(200, json={"info": {"version": "1.0.0rc1"}}, request=httpx.Request("GET", "https://x"))])
def test_fetch_latest_returns_none_for_anything_unusable(monkeypatch, response):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: response)
    assert update.fetch_latest() is None


def test_fetch_latest_returns_none_when_offline(monkeypatch):
    def offline(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", offline)
    assert update.fetch_latest() is None


@pytest.mark.parametrize("prefix, expected", [("/home/u/.local/pipx/venvs/harumi", "pipx upgrade harumi"),
                                              ("/home/u/.local/share/uv/tools/harumi", "uv tool upgrade harumi"),
                                              ("/usr/lib/python3", "python3 -m pip install --upgrade harumi")])
def test_upgrade_command_matches_how_it_was_installed(monkeypatch, prefix, expected):
    monkeypatch.setattr(update.sys, "prefix", prefix)
    assert update.upgrade_command() == expected


# -- changelog + command ------------------------------------------------------


def test_the_bundled_changelog_covers_the_installed_version():
    versions = [r.version for r in changelog.load()]
    assert update.__version__ in versions
    assert all(r.notes for r in changelog.load())


def test_changelog_is_newest_first():
    parsed = [update.parse_version(r.version) for r in changelog.load()]
    assert parsed == sorted(parsed, reverse=True)


def test_whats_new_prints_the_installed_versions_notes_and_all_prints_history():
    runner = CliRunner()
    one = runner.invoke(cli.app, ["whats-new"])
    everything = runner.invoke(cli.app, ["whats-new", "--all"])

    assert one.exit_code == 0, one.output
    assert f"harumi {update.__version__}" in one.output
    assert "--all" in one.output
    assert "harumi 0.10.0" not in one.output
    assert "harumi 0.10.0" in everything.output


@pytest.mark.parametrize(
    "argv, active",
    [
        (["--env", "staging", "whats-new"], False),  # the value of --env is not the command
        (["whats-new", "--all"], False),
        (["--env", "staging", "--version"], False),
        (["run", "--version", "x"], True),  # a --version after the command isn't the global flag
        (["--env", "whats-new", "run"], True),  # "whats-new" here is the env's value
    ],
)
def test_command_and_global_flag_detection_uses_position_not_substrings(argv, active):
    assert update.enabled(argv, {}, Tty()) is active
