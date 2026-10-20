"""`harumi specs` and `harumi usage`: readable, and each says what to do next.

Driven against a fake API with real payload shapes, at 80 columns (the width of a
default terminal), because the first thing wrong with `specs` was that its table was
cut to fit.
"""

from __future__ import annotations

import re

import httpx
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from harumi import cli, config
from harumi.client import Client

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A throwaway home: these tests must not read, or depend on, the real ~/.harumi."""
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    # The console was built at import with the suite's wide terminal; narrow it for real.
    monkeypatch.setattr(cli.console, "_width", 80)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


def spec(name, label, cpu, memory, *, gpu=False, paid=False):
    return {
        "name": name, "display_name": label, "language": "python", "description": label,
        "size": {"name": "x", "cpu": cpu, "memory": memory, "gpu": gpu}, "subscription_required": paid,
    }


SPECS = [
    spec("or_python_small", "Python (Small)", 1, "2Gi"),
    spec("or_python_medium", "Python (Medium)", 2.0, "8Gi"),
    spec("gurobi_python_large_gpu_extra", "Gurobi + Python (Large)", 8, "32Gi", paid=True),
    spec("gpu_torch", "PyTorch (GPU)", 4, "16Gi", gpu=True, paid=True),
]


def usage_body(**over):
    body = {
        "billing_account_id": "acct-1", "plan_code": "pro", "tier": "pro", "balance_credits": 200_000,
        "included_credits": 396_000, "period_start": "2026-10-01T00:00:00+00:00",
        "period_end": "2026-11-01T00:00:00+00:00", "weekly_used_pct": 48.2, "monthly_used_pct": 16.0,
        "weekly_resets_at": "2026-10-20T15:00:00+00:00", "monthly_resets_at": "2026-11-01T00:00:00+00:00",
        "overage_allowed": True, "overage_enabled": False, "overage_cap_cents": 0, "overage_spent_cents": 0,
        "overage_cap_credits": 0, "entries": [],
    }
    body.update(over)
    return body


ENTITLEMENT = {
    "paywall_enabled": True, "is_harumi": False, "any_active": True, "organizations": [],
    "personal": {"customer_id": None, "active": True, "tier": "pro", "reason": None},
}


@pytest.fixture
def api(monkeypatch):
    state = {"specs": SPECS, "usage": usage_body()}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sandbox/specs"):
            return httpx.Response(200, json=state["specs"])
        if path.endswith("/billing/usage"):
            return httpx.Response(200, json=state["usage"])
        if path.endswith("/billing/entitlement/me"):
            return httpx.Response(200, json=ENTITLEMENT)
        return httpx.Response(404, json={"detail": "not routed"})

    monkeypatch.setattr(
        cli, "_get_client", lambda *a, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(handler))
    )
    return state


def plain(text: str) -> str:
    return " ".join(text.split())


# -- specs ------------------------------------------------------------------------------------------


def test_no_kernel_name_is_cut_off_at_80_columns(api):
    """A kernel name is what someone copies into --kernel; a name ending in an ellipsis is useless."""
    out = runner.invoke(cli.app, ["specs"]).output

    for spec_ in SPECS:
        assert spec_["name"] in out, f"{spec_['name']} was cut off:\n{out}"
    assert "…" not in out


def test_the_headers_are_words_not_truncated_field_names(api):
    out = runner.invoke(cli.app, ["specs"]).output
    header = out.splitlines()[1]

    for word in ("name", "cpu", "memory", "gpu", "plan", "label"):
        assert word in header, header
    assert "subscription" not in out and "display_name" not in out  # raw field names, and cut-off ones


def table_rows(out: str) -> dict[str, list[str]]:
    """name -> the other cells, for each row that starts a new kernel (wrapped lines are skipped)."""
    rows = {}
    for line in out.splitlines():
        cells = [c.strip() for c in line.strip("│ ").split("│")]
        if len(cells) > 1 and cells[0]:
            rows[cells[0]] = cells[1:]
    return rows


def test_cpu_is_a_plain_number_and_a_gpu_kernel_says_so(api):
    header = next(l for l in runner.invoke(cli.app, ["specs"]).output.splitlines() if "name" in l and "cpu" in l)
    columns = [c.strip() for c in header.strip("┃ ").split("┃")]
    rows = table_rows(runner.invoke(cli.app, ["specs"]).output)
    cell = lambda kernel, column: rows[kernel][columns.index(column) - 1]  # noqa: E731

    assert cell("or_python_small", "cpu") == "1" and cell("or_python_medium", "cpu") == "2"  # not 1.0 / 2.0
    assert cell("gpu_torch", "gpu") == "yes" and cell("or_python_small", "gpu") == "no"
    assert cell("gpu_torch", "plan") == "paid" and cell("or_python_small", "plan") == "any"


def test_it_ends_by_saying_how_to_use_a_kernel(api):
    out = plain(runner.invoke(cli.app, ["specs"]).output)

    assert "harumi run --kernel <name>" in out


def test_no_specs_says_so_instead_of_printing_an_empty_table(api):
    api["specs"] = []

    result = runner.invoke(cli.app, ["specs"])

    assert result.exit_code == 0 and "No kernels" in result.output


@pytest.mark.parametrize("path", [["run"], ["schedules", "add"], ["schedules", "update"]])
def test_every_kernel_option_says_where_the_names_come_from(path):
    out = plain(runner.invoke(cli.app, [*path, "--help"]).output)

    assert "harumi specs" in out, f"harumi {' '.join(path)} --kernel does not point at `harumi specs`"


# -- usage ------------------------------------------------------------------------------------------


def test_reaching_a_limit_does_not_tell_you_to_run_usage_from_usage(api):
    api["usage"] = usage_body(weekly_used_pct=104.0)

    out = plain(runner.invoke(cli.app, ["usage"]).output)

    assert "reached a limit" in out
    assert "Run harumi usage" not in out and "run harumi usage" not in out.lower()


def test_the_limit_hint_offers_overage_only_where_the_plan_has_it(api):
    api["usage"] = usage_body(weekly_used_pct=104.0, overage_allowed=True)
    with_overage = plain(runner.invoke(cli.app, ["usage"]).output)
    api["usage"] = usage_body(weekly_used_pct=104.0, overage_allowed=False)
    without = plain(runner.invoke(cli.app, ["usage"]).output)

    assert "overage" in with_overage.split("reached a limit")[1]
    assert "overage" not in without.split("reached a limit")[1] and "upgrade" in without.lower()


def test_a_plan_the_cli_has_no_label_for_is_printed_as_a_name(api):
    api["usage"] = usage_body(tier="trial", plan_code="trial", overage_allowed=False)

    out = plain(runner.invoke(cli.app, ["usage"]).output)

    assert "Plan: Trial" in out and "Plan: trial" not in out


def test_an_unknown_multi_word_plan_is_readable_too(api):
    api["usage"] = usage_body(tier="team_plus", plan_code="team_plus")

    assert "Plan: Team Plus" in plain(runner.invoke(cli.app, ["usage"]).output)


def test_known_plans_keep_their_labels(api):
    assert "Plan: Pro" in plain(runner.invoke(cli.app, ["usage"]).output)
    assert cli._tier_label("harumi_io") == "harumi-io" and cli._tier_label(None) == "none"


# -- the skill reference must keep up with the commands -----------------------------------------------------


def reference_section(heading: str, until: str) -> str:
    import pathlib

    ref = (pathlib.Path(__file__).resolve().parents[1] / ".agents/skills/harumi-cli/references/commands.md").read_text(encoding="utf-8")
    start = ref.index(heading)
    return ref[start : ref.index(until, start + len(heading))]


@pytest.mark.parametrize(
    "command, heading, until",
    [("specs", "## `specs`", "## `blueprints`"), ("usage", "## `usage`", "## `specs`"), ("compare", "## `compare`", "## Editing the repo")],
)
def test_every_option_of_these_commands_is_in_the_skill_reference(command, heading, until):
    """An agent reads the reference to learn what the CLI can do; an option missing from it is one it
    will not use. (An earlier edit to this file was lost without anyone noticing.)"""
    section = reference_section(heading, until)
    params = get_command(cli.app).commands[command].params
    options = [o for p in params for o in getattr(p, "opts", []) if o.startswith("--") and o != "--help"]

    assert options, "the parse found no options, so this test would prove nothing"
    assert [o for o in options if o not in section] == []


def test_the_reference_describes_the_new_behaviour_not_the_old():
    compare = reference_section("## `compare`", "## Editing the repo")
    specs = reference_section("## `specs`", "## `blueprints`")

    assert "never prompts" in compare and "RUN_ID" in compare and "harumi runs download" in compare
    assert "subscription_required" not in specs and "display_name" not in specs  # the old column names
