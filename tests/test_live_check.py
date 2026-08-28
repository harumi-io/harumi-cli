"""Offline guards for the live-check harness (scripts/live_check.py).

The harness itself only runs against a deployed backend, which means the part
most likely to be wrong — a mistyped flag, a missing required argument, a step
that reads an id nothing captured — would otherwise only surface mid-run, on a
real environment, after a real project had been created. These tests move all of
that to normal CI.

They also lock in the property that makes the coverage ledger meaningful: every
command in cli-surface.json is classified, so adding a command forces an
explicit decision instead of silently widening the untested gap.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from scripts.live_check import (
    CANARY,
    LOCAL,
    MANUAL,
    PLAN,
    READ,
    REPO_ROOT,
    TIERS,
    Result,
    Runner,
    Step,
    _seed_credentials,
    ledger,
    load_surface,
    validate_plan,
)


class TestPlanIsValid:
    def test_the_committed_plan_has_no_problems(self):
        """The single assertion that covers mistyped flags, missing required
        arguments, unknown commands, forward references and a missing teardown.
        Each of those failure modes is exercised individually below."""
        assert validate_plan() == []

    def test_no_helper_is_defined_twice(self):
        """Regression: `_positional_count_required` was accidentally defined
        twice; the second definition silently shadowed the first as dead code.
        A harmless duplicate today is a real bug the next time only one copy
        gets edited."""
        import ast

        source = REPO_ROOT.joinpath("scripts", "live_check.py").read_text()
        names = [node.name for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)]
        duplicates = {name for name in names if names.count(name) > 1}
        assert duplicates == set(), f"duplicate top-level function definitions: {duplicates}"

    def test_every_command_in_the_surface_is_classified(self):
        surface = set(load_surface())
        assert surface - set(TIERS) == set(), "new command(s) need a tier in live_check.TIERS"
        assert set(TIERS) - surface == set(), "TIERS names command(s) that no longer exist"

    def test_no_manual_command_is_ever_executed(self):
        """The safety property: a command classified as unsafe (emails a real
        person, deletes a real org) must not appear in the executed plan."""
        executed = {step.path for step in PLAN}
        manual = {path for path, (tier, _) in TIERS.items() if tier == MANUAL}
        assert executed & manual == set()

    def test_every_manual_command_explains_itself(self):
        for path, (tier, note) in TIERS.items():
            if tier == MANUAL:
                assert note, f"{path} is MANUAL without a reason, so the ledger can't justify the gap"

    def test_the_canary_is_created_before_anything_uses_it_and_deleted_last(self):
        paths = [step.path for step in PLAN]
        assert paths.index("projects create") < paths.index("repo put")
        deletes = [step for step in PLAN if step.path == "projects delete"]
        assert deletes, "expected at least one `projects delete` teardown step"
        for delete in deletes:
            assert delete.teardown, "delete must be a teardown step or a mid-plan failure leaks the project"

    def test_project_deletion_only_ever_targets_a_captured_id(self):
        """A hardcoded id here would delete someone's real project. `import`
        creates an independent second project, so it gets its own delete
        targeting its own captured id rather than reusing {project}."""
        deletes = [step for step in PLAN if step.path == "projects delete"]
        targets = {delete.args[0] for delete in deletes}
        assert targets == {"{project}", "{import_project}"}

    def test_compute_costing_steps_are_gated(self):
        for step in PLAN:
            if step.path in {"run", "runs get", "runs cancel", "outputs"}:
                assert step.gated, f"{step.path} costs real compute and must need --include-run"


class TestValidationActuallyCatchesMistakes:
    """A validator that cannot fail is worse than none — it reads as coverage
    while proving nothing. These pin each failure mode it is supposed to catch.
    """

    @pytest.fixture
    def patched(self, monkeypatch):
        def apply(plan=None, tiers=None):
            if plan is not None:
                monkeypatch.setattr("scripts.live_check.PLAN", plan)
            if tiers is not None:
                monkeypatch.setattr("scripts.live_check.TIERS", tiers)
            return validate_plan()

        return apply

    def test_unknown_flag_is_rejected(self, patched):
        problems = patched(plan=PLAN + (Step("whoami", ("--not-a-flag",)),))
        assert any("unknown flag --not-a-flag" in p for p in problems)

    def test_missing_required_positional_is_rejected(self, patched):
        problems = patched(plan=PLAN + (Step("projects get"),))
        assert any("positional" in p for p in problems)

    def test_missing_required_option_is_rejected(self, patched):
        # `schedules add` requires --cron.
        problems = patched(plan=PLAN + (Step("schedules add", ("--project", "p")),))
        assert any("--cron" in p for p in problems)

    def test_nonexistent_command_is_rejected(self, patched):
        problems = patched(plan=PLAN + (Step("repo teleport"),))
        assert any("not a real command" in p for p in problems)

    def test_executing_a_manual_command_is_rejected(self, patched):
        problems = patched(plan=PLAN + (Step("org delete", ("org-1", "--yes")),))
        assert any("MANUAL" in p for p in problems)

    def test_using_an_id_before_it_is_captured_is_rejected(self, patched):
        problems = patched(plan=(Step("projects get", ("{project}",)),))
        assert any("before anything captures it" in p for p in problems)

    def test_an_unclassified_command_is_reported_not_crashed(self, patched):
        """Regression: this raised KeyError instead of reporting the problem."""
        problems = patched(tiers={k: v for k, v in TIERS.items() if k != "specs"})
        assert any("not classified" in p for p in problems)

    def test_a_manual_command_without_a_reason_is_rejected(self, patched):
        problems = patched(tiers={**TIERS, "org delete": (MANUAL, "")})
        assert any("no reason" in p for p in problems)

    def test_dropping_the_teardown_is_rejected(self, patched):
        problems = patched(plan=tuple(s for s in PLAN if s.path != "projects delete"))
        assert any("leak the canary" in p for p in problems)


class TestLedger:
    def test_static_ledger_accounts_for_every_command(self):
        report = ledger()
        total = len(report["covered"]) + len(report["manual"]) + len(report["gap"])
        assert total == len(TIERS)

    def test_the_committed_plan_leaves_no_unexplained_gap(self):
        """Anything automatable but uncovered is a real gap. Closing it means
        either planning a step or reclassifying with a stated reason."""
        assert ledger()["gap"] == []

    def test_a_partial_run_reports_the_shortfall(self):
        report = ledger({"whoami", "specs"})
        assert report["covered"] == ["specs", "whoami"]
        assert any("projects list" in line for line in report["gap"])


class TestRunnerSubstitution:
    def _runner(self, tmp_path, **kwargs):
        return Runner(
            binary="harumi",
            env_name="staging",
            workdir=tmp_path,
            tmpdir=tmp_path,
            include_run=False,
            **kwargs,
        )

    def test_a_missing_dependency_skips_instead_of_sending_a_literal_brace(self, tmp_path):
        """Without this, a failed capture would send the literal '{project}' to
        the backend and the failure would look like an API bug."""
        runner = self._runner(tmp_path)
        result = runner.execute(Step("projects get", ("{project}",)))
        assert result.status == "skip"
        assert "unresolved" in result.detail

    def test_gated_steps_are_skipped_without_include_run(self, tmp_path):
        runner = self._runner(tmp_path)
        assert runner.execute(Step("run", gated=True)).status == "skip"

    def test_the_project_id_is_read_from_the_binding_not_scraped_from_output(self, tmp_path):
        """The .harumi binding is a contract; the printed line is cosmetic."""
        (tmp_path / ".harumi").mkdir()
        (tmp_path / ".harumi" / "config.json").write_text(json.dumps({"project_id": "bound-id-123"}))
        runner = self._runner(tmp_path)
        captured = runner._capture(Step("projects create", capture="project"), "Created project (id=printed-id)")
        assert captured == "bound-id-123"

    def test_a_uuid_is_recovered_from_output_when_there_is_no_binding(self, tmp_path):
        runner = self._runner(tmp_path)
        captured = runner._capture(
            Step("schedules add", capture="schedule"),
            "Created schedule 3f8b1c2d-4e5a-6b7c-8d9e-0f1a2b3c4d5e (cron=0 3 * * *, UTC).",
        )
        assert captured == "3f8b1c2d-4e5a-6b7c-8d9e-0f1a2b3c4d5e"

    def test_run_captures_the_run_id_not_the_execution_log_id(self, tmp_path):
        """`harumi run` prints execution_log_id first and run_id second, but
        `runs get` only accepts the latter. Taking "the first UUID" here would
        404 on a live backend — a failure that cannot show up offline.
        """
        step = next(s for s in PLAN if s.path == "run")
        stdout = (
            "Queued (execution_log_id=aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa, "
            "run_id=bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb). status=queued"
        )
        assert self._runner(tmp_path)._capture(step, stdout) == "bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb"

    def test_execute_detaches_the_child_so_hidden_prompts_read_stdin_not_the_tty(self, tmp_path, monkeypatch):
        """Regression: `secrets set` / `share set-password` prompt via
        getpass.getpass(), which opens /dev/tty directly and ignores a piped
        stdin unless the child has no controlling terminal. Without
        start_new_session=True this hangs forever instead of reading the
        password the harness already supplies via `input=`."""
        captured_kwargs = {}

        def fake_run(argv, **kwargs):
            captured_kwargs.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        monkeypatch.setattr("scripts.live_check.subprocess.run", fake_run)
        self._runner(tmp_path).execute(Step("whoami"))
        assert captured_kwargs.get("start_new_session") is True

    def test_two_canary_names_generated_in_the_same_second_do_not_collide(self):
        """A bare timestamp collides if two runs start in the same second (e.g.
        a scheduled job overlapping a manual run) — `projects create` would
        then fail on a name clash that looks like a backend bug."""
        from scripts.live_check import _canary_name

        assert len({_canary_name() for _ in range(50)}) == 50

    def test_a_malformed_binding_falls_back_to_scraping_a_uuid_instead_of_crashing(self, tmp_path):
        """A half-written or corrupt .harumi/config.json must not take down the
        whole harness — fall through to the UUID scrape like there was no
        binding at all."""
        (tmp_path / ".harumi").mkdir()
        (tmp_path / ".harumi" / "config.json").write_text("{not valid json")
        runner = self._runner(tmp_path)
        captured = runner._capture(
            Step("projects create", capture="project"),
            "Created project (id=11111111-2222-3333-4444-555555555555).",
        )
        assert captured == "11111111-2222-3333-4444-555555555555"

    def test_teardown_runs_even_when_a_main_step_raises(self, tmp_path, monkeypatch):
        """Regression: run_plan()'s main loop had no try/finally, so a
        KeyboardInterrupt (or any other exception) during the main steps would
        skip straight past `projects delete` / `logout` and leak the canary
        project on the real backend."""
        executed: list[str] = []

        def fake_execute(step):
            executed.append(step.path)
            if step.path == "repo put":
                raise KeyboardInterrupt
            return Result(step, [], "ok")

        runner = self._runner(tmp_path)
        monkeypatch.setattr(runner, "execute", fake_execute)
        with pytest.raises(KeyboardInterrupt):
            runner.run_plan()

        assert "projects delete" in executed
        assert "logout" in executed

    def test_seed_git_repo_leaves_a_committed_head_the_cli_can_push_from(self, tmp_path):
        """Regression: `init` silently skips remote setup, and `run`'s dirty/
        unpushed check errors outright, when the bound directory isn't a real
        git repo — which an empty tempdir never is. `_seed_git_repo` must
        leave a workdir indistinguishable (for git's purposes) from a real
        checkout: a repo with a HEAD commit, so `push_scratch`'s `commit-tree
        -p HEAD` has a parent to attach to."""
        runner = self._runner(tmp_path)
        runner._seed_git_repo()

        assert (tmp_path / ".git").is_dir()
        assert (tmp_path / "main.py").exists()
        head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        assert head.stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        assert status.stdout.strip() == ""

    def test_seed_import_folder_has_no_project_dependency(self, tmp_path):
        """`import` never takes --project — it always creates a new project —
        so its input folder must be independently buildable with no {project}
        substitution, unlike every other CANARY step."""
        runner = self._runner(tmp_path)
        folder = runner._seed_import_folder()
        assert folder.is_dir()
        assert (folder / "main.py").exists()


class TestLoadSurface:
    def test_a_missing_cli_surface_json_gives_an_actionable_message_not_a_traceback(self, monkeypatch, tmp_path):
        monkeypatch.setattr("scripts.live_check.SURFACE_PATH", tmp_path / "missing.json")
        with pytest.raises(SystemExit, match="Run: python scripts/emit_cli_surface.py"):
            load_surface()

    def test_a_malformed_cli_surface_json_gives_an_actionable_message_not_a_traceback(self, monkeypatch, tmp_path):
        bad = tmp_path / "cli-surface.json"
        bad.write_text("{not valid json")
        monkeypatch.setattr("scripts.live_check.SURFACE_PATH", bad)
        with pytest.raises(SystemExit, match="malformed"):
            load_surface()


class TestSeedCredentials:
    """Every file this writes holds a live session token, so permissions are a
    security property, not a nice-to-have."""

    def _staged_source(self, tmp_path, env_name="staging", with_org=True):
        source_dir = tmp_path / "source" / "environments" / env_name
        source_dir.mkdir(parents=True)
        (source_dir / "credentials.json").write_text(json.dumps({"access_token": "a", "refresh_token": "r"}))
        if with_org:
            (source_dir / "config.json").write_text(json.dumps({"org_id": "org-1"}))
        return tmp_path / "source"

    def test_copying_an_existing_session_locks_down_credentials_permissions(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HARUMI_HOME", str(self._staged_source(tmp_path)))
        monkeypatch.delenv("HARUMI_LIVE_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("HARUMI_LIVE_REFRESH_TOKEN", raising=False)

        home = tmp_path / "sandbox-home"
        _seed_credentials(home, "staging")

        target = home / "environments" / "staging" / "credentials.json"
        assert target.exists()
        assert oct(target.stat().st_mode)[-3:] == "600"

    def test_copying_the_org_config_also_locks_down_permissions(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HARUMI_HOME", str(self._staged_source(tmp_path)))
        monkeypatch.delenv("HARUMI_LIVE_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("HARUMI_LIVE_REFRESH_TOKEN", raising=False)

        home = tmp_path / "sandbox-home"
        _seed_credentials(home, "staging")

        target = home / "environments" / "staging" / "config.json"
        assert target.exists()
        assert oct(target.stat().st_mode)[-3:] == "600"


class TestTierSanity:
    def test_read_and_local_tiers_never_mutate(self):
        """Tier names are load-bearing: READ/LOCAL is the promise that makes
        running against production acceptable."""
        mutating_verbs = {"create", "delete", "remove", "add", "set", "rm", "put", "mv", "rename", "update"}
        for path, (tier, _) in TIERS.items():
            if tier in {READ, LOCAL}:
                leaf = path.split()[-1]
                assert leaf not in mutating_verbs or tier == LOCAL, (
                    f"{path} is classified {tier} but its verb suggests it mutates"
                )

    def test_canary_steps_all_scope_themselves_to_a_project(self):
        """A canary-tier command with no project scope would act on the account
        at large."""
        exempt = {"projects create", "import", "init", "config set-org", "env use", "run", "outputs"}
        for step in PLAN:
            if TIERS[step.path][0] != CANARY or step.path in exempt:
                continue
            assert (
                "{project}" in step.args
                or "{share}" in step.args
                or "{schedule}" in step.args
                or "{import_project}" in step.args
            ), f"{step.path} mutates without scoping to the canary"


@pytest.mark.live
def test_the_cli_works_against_a_deployed_backend():
    """The only test here that touches a network.

    Deselected by default (`addopts = -m 'not live'`); run it deliberately:

        pytest -m live                       # staging
        HARUMI_LIVE_ENV=production pytest -m live

    Failures name the command and the backend's own error message. Prefer
    `python scripts/live_check.py` for day-to-day use — it prints the coverage
    ledger, which this wrapper does not.
    """
    import os

    from scripts.live_check import main

    env = os.environ.get("HARUMI_LIVE_ENV", "staging")
    argv = ["--env", env] + (["--allow-prod"] if env == "production" else [])
    assert main(argv) == 0, "see the PASS/FAIL lines above for the failing command"
