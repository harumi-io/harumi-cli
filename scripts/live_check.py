#!/usr/bin/env python3
"""Drive the real `harumi` CLI against a deployed backend, and report what that
leaves untested.

Why this exists
---------------
The pytest suite is entirely offline: every request goes through
`httpx.MockTransport`, which proves the CLI *builds* the right request but never
that a deployed backend *accepts* it. A mocked 200 and a real 200 are not the
same claim. Nothing in CI has ever called a live endpoint, so a command could
ship broken against production and every check would stay green.

Three separate things live in this module, on purpose:

* ``TIERS`` classifies every command in ``cli-surface.json`` by whether it can
  be driven against a live backend at all. A command that cannot — it emails a
  real person, needs a customer's database, needs a one-time code from a real
  mailbox — is recorded *with the reason*. That turns "untested" from an
  oversight into a documented decision.
* ``PLAN`` is the ordered list of steps actually executed, against a single
  disposable "canary" project that the harness creates and then deletes.
* ``ledger()`` diffs the two, so every run says which of the 70 commands it
  actually covered and which it did not.

Both TIERS and PLAN are plain data, and ``tests/test_live_check.py`` validates
them against ``cli-surface.json`` *offline*: every command classified, every
planned step a real command, every required argument supplied, every flag a real
flag. That split is the whole point — a live run needs credentials and (for
staging) the VPN, so the part most likely to be wrong has to be checkable
without either.

Usage
-----
    python scripts/live_check.py                     # staging (default)
    python scripts/live_check.py --plan              # print the plan, run nothing
    python scripts/live_check.py --include-run       # also queue a real solver run
    python scripts/live_check.py --env production --allow-prod

Targeting production is deliberately two flags: it creates real rows in a real
workspace, and `--include-run` there burns real sandbox compute.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SURFACE_PATH = REPO_ROOT / "cli-surface.json"

# Tiers -----------------------------------------------------------------------
# LOCAL   no network at all; safe anywhere, proves the command wires up
# READ    read-only request; safe against any environment including production
# CANARY  mutates, but only ever inside the disposable project we own
# MANUAL  cannot be driven against a live backend; the reason is mandatory
LOCAL = "local"
READ = "read"
CANARY = "canary"
MANUAL = "manual"

# Every command path in cli-surface.json -> (tier, note).
# A note is required for MANUAL and explains what blocks automation; for the
# other tiers it is optional colour. tests/test_live_check.py fails if this
# mapping and cli-surface.json disagree by even one command, so adding a
# command forces a decision here rather than silently widening the gap.
TIERS: dict[str, tuple[str, str]] = {
    # -- session / environment ------------------------------------------------
    "login": (MANUAL, "needs a one-time code from a real mailbox; there is no --code flag"),
    "logout": (LOCAL, "clears the throwaway credentials, so it runs last"),
    "whoami": (READ, "cannot be faked by a same-named binary, so it proves the session"),
    "profile show": (READ, ""),
    "profile set": (MANUAL, "would mutate the real signed-in user's name/bio"),
    "env list": (LOCAL, ""),
    "env current": (LOCAL, ""),
    "env use": (LOCAL, "writes only the throwaway HARUMI_HOME config"),
    "config set-org": (LOCAL, "writes only the throwaway HARUMI_HOME config"),
    # -- discovery ------------------------------------------------------------
    "specs": (READ, "proves real API reachability"),
    "templates": (READ, ""),
    "notebooks": (READ, ""),
    "outputs": (READ, ""),
    # -- organizations --------------------------------------------------------
    "org list": (READ, ""),
    "org members": (READ, ""),
    "org create": (MANUAL, "creates a real, billable organization"),
    "org delete": (MANUAL, "destroys a real organization and everything in it"),
    "org rename": (MANUAL, "renames a real organization other people can see"),
    "org invite": (MANUAL, "sends email to a real person"),
    "org remove": (MANUAL, "removes a real member's access"),
    "org role": (MANUAL, "changes a real member's permissions"),
    # -- projects -------------------------------------------------------------
    "projects list": (READ, ""),
    "projects get": (READ, ""),
    "projects create": (CANARY, "creates the canary project the rest of the plan uses"),
    "projects rename": (CANARY, ""),
    "projects delete": (CANARY, "teardown; only ever the canary id"),
    "init": (CANARY, "binds a throwaway directory to the canary"),
    "import": (CANARY, "creates a project from a throwaway folder, so it is self-contained like `projects create`"),
    # -- repo -----------------------------------------------------------------
    "repo ls": (READ, ""),
    "repo dir": (READ, ""),
    "repo cat": (READ, ""),
    "repo download": (READ, ""),
    "repo branches": (READ, ""),
    "repo put": (CANARY, ""),
    "repo mv": (CANARY, ""),
    "repo rm": (CANARY, ""),
    "repo branch-create": (CANARY, ""),
    "repo branch-rm": (CANARY, ""),
    "repo promote": (CANARY, ""),
    # -- runs -----------------------------------------------------------------
    "run": (CANARY, "queues real sandbox compute, so it is behind --include-run"),
    "runs list": (READ, ""),
    "runs get": (READ, ""),
    "runs cancel": (CANARY, "only cancels the run this harness queued"),
    # -- schedules ------------------------------------------------------------
    "schedules list": (READ, ""),
    "schedules get": (READ, ""),
    "schedules add": (CANARY, ""),
    "schedules update": (CANARY, ""),
    "schedules remove": (CANARY, ""),
    # -- secrets --------------------------------------------------------------
    "secrets list": (READ, ""),
    "secrets set": (CANARY, ""),
    "secrets rm": (CANARY, ""),
    # -- share links ----------------------------------------------------------
    "share list": (READ, ""),
    "share get": (READ, ""),
    "share add": (CANARY, ""),
    "share update": (CANARY, ""),
    "share rotate": (CANARY, ""),
    "share set-password": (CANARY, ""),
    "share rm-password": (CANARY, ""),
    "share remove": (CANARY, ""),
    # -- datasources ----------------------------------------------------------
    # The whole group needs a database the backend can actually reach. Creating
    # one against production would also mean putting real credentials in CI.
    "datasources list": (READ, ""),
    "datasources get": (MANUAL, "needs a datasource to already exist; the canary project has none"),
    "datasources add": (MANUAL, "needs a reachable customer database, and mTLS certs when proxied"),
    "datasources update": (MANUAL, "would mutate a real datasource"),
    "datasources remove": (MANUAL, "would delete a real datasource"),
    "datasources test": (MANUAL, "needs a reachable customer database"),
    "datasources query": (MANUAL, "needs an existing datasource with a live database behind it"),
    # -- local tooling --------------------------------------------------------
    "dashboard widgets": (LOCAL, ""),
    "dashboard validate": (LOCAL, ""),
    "skill install": (LOCAL, "run with --dry-run so it never writes real skill dirs"),
    "skill path": (LOCAL, ""),
}


# The plan --------------------------------------------------------------------

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


@dataclass(frozen=True)
class Step:
    """One command invocation.

    `args` holds the argv *after* the command words, and may contain
    `{placeholders}` filled from ids captured earlier in the run (see
    `Runner._resolve`). Keeping a step as data rather than a function call is
    what lets the offline tests check it against cli-surface.json.
    """

    path: str
    args: tuple[str, ...] = ()
    stdin: Optional[str] = None
    capture: Optional[str] = None
    # Where to find the id in stdout. Defaults to "the first UUID printed",
    # which is unambiguous on a fresh canary. Set it when a command prints
    # several ids and the first one is the wrong one — `run` prints
    # execution_log_id before the run_id that `runs get` actually wants.
    capture_re: Optional[str] = None
    # True for steps that cost real compute; skipped unless --include-run.
    gated: bool = False
    # Teardown steps run even when an earlier step failed.
    teardown: bool = False


# Ordered. Dependencies are real: `runs get` needs a run id, `repo cat` needs a
# file that an earlier `repo put` created, and everything after `projects create`
# needs the canary id.
PLAN: tuple[Step, ...] = (
    # -- preflight: no project needed ----------------------------------------
    Step("env current"),
    Step("env list", ("--all",)),
    Step("whoami"),
    Step("specs"),
    Step("templates"),
    Step("profile show"),
    Step("org list"),
    # Skipped automatically for a personal-workspace session, where {org} is empty.
    Step("org members", ("{org}",)),
    Step("projects list"),
    Step("dashboard widgets"),
    Step("dashboard validate", ("{spec}",)),
    Step("skill path"),
    Step("skill install", ("--dry-run",)),
    # -- create the canary ----------------------------------------------------
    # --bind writes .harumi/config.json in the temp cwd, which is both how we
    # recover the project id without scraping output and what lets the
    # binding-dependent commands (`run`, `outputs`) work with no --project.
    Step("projects create", ("{canary}",), capture="project"),
    Step("projects get", ("{project}",)),
    Step("projects rename", ("{project}", "{canary}-renamed")),
    Step("init", ("--project", "{project}")),
    # -- repo: write, read, move, branch, promote -----------------------------
    Step("repo branches", ("--project", "{project}")),
    Step("repo ls", ("--project", "{project}")),
    Step("repo put", ("{seed}", "main.py", "--project", "{project}", "-m", "livecheck: seed entrypoint")),
    Step("repo put", ("{seed}", "livecheck/hello.py", "--project", "{project}", "-m", "livecheck: add")),
    Step("repo cat", ("livecheck/hello.py", "--project", "{project}")),
    Step("repo dir", ("livecheck", "--project", "{project}")),
    Step("repo mv", ("livecheck/hello.py", "livecheck/renamed.py", "--project", "{project}", "-m", "livecheck: mv")),
    Step("repo branch-create", ("livecheck-branch", "--project", "{project}")),
    # Give the branch a commit of its own, so `promote` has a real diff to
    # merge rather than erroring on an empty one.
    Step(
        "repo put",
        ("{seed}", "livecheck/on-branch.py", "--branch", "livecheck-branch", "--project", "{project}", "-m", "livecheck: branch commit"),
    ),
    Step("repo promote", ("livecheck-branch", "--project", "{project}", "--title", "livecheck promote")),
    Step("repo branch-rm", ("livecheck-branch", "--yes", "--project", "{project}")),
    Step("repo rm", ("livecheck/renamed.py", "--yes", "--project", "{project}", "-m", "livecheck: rm")),
    Step("repo download", ("--output", "{tmp}/repo.zip", "--project", "{project}")),
    Step("notebooks", ("--project", "{project}")),
    # -- import: turns a plain folder into its own second project ------------
    # Independent of the canary project above — `import` never takes
    # --project, it always creates a new one — so it gets its own capture
    # key and its own teardown delete instead of reusing {project}.
    Step("import", ("{import_folder}", "--project-name", "{canary}-import"), capture="import_project"),
    # -- secrets --------------------------------------------------------------
    Step("secrets set", ("LIVECHECK_TOKEN", "--project", "{project}"), stdin="livecheck-value\n"),
    Step("secrets list", ("--project", "{project}")),
    Step("secrets rm", ("LIVECHECK_TOKEN", "--yes", "--project", "{project}")),
    # -- schedules ------------------------------------------------------------
    Step("schedules add", ("--cron", "0 3 * * *", "--project", "{project}"), capture="schedule"),
    Step("schedules list", ("--project", "{project}")),
    Step("schedules get", ("{schedule}", "--project", "{project}")),
    Step("schedules update", ("{schedule}", "--cron", "0 4 * * *", "--project", "{project}")),
    Step("schedules remove", ("{schedule}", "--yes", "--project", "{project}")),
    # -- share links ----------------------------------------------------------
    Step("share add", ("--label", "livecheck", "--project", "{project}"), capture="share"),
    Step("share list", ("--project", "{project}")),
    Step("share get", ("{share}", "--project", "{project}")),
    Step("share update", ("{share}", "--label", "livecheck-updated", "--project", "{project}")),
    Step("share set-password", ("{share}", "--project", "{project}"), stdin="livecheck-pw\nlivecheck-pw\n"),
    Step("share rm-password", ("{share}", "--project", "{project}")),
    Step("share rotate", ("{share}", "--yes", "--project", "{project}")),
    Step("share remove", ("{share}", "--yes", "--project", "{project}")),
    # -- datasources (read-only half) -----------------------------------------
    Step("datasources list", ("--project", "{project}")),
    # -- runs -----------------------------------------------------------------
    Step("runs list", ("--project", "{project}")),
    # `run` has no --project: it reads the .harumi binding, which is why the
    # harness runs from the bound temp directory. It prints execution_log_id
    # *before* run_id, and `runs get` wants the latter — hence the explicit
    # pattern instead of "first UUID wins".
    Step(
        "run",
        ("--command", "python main.py"),
        capture="run",
        capture_re=r"run_id=([0-9a-fA-F-]{36})",
        gated=True,
    ),
    Step("runs get", ("{run}", "--project", "{project}"), gated=True),
    Step("runs cancel", ("{run}", "--project", "{project}"), gated=True),
    Step("outputs", ("--project", "{project}"), gated=True),
    # -- teardown -------------------------------------------------------------
    Step("config set-org", ("{org}",)),
    Step("env use", ("{env}",)),
    Step("projects delete", ("{import_project}", "--yes"), teardown=True),
    Step("projects delete", ("{project}", "--yes"), teardown=True),
    Step("logout", teardown=True),
)


# Offline validation ----------------------------------------------------------
# These run in normal CI via tests/test_live_check.py. They are the reason the
# plan can be trusted without a live backend: a typo'd flag or a missing
# required argument fails here, not thirty seconds into a production run.


def load_surface() -> dict[str, dict]:
    """The committed CLI contract, keyed by command path."""
    if not SURFACE_PATH.exists():
        raise SystemExit(
            f"{SURFACE_PATH} not found. Run: python scripts/emit_cli_surface.py > cli-surface.json"
        )
    try:
        data = json.loads(SURFACE_PATH.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{SURFACE_PATH} is malformed: {exc}") from exc
    return {command["path"]: command for command in data["commands"]}


def _positional_count_required(command: dict) -> int:
    return sum(
        1 for param in command["params"] if not param["opts"][0].startswith("-") and param["required"]
    )


def validate_plan() -> list[str]:
    """Every way the plan can be wrong that does not need a network to detect."""
    surface = load_surface()
    problems: list[str] = []

    missing = sorted(set(surface) - set(TIERS))
    if missing:
        problems.append(f"commands in cli-surface.json but not classified in TIERS: {missing}")
    stale = sorted(set(TIERS) - set(surface))
    if stale:
        problems.append(f"commands classified in TIERS but absent from cli-surface.json: {stale}")

    for path, (tier, note) in TIERS.items():
        if tier == MANUAL and not note:
            problems.append(f"{path!r} is MANUAL but gives no reason")
        if tier not in {LOCAL, READ, CANARY, MANUAL}:
            problems.append(f"{path!r} has unknown tier {tier!r}")

    for step in PLAN:
        command = surface.get(step.path)
        if command is None:
            problems.append(f"plan step {step.path!r} is not a real command")
            continue

        tier = TIERS.get(step.path, ("", ""))[0]
        if tier == MANUAL:
            problems.append(f"plan step {step.path!r} is classified MANUAL and must not be executed")

        known = _split_opts_of(command)
        supplied_flags = {token for token in step.args if token.startswith("-")}
        for flag in sorted(supplied_flags - known):
            problems.append(f"plan step {step.path!r} passes unknown flag {flag}")

        # Required options must be present; required positionals must be filled.
        positionals = [token for token in step.args if not token.startswith("-")]
        consumed = 0
        for token in step.args:
            if token.startswith("-") and token in known:
                param = next(p for p in command["params"] if token in _split_opts(p))
                if param["type"] != "boolean":
                    consumed += 1
        for param in command["params"]:
            if not param["required"]:
                continue
            first = param["opts"][0]
            if first.startswith("-"):
                if not any(opt in supplied_flags for opt in _split_opts(param)):
                    problems.append(f"plan step {step.path!r} omits required {first}")
        if len(positionals) - consumed < _positional_count_required(command):
            problems.append(
                f"plan step {step.path!r} supplies {len(positionals) - consumed} positional(s), "
                f"needs {_positional_count_required(command)}"
            )

    if not any(step.teardown and step.path == "projects delete" for step in PLAN):
        problems.append("plan has no `projects delete` teardown step; a failed run would leak the canary")

    captured: set[str] = set()
    for step in PLAN:
        for token in step.args:
            # A capture must come *earlier* in the plan than its use, otherwise
            # the live run would substitute an empty string and the command
            # would fail for a reason that looks like a backend bug.
            for name in re.findall(r"\{(\w+)\}", token):
                if name in _RUNTIME_PLACEHOLDERS:
                    continue
                if name not in captured:
                    problems.append(f"plan step {step.path!r} uses {{{name}}} before anything captures it")
        if step.capture:
            captured.add(step.capture)

    return problems


def _split_opts_of(command: dict) -> set[str]:
    """Every flag spelling the command accepts, including both halves of a
    Typer boolean pair like "--bind/--no-bind"."""
    names: set[str] = set()
    for param in command["params"]:
        names |= _split_opts(param)
    return names


def _split_opts(param: dict) -> set[str]:
    names: set[str] = set()
    for opt in param["opts"]:
        if not opt.startswith("-"):
            continue
        names.add(opt)
        if "/" in opt:
            names.update(part for part in opt.split("/") if part.startswith("-"))
    return names


# Placeholders filled from the environment rather than captured from output.
_RUNTIME_PLACEHOLDERS = frozenset({"canary", "seed", "spec", "import_folder", "tmp", "org", "env"})


def ledger(exercised: Optional[set[str]] = None) -> dict[str, list[str]]:
    """What the run covered, and what it did not.

    `exercised` defaults to the plan, which answers the static question ("what
    *could* this harness cover?"). Passing the paths a real run actually
    completed answers the live one.
    """
    covered = {step.path for step in PLAN} if exercised is None else exercised
    report: dict[str, list[str]] = {"covered": sorted(covered), "manual": [], "gap": []}
    for path, (tier, note) in sorted(TIERS.items()):
        if path in covered:
            continue
        if tier == MANUAL:
            report["manual"].append(f"{path} — {note}")
        else:
            report["gap"].append(f"{path} ({tier})")
    return report


# The live runner -------------------------------------------------------------


@dataclass
class Result:
    step: Step
    argv: list[str]
    status: str  # "ok" | "fail" | "skip"
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Runner:
    """Executes the plan against a real backend, in a sandbox of its own."""

    binary: str
    env_name: str
    workdir: Path
    tmpdir: Path
    include_run: bool
    org: str = ""
    process_env: dict[str, str] = field(default_factory=dict)
    context: dict[str, str] = field(default_factory=dict)
    results: list[Result] = field(default_factory=list)

    def _resolve(self, token: str) -> Optional[str]:
        """Substitute {placeholders}; None means a dependency is missing."""
        out = token
        for name in re.findall(r"\{(\w+)\}", token):
            value = self.context.get(name)
            if not value:
                return None
            out = out.replace("{" + name + "}", value)
        return out

    def execute(self, step: Step) -> Result:
        if step.gated and not self.include_run:
            return Result(step, [], "skip", "needs --include-run (costs real compute)")

        args: list[str] = []
        for token in step.args:
            resolved = self._resolve(token)
            if resolved is None:
                return Result(step, [], "skip", f"unresolved dependency in {token!r}")
            args.append(resolved)

        argv = [self.binary, *step.path.split(), *args]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=self.workdir,
                env=self.process_env,
                input=step.stdin or "",
                capture_output=True,
                text=True,
                timeout=600,
                # Hidden prompts (secrets set, share set-password) call
                # getpass.getpass(), which opens /dev/tty *directly* and only
                # falls back to stdin if that open fails. Without this, the
                # child inherits our controlling terminal, getpass reads from
                # it instead of `input=` above, and the run hangs forever
                # waiting on a terminal nothing is typing into. Detaching the
                # child into its own session makes /dev/tty unavailable to it,
                # so getpass falls back to the stdin we actually provided.
                #
                # ponytail: this also means Ctrl-C no longer reaches the child
                # directly (it's outside the terminal's foreground process
                # group), so a hang for any *other* reason now leaves an
                # orphaned `harumi` process running until its own 600s timeout
                # instead of dying with the parent. Upgrade path if that shows
                # up in practice: use Popen instead of run() and explicitly
                # os.killpg() on KeyboardInterrupt/timeout.
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return Result(step, argv, "fail", "timed out after 600s", time.monotonic() - started)
        elapsed = time.monotonic() - started

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return Result(step, argv, "fail", detail[-1] if detail else f"exit {proc.returncode}", elapsed)

        if step.capture:
            captured = self._capture(step, proc.stdout)
            if not captured:
                return Result(step, argv, "fail", f"could not read a {step.capture} id from output", elapsed)
            self.context[step.capture] = captured

        return Result(step, argv, "ok", "", elapsed)

    def _capture(self, step: Step, stdout: str) -> str:
        """Pull an id out of a successful command's output.

        `projects create` binds the directory, so prefer reading the id back out
        of .harumi/config.json — that is a contract, whereas the printed line is
        cosmetic. Everything else falls back to the first UUID printed, which is
        unambiguous because the canary starts empty.
        """
        if step.capture == "project":
            binding = self.workdir / ".harumi" / "config.json"
            if binding.exists():
                try:
                    project_id = json.loads(binding.read_text()).get("project_id", "")
                except (json.JSONDecodeError, OSError):
                    project_id = ""
                if project_id:
                    return str(project_id)
        if step.capture_re:
            match = re.search(step.capture_re, stdout)
            return match.group(1) if match else ""
        match = UUID_RE.search(stdout)
        return match.group(0) if match else ""

    def run_plan(self) -> list[Result]:
        self.context.update(
            {
                "canary": _canary_name(),
                "seed": str(self._seed_file()),
                "spec": str(self._seed_dashboard()),
                "import_folder": str(self._seed_import_folder()),
                "tmp": str(self.tmpdir),
                "org": self.org,
                "env": self.env_name,
            }
        )
        self._seed_git_repo()

        main_steps = [s for s in PLAN if not s.teardown]
        teardown = [s for s in PLAN if s.teardown]

        try:
            for step in main_steps:
                result = self.execute(step)
                self.results.append(result)
                print(_format(result), flush=True)
                # A failed `projects create` means nothing downstream can work;
                # stop early rather than emitting forty confusing skips.
                if result.status == "fail" and step.path == "projects create":
                    print("  canary project was not created — skipping to teardown", flush=True)
                    break
        finally:
            # Teardown must run even on Ctrl-C or an unexpected exception —
            # not just the checked `projects create` failure above — or the
            # canary project leaks on the real backend.
            for step in teardown:
                result = self.execute(step)
                self.results.append(result)
                print(_format(result), flush=True)

        return self.results

    def _seed_file(self) -> Path:
        """A tiny, dependency-free program to upload and (optionally) run."""
        path = self.tmpdir / "seed_main.py"
        path.write_text('print("harumi live check ok")\n')
        return path

    def _seed_import_folder(self) -> Path:
        """A plain folder for `harumi import` to turn into a new project.

        `import` accepts any directory — there is no export manifest or
        schema to satisfy — and `push_folder()` (git.py) `git init`s its own
        throwaway repo in-place, so this just needs something in it.
        """
        folder = self.tmpdir / "import-seed"
        folder.mkdir(exist_ok=True)
        (folder / "main.py").write_text('print("harumi live check import ok")\n')
        return folder

    def _seed_git_repo(self) -> None:
        """`harumi init` and `harumi run` both assume the bound directory came
        from `git clone`/`git init` — a real project checkout, never an empty
        folder. Without this, `init` silently skips remote setup ("Not inside
        a git repo") and `run`'s dirty/unpushed check (plain `git status` /
        `git rev-list`) fails outright with "not a git repository". Seed one
        commit so the canary's workdir has the same shape a real checkout
        would, and `run`'s scratch-push path has a HEAD to parent onto.
        """
        target = self.workdir / "main.py"
        target.write_text(self._seed_file().read_text())
        git = ["git", "-C", str(self.workdir)]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["config", "user.email", "livecheck@harumi.io"], check=True)
        subprocess.run(git + ["config", "user.name", "livecheck"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "livecheck: seed"], check=True)

    def _seed_dashboard(self) -> Path:
        """A minimal spec that must validate clean, so any reported issue is a
        real regression in the widget contract rather than a bad fixture."""
        path = self.tmpdir / "dashboard.toml"
        path.write_text(
            '[[widgets]]\ntype = "metric"\nid = "objective"\ntitle = "Objective"\nvalue_key = "objective"\n'
        )
        return path


def _format(result: Result) -> str:
    mark = {"ok": "PASS", "fail": "FAIL", "skip": "SKIP"}[result.status]
    line = f"[{mark}] {result.step.path}"
    if result.seconds:
        line += f" ({result.seconds:.1f}s)"
    if result.detail:
        line += f" — {result.detail}"
    return line


# Wiring ----------------------------------------------------------------------


def _canary_name() -> str:
    """A timestamp alone collides if two runs start in the same second (e.g. a
    scheduled job overlapping a manual one); the random suffix makes that a
    non-issue without needing any cross-run coordination."""
    return f"livecheck-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _build_env(env_name: str, home: Path) -> dict[str, str]:
    process_env = dict(os.environ)
    process_env["HARUMI_HOME"] = str(home)
    process_env["HARUMI_ENV"] = env_name
    # Rich wraps and ellipsizes to the terminal width, which would corrupt the
    # UUIDs this harness reads back out of command output.
    process_env["COLUMNS"] = "400"
    process_env["NO_COLOR"] = "1"
    return process_env


def _seed_credentials(home: Path, env_name: str) -> str:
    """Copy the caller's session into the sandbox, or mint one from env vars.

    `harumi login` cannot run unattended (the OTP arrives by email), so the only
    non-interactive path is writing credentials.json directly. Working in a copy
    matters: every request refreshes the token when it is close to expiring and
    rewrites this file, so pointing at the real ~/.harumi would let a harness run
    rotate the developer's own session.
    """
    target_dir = home / "environments" / env_name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "credentials.json"

    access = os.environ.get("HARUMI_LIVE_ACCESS_TOKEN", "")
    refresh = os.environ.get("HARUMI_LIVE_REFRESH_TOKEN", "")
    if refresh or access:
        target.write_text(json.dumps({"access_token": access, "refresh_token": refresh}))
        target.chmod(0o600)
        return "env vars (HARUMI_LIVE_*)"

    source = Path(os.environ.get("HARUMI_HOME", Path.home() / ".harumi"))
    existing = source / "environments" / env_name / "credentials.json"
    if not existing.exists():
        raise SystemExit(
            f"No session for {env_name!r}. Either run `harumi --env {env_name} login` first, "
            f"or set HARUMI_LIVE_REFRESH_TOKEN (preferred in CI — a stale access token self-heals)."
        )
    shutil.copy2(existing, target)
    target.chmod(0o600)
    # Carry the saved org across too, so `org members` and `config set-org` have
    # something real to point at.
    env_config = source / "environments" / env_name / "config.json"
    if env_config.exists():
        env_config_target = target_dir / "config.json"
        shutil.copy2(env_config, env_config_target)
        env_config_target.chmod(0o600)
    return str(existing)


def _resolve_org(home: Path, env_name: str) -> str:
    config = home / "environments" / env_name / "config.json"
    if config.exists():
        return str(json.loads(config.read_text()).get("org_id", "") or "")
    return os.environ.get("HARUMI_ORG", "")


def _print_ledger(report: dict[str, list[str]], header: str) -> None:
    print(f"\n=== {header} ===")
    print(f"covered ({len(report['covered'])}/{len(TIERS)}):")
    for path in report["covered"]:
        print(f"  + {path}")
    if report["gap"]:
        print(f"\nautomatable but NOT covered ({len(report['gap'])}) — real gaps:")
        for line in report["gap"]:
            print(f"  ! {line}")
    print(f"\nnot automatable against a live backend ({len(report['manual'])}):")
    for line in report["manual"]:
        print(f"  - {line}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default="staging", help="Backend environment to target (default: staging).")
    parser.add_argument(
        "--allow-prod",
        action="store_true",
        help="Required second opt-in when --env production: the run creates real rows.",
    )
    parser.add_argument("--include-run", action="store_true", help="Also queue a real solver run (costs compute).")
    parser.add_argument("--plan", action="store_true", help="Print the plan and coverage ledger, run nothing.")
    parser.add_argument("--keep", action="store_true", help="Do not delete the sandbox HARUMI_HOME/workdir.")
    args = parser.parse_args(argv)

    problems = validate_plan()
    if problems:
        print("The plan is invalid — refusing to run:")
        for problem in problems:
            print(f"  - {problem}")
        return 2

    if args.plan:
        for step in PLAN:
            flags = " ".join(step.args)
            note = "  [gated]" if step.gated else ("  [teardown]" if step.teardown else "")
            print(f"harumi {step.path} {flags}".rstrip() + note)
        _print_ledger(ledger(), "coverage this plan can reach")
        return 0

    if args.env == "production" and not args.allow_prod:
        print("Refusing to touch production without --allow-prod (it creates real rows in a real workspace).")
        return 2

    binary = os.environ.get("HARUMI_BIN") or shutil.which("harumi")
    if not binary:
        print("No `harumi` on PATH. Install it (`pip install -e .`) or set HARUMI_BIN.")
        return 2

    sandbox = Path(tempfile.mkdtemp(prefix="harumi-livecheck-"))
    home, workdir, tmpdir = sandbox / "home", sandbox / "work", sandbox / "tmp"
    for path in (home, workdir, tmpdir):
        path.mkdir(parents=True)

    try:
        source = _seed_credentials(home, args.env)
        runner = Runner(
            binary=binary,
            env_name=args.env,
            workdir=workdir,
            tmpdir=tmpdir,
            include_run=args.include_run,
            org=_resolve_org(home, args.env),
            process_env=_build_env(args.env, home),
        )
        print(f"harumi:  {binary}")
        print(f"env:     {args.env}")
        print(f"session: {source}")
        print(f"sandbox: {sandbox}\n")

        results = runner.run_plan()
    except KeyboardInterrupt:
        # run_plan()'s own try/finally already ran teardown (deleting the
        # canary) before this unwinds here, so it is safe to just stop.
        print("\nInterrupted — teardown ran for whatever had already been created.")
        return 130
    finally:
        if args.keep:
            print(f"\nsandbox kept at {sandbox}")
        else:
            shutil.rmtree(sandbox, ignore_errors=True)

    passed = {r.step.path for r in results if r.status == "ok"}
    failed = [r for r in results if r.status == "fail"]
    skipped = [r for r in results if r.status == "skip"]

    _print_ledger(ledger(passed), f"live coverage on {args.env}")
    print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
    if failed:
        print("\nfailures:")
        for result in failed:
            print(f"  {result.step.path}: {result.detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
