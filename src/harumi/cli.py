"""`harumi` command-line interface.

    harumi login [--signup]
    harumi logout
    harumi whoami
    harumi status
    harumi whats-new [--all]
    harumi profile set
    harumi specs
    harumi blueprints
    harumi start
    harumi new <NAME> [--blueprint <slug>] [--personal] [--customer-id <id>] [--dir <path>] [--no-clone]
    harumi import [path] [--from-git <url>] [--project-name <name>]
    harumi clone <PROJECT_ID> [--dir <path>]
    harumi link --project <id> [--api-url <url>] [--git-url <url>]
    harumi run [--branch <b>] [--commit <sha>] [--command <c>] [--kernel <k>] [--watch]
    harumi runs list|get|cancel|download [--project <id>]
    harumi compare <run-id>... [--json] [--project <id>]
    harumi check [--ref <ref>] [--project <id>]
    harumi agent-skills install
    harumi skills library|add
    harumi projects list|get|rename|rm
    harumi files ls|put|get|rm [--project <id>]
    harumi dashboard widgets|validate [--project <id>]
    harumi share list|get|add|update|rm|rotate [--project <id>]
    harumi datasources list|get|add|update|rm|test|query [--project <id>]
    harumi schedules list|get|add|update|rm [--project <id>]
    harumi secrets list|set|rm [--project <id>]
    harumi memories list|add|edit|rm [--project <id>]
    harumi org list|create|rename|use|rm|members|invite|role|rm-member

Deprecated names (`push`, `repo`, `outputs`, `projects create`, `config
set-org`, `skill`, `profile show`, `... remove`/`delete`,
`share set-password`/`rm-password`, `run --output-dir`) still work in 0.11.x
as hidden commands that print what to use instead, and are removed in 0.12.0.

Onboarding — three journeys, three commands
---------------------------------------------
Every project starts one of three ways, and each has its own verb:

* Nothing yet: `harumi new <NAME>` creates the project, then clones its
  freshly-provisioned (and scaffold-seeded) repo into a local directory.
* An existing local folder of code: `harumi import [PATH]` creates a project
  and force-pushes the folder's contents as its first commit.
* A project that already exists in Harumi (created via the web app, or by a
  teammate): `harumi clone <PROJECT_ID>` fetches its repo into a local
  directory.

`harumi start` asks which of the three applies and dispatches to it — the
one entry point to point a confused user at. `harumi link` is the narrower
primitive underneath `clone`: it binds the *current* directory to a project
without cloning, for the rare case where the code is already checked out
by hand.

Git-ref execution model
-----------------------
Every run goes through the project's Harumi Git (Gitea) repo.  If the
working tree is dirty or has unpushed commits, the CLI auto-pushes a
throwaway scratch branch so the run still executes without forcing the
user to commit manually.

Requires a working directory produced by `harumi new`/`harumi import`/
`harumi clone` (or `harumi link`, if the checkout already exists).

Repo files
----------
There is no `harumi repo` any more: a project's repo is an ordinary git repo,
and `harumi clone` sets up a remote named `harumi` with the token already in
its URL. Read it with `ls`/`cat`/`git show`, change it by editing files and
`git commit && git push harumi <branch>`. `harumi check` (server-side
readiness) and `harumi skills add` (library presets) are the repo-adjacent
commands that do something git can't.

Project files
-------------
`harumi files` manages a project's non-git file storage — distinct from
the git repo (git-tracked, lands in a commit): uploads land in a shared
bucket under the project's own prefix and appear at `inputs/` inside every
run's sandbox. `files put` refuses an upload that would push the project
past the same per-project file-count/total-size cap the run-time sync
enforces, so a bad upload fails at upload time instead of bricking every
run afterward.

Datasources
-----------
`harumi datasources` manages project-scoped database connections.
Credentials are only ever prompted interactively (hidden input) — never
accepted as a flag — and are stored server-side in AWS SSM. `datasources
query` runs a SELECT/WITH-only, row-capped proxy query so users can validate
SQL before wiring it into solver code.
A datasource reachable only through the mTLS proxy needs `--use-proxy` plus
its whole set: `--proxy-host`, `--proxy-port` and the three
`--proxy-tls-*` PEM paths. Those take file paths rather than inline values so
a certificate never lands in shell history; the CLI reads them and the API
stores them alongside the password in SSM. `datasources update` can rotate
any one of them on its own.

Dashboards & widgets
---------------------
A project renders one dashboard per repo-committed spec — every
`dashboard/*.toml` plus the legacy root `dashboard.toml` — each bound by
dot-path keys to `output/output.json` (the file a run writes), and each an
entry in the platform's dashboard picker.
`harumi dashboard widgets` prints the current widget-type reference;
`harumi dashboard validate` checks every spec against that contract
and, given a run's output, flags dot-paths that won't resolve — the
platform silently drops a bad widget instead of erroring, so validating
before committing is the only way to catch a typo up front.

Sharing
-------
`harumi share` manages a project's public, unauthenticated dashboard links —
a project can have several, each independently revocable and optionally
password-protected. Every permission (assistant, run history, run control,
inputs/outputs) defaults to off on `add`, so creating a link never silently
grants more than a bare read-only, latest-run-only dashboard view.
`run-control` capabilities additionally require the viewer to be signed in.
`rotate`/`update --password` invalidate previously issued viewer sessions for
that link only.

Schedules
---------
`harumi schedules` manages project-scoped cron schedules for git-ref runs.
Cron is a raw 5-field expression interpreted in UTC; there is no
pause/enable flag — delete the schedule to stop it firing.

Secrets
-------
`harumi secrets` manages project-scoped environment variables, stored as
SSM SecureStrings and injected into kernels/apps. There is no update
endpoint — `secrets set` on an existing name overwrites it.

Creating projects
-----------------
`harumi new <NAME> --no-clone` calls `POST /projects`, which provisions the
project's Gitea repo server-side, with no local filesystem side effects — it
does not bind or clone anything. Plain `harumi new` is the same call followed
by a clone-and-bind.
"""

from __future__ import annotations

import base64
import functools
import json
import mimetypes
import os
import posixpath
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from harumi import __version__, auth, interactive, quickstart
from harumi import skills as skills_mod
from harumi.client import Client, upload_is_staged
from harumi.file_sync_cap import (
    MAX_PROJECT_SYNC_FILES,
    MAX_PROJECT_SYNC_TOTAL_BYTES,
    check_project_sync_cap,
)
from harumi.config import (
    DEFAULT_ENVIRONMENT,
    ENVIRONMENTS,
    Config,
    ProjectBinding,
    active_environment,
    active_platform_url,
    active_status_url,
    is_internal_email,
    load_credentials,
    load_git_token,
    load_git_username,
    resolve_environment,
    save_environment,
    save_git_token,
    status_hint_url,
)
from harumi.dashboard import (
    DashboardSchemaError,
    DashboardTomlError,
    local_dashboard_paths,
    pick_dashboard_paths,
    validate_dashboard_toml,
    widget_schemas,
)
from harumi.errors import (
    ApiError,
    ApiUnreachableError,
    HarumiError,
    NotAuthenticatedError,
    SessionRefreshError,
)
from harumi.git import (
    LARGE_FILE_BYTES,
    GitError,
    ImportPlan,
    NotAHarumiRepoError,
    clone_repo,
    current_branch,
    delete_remote_scratch,
    ensure_remote,
    has_unpushed_commits,
    import_excludes,
    is_dirty,
    plan_import,
    push_folder,
    push_scratch,
    repo_root,
)
from harumi.models import LoggedUser, ProjectFile, ProjectShareLink

app = typer.Typer(
    name="harumi",
    help="Run local optimization code on Harumi's infrastructure via Harumi Git.",
    # Bare `harumi` is the quick start (see `_quickstart`), not a help dump.
    no_args_is_help=False,
    invoke_without_command=True,
)
console = Console()
err_console = Console(stderr=True)

# figlet "ansi_shadow" with a 2-column gap inserted between each letter for
# readability, generated once and pasted in rather than taking a pyfiglet
# runtime dependency for six letters that never change.
_BANNER = r"""
██╗  ██╗   █████╗   ██████╗   ██╗   ██╗  ███╗   ███╗  ██╗
██║  ██║  ██╔══██╗  ██╔══██╗  ██║   ██║  ████╗ ████║  ██║
███████║  ███████║  ██████╔╝  ██║   ██║  ██╔████╔██║  ██║
██╔══██║  ██╔══██║  ██╔══██╗  ██║   ██║  ██║╚██╔╝██║  ██║
██║  ██║  ██║  ██║  ██║  ██║  ╚██████╔╝  ██║ ╚═╝ ██║  ██║
╚═╝  ╚═╝  ╚═╝  ╚═╝  ╚═╝  ╚═╝   ╚═════╝   ╚═╝     ╚═╝  ╚═╝
""".strip("\n")


def _print_banner_if_bare_entrypoint() -> None:
    """Show the banner only for the two purely-decorative invocations: bare
    `harumi` and `harumi --help`.

    Deliberately excludes `--version`: `.agents/skills/harumi-cli-setup/
    SKILL.md` documents `harumi --version` as printing *exactly* `harumi
    <x.y.z>` and uses that exact shape both to verify a fresh install and to
    disambiguate the real CLI from an unrelated same-named `harumi` shadowing
    it on PATH. Prepending ASCII art would silently break that machine-parsed
    contract (and anything scripted against it) for no benefit — the banner
    is decoration for a human looking at help text, not something `--version`
    callers expect.

    Also deliberately narrow the other way: `harumi <command> --help` (e.g.
    `harumi login --help`) does NOT match, so the banner decorates the entry
    point once rather than prepending itself to every command's help text.
    Shown only when stdout is a terminal (`_stdout_is_terminal`): piped or logged
    output stays free of decoration.

    Checked against raw argv in `main()` (the actual process entry point)
    rather than as a Typer/Click callback, so it can't interfere with
    Click's own eager `--help` handling — and `tests/test_cli.py` invokes
    `app()` directly via `CliRunner`, never `main()`, so this never touches
    test output.
    """
    if sys.argv[1:] in ([], ["--help"]) and _stdout_is_terminal():
        console.print(f"[bold magenta]{_BANNER}[/bold magenta]")
        _print_skill_install_hint()


def _stdout_is_terminal() -> bool:
    """Whether output goes to a person's terminal. Art and nudges are for them;
    `harumi --help | less`, a log file or a coding agent reading stdout get the
    plain text only."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _print_skill_install_hint() -> None:
    """Nudge toward `harumi agent-skills install` when a coding agent detected on
    this machine (Cursor/Claude Code/Codex) doesn't have the skill yet.

    `pip`/`pipx`/`uv` have no post-install hook to seed the skill
    automatically as part of the install itself, so the next bare `harumi`
    invocation is the closest automatic moment to catch a human who
    installed the CLI without an agent driving `harumi-cli-setup` for them.
    Only ever prints a recommendation — never writes skill files unprompted,
    matching `harumi agent-skills install`'s existing consent-based behavior.

    ponytail: catches bare `Exception`, not a specific type. This probes
    `~/.cursor`, `~/.claude`, `~/.codex` (via `agents_missing_skill()`) on
    every bare invocation now, instead of only inside the opt-in `skill
    install` command — a purely decorative nudge must never turn a
    filesystem hiccup (odd container, permission error, symlink loop) into a
    crash of the CLI's most common entry point. Ceiling: a real bug in the
    probing logic would be silently swallowed here too. Upgrade path: narrow
    to `OSError` once `agents_missing_skill()` is proven to raise nothing
    else in practice.
    """
    try:
        missing = skills_mod.agents_missing_skill()
    except Exception:
        return
    if not missing:
        return
    names = ", ".join(a.label for a in missing)
    console.print(
        f"\n[dim]Using {names}? Run [/dim][bold]harumi agent-skills install[/bold]"
        "[dim] to teach it this CLI.[/dim]"
    )


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"harumi {__version__}")
        raise typer.Exit()


def _env_callback(value: Optional[str]) -> None:
    """Set the active environment for this invocation.

    Sets HARUMI_ENV so every Config.load() in this process resolves it — the
    single chokepoint every command funnels through.
    """
    if value is None:
        return
    try:
        resolve_environment(value)  # validate name
    except ValueError as exc:
        err_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1)
    os.environ["HARUMI_ENV"] = value


@app.callback()
def _main(
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the harumi CLI version and exit.",
    ),
    env: Optional[str] = typer.Option(
        None,
        "--env",
        callback=_env_callback,
        is_eager=True,
        # Staff-only (staging is internal), so it stays out of everyone else's help.
        hidden=True,
        help="Backend environment to target for this one command (production or staging). "
        "`harumi login` is how staff pick their default.",
    ),
) -> None:
    if ctx.invoked_subcommand is None:
        _quickstart()


@app.command(
    "help",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def help_command(ctx: typer.Context) -> None:
    """`harumi help` is `harumi --help`; `harumi help run` is `harumi run --help`."""
    _run_cli([*ctx.args, "--help"])


def _run_cli(argv: list[str]) -> None:
    """Run another harumi command as if the user had typed it, then exit.

    Used by the quick start so a menu choice behaves exactly like the typed
    command: same prompts, same error handling, same exit code.
    """
    from typer.main import get_command

    get_command(app).main(args=argv, prog_name="harumi")


def _quickstart_state() -> quickstart.State:
    creds = load_credentials()
    binding = ProjectBinding.load()
    return quickstart.State(
        logged_in=bool(creds and creds.get("access_token")),
        environment=active_environment(),
        email=(creds or {}).get("email"),
        project_id=binding.project_id if binding else None,
    )


def _quickstart(*, show_status: bool = True) -> None:
    """Bare `harumi`: say where the user stands and offer the next step.

    A terminal gets an arrow-key menu; anything else (pipe, CI, a coding agent)
    gets the same content as plain text and the command exits 0 — it must never
    sit waiting for a key press nobody can send.
    """
    state = _quickstart_state()

    if not interactive.is_interactive():
        for line in quickstart.plain_text(state):
            console.print(line, soft_wrap=True)
        return

    # Right after login the screen already says who is logged in and where.
    console.print(f"\n{quickstart.status_line(state)}\n" if show_status else "")
    options = [
        interactive.Option(a.label, a.key, hint=a.command) for a in quickstart.actions_for(state)
    ]
    options += [
        interactive.Option("Show all commands", quickstart.HELP, hint="harumi --help"),
        interactive.Option("Quit", quickstart.QUIT),
    ]
    choice = interactive.select("What would you like to do?", options)

    if choice in (None, quickstart.QUIT):
        return
    if choice == quickstart.HELP:
        _run_cli(["--help"])
    elif choice == quickstart.LOGIN:
        _run_cli(["login"])
    elif choice == quickstart.SIGNUP:
        _run_cli(["login", "--signup"])
    elif choice == quickstart.NEW:
        _run_cli(["new", typer.prompt("Project name")])
    elif choice == quickstart.IMPORT:
        _run_cli(["import"])
    elif choice == quickstart.CLONE:
        _run_cli(["clone"])
    elif choice == quickstart.RUN:
        _run_cli(["run", "--watch"])
    elif choice == quickstart.CHECK:
        _run_cli(["check"])
    elif choice == quickstart.RUNS:
        _run_cli(["runs", "list"])


def _get_client(
    api_url: Optional[str] = None,
    git_url: Optional[str] = None,
    org: Optional[str] = None,
) -> Client:
    client = Client(api_url=api_url, git_url=git_url, org_id=org)
    if not auth.has_session():
        # Before the command prints "Creating project..." or similar and then
        # fails halfway: nothing here can work without a login.
        raise NotAuthenticatedError(environment=client.config.environment)
    return client


def _fail(message: str) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code=1)


def _warn_deprecated(old: str, use: str) -> None:
    """Tell the user a name still works but is going away. Goes to stderr so a
    script piping the command's stdout is unaffected."""
    # soft_wrap: the replacement is a command line someone will copy, so it must
    # not be broken across lines at the terminal width.
    err_console.print(
        f"[yellow]deprecated:[/yellow] `{escape(old)}` is removed in 0.12.0. Use: {escape(use)}",
        soft_wrap=True,
    )


def _deprecated(old: str, use: str):
    """Decorator: warn that command `old` is deprecated in favour of `use`,
    then run it unchanged. Keeps the wrapped signature so Typer still builds
    the same options."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            _warn_deprecated(old, use)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def _hidden_alias(group: typer.Typer, old_name: str, fn, old: str, use: str) -> None:
    """Register `fn` under the retired `old_name` as a hidden command that
    warns first. The command is left out of --help and cli-surface.json."""
    group.command(old_name, hidden=True)(_deprecated(old, use)(fn))


# What is taken out of a name someone else chose (the pattern is raw: `re` reads the \x and \u escapes,
# so the source line has no invisible characters of its own):
#   C0 and C1 controls (terminal escapes)
#   U+00AD soft hyphen, U+061C Arabic letter mark, U+200B-U+200F zero-width and direction marks,
#   U+202A-U+202E bidirectional embeddings and overrides, U+2060-U+2064 word joiner and invisible
#   operators, U+2066-U+2069 isolates, U+FEFF byte-order mark.
# The line and paragraph separators U+2028/U+2029 are not in the class: `_printable` turns them into spaces
# first, like a tab or a newline, so the words around them stay apart.
# Without the invisible ones a name like "Client<RLO> dashboard" can read as a different name in a delete prompt.
_CONTROL_CHARS = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


def _printable(text: object) -> str:
    """`text` with terminal control characters taken out, for names someone else chose.

    A share link's label or a file's name is typed by whoever created it and shown to anyone who
    runs the command on a shared project. An escape sequence in it would be run by their terminal.
    Tabs and newlines are control characters too, but harmless in a name, so they are kept as spaces.
    """
    spaced = str(text).replace("\t", " ").replace("\n", " ").replace("\u2028", " ").replace("\u2029", " ")
    return _CONTROL_CHARS.sub("", spaced)


def _format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _slugify(name: str) -> str:
    """Turn a project name into a filesystem-safe directory name.

    Lowercases, replaces every run of non-alphanumeric characters with a
    single `-`, and strips leading/trailing `-`. Falls back to "project" if
    that leaves nothing (e.g. a name that's all punctuation/emoji).
    """
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def _print_project_workspace(customer_id: Optional[str]) -> None:
    """Report which workspace a freshly-created project landed in.

    `projects list` filters by the configured org, so a project created in the
    other workspace would otherwise just look missing.
    """
    if customer_id:
        console.print(f"Workspace: organization [bold]{customer_id}[/bold].")
    else:
        console.print("Workspace: your [bold]personal[/bold] workspace.")


def _resolve_project(project: Optional[str]) -> str:
    """Resolve a project id from --project, falling back to the .harumi binding."""
    if project:
        return project
    binding = ProjectBinding.load()
    if binding:
        return binding.project_id
    _fail(
        "Provide --project or run from a directory bound to a project "
        "(see `harumi start`, `harumi new`, `harumi import`, `harumi clone`, or `harumi link`)."
    )
    raise AssertionError("unreachable")  # _fail always raises


def _billing_url() -> str:
    return f"{active_platform_url()}/settings?tab=billing"


# What to do next for each 402 reason harumi-api sends as `code`
# (`src/api/billing/admission.py` `_REASONS_402`). Its `detail` already says
# what happened; this only adds where to go.
_LIMIT_HINT = "Run [bold]harumi usage[/bold] to see your limits, or visit {billing} to upgrade or turn on overage."
_UPGRADE_HINT = "Upgrade at {billing}."
_ORG_ADMIN_HINT = "Ask your organization admin to raise your limit."
_PAYWALL_HINTS = {
    "no_subscription": "Subscribe at {billing}, or work in an organization that has a plan (`harumi org use`).",
    "org_not_subscribed": (
        "Ask your organization admin to contact sales at https://harumi.io/contact, "
        "or switch workspace with `harumi org use` / `--personal`."
    ),
    "weekly_limit_reached": _LIMIT_HINT,
    "monthly_limit_reached": _LIMIT_HINT,
    "insufficient_credits": _LIMIT_HINT,
    "member_weekly_limit_reached": _ORG_ADMIN_HINT,
    "member_monthly_limit_reached": _ORG_ADMIN_HINT,
    "free_kernel_time_exhausted": _UPGRADE_HINT,
    "free_concurrent_runs": "Wait for your current run to finish, or upgrade at {billing}.",
    # The 7-day trial and the other plan limits (harumi-api#462). Each hint says the next
    # step; the API's `detail` already says what was hit.
    "concurrent_runs_limit_reached": "Wait for a run to finish before starting another.",
    "live_kernel_limit_reached": "Stop another kernel first, or upgrade at {billing}.",
    "storage_quota_reached": "Delete some files with `harumi files rm`, or upgrade at {billing}.",
    "project_limit_reached": "Delete a project you no longer need, or upgrade at {billing}.",
    "app_limit_reached": "Stop another app first, or upgrade at {billing}.",
    "app_time_exhausted": "Upgrade at {billing} to keep your app running.",
    "schedule_limit_reached": "Delete a schedule with `harumi schedules rm`, or upgrade at {billing}.",
    "schedule_too_frequent": "Choose a longer interval, or upgrade at {billing}.",
    "insight_limit_reached": "It resets tomorrow, or upgrade at {billing}.",
    "byok_not_in_plan": "Use Harumi's own models, or upgrade at {billing} to bring your own key.",
    "kernel_size_not_in_plan": "Pick a smaller kernel, or upgrade at {billing}.",
    "kernel_requires_higher_plan": "Pick a smaller kernel, or upgrade at {billing}.",
    "gurobi_requires_higher_plan": "Contact sales at https://harumi.io/contact for an Enterprise plan.",
    # A share link's visitor neither pays nor can act on the owner's plan.
    "share_link_cap_reached": "",
    "share_link_owner_out_of_credits": "",
    "share_link_owner_not_subscribed": "",
}


def _paywall_hint(code: Optional[str]) -> str:
    """The next step for a 402 `code`; the limit hint for an unknown or missing one."""
    template = _PAYWALL_HINTS.get(code or "", _LIMIT_HINT)
    return template.format(billing=_billing_url())


def _format_api_error(exc: ApiError) -> str:
    """`str(exc)` for every `ApiError` except a 402, which gets a message that
    actually tells the user what to do — "harumi-api returned HTTP 402: ..."
    is accurate but doesn't point anywhere. 402 here always means billing:
    the workspace has no plan, is over a weekly/monthly limit, or the plan
    doesn't cover the kernel. harumi-api sends the reason as `code`, which
    picks the hint; its `detail` is shown as is.
    """
    if isinstance(exc, ApiUnreachableError) or exc.status_code >= 500:
        # harumi-api gave no answer, or a server-side failure: likely an outage, not user error.
        # Not every ApiError(0, ...) qualifies: a failed presigned S3 transfer is not Harumi being down.
        hint_url = status_hint_url()
        if hint_url is None:
            return escape(str(exc))
        return f"{escape(str(exc))}\nCheck {hint_url} for platform status, or run [bold]harumi status[/bold]."
    if exc.status_code != 402:
        # `_fail` prints through Rich markup, and server text can hold `[/x]`-style brackets.
        return escape(str(exc))
    hint = _paywall_hint(exc.code)
    return f"{escape(exc.detail)}\n{hint}" if hint else escape(exc.detail)


def _handle_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except NotAHarumiRepoError as exc:
            _fail(str(exc))
        except GitError as exc:
            _fail(str(exc))
        except NotAuthenticatedError as exc:
            _fail(str(exc))
        except interactive.NonInteractiveError as exc:
            # Lists project names and other server text; keep Rich from reading
            # a `[x]` in one as markup.
            _fail(escape(str(exc)))
        except ApiError as exc:
            _fail(_format_api_error(exc))
        except HarumiError as exc:
            _fail(str(exc))
        except DashboardSchemaError as exc:
            # The vendored widget contract is unusable. Only the dashboard
            # commands need it, so this surfaces as a normal command failure
            # rather than a traceback (or, if it were loaded at import, as every
            # command in the CLI refusing to start).
            _fail(
                f"{exc}\nIf the installed dashboard-schema.json was edited by hand, "
                "reinstall the CLI. Otherwise this is a packaging bug — please report it "
                "at https://github.com/harumi-io/harumi-cli/issues"
            )
        except OSError as exc:
            # Every path this CLI writes to or reads from is user-supplied (`-o`,
            # `--path`, an import/export directory), so an unwritable
            # destination is a mistake to report, not a bug to traceback:
            # `files get x -o /etc/foo` raises PermissionError, and `-o somedir`
            # (or a remote path whose basename comes out empty) raises
            # IsADirectoryError. Broadened from FileNotFoundError at this one
            # boundary rather than per call site, same reasoning as the
            # UnicodeDecodeError clause below — every OSError subclass carries
            # errno + filename in its str(), so the message still names the path.
            _fail(str(exc))
        except UnicodeDecodeError:
            # Every text file this CLI reads is either user-supplied (a PEM, a
            # dashboard spec, a run-output JSON) or pulled from the project repo,
            # so non-UTF-8 bytes here mean the wrong path was passed — an honest
            # mistake, not something worth a traceback. Caught at this boundary
            # rather than per call site because `_read_pem`, `dashboard validate`'s
            # --path/--against and its base64 repo-file decode all reach it.
            _fail(
                "That file isn't UTF-8 text — check the path points at the text "
                "file you meant, not a binary one."
            )

    return wrapper


# ---------------------------------------------------------------------------
# Auth commands
# ---------------------------------------------------------------------------

@app.command()
@_handle_errors
def login(
    email: Optional[str] = typer.Option(None, help="Account email. Prompted if omitted."),
    signup: bool = typer.Option(
        False, "--signup", help="Create a new account for this email before sending the code."
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override harumi-api base URL."),
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Override Harumi Git base URL."),
) -> None:
    """Log in via one-time email code and store the session locally.

    New to Harumi? Add --signup to create your account first. Without it, an
    email address that has no account yet is turned away.

    """
    config = Config.load(api_url=api_url, git_url=git_url)
    email = email or typer.prompt("Email", default=_stored_email())

    config, picked = _choose_login_environment(email, config, api_url=api_url, git_url=git_url)
    env = ENVIRONMENTS[config.environment]
    if config.environment != DEFAULT_ENVIRONMENT or config.api_url != env.api_url:
        # Not the ordinary setup (staff on staging, or a developer's own API): say where.
        console.print(
            f"Environment: [bold]{config.environment}[/bold] "
            f"([dim]{config.api_url}[/dim])"
            + (" [yellow](internal — VPN required)[/yellow]" if env.internal else "")
        )

    if _session_still_works(config, email):
        console.print(f"Already logged in as [bold]{email}[/bold]{_on(config.environment)}.")
    else:
        try:
            auth.request_otp(config, email, sign_up=signup)
        except ApiError as exc:
            # This matches the server's English wording. If that wording changes the match
            # simply fails and the raw error is shown, which is a worse message, not a wrong one.
            if not signup and exc.status_code == 422 and "signups not allowed" in str(exc).lower():
                _fail(
                    f"There is no Harumi account for {escape(email)} yet. New here? "
                    "Run [bold]harumi login --signup[/bold] to create one."
                )
            raise
        console.print(f"A login code was sent to [bold]{escape(email)}[/bold].")
        user = _verify_code(config, email)
        console.print(
            f"[bold green]Logged in[/bold green] as {escape(user.email or email)}{_on(config.environment)}."
        )

    if picked:
        # Only after the login worked: a failed attempt (VPN off, wrong code)
        # must not leave every later command pointed at an environment that
        # can't be reached.
        save_environment(config.environment)
        if env.internal:
            console.print(
                f"[dim]{config.environment} is now your default environment. "
                "Run harumi login and pick production to go back.[/dim]"
            )

    _resolve_and_store_org(config)
    _provision_git_token(config)

    # Logged in and settled: at a terminal, offer what to do first rather than
    # leaving the person at a bare prompt. Scripts and agents just finish.
    if interactive.is_interactive():
        _quickstart(show_status=False)


def _on(environment: str) -> str:
    """" on staging" for any environment but production; nothing for production.

    Nearly everyone only ever uses production, and for them the word is noise that
    suggests a choice they do not have. Anyone somewhere else is told where.
    """
    return "" if environment == DEFAULT_ENVIRONMENT else f" on [bold]{environment}[/bold]"


_CODE_ATTEMPTS = 3


def _verify_code(config: Config, email: str) -> LoggedUser:
    """Ask for the emailed code and check it.

    At a terminal a wrong code can be retyped (three tries); anywhere else there is
    nobody to retype it, so it fails once. Either way the person is told what
    happened in words, not as an HTTP status.
    """
    attempts = _CODE_ATTEMPTS if interactive.is_interactive() else 1
    for attempt in range(1, attempts + 1):
        # People paste "123 456" from an email; the spaces are not part of the code.
        code = "".join(typer.prompt("Enter the code").split())
        try:
            return auth.verify_otp(config, email, code)
        except ApiError as exc:
            if exc.status_code == 429:
                _fail("Too many attempts. Wait a minute, then run [bold]harumi login[/bold] again.")
            if exc.status_code not in (400, 401, 403, 422):
                raise  # an outage or something unexpected: the normal error path explains it
            if attempt < attempts:
                console.print("[yellow]That code was not accepted.[/yellow] Check it and try again.")
                continue
            _fail("That code was not accepted. Run [bold]harumi login[/bold] to get a new one.")


def _stored_email() -> Optional[str]:
    """An email to offer as the prompt's default: the active environment's session if
    there is one, else any other environment's.

    The second case matters for staff: each environment has its own session, so the
    first login to staging would otherwise have nothing to suggest even though they
    are signed in to production with the same address. It is only a suggestion; they
    can type another.
    """
    from harumi.config import credentials_path

    active = load_credentials()
    candidates = [active] if active else []
    for name in ENVIRONMENTS:
        if name == active_environment():
            continue
        try:
            data = json.loads(credentials_path(name).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            candidates.append(data)
    for creds in candidates:
        email = creds.get("email")
        if isinstance(email, str) and email:
            return email
    return None


def _choose_login_environment(
    email: str, config: Config, *, api_url: Optional[str], git_url: Optional[str]
) -> tuple[Config, bool]:
    """Let Harumi staff pick production or staging before the code is sent.

    Returns the config to log in with and whether the person picked it (which is
    what makes it their saved default). Nobody else is ever asked: clients have
    no staging account, an explicit `--env`/`HARUMI_ENV` already answers the
    question (and stays a one-off), and without a terminal there is nobody to ask.
    """
    if not is_internal_email(email) or os.environ.get("HARUMI_ENV") or not interactive.is_interactive():
        return config, False

    options = [
        interactive.Option(
            name,
            name,
            hint="internal, needs the VPN" if env.internal else "public",
        )
        for name, env in ENVIRONMENTS.items()
    ]
    picked = interactive.select(
        "Which environment do you want to log in to?", options, default=config.environment
    )
    if picked is None:
        console.print("Login cancelled.")
        raise typer.Exit(code=1)

    if picked != config.environment:
        # Same switch `--env` makes, so every later lookup in this process agrees.
        os.environ["HARUMI_ENV"] = str(picked)
        config = Config.load(api_url=api_url, git_url=git_url)
    return config, True


def _session_still_works(config: Config, email: str) -> bool:
    """Whether this environment already holds a live session for `email`.

    Each environment keeps its own session, so with this a person who switches
    between production and staging is not sent a new code each time. Any doubt
    (different email, expired session, API unreachable) falls back to the normal
    code flow, which reports the real problem.
    """
    creds = load_credentials()
    if not creds or not creds.get("access_token"):
        return False
    if str(creds.get("email") or "").lower() != email.strip().lower():
        return False
    try:
        _get_client(api_url=config.api_url, git_url=config.git_url).get_profile()
    except HarumiError:
        return False
    return True


PERSONAL_WORKSPACE = "personal"
RUNS_SHOWN = 25


def _workspace_options(orgs) -> list[interactive.Option]:
    """Each organization, then the personal workspace, which is always a choice."""
    options = []
    for org in orgs:
        role = org.role_name or org.role
        options.append(
            interactive.Option(org.business_name, org.id, hint=f"{role + '  ' if role else ''}{org.id[:8]}")
        )
    options.append(interactive.Option("Personal workspace", PERSONAL_WORKSPACE, hint="just you"))
    return options


def _apply_workspace(config: Config, choice: str) -> None:
    if choice == PERSONAL_WORKSPACE:
        config.clear_org_id()
    else:
        config.save_org_id(choice)


def _resolve_and_store_org(config: Config) -> None:
    """Settle which workspace this login works in, and say so.

    * No organizations: the personal workspace, automatically.
    * A saved organization you still belong to: keep it.
    * Otherwise, at a terminal, ask (organizations plus personal). Without one,
      keep what scripts always got: the single organization if there is exactly
      one, else the personal workspace with the choices listed.
    """
    if os.environ.get("HARUMI_ORG"):
        return  # set on purpose for this shell; not ours to change
    try:
        client = _get_client(api_url=config.api_url)
        orgs = client.list_organizations()
    except HarumiError:
        return

    if not orgs:
        config.clear_org_id()  # also drops a saved org you have since left
        console.print("Workspace: your [bold]personal[/bold] workspace.")
        return

    saved = next((o for o in orgs if o.id == config.org_id), None)
    if saved is not None:
        console.print(
            f"Workspace: organization [bold]{escape(saved.business_name)}[/bold]. "
            "[dim]Change it with harumi org use.[/dim]"
        )
        return

    if not interactive.is_interactive():
        _workspace_without_menu(config, orgs)
        return

    picked = interactive.select(
        "Which workspace do you want to use?", _workspace_options(orgs), default=orgs[0].id
    )
    if picked is None:
        console.print("[dim]Skipped. Choose one any time with harumi org use.[/dim]")
        return
    _apply_workspace(config, str(picked))
    console.print("[dim]Change it any time with harumi org use.[/dim]")


def _workspace_without_menu(config: Config, orgs) -> None:
    """The no-terminal path: nobody to ask, so be predictable and say what to do."""
    if len(orgs) == 1:
        config.save_org_id(orgs[0].id)
        console.print(f"Using organization: [bold]{escape(orgs[0].business_name)}[/bold]")
        return
    config.clear_org_id()
    console.print(
        "Workspace: your [bold]personal[/bold] workspace. You also belong to these "
        "organizations; pick one with [bold]harumi org use <ORG_ID>[/bold] or pass "
        "[bold]--org[/bold] on a command:"
    )
    table = Table("id", "business_name")
    for org in orgs:
        table.add_row(org.id, org.business_name)
    console.print(table)


def _provision_git_token(config: Config) -> None:
    """Request a per-user Gitea token from harumi-api and configure the
    `harumi` remote if we're already inside a bound project directory."""
    try:
        client = _get_client(api_url=config.api_url)
        creds = client.get_git_token()
        save_git_token(creds.token, creds.git_url, creds.username)
    except HarumiError as exc:
        console.print(f"[yellow]Could not provision a Gitea token: {exc}[/yellow]")
        return

    binding = ProjectBinding.load()
    if binding is not None and repo_root() is not None:
        ensure_remote(
            clone_url=binding.repo.clone_url,
            username=creds.username,
            token=creds.token,
        )


@app.command()
def logout() -> None:
    """Log out: forget the login stored on this machine."""
    # Resolve the active env (honors --env / HARUMI_ENV / saved default).
    Config.load()
    was_logged_in = auth.has_session()
    auth.logout()
    where = _on(active_environment())
    if was_logged_in:
        console.print(f"Logged out{where}. [dim]Sign in again with harumi login.[/dim]")
    else:
        console.print(f"You were not logged in{where}; there was nothing to clear.")


@app.command()
def status() -> None:
    """Show the live status of the Harumi platform (from the public status page)."""
    url = active_status_url()  # honors --env / HARUMI_ENV / the saved default
    try:
        response = httpx.get(f"{url}/api/v1/endpoints/statuses", timeout=10.0)
        response.raise_for_status()
        endpoints = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        _fail(f"Could not read the status page at {url}: {escape(str(exc))}")
    # Anything but a non-empty list of objects (a proxy/captive-portal reply, an error
    # object) means nothing was actually read; "all up" would be a lie.
    if not isinstance(endpoints, list) or not endpoints or not all(isinstance(e, dict) for e in endpoints):
        _fail(f"Unexpected response from the status page at {url}.")

    table = Table("group", "service", "status")
    down = 0
    for endpoint in endpoints:
        results = endpoint.get("results") or []
        latest = results[-1] if results and isinstance(results[-1], dict) else None
        if latest is None:
            state = "[dim]unknown[/dim]"
        elif latest.get("success"):
            state = "[green]up[/green]"
        else:
            state = "[bold red]down[/bold red]"
            down += 1
        table.add_row(str(endpoint.get("group") or "-"), str(endpoint.get("name", "?")), state)
    console.print(table)
    if down:
        _fail(f"{down} service(s) down. Details: {url}")


@app.command()
@_handle_errors
def whoami(
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show the logged-in user: email, name and organization."""
    client = _get_client(api_url=api_url, org=org)
    profile = client.get_profile()
    console.print(
        f"[bold]{profile.email or '?'}[/bold]  (id: {profile.id or '?'})  "
        + (f"[dim]env: {client.config.environment}[/dim]" if client.config.environment != DEFAULT_ENVIRONMENT else "")
    )
    name = " ".join(part for part in (profile.first_name, profile.last_name) if part)
    for label, value in (
        ("Name", name),
        ("Bio", profile.bio),
        ("Organization", client.config.org_id),
    ):
        if value:
            console.print(f"{label}: {escape(str(value))}")


@app.command("whats-new")
def whats_new(
    all_: bool = typer.Option(False, "--all", "-a", help="Show every release's notes, newest first."),
) -> None:
    """Show what changed in the installed version (or every version with --all).

    Works offline: the notes ship inside the package.
    """
    from rich.markdown import Markdown

    from harumi import changelog

    releases = changelog.load()
    if not releases:
        _fail("No release notes are bundled with this install.")
    if not all_:
        releases = [next((r for r in releases if r.version == __version__), releases[0])]
    for release in releases:
        title = f"harumi {release.version}" + (f" ({release.date})" if release.date else "")
        console.print(f"[bold magenta]{title}[/bold magenta]")
        console.print(Markdown(release.notes))
    if not all_:
        console.print("[dim]Older releases: harumi whats-new --all[/dim]")


_TIER_LABELS = {
    "free": "Free",
    "personal": "Personal",
    "pro": "Pro",
    "enterprise": "Enterprise",
    "harumi_io": "harumi-io",
}


def _tier_label(tier: Optional[str]) -> str:
    """A plan's name for people. Known plans have their own spelling; a plan the CLI has
    not heard of yet (the 7-day trial arrived this way) is shown as words, not as a code."""
    if not tier:
        return "none"
    return _TIER_LABELS.get(tier) or tier.replace("_", " ").replace("-", " ").title()


def _fmt_reset(iso: Optional[str], *, with_time: bool) -> str:
    """`2026-10-20T15:00:00+00:00` as local "Tue Oct 20 15:00" (or just the date)."""
    if not iso:
        return "?"
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso.split("T", 1)[0]
    return when.strftime("%a %b %d %H:%M" if with_time else "%b %d")


def _ago(when: Optional[datetime]) -> str:
    """"3 days ago"-style age for a timestamp, or "" when there isn't one."""
    if when is None:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds < 60:
        return "just now"  # also a clock a little ahead of the server's
    for limit, unit_seconds, unit in (
        (3600, 60, "minute"),
        (86400, 3600, "hour"),
        (86400 * 30, 86400, "day"),
        (86400 * 365, 86400 * 30, "month"),
    ):
        if seconds < limit:
            n = int(seconds // unit_seconds)
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    n = int(seconds // (86400 * 365))
    return f"{n} year{'' if n == 1 else 's'} ago"


def _duration(seconds: Optional[float]) -> str:
    """"45s", "1m 12s", "2h 03m": how long something took."""
    if seconds is None or seconds < 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _run_duration(run) -> Optional[float]:
    """Seconds between a run's start and end, when the server recorded both."""
    if run.started is None or run.ended is None:
        return None
    started, ended = run.started, run.ended
    if (started.tzinfo is None) != (ended.tzinfo is None):
        started, ended = started.replace(tzinfo=None), ended.replace(tzinfo=None)
    return (ended - started).total_seconds()


def _tail_lines(text: str, keep: int) -> tuple[str, int]:
    """The last `keep` non-empty-trailing lines of `text`, and how many were left out."""
    lines = text.rstrip().splitlines()
    return "\n".join(lines[-keep:]), max(0, len(lines) - keep)


def _status_markup(status: str) -> str:
    """A run status, coloured by what it means. Plain words, so it still reads without colour."""
    colour = {
        "completed": "green",
        "finished": "green",
        "failed": "bold red",
        "timeout": "bold red",
        "cancelled": "yellow",
        "queued": "cyan",
        "running": "cyan",
    }.get(status)
    return f"[{colour}]{escape(status)}[/{colour}]" if colour else escape(status)


def _run_started(run) -> str:
    return _ago(run.started or run.created_at)


def _newest_runs_first(runs):
    """Runs by the date the menu shows (started, else created), newest first; undated last.

    Not trusting the order the API sends them in: cutting to the latest few before sorting
    would quietly offer the wrong few if that order ever changed.
    """
    floor = datetime.min.replace(tzinfo=timezone.utc)

    def key(r):
        when = r.started or r.created_at or floor
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)

    return sorted(runs, key=key, reverse=True)


def _run_option(r) -> interactive.Option:
    """One run as a menu row: a short id and its status, with its branch and age beside it."""
    hint = "  ".join(p for p in (r.git_branch or "", f"started {_run_started(r)}" if _run_started(r) else "") if p)
    return interactive.Option(f"{r.id[:8]}  {r.status}", r.id, hint=hint)


def _pick_run(client: Client, project_id: str, runs, *, action: str, flag: str) -> Optional[str]:
    """Choose one of `runs`; its id, or None if the user backed out."""
    options = [_run_option(r) for r in runs]
    return interactive.select(f"Which run do you want to {action}?", options, flag=flag)  # type: ignore[return-value]


def _shown_path(path: Path) -> str:
    """`path` as someone should type it: relative to where they are when that is
    shorter and stays below it, absolute otherwise."""
    try:
        rel = path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return str(path)
    return str(rel)


def _quote_for_shell(text: str, *, windows: Optional[bool] = None) -> str:
    """`text` as one argument that pastes into the user's shell.

    POSIX shells want single quotes; cmd.exe and PowerShell want double quotes, and
    single quotes would be taken literally by cmd.exe.
    """
    if os.name == "nt" if windows is None else windows:
        return subprocess.list2cmdline([text])
    return shlex.quote(text)


def _next_steps(folder: Path) -> None:
    """What to type after a project has been set up locally."""
    where = _shown_path(folder)
    # Quoted for the shell (a folder called "My Project" must paste), then escaped for
    # Rich (a "[" in a path is markup otherwise and silently vanishes).
    cd = f"cd {_quote_for_shell(where)}" if where != "." else ""
    steps = " then ".join(f"[bold]{escape(c)}[/bold]" for c in (cd, "harumi run --watch") if c)
    # soft_wrap: this is a command line someone will copy, so a long path must
    # not be broken across lines at the terminal width.
    console.print(f"\nNext: {steps}", soft_wrap=True)


def _recent_first(projects):
    """Most recently updated first, so the project you are working on leads.

    The server returns creation order. A project with no `updated_at` goes last
    rather than first, and ties keep the server's order.
    """
    floor = datetime.min.replace(tzinfo=timezone.utc)

    def key(p):
        when = p.updated_at or p.created_at or floor
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)

    return sorted(projects, key=key, reverse=True)


def _workspace_names(client: Client) -> dict[str, str]:
    """Organization id -> name, for labelling projects. Empty if it can't be fetched.

    Takes the caller's client rather than building one: `--api-url` and `--org` apply
    to the whole command, so the names must come from the API the projects came from.
    """
    try:
        return {o.id: o.business_name for o in client.list_organizations()}
    except HarumiError:
        return {}


def _workspace_label(project, names: dict[str, str]) -> str:
    if not project.customer_id:
        return "Personal"
    return names.get(project.customer_id) or project.customer_id[:8]


def _fmt_dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _meter_line(label: str, pct: Optional[float], resets_at: Optional[str], *, with_time: bool) -> str:
    if pct is None:
        return f"{label}: [dim]no limit[/dim]"
    color = "red" if pct >= 100 else "yellow" if pct >= 80 else "green"
    return (
        f"{label}: [bold {color}]{pct:.0f}%[/bold {color}] used"
        f"  [dim](resets {_fmt_reset(resets_at, with_time=with_time)})[/dim]"
    )


def _workspace_entitlement(client, org_id: Optional[str]):
    """The entitlement of the workspace `harumi usage` is showing, or `None`
    when it can't be read (an older server, an outage) — the usage itself
    still prints."""
    try:
        ent = client.get_entitlement()
    except ApiError:
        return None, None
    if not org_id:
        return ent, ent.personal
    return ent, next((o for o in ent.organizations if o.customer_id == org_id), None)


@app.command()
@_handle_errors
def usage(
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(
        None, "--org", help="Show an organization's pooled allowance instead of your personal one."
    ),
) -> None:
    """Show your (or --org's) plan, weekly and monthly usage, and overage.

    Usage is shown as a percentage of each limit; overage in dollars. These
    are the same limits `harumi run` and chat hit with a 402.
    """
    client = _get_client(api_url=api_url, org=org)
    org_id = client.config.org_id
    ent, workspace = _workspace_entitlement(client, org_id)
    if org_id:
        org_name = (workspace.name if workspace else None) or org_id
        where = f"organization [bold]{escape(org_name)}[/bold]"
    else:
        where = "your [bold]personal[/bold] workspace"

    if ent is not None and ent.paywall_enabled and workspace is not None and not workspace.active:
        console.print(f"Workspace: {where} — [bold red]no active plan[/bold red]")
        reason = workspace.reason or ("org_not_subscribed" if org_id else "no_subscription")
        console.print(f"[yellow]{_paywall_hint(reason)}[/yellow]")
        return

    usage_data = client.get_credit_usage()
    tier = usage_data.tier or (workspace.tier if workspace else None) or usage_data.plan_code
    console.print(f"Workspace: {where}")
    if usage_data.unlimited or tier == "harumi_io":
        console.print(f"Plan: [bold]{_tier_label(tier)}[/bold] — Harumi team, unlimited.")
        return

    status = usage_data.subscription_status
    status_note = ""
    if status == "past_due" and usage_data.grace_ends_at:
        status_note = (
            f"  [yellow](payment failed — read-only after "
            f"{_fmt_reset(usage_data.grace_ends_at, with_time=False)})[/yellow]"
        )
    console.print(f"Plan: [bold]{_tier_label(tier)}[/bold]{status_note}")

    weekly_pct, monthly_pct = usage_data.weekly_used_pct, usage_data.monthly_used_pct
    server_has_pcts = bool({"weekly_used_pct", "monthly_used_pct"} & usage_data.model_fields_set)
    if not server_has_pcts and usage_data.included_credits:
        # An older server without the percentage fields: derive the month's.
        used = max(0, usage_data.included_credits - usage_data.balance_credits)
        monthly_pct = 100.0 * used / usage_data.included_credits
        console.print(_meter_line("Monthly", monthly_pct, usage_data.period_end, with_time=False))
    else:
        console.print(_meter_line("Weekly", weekly_pct, usage_data.weekly_resets_at, with_time=True))
        console.print(_meter_line("Monthly", monthly_pct, usage_data.monthly_resets_at, with_time=False))

    member = usage_data.member
    if member is not None and (member.weekly_used_pct is not None or member.monthly_used_pct is not None):
        parts = []
        if member.weekly_used_pct is not None:
            parts.append(f"{member.weekly_used_pct:.0f}% of your weekly limit")
        if member.monthly_used_pct is not None:
            parts.append(f"{member.monthly_used_pct:.0f}% of your monthly limit")
        console.print(f"Your share: {' · '.join(parts)} used (set by your organization).")

    if tier == "free" or (not usage_data.overage_allowed and not usage_data.overage_enabled):
        console.print("Overage: [dim]not available on this plan[/dim].")
    elif usage_data.overage_paused:
        console.print("Overage: [bold red]paused[/bold red] — an overage invoice is unpaid.")
    elif usage_data.overage_enabled:
        console.print(
            f"Overage: [bold green]on[/bold green] — {_fmt_dollars(usage_data.overage_spent_cents)} "
            f"of {_fmt_dollars(usage_data.overage_cap_cents)} used this period."
        )
    else:
        console.print("Overage: [dim]off[/dim] — work stops (402) once a limit is reached.")

    over = any(p is not None and p >= 100 for p in (weekly_pct, monthly_pct))
    if over and not usage_data.overage_enabled:
        # Not _paywall_hint: that one sends you to `harumi usage`, which is where you are.
        # And overage is only an option on plans that have it.
        way_on = "upgrade or turn on overage" if usage_data.overage_allowed else "upgrade"
        console.print(f"[yellow]You've reached a limit. Visit {_billing_url()} to {way_on}.[/yellow]")


# ---------------------------------------------------------------------------
# harumi env
# ---------------------------------------------------------------------------

@app.command(
    "env",
    hidden=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def env_removed() -> None:
    """Removed: Harumi staff pick their environment when they log in."""
    _fail(
        "`harumi env` was removed. Harumi staff choose an environment when they run "
        "[bold]harumi login[/bold] (that choice is remembered).\n"
        "For scripts and CI, where there is no one to ask: set [bold]HARUMI_ENV=staging[/bold] "
        "(for example in the job's environment) to use it for every command, or pass "
        "[bold]--env staging[/bold] to a single one."
    )


# ---------------------------------------------------------------------------
# Profile sub-commands
# ---------------------------------------------------------------------------

profile_app = typer.Typer(help="View and update your account profile.")
app.add_typer(profile_app, name="profile")


@profile_app.command("show", hidden=True)
@_deprecated("harumi profile show", "harumi whoami")
@_handle_errors
def profile_show(
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show your account profile."""
    client = _get_client(api_url=api_url, org=org)
    p = client.get_profile()
    table = Table("field", "value")
    for field in ("id", "email", "first_name", "last_name", "bio"):
        table.add_row(field, str(getattr(p, field, "") or ""))
    console.print(table)


@profile_app.command("set")
@_handle_errors
def profile_set(
    first_name: Optional[str] = typer.Option(None, "--first-name", help="New first name."),
    last_name: Optional[str] = typer.Option(None, "--last-name", help="New last name."),
    bio: Optional[str] = typer.Option(None, "--bio", help="New bio text."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Update your account profile. Only provided fields are changed."""
    body: dict = {}
    if first_name:
        body["first_name"] = first_name
    if last_name:
        body["last_name"] = last_name
    if bio is not None:
        body["bio"] = bio

    if not body:
        _fail("No fields to update. Pass at least one flag (e.g. --first-name).")

    client = _get_client(api_url=api_url, org=org)
    p = client.update_profile(body)
    console.print(f"[bold green]Updated[/bold green] profile for [bold]{p.email}[/bold].")


# ---------------------------------------------------------------------------
# Config sub-commands
# ---------------------------------------------------------------------------

config_app = typer.Typer(help="Deprecated: use `harumi org use`.")
app.add_typer(config_app, name="config", hidden=True)


@_handle_errors
def org_use(
    org_id: Optional[str] = typer.Argument(
        None, help="Organization id to work in. Pick from a list if omitted."
    ),
    personal: bool = typer.Option(
        False, "--personal", help="Work in your personal workspace instead of an organization."
    ),
) -> None:
    """Choose the workspace (an organization, or your personal one) for every request.

    Leave the id out to pick from your organizations. The choice is remembered
    and sent with every request.
    """
    if personal and org_id:
        _fail("--personal and an organization id are mutually exclusive. Pass only one.")
    config = Config.load()

    if personal:
        config.clear_org_id()
        console.print("Workspace set to your [bold]personal[/bold] workspace.")
        return

    if org_id is None:
        orgs = _get_client().list_organizations()
        if not orgs:
            _fail("You don't belong to any organization, so you're already in your personal workspace.")
        picked = interactive.select(
            "Which workspace do you want to use?",
            _workspace_options(orgs),
            flag="the ORG_ID argument (or --personal)",
            default=config.org_id,
        )
        if picked is None:
            return
        if picked == PERSONAL_WORKSPACE:
            config.clear_org_id()
            console.print("Workspace set to your [bold]personal[/bold] workspace.")
            return
        org_id = str(picked)
        label = next((o.business_name for o in orgs if o.id == org_id), org_id)
    else:
        label = org_id

    config.save_org_id(org_id)
    console.print(f"Workspace set to organization [bold]{escape(label)}[/bold].")


config_app.command("set-org")(_deprecated("harumi config set-org", "harumi org use <ORG_ID>")(org_use))


# ---------------------------------------------------------------------------
# harumi agent-skills — seed the bundled agent skills onto this machine
# ---------------------------------------------------------------------------

agent_skills_app = typer.Typer(
    help="Install the bundled harumi-cli agent skills (Cursor, Claude Code, etc.). "
    "For a project's own skills, see `harumi skills`.",
    no_args_is_help=True,
)
app.add_typer(agent_skills_app, name="agent-skills")


@agent_skills_app.command("install")
def agent_skills_install(
    agent: Optional[list[str]] = typer.Option(
        None,
        "--agent",
        "-a",
        help=f"Target agent(s): {', '.join(skills_mod.AGENT_KEYS)}. Default: auto-detect.",
    ),
    project: bool = typer.Option(
        False, "--project", help=f"Write to ./{skills_mod.PROJECT_SKILLS_DIR} instead of a global agent directory."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print each source -> destination without writing anything."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite a destination even if it doesn't look like a skill."),
) -> None:
    """Copy the harumi-cli and harumi-cli-setup skills onto this machine.

    With no flags, auto-detects installed agents (Cursor/Claude Code/Codex)
    by their config directory and writes to each one's global skills folder.
    """
    try:
        written = skills_mod.install(agent_keys=agent, project=project, dry_run=dry_run, force=force)
        sources = {d.name: d for d in skills_mod.bundled_skill_dirs()}
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        _fail(str(exc))
        return
    verb = "Would write" if dry_run else "Wrote"
    for path in written:
        source = sources.get(path.name)
        console.print(f"[dim]{verb}:[/dim] {source} -> {path}" if source else f"[dim]{verb}:[/dim] {path}")
    if not dry_run:
        console.print(f"[bold green]Installed[/bold green] {len(written)} skill director{'y' if len(written) == 1 else 'ies'}.")


# Retired in 0.11: `harumi skill` (singular) collided with `harumi skills`.
skill_app = typer.Typer(help="Deprecated: use `harumi agent-skills`.")
app.add_typer(skill_app, name="skill", hidden=True)


@skill_app.command("path")
@_deprecated("harumi skill path", "harumi agent-skills install --dry-run")
def skill_path() -> None:
    """Print the local directory containing the bundled skills."""
    for skill_dir in skills_mod.bundled_skill_dirs():
        console.print(str(skill_dir))


# ---------------------------------------------------------------------------
# harumi skills — the platform's skill library, and the project's own skills
# ---------------------------------------------------------------------------

skills_app = typer.Typer(
    help="Preset skills from the Harumi skill library. (A project's own skills are plain files at "
    "skills/<slug>/SKILL.md in its repo: edit them in your clone and push with git.)",
    no_args_is_help=True,
)
app.add_typer(skills_app, name="skills")


@skills_app.command("library")
@_handle_errors
def skills_library(
    category: Optional[str] = typer.Option(None, "--category", help="Only presets in this category."),
    query: Optional[str] = typer.Option(
        None, "--query", "-q", help="Only presets whose name, description or tags contain this text."
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List the preset skills you can install into a project with `harumi skills add`."""
    client = _get_client(api_url=api_url, org=org)
    presets = client.list_skill_library()
    if category:
        presets = [p for p in presets if p.category.lower() == category.lower()]
    if query:
        needle = query.lower()
        presets = [
            p for p in presets if any(needle in text.lower() for text in (p.name, p.description, *p.tags))
        ]
    if not presets:
        console.print("No skills found.")
        return

    table = Table("slug", "name", "category", "version", "description")
    table.columns[0].no_wrap = True  # the slug is what you copy into `skills add`
    for p in sorted(presets, key=lambda p: p.slug):
        table.add_row(p.slug, p.name, p.category, str(p.version), p.description)
    console.print(table)


@skills_app.command("add")
@_handle_errors
def skills_add(
    slug: str = typer.Argument(..., help="Preset slug (see `harumi skills library`)."),
    force: bool = typer.Option(
        False, "--force", help="Overwrite skills/<slug>/SKILL.md even if it already exists."
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Install a library preset into this project's clone as skills/<slug>/SKILL.md.

    Writes the file only; commit and push it so the project's assistant can load it.
    The file records `source: harumi/<slug>` and the preset `version` in its frontmatter.
    """
    binding = ProjectBinding.load()
    if binding is None:
        _fail(
            "No Harumi project found in this directory (or any parent). Run this inside a clone: "
            "`harumi clone <PROJECT_ID>`, or `harumi link --project <PROJECT_ID>`."
        )
        return
    project_dir = binding.config_path.parent.parent
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", slug) or ".." in slug:
        _fail(f"{slug!r} is not a valid skill slug (lowercase letters, digits, '.', '_' and '-').")

    dest = project_dir / skills_mod.SKILLS_DIR / slug / "SKILL.md"
    if dest.exists() and not force:
        existing = skills_mod.frontmatter_source(dest.read_text(encoding="utf-8", errors="replace"))
        if existing == skills_mod.library_source(slug):
            _fail(f"{slug!r} is already installed at {dest}. Pass --force to reinstall it.")
        _fail(
            f"{dest} already exists and is not the library's {slug!r} preset. "
            "Pass --force to overwrite it."
        )

    client = _get_client(api_url=api_url, org=org)
    preset = client.get_skill_library(slug)
    try:
        content = skills_mod.with_install_provenance(preset.content, preset.slug, preset.version)
    except ValueError as exc:
        _fail(str(exc))
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    rel = f"{skills_mod.SKILLS_DIR}/{slug}"
    console.print(
        f"[bold green]Installed[/bold green] [bold]{preset.name}[/bold] (v{preset.version}) at {dest}.", soft_wrap=True
    )
    console.print(
        "Commit and push it so the project's assistant can use it:\n"
        f"  [bold]git add {rel} && git commit -m \"Install {slug} skill\" && git push harumi[/bold]"
    )


# ---------------------------------------------------------------------------
# Discovery commands
# ---------------------------------------------------------------------------

@app.command()
@_handle_errors
def specs(
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List the kernels a run can use: size, GPU, and which plans include them.

    Pass a kernel's name to `harumi run --kernel`, or set it as `kernel` in harumi.toml.
    """
    client = _get_client(api_url=api_url, org=org)
    specs_ = client.get_specs()
    if not specs_:
        console.print("No kernels are available to this workspace.")
        return
    # The name column never shrinks: it is what someone copies into --kernel, and a name
    # cut to fit is no use. The rest wrap or give way.
    # Short headers and the long label last, so the label is what wraps or gives way and
    # no header is cut ("subscription" became "subscript…" when it was a column of its own).
    table = Table("name", "cpu", "memory", "gpu", "plan", "label")
    table.columns[0].no_wrap = True
    table.columns[0].overflow = "fold"
    for spec in specs_:
        table.add_row(
            escape(spec.name),
            f"{spec.size.cpu:g}",
            escape(spec.size.memory),
            "yes" if spec.size.gpu else "no",
            "paid" if spec.subscription_required else "any",
            escape(spec.display_name),
        )
    console.print(table)
    console.print("[dim]Use one with [bold]harumi run --kernel <name>[/bold].[/dim]")


@app.command()
@_handle_errors
def blueprints(
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List project blueprints. Pass a blueprint's slug as `new --blueprint`."""
    client = _get_client(api_url=api_url, org=org)
    items = client.list_blueprints()
    if not items:
        console.print("No blueprints found.")
        return

    table = Table("slug", "name", "description")
    for b in items:
        table.add_row(b.slug, b.name, b.description)
    console.print(table)


# ---------------------------------------------------------------------------
# Shared binding/remote helper
# ---------------------------------------------------------------------------

def _bind_and_configure_remote(project_id: str, repo, cwd: Optional[Path] = None) -> bool:
    """Write .harumi/config.json for `project_id`/`repo` and configure the
    `harumi` git remote in the target directory (defaults to cwd), if possible.

    Shared by `link` (binding an existing checkout) and `new`/`push` (binding
    a just-created project after cloning/pushing its repo). `repo` is a
    `RepoInfo`-shaped object (owner/name/clone_url/default_branch).

    Returns whether the remote was actually configured — callers use this to
    decide whether it's safe to print a "you're ready to go" message.
    """
    from harumi.config import ProjectBinding, RepoBinding

    target = cwd or Path.cwd()
    binding = ProjectBinding.write(
        target,
        project_id=project_id,
        repo=RepoBinding(
            owner=repo.owner,
            name=repo.name,
            clone_url=repo.clone_url,
            default_branch=repo.default_branch,
        ),
    )
    console.print(
        f"Wrote [bold]{binding.config_path}[/bold] "
        f"(project={project_id}, repo={repo.owner}/{repo.name})."
    )
    console.print(f"View project: {active_platform_url()}/projects/{project_id}")

    # Configure the harumi git remote if we're inside a git repo.
    if repo_root(cwd=target) is None:
        console.print(
            "[yellow]Not inside a git repo — skipping remote setup.[/yellow]\n"
            f"Run [bold]git init[/bold] in {target}, then "
            f"[bold]harumi link --project {project_id}[/bold] again."
        )
        return False

    git_token = load_git_token()
    if not git_token:
        console.print(
            "[yellow]No Gitea token found — skipping remote setup.[/yellow]\n"
            "Run [bold]harumi login[/bold] to provision one."
        )
        return False

    username = load_git_username()
    if not username:
        console.print(
            "[yellow]No Gitea username on file — skipping remote setup.[/yellow]\n"
            "Run [bold]harumi login[/bold] again to re-provision your Gitea credentials."
        )
        return False

    ensure_remote(
        clone_url=repo.clone_url,
        username=username,
        token=git_token,
        cwd=target,
    )
    console.print("[bold green]Remote `harumi` configured.[/bold green]")
    return True


def _pick_project(client: Client, *, action: str, flag: str = "the PROJECT_ID argument") -> Optional[str]:
    """Let the user choose one of their projects; return its id, or None if they quit.

    With no terminal this raises `NonInteractiveError`, which lists the projects
    and says to pass the id, so a script or agent learns what to type instead of
    hanging on a prompt.
    """
    projects, total = client.list_projects_with_total()
    if not projects:
        _fail("You have no projects yet. Run [bold]harumi new <NAME>[/bold] to create one.")
    projects = _recent_first(projects)
    names = _workspace_names(client) if len({p.customer_id for p in projects}) > 1 else {}
    options = [
        interactive.Option(
            p.name,
            p.id,
            hint="  ".join(
                part
                for part in (
                    _workspace_label(p, names) if names else "",
                    _ago(p.updated_at or p.created_at),
                    p.id,
                )
                if part
            ),
        )
        for p in projects
    ]
    if total > len(projects):
        console.print(f"[dim]Showing {len(projects)} of {total} projects.[/dim]")
    picked = interactive.select(
        f"Which project do you want to {action}?", options, flag=flag
    )
    return picked  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# harumi link — bind an already-checked-out directory to a project
# ---------------------------------------------------------------------------

@app.command()
@_handle_errors
def link(
    project: Optional[str] = typer.Option(
        None, "--project", "-p", help="Harumi project id to bind this directory to. Pick from a list if omitted."
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Override the Harumi Git base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Bind the current directory to an existing Harumi project and configure the git remote.

    For the rare case where a project's code is already checked out by hand
    (or you're re-binding after moving the folder). Fetches the project's
    Gitea repo from harumi-api, writes .harumi/config.json, and configures
    the `harumi` git remote for HTTPS+token pushes — it does NOT fetch or
    clone the code itself.

    Starting fresh? Use `harumi new`, `harumi import`, or `harumi clone`
    instead — each of those binds automatically as part of getting you a
    working local checkout.
    """
    client = _get_client(api_url=api_url, git_url=git_url, org=org)

    if project is None:
        project = _pick_project(client, action="link this folder to", flag="--project")
        if project is None:  # backed out of the menu
            return

    console.print(f"Fetching repo for project [bold]{project}[/bold]...")
    repo = client.get_project_repo(project)

    _bind_and_configure_remote(project, repo)


# ---------------------------------------------------------------------------
# harumi import — turn an existing local folder into a new Harumi project
# ---------------------------------------------------------------------------

_DEFAULT_HARUMI_TOML = """\
# harumi.toml — how Harumi runs this project.
# Committed alongside your code: a run is whatever this file says, on the live branch.
[run]
command = "python main.py"
kernel = "or_python_small"
"""


def _ensure_scaffold(folder: Path) -> None:
    """Write a minimal `harumi.toml` into `folder` if one isn't already there.

    `harumi import` force-pushes the folder as-is, overwriting whatever
    scaffold `POST /projects` seeded server-side — so, unlike `harumi new`,
    a pushed project has no run manifest unless the folder already had one.
    Without this, the very first `harumi run` afterward fails with a missing
    harumi.toml, which looks like a bug rather than a documented tradeoff of
    pushing your own files over the seed.
    """
    manifest = folder / "harumi.toml"
    if manifest.exists():
        return
    manifest.write_text(_DEFAULT_HARUMI_TOML)
    console.print(
        f"[dim]No harumi.toml in {folder} — wrote a minimal one "
        "(command: python main.py, kernel: or_python_small). Edit it if your "
        "entry point is different.[/dim]"
    )


def _merge_git_repo_flat(from_git: str, dest: Path) -> None:
    """Clone `from_git` and copy its tree (minus .git) FLAT into `dest`.

    Mirrors how the legacy sandbox cloned the connected GitHub repo into the run
    working directory (next to the code), so relative imports/opens keep working.
    On a filename collision the existing (exported) file wins — it is the
    authoritative runnable artifact — and we warn.
    """
    import shutil
    import tempfile

    from harumi.git import _run  # local git subprocess runner

    with tempfile.TemporaryDirectory() as tmp:
        clone_dir = Path(tmp) / "repo"
        console.print(f"Cloning [bold]{from_git}[/bold]...")
        _run(["clone", "--depth", "1", from_git, str(clone_dir)])
        shutil.rmtree(clone_dir / ".git", ignore_errors=True)

        collisions: list[str] = []
        for src in clone_dir.rglob("*"):
            rel = src.relative_to(clone_dir)
            target = dest / rel
            if src.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if target.exists():
                collisions.append(str(rel))
                continue  # exported file wins
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)

    if collisions:
        console.print(
            "[yellow]Kept the existing version of "
            f"{len(collisions)} file(s) that also exist in the repo "
            f"(e.g. {', '.join(collisions[:3])}); merge manually if needed.[/yellow]"
        )


@app.command("import")
@_handle_errors
def import_project(
    path: Path = typer.Argument(
        Path("."),
        help="Folder of existing code to turn into a new project. Defaults to the current directory.",
    ),
    project_name: Optional[str] = typer.Option(
        None, "--project-name", help="Name for the new project (defaults to the folder name)."
    ),
    from_git: Optional[str] = typer.Option(
        None,
        "--from-git",
        help="Also clone this git URL (e.g. an old GitHub repo) flat into the folder before pushing.",
    ),
    bind: bool = typer.Option(
        True, "--bind/--no-bind", help="Bind the folder to the new project afterward (default: yes)."
    ),
    personal: bool = typer.Option(
        False,
        "--personal",
        help="Create in your personal workspace, ignoring the configured org.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Don't ask for a name or confirmation: use the folder name and go."
    ),
    include_tracked_secrets: bool = typer.Option(
        False,
        "--include-tracked-secrets",
        help="Upload secret-looking files that git already tracks, or has in its history. Without a "
        "terminal, or with --yes, the import stops instead.",
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Override the Harumi Git base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Turn an existing local folder of code into a new Harumi project.

    Use this when you already have code on your machine (from anywhere —
    an old GitHub repo, a colleague's zip, a script you've been iterating
    on locally) and want to move it into Harumi. Creates a project, then
    force-pushes the folder's contents as the repo's first commit — this
    intentionally overwrites the server-seeded scaffold, so if the folder
    has no `harumi.toml` one is written for you before pushing.

    First it shows what would be uploaded. Virtual environments, `node_modules`
    and files that look like secrets (`.env`, `*.pem`, `*.key`) are left out,
    and at a terminal you are asked for a name and to confirm.

    Starting with nothing at all? Use `harumi new <NAME>` instead — it
    clones the seeded scaffold rather than overwriting it.
    """
    _push_impl(
        path=path,
        project_name=project_name,
        from_git=from_git,
        bind=bind,
        personal=personal,
        assume_yes=yes,
        include_tracked_secrets=include_tracked_secrets,
        api_url=api_url,
        git_url=git_url,
        org=org,
    )


def _print_import_plan(folder: Path, plan: ImportPlan) -> None:
    """What an import is about to upload and what it leaves out, in a few lines."""
    history = (
        f", plus {plan.history_commits} commit{'' if plan.history_commits == 1 else 's'} of git history"
        if plan.history_commits
        else ""
    )
    console.print(
        f"Importing [bold]{escape(str(folder))}[/bold]: [bold]{plan.files}[/bold] "
        f"file{'' if plan.files == 1 else 's'}, {_format_bytes(plan.total_bytes)}{history}."
    )
    left_out = [f"{escape(d)}/ ({n} file{'' if n == 1 else 's'})" for d, n in plan.skipped_dirs]
    if plan.skipped_secrets:
        left_out.append("files that look like secrets: " + ", ".join(escape(f) for f in plan.skipped_secrets[:5]))
        if len(plan.skipped_secrets) > 5:
            left_out[-1] += f" and {len(plan.skipped_secrets) - 5} more"
    if left_out:
        console.print("[dim]Left out: " + "; ".join(left_out) + ".[/dim]")
    if plan.skipped_secrets:
        console.print("[dim]API keys belong in [bold]harumi secrets set NAME[/bold], not in the code.[/dim]")
    big = [(f, n) for f, n in plan.largest if n >= LARGE_FILE_BYTES]
    if big:
        console.print(
            "[yellow]Large files make the upload slow:[/yellow] "
            + ", ".join(f"{escape(f)} ({_format_bytes(n)})" for f, n in big)
            + ". Data files usually belong in [bold]harumi files put[/bold]."
        )
    if plan.included_secrets:
        console.print(
            "[bold yellow]Heads-up:[/bold yellow] "
            + ", ".join(escape(f) for f in plan.included_secrets[:5])
            + " looks like a secret, but git already tracks it, so it [bold]will be uploaded[/bold]"
            + (" with its history" if plan.history_commits else "")
            + ". Stop here if that is not what you want, and remove it from git first "
            "([bold]git rm --cached <file>[/bold])."
        )
    in_history_only = [f for f in plan.history_secrets if f not in plan.included_secrets]
    if in_history_only:
        console.print(
            "[bold yellow]Heads-up:[/bold yellow] your git history contains "
            + ", ".join(escape(f) for f in in_history_only[:5])
            + (f" and {len(in_history_only) - 5} more" if len(in_history_only) > 5 else "")
            + ". It is not in your files any more, but the history is [bold]uploaded as it is[/bold], "
            "so anyone with access to the project could read it. Rotate that secret, or start from a clean copy "
            "of the folder without the .git folder."
        )


def _ask_project_name(default: Optional[str]) -> str:
    """Ask for a name; a blank answer is asked again rather than sent to the server."""
    while True:
        name = (typer.prompt("Project name", default=default) if default else typer.prompt("Project name")).strip()
        if name:
            return name
        console.print("A project needs a name.")


def _push_impl(
    *,
    path: Path,
    project_name: Optional[str],
    from_git: Optional[str],
    bind: bool,
    personal: bool,
    api_url: Optional[str],
    git_url: Optional[str],
    org: Optional[str],
    assume_yes: bool = False,
    include_tracked_secrets: bool = False,
) -> None:
    """Body of `harumi import`, factored out so `harumi start` can call it
    directly without going through Typer's CLI-parsing machinery (whose
    `Option`/`Argument` defaults are sentinel objects, not the real
    defaults, when a decorated command function is called as plain Python).
    """
    folder = path.resolve()
    if not folder.is_dir():
        _fail(f"Not a directory: {folder}")

    linked = folder / ".harumi" / "config.json"
    if linked.exists():
        binding = ProjectBinding.load(folder)
        which = f" ({binding.project_id})" if binding else ""
        _fail(
            f"{folder} is already a Harumi project{which}, and `harumi import` always creates a "
            "[bold]new[/bold] one.\n"
            "To send your changes to this project, commit and run [bold]git push harumi <branch>[/bold]; "
            "to just run them, use [bold]harumi run[/bold].\n"
            "If you really want a second project from this folder, delete its .harumi folder first."
        )

    client = _get_client(api_url=api_url, git_url=git_url, org=org)
    # Before the merge and scaffold below change the user's folder.
    _require_entitled_workspace(client, None if personal else client.config.org_id)

    if from_git:
        _merge_git_repo_flat(from_git, folder)

    # The summary has to count what will really be pushed, which includes the
    # harumi.toml written below. So preview with the file counted in, and only
    # write it once the user has agreed.
    plan = plan_import(folder)
    needs_manifest = not (folder / "harumi.toml").exists()
    if needs_manifest:
        plan.files += 1
        plan.total_bytes += len(_DEFAULT_HARUMI_TOML.encode())
    _print_import_plan(folder, plan)
    can_ask = interactive.is_interactive() and not assume_yes
    at_risk = sorted({*plan.included_secrets, *plan.history_secrets})
    if at_risk and not can_ask and not include_tracked_secrets:
        # Nobody to say no: --yes is consent to "go ahead", not to publish a credential.
        _fail(
            "Not importing: git already has " + ", ".join(escape(f) for f in at_risk[:5])
            + (f" and {len(at_risk) - 5} more" if len(at_risk) > 5 else "")
            + ", which look like secrets, and they would be uploaded.\n"
            "Remove them from git ([bold]git rm --cached <file>[/bold]) and rotate them, or, "
            "if you really want them uploaded, run again with [bold]--include-tracked-secrets[/bold]."
        )
    name = project_name or folder.name
    if can_ask:
        if project_name is None:
            name = _ask_project_name(folder.name)
        if not typer.confirm(
            "Create the project and upload these files?", default=not at_risk
        ):
            console.print("Nothing was created.")
            return

    _ensure_scaffold(folder)

    console.print(f"Creating project [bold]{escape(name)}[/bold]...")
    project = client.create_project(name, personal=personal)
    console.print(
        f"[bold green]Created[/bold green] project [bold]{project.name}[/bold] (id={project.id})."
    )
    _print_project_workspace(project.customer_id)

    if project.repo is None:
        console.print(
            "[yellow]No Gitea repo was provisioned for this project "
            "(Harumi Git may not be configured on this backend); nothing pushed.[/yellow]"
        )
        return

    repo = project.repo
    git_token = load_git_token()
    if not git_token:
        console.print(
            "[yellow]No Gitea token found — can't push.[/yellow] "
            "Run [bold]harumi login[/bold], then push manually from the folder."
        )
        return

    username = load_git_username()
    if not username:
        _fail(
            "No Gitea username on file. Run [bold]harumi login[/bold] again to "
            "re-provision your Gitea credentials, then retry."
        )

    console.print("Pushing your code...")
    push_folder(
        folder,
        clone_url=repo.clone_url,
        username=username,
        token=git_token,
        branch=repo.default_branch,
        message="Push local project",
        exclude=import_excludes(),
    )
    console.print(f"[bold green]Pushed[/bold green] to Harumi ({repo.default_branch}).")

    if not bind:
        console.print(f"View project: {active_platform_url()}/projects/{project.id}")
        return

    if _bind_and_configure_remote(project.id, repo, cwd=folder):
        _next_steps(folder)


# ---------------------------------------------------------------------------
# harumi new — create a project from nothing and clone its scaffold locally
# ---------------------------------------------------------------------------

@app.command()
@_handle_errors
def new(
    name: Optional[str] = typer.Argument(None, help="Project name. Asked for at a terminal if omitted."),
    dir: Optional[Path] = typer.Option(
        None, "--dir", help="Local directory to clone the new project into. Defaults to ./<slugified-name>."
    ),
    customer_id: Optional[str] = typer.Option(
        None,
        "--customer-id",
        help="Organization id to create the project under. Defaults to the configured org.",
    ),
    personal: bool = typer.Option(
        False,
        "--personal",
        help="Create in your personal workspace, ignoring the configured org.",
    ),
    blueprint: Optional[str] = typer.Option(
        None, "--blueprint", help="Blueprint slug to seed the project from (see `harumi blueprints`, optional)."
    ),
    no_clone: bool = typer.Option(
        False,
        "--no-clone",
        help="Only create the project: don't clone or bind anything locally (for scripts).",
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Override the Harumi Git base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Create a brand-new Harumi project and clone its scaffold locally.

    Use this when you have nothing yet. Creates the project (which
    provisions a Harumi Git repo pre-seeded with a runnable `harumi.toml` +
    `main.py` + `dashboard.toml`, or a blueprint's files if `--blueprint` is
    given), then clones that repo into a local directory and binds it — so
    `harumi run` works immediately, no manual push required.

    Already have code you want to bring in instead? Use `harumi import`.
    Project already exists in Harumi (web app or a teammate made it)? Use
    `harumi clone <PROJECT_ID>`.
    """
    if name is None:
        if not interactive.is_interactive():
            _fail("Give the project a name: [bold]harumi new \"My Project\"[/bold].")
        name = typer.prompt("Project name")
    _new_impl(
        name=name,
        dir=dir,
        customer_id=customer_id,
        personal=personal,
        blueprint=blueprint,
        clone=not no_clone,
        api_url=api_url,
        git_url=git_url,
        org=org,
    )


def _require_entitled_workspace(client, customer_id: Optional[str]) -> None:
    """Stop before creating anything when the target workspace has no plan.

    `customer_id=None` is the personal workspace. Advisory only: skipped when
    the paywall is off or the lookup fails (an older server without
    `GET /billing/entitlement/me`, an outage) — `POST /projects` still answers
    402 then.
    """
    try:
        ent = client.get_entitlement()
    except ApiError:
        return
    if not ent.paywall_enabled:
        return
    if customer_id is None:
        if ent.personal.active:
            return
        message = "Your personal workspace doesn't have an active plan.\n" + _paywall_hint(
            ent.personal.reason or "no_subscription"
        )
        entitled_org = next((o for o in ent.organizations if o.active), None)
        if entitled_org is not None:
            message += (
                f"\nOr create it in {escape(entitled_org.name or entitled_org.customer_id)}: "
                f"[bold]harumi org use {escape(entitled_org.customer_id)}[/bold]"
            )
        _fail(message)
    org = next((o for o in ent.organizations if o.customer_id == customer_id), None)
    if org is not None and not org.active:
        _fail(
            f"Organization {escape(org.name or customer_id)} doesn't have an active plan yet.\n"
            + _paywall_hint(org.reason or "org_not_subscribed")
        )


def _new_impl(
    *,
    name: str,
    dir: Optional[Path],
    customer_id: Optional[str],
    personal: bool,
    blueprint: Optional[str],
    api_url: Optional[str],
    git_url: Optional[str],
    org: Optional[str],
    clone: bool = True,
) -> None:
    """Body of `harumi new` — see `_push_impl` for why this is factored out."""
    if personal and customer_id:
        _fail("--personal and --customer-id are mutually exclusive. Pass only one.")
    if not clone and dir is not None:
        _fail("--dir only applies when cloning. Drop --no-clone, or drop --dir.")

    client = _get_client(api_url=api_url, git_url=git_url, org=org)
    _require_entitled_workspace(client, None if personal else (customer_id or client.config.org_id))

    console.print(f"Creating project [bold]{name}[/bold]...")
    project = client.create_project(
        name, customer_id=customer_id, blueprint=blueprint, personal=personal
    )
    console.print(f"[bold green]Created[/bold green] project [bold]{project.name}[/bold] (id={project.id}).")
    _print_project_workspace(project.customer_id)

    if not clone:
        console.print(f"Clone it locally: [bold]harumi clone {project.id}[/bold]")
        return

    if project.repo is None:
        console.print(
            "[yellow]No Gitea repo was provisioned for this project "
            "(Harumi Git may not be configured on this backend).[/yellow]"
        )
        return

    dest = (dir or Path.cwd() / _slugify(project.name)).resolve()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        _fail(f"{dest} already exists and isn't empty. Pass --dir to clone somewhere else.")

    _clone_and_bind(project.id, project.repo, dest)


# ---------------------------------------------------------------------------
# harumi clone — fetch an existing Harumi project's code locally
# ---------------------------------------------------------------------------

def _clone_and_bind(project_id: str, repo, dest: Path) -> None:
    """Clone `repo` into `dest`, then bind `dest` to `project_id`.

    Shared by `new` (a repo it just created) and `clone` (a repo that
    already existed). Requires a Gitea token/username on file — `harumi
    login` provisions both. A missing credential is a warning, not a
    failure: the project itself was already created/exists server-side by
    the time this runs, so this mirrors `_push_impl`'s convention for the
    same precondition rather than reporting what looks like a failed
    project creation.
    """
    git_token = load_git_token()
    if not git_token:
        console.print(
            "[yellow]No Gitea token found — can't clone.[/yellow] "
            "Run [bold]harumi login[/bold], then [bold]harumi clone "
            f"{project_id}[/bold] to try again."
        )
        return

    username = load_git_username()
    if not username:
        console.print(
            "[yellow]No Gitea username on file — can't clone.[/yellow] "
            "Run [bold]harumi login[/bold] again to re-provision your Gitea "
            f"credentials, then [bold]harumi clone {project_id}[/bold] to try again."
        )
        return

    console.print(f"Cloning into [bold]{dest}[/bold]...", soft_wrap=True)
    clone_repo(
        clone_url=repo.clone_url,
        username=username,
        token=git_token,
        dest=dest,
    )

    if _bind_and_configure_remote(project_id, repo, cwd=dest):
        _next_steps(dest)


@app.command()
@_handle_errors
def clone(
    project: Optional[str] = typer.Argument(
        None,
        help="Harumi project id to clone (see `harumi projects list`). Pick from a list if omitted.",
    ),
    dir: Optional[Path] = typer.Option(
        None, "--dir", help="Local directory to clone into. Defaults to ./<slugified-project-name>."
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Override the Harumi Git base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Fetch an existing Harumi project's code onto your machine.

    Use this for a project that already exists in Harumi — created via the
    web app, by a teammate, or in an earlier session — and binds the clone
    so `harumi run` works right away. Leave the id out to pick from your
    projects, or find one with [bold]harumi projects list[/bold].
    """
    _clone_impl(project=project, dir=dir, api_url=api_url, git_url=git_url, org=org)


def _clone_impl(
    *,
    project: Optional[str],
    dir: Optional[Path],
    api_url: Optional[str],
    git_url: Optional[str],
    org: Optional[str],
) -> None:
    """Body of `harumi clone` — see `_push_impl` for why this is factored out."""
    client = _get_client(api_url=api_url, git_url=git_url, org=org)

    if project is None:
        project = _pick_project(client, action="clone")
        if project is None:  # the user backed out of the menu
            return

    console.print(f"Fetching repo for project [bold]{project}[/bold]...")
    repo = client.get_project_repo(project)

    proj = client.get_project(project)
    dest = (dir or Path.cwd() / _slugify(proj.name)).resolve()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        _fail(f"{dest} already exists and isn't empty. Pass --dir to clone somewhere else.")

    _clone_and_bind(project, repo, dest)


# ---------------------------------------------------------------------------
# harumi start — the one entry point for "how do I begin"
# ---------------------------------------------------------------------------

@app.command()
@_handle_errors
def start() -> None:
    """Interactively figure out which of the three onboarding journeys you
    want, and run it.

    Equivalent to picking one of `harumi new`, `harumi import`, or
    `harumi clone` yourself — this just asks the question first.
    """
    if not auth.has_session():
        # Before the questions: answering three of them only to be told to log in is a waste.
        _fail(str(NotAuthenticatedError(environment=active_environment())))
    console.print("[bold]What do you have?[/bold]\n")
    console.print("  1. Nothing yet — start a brand-new project")
    console.print("  2. A folder of code on this machine — bring it into Harumi")
    console.print("  3. A project that already exists in Harumi — get its code here")
    choice = ""
    while choice not in {"1", "2", "3"}:
        choice = typer.prompt("Choice (1/2/3)")
    if choice in {"1", "2"}:
        # Both create a project in the configured org (or Personal): check
        # that workspace before asking for a name or path. Cloning (3) stays
        # open: a read-only project can still be fetched.
        client = _get_client()
        _require_entitled_workspace(client, client.config.org_id)

    if choice == "1":
        name = _ask_project_name(None)
        _new_impl(
            name=name,
            dir=None,
            customer_id=None,
            personal=False,
            blueprint=None,
            api_url=None,
            git_url=None,
            org=None,
        )
    elif choice == "2":
        path_str = typer.prompt("Path to the folder", default=".")
        _push_impl(
            path=Path(path_str),
            project_name=None,
            from_git=None,
            bind=True,
            personal=False,
            api_url=None,
            git_url=None,
            org=None,
        )
    else:
        _clone_impl(project=None, dir=None, api_url=None, git_url=None, org=None)


# ---------------------------------------------------------------------------
# harumi projects
# ---------------------------------------------------------------------------

projects_app = typer.Typer(help="List and manage Harumi projects (create one with `harumi new`).")
app.add_typer(projects_app, name="projects")


@projects_app.command("create", hidden=True)
@_deprecated("harumi projects create", "harumi new <NAME> --no-clone")
@_handle_errors
def projects_create(
    name: str = typer.Argument(..., help="Project name."),
    customer_id: Optional[str] = typer.Option(
        None,
        "--customer-id",
        help="Organization id to create the project under. Defaults to the configured org.",
    ),
    personal: bool = typer.Option(
        False,
        "--personal",
        help="Create in your personal workspace, ignoring the configured org.",
    ),
    blueprint: Optional[str] = typer.Option(
        None, "--blueprint", help="Blueprint slug to seed the project from (see `harumi blueprints`, optional)."
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Override the Harumi Git base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Create a new Harumi project. Doesn't touch the local directory or git."""
    _new_impl(
        name=name,
        dir=None,
        customer_id=customer_id,
        personal=personal,
        blueprint=blueprint,
        clone=False,
        api_url=api_url,
        git_url=git_url,
        org=org,
    )


@projects_app.command("list")
@_handle_errors
def projects_list(
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-n", min=1, help="Show only the most recently updated N projects."
    ),
) -> None:
    """List your projects, most recently updated first."""
    client = _get_client(api_url=api_url, org=org)
    projects, total = client.list_projects_with_total()
    if not projects:
        console.print("No projects found.")
        return

    projects = _recent_first(projects)
    shown = projects[:limit] if limit else projects
    names = _workspace_names(client) if len({p.customer_id for p in projects}) > 1 else {}

    columns = ["id", "name", "updated"] + (["workspace"] if names else []) + ["kernel_spec", "role"]
    table = Table(*columns)
    for p in shown:
        row = [p.id, p.name, _ago(p.updated_at or p.created_at)]
        if names:
            row.append(_workspace_label(p, names))
        row += [p.kernel_spec or "", p.role_name or ""]
        table.add_row(*row)
    console.print(table)
    if len(shown) < total:
        console.print(
            f"[dim]Showing {len(shown)} of {total} projects, most recently updated first."
            + ("" if limit else " The rest could not be fetched.")
            + (" Drop --limit to see more." if limit and len(shown) < len(projects) else "")
            + "[/dim]"
        )


@projects_app.command("get")
@_handle_errors
def projects_get(
    project_id: Optional[str] = typer.Argument(None, help="Project id. Pick from a list if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show a project: its name, where it lives, and how to open it."""
    client = _get_client(api_url=api_url, org=org)
    if project_id is None:
        project_id = _pick_project(client, action="show")
        if project_id is None:
            return
    p = client.get_project(project_id)
    names = _workspace_names(client) if p.customer_id else {}
    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="dim")
    facts.add_column()
    for label, value in (
        ("Name", escape(p.name)),
        ("Id", escape(p.id)),
        ("Workspace", escape(_workspace_label(p, names))),
        ("Your role", escape(p.role_name or "")),
        ("Kernel", escape(p.kernel_spec or "")),
        ("Created", _ago(p.created_at)),
        ("Updated", _ago(p.updated_at)),
        ("Open", f"{active_platform_url()}/projects/{p.id}"),
    ):
        if value:
            facts.add_row(label, value)
    console.print(facts)


@projects_app.command("rename")
@_handle_errors
def projects_rename(
    project_id: Optional[str] = typer.Argument(None, help="Project id. Pick from a list if omitted."),
    name: Optional[str] = typer.Argument(None, help="New project name. Asked for if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Rename a project."""
    client = _get_client(api_url=api_url, org=org)
    if project_id is None:
        project_id = _pick_project(client, action="rename")
        if project_id is None:
            return
    if name is None:
        if not interactive.is_interactive():
            _fail("Give the new name: [bold]harumi projects rename <PROJECT_ID> \"New name\"[/bold].")
        current = client.get_project(project_id).name
        name = typer.prompt("New name", default=current)
    p = client.update_project(project_id, {"name": name})
    console.print(f"[bold green]Renamed[/bold green] project to [bold]{escape(p.name)}[/bold].")


# How long a project stays in the Trash before the daily sweep deletes it. harumi-api's
# `project_trash_retention_days` (7 by default; the API does not send it to clients) and the
# web app's Trash page both use 7.
_TRASH_DAYS = 7


@projects_app.command("rm")
@_handle_errors
def projects_rm(
    project_id: Optional[str] = typer.Argument(None, help="Project id. Pick from a list if omitted."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Move a project to the Trash. You can restore it for 7 days.

    The project stops at once: its schedules are cancelled, a deployed app is shut down and its share links are removed (restoring does not bring them back).

    It stays in the Trash for 7 days. Restore it from the Trash in the web app; after that it is deleted for good.
    """
    if project_id is None and yes:
        # --yes skips the typed-name check, so nothing would stand between a slip in a
        # list and a deleted project. With --yes the id has to be written down.
        _fail("Say which project to move to the Trash: [bold]harumi projects rm <project id> --yes[/bold]. "
              "Find the id with [bold]harumi projects list[/bold].")
    client = _get_client(api_url=api_url, org=org)
    if project_id is None:
        # Picking is not consent: the typed-name confirmation below still applies.
        project_id = _pick_project(client, action="move to the Trash")
        if project_id is None:
            return

    if not yes:
        p = client.get_project(project_id)
        console.print(
            f"[bold]{escape(p.name)}[/bold] goes to the Trash. It stops running now (schedules cancelled, "
            f"app shut down) and its share links are removed. You can restore it within {_TRASH_DAYS} days."
        )
        typed = typer.prompt(f"Type the project name ({p.name!r}) to move it to the Trash")
        if typed != p.name:
            console.print("Aborted (name did not match).")
            return

    deleted = client.delete_project(project_id)
    console.print(
        f"[bold]Moved[/bold] project [bold]{escape(deleted.name)}[/bold] to the Trash. "
        "It has stopped running and its share links are removed."
    )
    console.print(
        f"Restore it within {_TRASH_DAYS} days from the Trash in the web app ({active_platform_url()}); "
        "after that it is deleted for good.",
        soft_wrap=True,
    )


# ---------------------------------------------------------------------------
# harumi run
# ---------------------------------------------------------------------------

@app.command()
@_handle_errors
def run(
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="Run a specific branch."),
    commit: Optional[str] = typer.Option(None, "--commit", help="Run a specific commit SHA."),
    command: Optional[str] = typer.Option(None, "--command", "-c", help="Override the harumi.toml command."),
    kernel: Optional[str] = typer.Option(None, "--kernel", "-k", help="Kernel to run on (see `harumi specs`), overriding harumi.toml."),
    watch: bool = typer.Option(False, "--watch", "-w", help="Block until the run finishes."),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        "-o",
        hidden=True,
        help="Deprecated: use `harumi runs download`.",
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Override the Harumi Git base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Run the project via its Harumi Git repo.

    By default, automatically pushes the working tree to a scratch branch so
    you can iterate without committing manually.  Pass --branch or --commit
    to run a specific ref instead.

    Requires a working directory produced by `harumi new`/`harumi import`/
    `harumi clone` (or `harumi link`, if the checkout already exists).
    """
    if output_dir:
        _warn_deprecated("harumi run --output-dir", "harumi runs download [RUN_ID] -o DIR")
    binding = ProjectBinding.load()
    if binding is None:
        _fail(
            "No Harumi project found in this directory (or any parent). "
            "Run `harumi new` to start one, `harumi clone <PROJECT_ID>` to fetch "
            "an existing one, or `harumi link --project <PROJECT_ID>` if this "
            "directory already has the project's code."
        )

    client = _get_client(api_url=api_url, git_url=git_url, org=org)
    project_id = binding.project_id  # type: ignore[union-attr]
    repo = binding.repo  # type: ignore[union-attr]

    scratch_branch: Optional[str] = None

    if branch or commit:
        # Explicit ref — run it directly, no scratch needed.
        run_branch = branch
        run_commit = commit
        console.print(_running_line(project_id, run_branch, run_commit))
    else:
        # No explicit ref — inspect the working tree.
        dirty = is_dirty()
        unpushed = has_unpushed_commits()

        if not dirty and not unpushed:
            # Clean and pushed: run the current branch directly.
            run_branch = current_branch() or repo.default_branch
            run_commit = None
            console.print(_running_line(project_id, run_branch, None))
        else:
            # Dirty or unpushed: push a scratch branch.
            git_token = load_git_token()
            if not git_token:
                _fail(
                    "No Gitea token found. Run [bold]harumi login[/bold] to provision one."
                )

            username = load_git_username()
            if not username:
                _fail(
                    "No Gitea username on file. Run [bold]harumi login[/bold] again to "
                    "re-provision your Gitea credentials."
                )

            what = " and ".join(
                part
                for part, present in (("uncommitted changes", dirty), ("unpushed commits", unpushed))
                if present
            )
            console.print(
                f"Running project [bold]{project_id}[/bold] with your {what} "
                "[dim](sent on a temporary branch; your branches are not touched)[/dim]"
            )

            scratch_branch, _ = push_scratch(username=username, token=git_token)  # type: ignore[arg-type]
            run_branch = scratch_branch
            run_commit = None

    try:
        try:
            response = client.execute_project(
                project_id,
                branch=run_branch,
                commit=run_commit,
                command=command,
                kernel_spec=kernel,
            )
        except ApiError as exc:
            # A failed token refresh is the login's problem, not the project's, whatever
            # status it came back with.
            if exc.status_code in _RUN_REJECTED and not isinstance(exc, SessionRefreshError):
                _fail(
                    f"{_format_api_error(exc)}\n"
                    "Run [bold]harumi check[/bold] to see what is blocking this project."
                )
            raise
        run_id = response.project_run_id
        console.print(f"Queued run [bold]{run_id}[/bold]" if run_id else "Queued.")

        if not watch:
            if run_id:
                console.print(
                    f"[dim]Check on it with [bold]harumi runs get {run_id}[/bold], "
                    "or add --watch to wait for it here.[/dim]"
                )
            return

        if not run_id:
            console.print("[yellow]No run id returned; cannot watch this run.[/yellow]")
            return

        result = _watch_run(client, project_id, run_id)
        if result.succeeded:
            took = _duration(_run_duration(result))
            console.print(
                f"[bold green]Run {result.status}[/bold green]" + (f" in {took}." if took else ".")
            )
            if output_dir:
                from harumi.execution import download_run_output

                try:
                    zip_path = download_run_output(client.api, project_id, result, output_dir)
                    console.print(f"Downloaded output to {zip_path}", soft_wrap=True)
                except HarumiError as exc:
                    console.print(f"[yellow]{exc}[/yellow]")
            else:
                console.print(
                    f"[dim]Download its output with [bold]harumi runs download {result.id}[/bold].[/dim]"
                )
        else:
            _print_failed_run(result)
            raise typer.Exit(code=1)

    finally:
        if scratch_branch:
            delete_remote_scratch(scratch_branch)


# A server that refuses to start the run for a reason about the project (a bad
# manifest, a missing repo, a conflict) rather than about you: `harumi check` says why.
_RUN_REJECTED = {400, 404, 409, 422}
_STDERR_SHOWN = 20  # lines of a failed run's stderr shown before pointing at `runs get`


def _running_line(project_id: str, branch: Optional[str], commit: Optional[str]) -> str:
    where = " ".join(
        part
        for part in (
            f"branch [bold]{branch}[/bold]" if branch else "",
            f"commit [bold]{commit[:8]}[/bold]" if commit else "",
        )
        if part
    )
    return f"Running project [bold]{project_id}[/bold]" + (f" on {where}" if where else "")


def _live_display_ok() -> bool:
    """Whether to animate (spinner, a line that rewrites itself).

    Needs a real TTY, not just rich's opinion: rich treats any FORCE_COLOR as
    "this is a terminal", and CI systems set it to get colour. An animation there
    would fill the build log with control codes.
    """
    try:
        return bool(console.is_terminal and console.file.isatty())
    except (AttributeError, ValueError):
        return False


def _watch_run(client: Client, project_id: str, run_id: str):
    """Wait for a run to finish, showing progress, and return it.

    At a terminal: one live line (spinner, status, time elapsed). Otherwise: one
    line each time the status changes, so a log or a pipe is not flooded.
    Ctrl-C stops the *watching*, never the run, and says how to cancel it.
    """
    import time

    from rich.live import Live
    from rich.spinner import Spinner

    from harumi.execution import wait_for_run

    started = time.monotonic()
    state = {"status": "queued"}
    live = _live_display_ok()

    def on_poll(run) -> None:
        if run.status != state["status"] and not live:
            console.print(f"[dim]{_duration(time.monotonic() - started):>7}[/dim]  {run.status}")
        state["status"] = run.status

    spinner = Spinner("dots")

    def frame():
        spinner.update(
            text=f" Run {state['status']} · {_duration(time.monotonic() - started)}  [dim](ctrl-c stops watching, not the run)[/dim]"
        )
        return spinner

    try:
        if live:
            with Live(get_renderable=frame, console=console, refresh_per_second=8, transient=True):
                return wait_for_run(client.api, project_id, run_id, on_poll=on_poll)
        console.print(f"Waiting for run {run_id} to finish...")
        return wait_for_run(client.api, project_id, run_id, on_poll=on_poll)
    except KeyboardInterrupt:
        console.print(
            f"\nStopped watching. [bold]The run is still going.[/bold]\n"
            f"  Follow it:  [bold]harumi runs get {run_id}[/bold]\n"
            f"  Cancel it:  [bold]harumi runs cancel {run_id}[/bold]"
        )
        raise typer.Exit(code=130)


def _print_failed_run(run) -> None:
    """The cause of a run that did not succeed, without a wall of output."""
    took = _duration(_run_duration(run))
    headline = f"Run {run.status}" + (f" after {took}" if took else "")
    if run.exit_code is not None:
        headline += f" (exit code {run.exit_code})"
    console.print(f"[bold red]{escape(headline)}.[/bold red]")
    if run.error:
        console.print(f"[bold]Error:[/bold] {escape(run.error)}")
    if run.stderr and run.stderr.strip():
        tail, hidden = _tail_lines(run.stderr, _STDERR_SHOWN)
        console.print("[dim]Last lines of stderr:[/dim]")
        console.print(escape(tail), soft_wrap=True, highlight=False)
        if hidden:
            console.print(f"[dim]({hidden} earlier lines not shown.)[/dim]")
    elif not run.error:
        console.print("[dim]No error output was captured.[/dim]")
    console.print(f"[dim]Everything it printed: [bold]harumi runs get {run.id}[/bold][/dim]")


# ---------------------------------------------------------------------------
# harumi runs
# ---------------------------------------------------------------------------

runs_app = typer.Typer(help="List, inspect, cancel, and download project runs.")
app.add_typer(runs_app, name="runs")


@runs_app.command("list")
@_handle_errors
def runs_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
    limit: Optional[int] = typer.Option(
        None, "--limit", "-n", min=1, help=f"How many of the latest runs to show (default {RUNS_SHOWN}). Use --all for every run."
    ),
    all_: bool = typer.Option(False, "--all", help="Show every run, however many there are."),
) -> None:
    """List runs for a project, newest first (the latest 25 unless you ask for more)."""
    if all_ and limit is not None:
        # Even `--limit 25 --all`: two ways of saying how many must not quietly disagree.
        _fail("--limit and --all are mutually exclusive. Pass only one.")
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    runs = client.list_runs(project_id)
    if not runs:
        console.print("No runs found.")
        return

    shown = runs if all_ else runs[: limit or RUNS_SHOWN]
    table = Table("id", "status", "source", "branch", "started", "took")
    for r in shown:
        table.add_row(
            r.id,
            _status_markup(r.status),
            r.source or "",
            r.git_branch or "",
            _run_started(r),
            _duration(_run_duration(r)),
        )
    console.print(table)
    if len(shown) < len(runs):
        console.print(
            f"[dim]Showing the latest {len(shown)} of {len(runs)} runs. "
            "Use --limit N for more, or --all for every run.[/dim]"
        )


@runs_app.command("get")
@_handle_errors
def runs_get(
    run_id: Optional[str] = typer.Argument(None, help="Run id. Defaults to the project's latest run."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show one run: how it went, then everything it printed (the latest run when no id is given)."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    r = client.get_run(project_id, run_id) if run_id else client.get_latest_run(project_id)
    if r is None:
        _fail("This project has no runs yet. Start one with [bold]harumi run --watch[/bold].")
        return

    status = _status_markup(r.status) + (f" (exit code {r.exit_code})" if r.exit_code is not None else "")
    took = _duration(_run_duration(r))
    started = _run_started(r)
    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="dim")
    facts.add_column()
    for label, value in (
        ("Run", escape(r.id)),
        ("Status", status),
        ("Took", took + (f"  [dim](started {started})[/dim]" if started else "") if took else (f"started {started}" if started else "")),
        (
            "Code",
            " @ ".join(p for p in (escape(r.git_branch or ""), escape((r.git_commit or "")[:8])) if p),
        ),
        ("Command", escape(r.command or "")),
        ("Kernel", escape(r.kernel_spec or "")),
        ("Started by", escape(" ".join(p for p in (r.source or "", r.triggered_by or "") if p))),
    ):
        if value:
            facts.add_row(label, value)
    console.print(facts)

    for title, text in (("stdout", r.stdout), ("stderr", r.stderr)):
        if text and text.strip():
            console.print(f"\n[bold]{title}:[/bold]")
            console.print(escape(text.rstrip()), soft_wrap=True, highlight=False)
    if r.error:
        console.print(f"\n[bold red]error:[/bold red] {escape(r.error)}")

    console.print()
    if not r.finished:
        console.print(f"[dim]Still going. Cancel it with [bold]harumi runs cancel {r.id}[/bold].[/dim]")
    elif r.output_url:
        console.print(f"[dim]It left output: [bold]harumi runs download {r.id}[/bold][/dim]")


@runs_app.command("download")
@_handle_errors
def runs_download(
    run_id: Optional[str] = typer.Argument(None, help="Run id. Defaults to the project's latest run."),
    output_dir: Path = typer.Option(
        Path("."), "--output-dir", "-o", help="Directory to download into. Default: current directory."
    ),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Download a run's committed output as a zip (the latest run when no id is given)."""
    from harumi.execution import download_run_output

    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    run = client.get_run(project_id, run_id) if run_id else client.get_latest_run(project_id)
    if run is None:
        _fail("Project has no runs yet.")
        return
    if not run.finished:
        _fail(
            f"Run {run.id} is still {run.status}; its output is ready once it finishes. "
            f"Watch it with [bold]harumi runs get {run.id}[/bold]."
        )
    if not run.output_url:
        _fail(
            f"Run {run.id} ended {run.status} without leaving any output. "
            f"See what it printed with [bold]harumi runs get {run.id}[/bold]."
        )
    zip_path = download_run_output(client.api, project_id, run, output_dir)
    try:
        size = f" ({_format_bytes(zip_path.stat().st_size)})"
    except OSError:
        size = ""
    console.print(f"Downloaded [bold]{escape(str(zip_path))}[/bold]{size}", soft_wrap=True)


@runs_app.command("cancel")
@_handle_errors
def runs_cancel(
    run_id: Optional[str] = typer.Argument(None, help="Run id. Pick from the runs still going if omitted."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Cancel a run that is still going (pick one from a list when no id is given)."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    if run_id is None:
        going = [r for r in client.list_runs(project_id) if not r.finished]
        if not going:
            console.print("Nothing to cancel: no run is going right now.")
            return
        run_id = _pick_run(client, project_id, going, action="cancel", flag="the RUN_ID argument")
        if run_id is None:
            return  # backed out of the menu; choosing a run was the confirmation

    r = client.cancel_run(project_id, run_id)
    if r.finished and r.status != "cancelled":
        console.print(f"Run {escape(r.id)} had already {_status_markup(r.status)}; there was nothing to cancel.")
        return
    console.print(f"Run [bold]{escape(r.id)}[/bold] is now {_status_markup(r.status)}.")


# ---------------------------------------------------------------------------
# harumi compare
# ---------------------------------------------------------------------------

_MIN_COMPARE_RUNS, _MAX_COMPARE_RUNS = 2, 4
_COMPARE_PICKER_RUNS = 25  # as many as `runs list` shows by default
_NO_VALUE = "—"


def _num(value: object) -> Optional[float]:
    """``value`` as a float, or None for anything that is not a plain number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _kpi_cell(value: object, unit: Optional[str], delta: object) -> str:
    """One KPI value for a column, with its change against the baseline when it has one.

    Plain text: the caller escapes it for rich, since a value can be any JSON the
    run's `output.json` held.
    """
    if value is None:
        return _NO_VALUE
    number = _num(value)
    text = f"{number:.4g}" if number is not None else str(value)
    if unit:
        text = f"{text} {unit}"
    change = _num(delta)
    return f"{text} ({change:+.4g})" if change is not None else text


_FIND_RUN_IDS = "Find run ids with [bold]harumi runs list[/bold], or give none at a terminal to pick them from a list."


def _pick_runs_to_compare(client: Client, project_id: str) -> Optional[list[str]]:
    """Ask for a baseline, then the runs to compare it with. None if the user backed out.

    Only runs that succeeded are offered: one still going has no output yet, and a failed
    one never will, so choosing it could only produce an empty column.
    """
    ready = _newest_runs_first([r for r in client.list_runs(project_id) if r.succeeded])[:_COMPARE_PICKER_RUNS]
    if len(ready) < _MIN_COMPARE_RUNS:
        _fail(
            "Comparing needs at least two runs that succeeded, and this project has "
            f"{'one' if ready else 'none'}. Run it again with [bold]harumi run --watch[/bold]."
        )
    flag = f"the RUN_ID arguments ({_MIN_COMPARE_RUNS} to {_MAX_COMPARE_RUNS}; the first is the baseline)"
    # With no terminal either menu raises NonInteractiveError, which `_handle_errors` prints
    # with the list of runs and `flag`, the same as `runs cancel`.
    baseline = _pick_run(client, project_id, ready, action="use as the baseline", flag=flag)
    if baseline is None:
        return None
    others = interactive.select_many(
        "Which runs do you want to compare it with?",
        [_run_option(r) for r in ready if r.id != baseline],
        at_least=_MIN_COMPARE_RUNS - 1,
        at_most=_MAX_COMPARE_RUNS - 1,
        flag=flag,
    )
    return None if not others else [baseline, *others]  # type: ignore[list-item]


def _check_compare_ids(run_ids: list[str]) -> list[str]:
    """The ids to compare, or a message about what is wrong with them."""
    ids = list(dict.fromkeys(run_ids))
    if len(ids) != len(run_ids):
        _fail(f"A run can only be compared once: one of those ids is in the list more than once. {_FIND_RUN_IDS}")
    if len(ids) < _MIN_COMPARE_RUNS:
        _fail(f"Comparing needs {_MIN_COMPARE_RUNS} to {_MAX_COMPARE_RUNS} runs, and the first is the baseline. {_FIND_RUN_IDS}")
    if len(ids) > _MAX_COMPARE_RUNS:
        _fail(f"Comparing takes at most {_MAX_COMPARE_RUNS} runs, and you gave {len(ids)}. {_FIND_RUN_IDS}")
    if any("," in run_id for run_id in ids):
        # The API takes the ids as one comma-separated parameter, so a comma would
        # silently become extra ids.
        _fail("Run ids can't contain a comma; separate them with spaces.")
    return ids


@app.command()
@_handle_errors
def compare(
    run_ids: Optional[list[str]] = typer.Argument(
        None, metavar="[RUN_ID]...", help="2-4 run ids; the first is the baseline. Pick from a list if omitted."
    ),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    as_json: bool = typer.Option(False, "--json", help="Print the API's comparison as JSON, for scripts."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Compare 2-4 runs: KPIs, the winner, and whether they used the same inputs.

    The first run is the baseline; each change is measured against it. KPIs are
    the project's `\\[\\[output.kpi]]` declarations in harumi.toml. With no run ids, at
    a terminal, you pick the baseline and then the runs to compare it with.
    """
    if run_ids:
        # Say what is wrong with the arguments before needing a project or a login: someone
        # outside a project who mistypes should hear about the typo, not about the project.
        ids = _check_compare_ids(run_ids)
        project_id = _resolve_project(project)
        client = _get_client(api_url=api_url, org=org)
    else:
        project_id = _resolve_project(project)
        client = _get_client(api_url=api_url, org=org)
        picked = _pick_runs_to_compare(client, project_id)
        if picked is None:
            return  # backed out of a menu
        ids = picked  # valid by construction: the menus take 2 to 4 distinct runs

    try:
        result = client.compare_runs(project_id, ids)
    except ApiError as exc:
        if exc.status_code == 404:
            # The API's own words say what is missing; this says where to find the real ids.
            _fail(f"{escape(str(exc))}\nIf a run id is wrong, list this project's runs with [bold]harumi runs list[/bold].")
        raise

    if as_json:
        # typer.echo, not the rich console: no wrapping or colour in piped output.
        typer.echo(json.dumps(result, indent=2))
        return

    # Everything below prints values the user or the run controls (branch names, KPI
    # labels and values, file paths), so each goes through escape(): `[x]` is markup to
    # rich, and a stray `[/x]` would raise after the request already succeeded.
    columns = result.get("columns") or []
    winner = result.get("winner_run_id")
    headers = []
    for i, col in enumerate(columns):
        tags = (["baseline"] if i == 0 else []) + (["winner"] if col.get("run_id") == winner else [])
        name = f"{col.get('branch') or '?'}@{(col.get('commit') or '')[:7]} ({col.get('status') or '?'})"
        headers.append(escape(name) + (f" \\[{', '.join(tags)}]" if tags else ""))

    table = Table("KPI", *headers)
    any_primary = False
    for kpi in result.get("kpis") or []:
        values = kpi.get("values") or []
        deltas = kpi.get("deltas") or []
        cells = [
            escape(
                _kpi_cell(
                    values[i] if i < len(values) else None,
                    kpi.get("unit"),
                    deltas[i] if i < len(deltas) else None,
                )
            )
            for i in range(len(columns))
        ]
        any_primary = any_primary or bool(kpi.get("primary"))
        label = str(kpi.get("label") or kpi.get("key") or "?") + (" *" if kpi.get("primary") else "")
        table.add_row(escape(label), *cells)
    table.add_row(
        "Runtime",
        *[
            f"{seconds:.0f} s" if (seconds := _num(col.get("runtime_s"))) is not None else _NO_VALUE
            for col in columns
        ],
    )
    console.print(table)

    if winner:
        branch = next((c.get("branch") for c in columns if c.get("run_id") == winner), None) or winner
        note = " (best value of the primary KPI, marked *)" if any_primary else ""
        console.print(f"Winner: [bold]{escape(str(branch))}[/bold]{note}")
        console.print(f"[dim]Get its files: [bold]harumi runs download {escape(str(winner))}[/bold][/dim]", soft_wrap=True)
    elif not result.get("kpis"):
        # An empty table whose "reason" is about a primary KPI misleads: there are no KPIs at all.
        console.print(
            "[yellow]This project declares no KPIs, so there is only runtime to compare.[/yellow] "
            "Declare them as [bold]\\[\\[output.kpi]][/bold] tables in harumi.toml."
        )
    elif result.get("no_winner_reason"):
        console.print(f"No winner: {escape(str(result['no_winner_reason']))}")

    fairness = result.get("fairness") or {}
    verdict = fairness.get("verdict")
    if verdict == "same":
        console.print("Inputs: every run used the same inputs.")
    elif verdict == "differs":
        console.print("[yellow]Inputs: the runs did not use the same inputs. Differing files:[/yellow]")
        for path in fairness.get("files") or []:
            console.print(f"  {escape(str(path))}")
    elif verdict == "unknown":
        console.print("Inputs: can't tell, at least one run has no record of its inputs.")

    for warning in result.get("warnings") or []:
        console.print(f"[yellow]Warning:[/yellow] {escape(str(warning))}")


# ---------------------------------------------------------------------------
# harumi outputs (deprecated alias of `runs list` / `runs download`)
# ---------------------------------------------------------------------------

@app.command(hidden=True)
@_deprecated("harumi outputs", "harumi runs list / harumi runs download [RUN_ID] -o DIR")
@_handle_errors
def outputs(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id (uses .harumi binding if omitted)."),
    latest: bool = typer.Option(False, "--latest", help="Show only the most recent run."),
    download: Optional[str] = typer.Option(None, "--download", help="Download this run id's committed output."),
    output_dir: Path = typer.Option(Path("."), "--output-dir", "-o", help="Directory to download into. Default: current directory."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List or download run outputs for a project.

    Deprecated alias for `harumi runs list`/`harumi runs download`; kept for
    backwards compatibility.
    """
    resolved_project = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    if download:
        from harumi.execution import download_run_output

        run = client.get_run(resolved_project, download)
        zip_path = download_run_output(client.api, resolved_project, run, output_dir)
        console.print(f"Downloaded to {zip_path}", soft_wrap=True)
        return

    if latest:
        run = client.get_latest_run(resolved_project)
        results = [run] if run else []
    else:
        results = client.list_runs(resolved_project)

    if not results:
        console.print("No runs found.")
        return

    table = Table("id", "status", "started", "ended", "git_branch")
    for r in results:
        table.add_row(
            r.id,
            r.status or "",
            str(r.started or ""),
            str(r.ended or ""),
            r.git_branch or "",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# harumi repo
# ---------------------------------------------------------------------------

repo_app = typer.Typer(
    help="Deprecated: the project's repo is a plain git repo. Use git in your clone (`harumi clone`)."
)
app.add_typer(repo_app, name="repo", hidden=True)


@repo_app.command("ls")
@_deprecated("harumi repo ls", 'git ls-files (another branch: git fetch harumi && git ls-tree -r --name-only harumi/<ref>)')
@_handle_errors
def repo_ls(
    ref: Optional[str] = typer.Option(None, "--ref", help="Branch/commit to list (defaults to the default branch)."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List all files in the repo (flat, recursive)."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    files = client.list_repo_files(project_id, ref=ref)
    if not files:
        console.print("No files found.")
        return

    table = Table("path", "size")
    for f in sorted(files, key=lambda f: f.path):
        table.add_row(f.path, str(f.size or ""))
    console.print(table)


@repo_app.command("cat")
@_deprecated("harumi repo cat", 'cat <path> (another branch: git fetch harumi && git show harumi/<ref>:<path>)')
@_handle_errors
def repo_cat(
    path: str = typer.Argument(..., help="File path within the repo."),
    ref: Optional[str] = typer.Option(None, "--ref", help="Branch/commit to read from."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write to this local path instead of stdout."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Print (or save) a file's content."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    file_content = client.get_repo_file(project_id, path, ref=ref)
    raw = base64.b64decode(file_content.content) if file_content.content else b""

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
        console.print(f"Wrote {len(raw)} bytes to {output}")
        return

    try:
        console.print(raw.decode("utf-8"))
    except UnicodeDecodeError:
        _fail(f"{path!r} is not valid UTF-8 text. Use --output to save it as a binary file instead.")


@repo_app.command("put")
@_deprecated("harumi repo put", 'copy the file into your clone, then git add <path> && git commit -m <msg> && git push harumi <branch>')
@_handle_errors
def repo_put(
    local_path: Path = typer.Argument(..., help="Local file to upload."),
    repo_path: str = typer.Argument(..., help="Destination path within the repo."),
    message: Optional[str] = typer.Option(None, "--message", "-m", help="Commit message."),
    branch: Optional[str] = typer.Option(None, "--branch", help="Target branch (defaults to the default branch)."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Create or update a file in the repo (create if absent, else update) as one commit."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    if not local_path.is_file():
        _fail(f"{local_path} is not a file.")

    content_b64 = base64.b64encode(local_path.read_bytes()).decode("ascii")

    # Determine create vs update by checking whether the file already exists.
    action = "update"
    try:
        client.get_repo_file(project_id, repo_path, ref=branch)
    except ApiError as exc:
        if exc.status_code == 404:
            action = "create"
        else:
            raise

    result = client.apply_repo_changes(
        project_id,
        operations=[{"action": action, "path": repo_path, "content": content_b64}],
        message=message,
        branch=branch,
    )
    console.print(
        f"[bold green]{'Created' if action == 'create' else 'Updated'}[/bold green] "
        f"{repo_path} (commit {result.commit_sha or '?'})."
    )


@repo_app.command("rm")
@_deprecated("harumi repo rm", 'git rm -r <path> && git commit -m <msg> && git push harumi <branch>')
@_handle_errors
def repo_rm(
    path: str = typer.Argument(..., help="File or folder path within the repo."),
    message: Optional[str] = typer.Option(None, "--message", "-m", help="Commit message."),
    branch: Optional[str] = typer.Option(None, "--branch", help="Target branch (defaults to the default branch)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Delete a file, or every file under a folder, as one commit."""
    project_id = _resolve_project(project)

    if not yes and not typer.confirm(
        f"Delete '{path}' from the repo? It is a new commit, so `git revert` brings it back."
    ):
        console.print("Aborted.")
        return

    client = _get_client(api_url=api_url, org=org)
    result = client.apply_repo_changes(
        project_id,
        operations=[{"action": "delete", "path": path}],
        message=message,
        branch=branch,
    )
    console.print(f"[bold red]Deleted[/bold red] {result.changed} file(s) (commit {result.commit_sha or '?'}).")


@repo_app.command("mv")
@_deprecated("harumi repo mv", 'git mv <from> <to> && git commit -m <msg> && git push harumi <branch>')
@_handle_errors
def repo_mv(
    from_path: str = typer.Argument(..., help="Source path (file or folder) within the repo."),
    to_path: str = typer.Argument(..., help="Destination path within the repo."),
    message: Optional[str] = typer.Option(None, "--message", "-m", help="Commit message."),
    branch: Optional[str] = typer.Option(None, "--branch", help="Target branch (defaults to the default branch)."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Rename/move a file, or every file under a folder, as one commit."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    result = client.apply_repo_changes(
        project_id,
        operations=[{"action": "move", "from_path": from_path, "path": to_path}],
        message=message,
        branch=branch,
    )
    console.print(
        f"[bold green]Moved[/bold green] {from_path} -> {to_path} "
        f"({result.changed} file(s), commit {result.commit_sha or '?'})."
    )


@repo_app.command("download")
@_deprecated("harumi repo download", 'git archive --format=zip -o repo.zip harumi/<ref> [<path>]')
@_handle_errors
def repo_download(
    output: Path = typer.Option(..., "--output", "-o", help="Local zip path to write."),
    path: str = typer.Option("", "--path", help="Folder within the repo to download (default: whole repo)."),
    ref: Optional[str] = typer.Option(None, "--ref", help="Branch/commit to download from."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Download the repo (or a folder within it) as a zip."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    zip_path = client.download_repo_archive(project_id, output, path=path, ref=ref)
    console.print(f"Downloaded to {zip_path}", soft_wrap=True)


@repo_app.command("branches")
@_deprecated("harumi repo branches", 'git fetch harumi && git branch -r')
@_handle_errors
def repo_branches(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List versions (git branches). The live branch is flagged."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    branches = client.list_repo_branches(project_id)
    if not branches:
        console.print("No branches found.")
        return

    table = Table("name", "commit_sha", "is_live")
    for b in branches:
        table.add_row(b.name, (b.commit_sha or "")[:8], "yes" if b.is_live else "")
    console.print(table)


@repo_app.command("branch-create")
@_deprecated("harumi repo branch-create", 'git fetch harumi && git switch -c <name> harumi/main && git push -u harumi <name>')
@_handle_errors
def repo_branch_create(
    name: str = typer.Argument(..., help="New branch (version) name."),
    from_branch: Optional[str] = typer.Option(None, "--from", help="Base branch (defaults to the live branch)."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Create a new version (git branch)."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    branch = client.create_repo_branch(project_id, name, from_branch=from_branch)
    console.print(f"[bold green]Created[/bold green] branch [bold]{branch.name}[/bold].")


@repo_app.command("branch-rm")
@_deprecated("harumi repo branch-rm", 'git push harumi --delete <name> (check `harumi schedules list` first: schedules can pin a branch)')
@_handle_errors
def repo_branch_rm(
    name: str = typer.Argument(..., help="Branch (version) name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Delete a version (git branch). Never the live branch."""
    project_id = _resolve_project(project)

    if not yes and not typer.confirm(f"Delete branch '{name}'? This cannot be undone."):
        console.print("Aborted.")
        return

    client = _get_client(api_url=api_url, org=org)
    client.delete_repo_branch(project_id, name)
    console.print(f"[bold red]Deleted[/bold red] branch [bold]{name}[/bold].")


@repo_app.command("promote")
@_deprecated("harumi repo promote", 'git fetch harumi && git switch main && git merge --ff-only harumi/main && git merge --no-ff harumi/<name> && git push harumi main')
@_handle_errors
def repo_promote(
    name: str = typer.Argument(..., help="Version (branch) to promote into live."),
    title: Optional[str] = typer.Option(None, "--title", help="PR/merge title."),
    delete_after: bool = typer.Option(False, "--delete-after", help="Delete the version branch after a successful promote."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Promote a version into the live branch via a merge commit."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    result = client.promote_repo_branch(project_id, name, title=title, delete_after=delete_after)
    if result.conflict:
        _fail(result.message or f"Version {name!r} could not be merged (conflicts).")
    console.print(f"[bold green]Promoted[/bold green] [bold]{name}[/bold] into live.")


@repo_app.command("dir")
@_deprecated("harumi repo dir", 'ls <path>')
@_handle_errors
def repo_dir(
    path: str = typer.Argument("", help="Folder within the repo (default: repo root)."),
    ref: Optional[str] = typer.Option(None, "--ref", help="Branch/commit to browse (defaults to the default branch)."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """One folder level of the repo (GitHub-style browser). Use `repo ls` for a flat, whole-repo listing."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    listing = client.list_repo_dir(project_id, path=path, ref=ref)
    if not listing.entries:
        console.print(f"No entries at {path or '/'!r} on {listing.ref!r}.")
        return

    table = Table("name", "type", "size", "last_commit")
    for e in sorted(listing.entries, key=lambda e: (e.type != "dir", e.name)):
        last = e.last_commit.message[:60] if e.last_commit else ""
        table.add_row(e.name, e.type, str(e.size or ""), last)
    console.print(table)


@repo_app.command("commits")
@_deprecated("harumi repo commits", 'git log --oneline [-- <path>]')
@_handle_errors
def repo_commits(
    path: Optional[str] = typer.Argument(None, help="File or folder to filter history to (default: the whole branch)."),
    ref: Optional[str] = typer.Option(None, "--ref", help="Branch/commit to read history on (defaults to the default branch)."),
    page: int = typer.Option(1, "--page", help="Page number, 1-indexed."),
    per_page: int = typer.Option(30, "--per-page", help="Commits per page (max 100)."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show recent commit history. Pass a file or folder to see just its history."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    result = client.list_repo_commits(project_id, ref=ref, path=path, page=page, per_page=per_page)
    if not result.commits:
        console.print(f"No commits on {result.ref!r}.")
        return

    table = Table("sha", "message", "author", "date")
    for c in result.commits:
        table.add_row(
            c.sha[:8],
            c.message.splitlines()[0] if c.message else "",
            c.author_name or c.author_login or "",
            str(c.committed_at) if c.committed_at else "",
        )
    console.print(table)


@app.command("check")
@_handle_errors
def check(
    ref: Optional[str] = typer.Option(None, "--ref", help="Branch/commit to check (defaults to the default branch)."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Report everything blocking this project from running, before you run it.

    Server-side checks on the repo: it exists, `harumi.toml` is present and valid,
    and the kernel is one the project may use. Exits 1 when something blocks the
    project, so a script can stop before `harumi run`.
    """
    _check(ref=ref, project=project, api_url=api_url, org=org, exit_when_blocked=True)


def _check(*, ref: Optional[str], project: Optional[str], api_url: Optional[str], org: Optional[str], exit_when_blocked: bool) -> None:
    """The body of `harumi check`. The deprecated `harumi repo readiness` shares it but
    keeps its old exit code (always 0), so a script that ignores the status is unaffected."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    readiness = client.get_project_readiness(project_id, ref=ref)
    failing = [c for c in readiness.checks if not c.ok]

    table = Table("check", "result", "detail")
    for c in readiness.checks:
        mark = "[green]ok[/green]" if c.ok else "[bold red]problem[/bold red]"
        table.add_row(_CHECK_LABELS.get(c.id, c.id), mark, escape(c.detail or ""))
    console.print(table)

    if readiness.ready and not failing:
        console.print(
            f"[bold green]Ready to run[/bold green] on {escape(readiness.ref)}. "
            "[dim]Next: [bold]harumi run --watch[/bold][/dim]"
        )
        return
    count = len(failing)
    if count:
        console.print(
            f"[bold red]Not ready[/bold red] on {escape(readiness.ref)}: "
            f"{count} problem{'' if count == 1 else 's'}. "
            "[dim]Fix them, then run [bold]harumi check[/bold] again.[/dim]"
        )
    else:
        # Every listed check passed, yet the server says no. Say so rather than "0 problems".
        console.print(
            f"[bold red]Not ready[/bold red] on {escape(readiness.ref)}: the server did not say why. "
            "[dim]Run [bold]harumi check[/bold] again, or ask your workspace admin.[/dim]"
        )
    if exit_when_blocked:
        raise typer.Exit(code=1)


# What each server-side readiness check is called when shown to a person.
_CHECK_LABELS = {
    "repo": "Repository exists",
    "manifest": "harumi.toml is present",
    "manifest_valid": "harumi.toml is valid",
    "kernel": "Kernel is allowed",
}


@repo_app.command("readiness")
@_deprecated("harumi repo readiness", "harumi check")
def _repo_readiness(
    ref: Optional[str] = typer.Option(None, "--ref", help="Branch/commit to check (defaults to the default branch)."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Deprecated: use `harumi check`. Unlike `check`, this always exits 0."""
    _check(ref=ref, project=project, api_url=api_url, org=org, exit_when_blocked=False)


# ---------------------------------------------------------------------------
# harumi files
# ---------------------------------------------------------------------------

files_app = typer.Typer(help="Manage the project's non-git file storage (uploads land at inputs/ inside every run).")
app.add_typer(files_app, name="files")


@files_app.command("ls")
@_handle_errors
def files_ls(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List every file uploaded to this project."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    file_list = client.list_project_files(project_id)
    if not file_list.files:
        console.print("No files uploaded yet. Add one with [bold]harumi files put <local file>[/bold].")
        return

    table = Table("name", "size", "modified")
    # A path someone will copy into `files get` or `files rm` is never cut to fit.
    table.columns[0].no_wrap = True
    table.columns[0].overflow = "fold"
    for f in sorted(file_list.files, key=lambda f: f.name):
        table.add_row(escape(_printable(f.name)), _format_bytes(f.size), _ago(f.last_modified) or "")
    console.print(table)

    count, total = len(file_list.files), sum(f.size for f in file_list.files)
    at_least = "at least " if file_list.is_truncated else ""
    console.print(
        f"{at_least}{count} file{'' if count == 1 else 's'}, {_format_bytes(total)} "
        f"[dim](of {MAX_PROJECT_SYNC_FILES} files and {_format_bytes(MAX_PROJECT_SYNC_TOTAL_BYTES)} every run can use)[/dim]"
    )
    if file_list.is_truncated:
        console.print("[yellow]This project has more files than shown, so the figures above are a minimum.[/yellow]")
    elif count >= MAX_PROJECT_SYNC_FILES * 0.9 or total >= MAX_PROJECT_SYNC_TOTAL_BYTES * 0.9:
        console.print("[yellow]This project is close to the limit. Remove files you no longer need with harumi files rm.[/yellow]")


@files_app.command("put")
@_handle_errors
def files_put(
    local_path: Path = typer.Argument(..., help="Local file to upload."),
    remote_path: Optional[str] = typer.Argument(None, help="Destination path within the project's files (default: the local file's name)."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Upload a local file, refusing an upload that would exceed the run-time sync cap."""
    project_id = _resolve_project(project)

    if not local_path.is_file():
        _fail(f"{local_path} is not a file.")

    dest_path = remote_path or local_path.name
    client = _get_client(api_url=api_url, org=org)

    existing = client.list_project_files(project_id)
    file_size = local_path.stat().st_size
    if existing.is_truncated:
        # A false "OK" here is worse than no local check at all — the
        # server-side cap still applies on upload, so skip a check that
        # would silently under-count against a listing we know is partial.
        console.print(
            "[yellow]Warning: couldn't verify this project's total file count/size "
            "(the listing was truncated); the upload may still be rejected by the "
            "server-side cap.[/yellow]"
        )
    else:
        # Uploading to a path that already exists overwrites that object, so the file
        # it displaces must not be counted — otherwise replacing a file in a project
        # sitting at the cap is refused even though the totals wouldn't move.
        violation = check_project_sync_cap(
            [f.size for f in existing.files if f.name != dest_path], [file_size]
        )
        if violation:
            if violation.reason == "file-count":
                _fail(
                    f"That upload would bring this project to {violation.would_be} files, "
                    f"over the {violation.limit}-file limit every run enforces. Remove some files first."
                )
            else:
                _fail(
                    f"That upload would bring this project to {_format_bytes(violation.would_be)}, "
                    f"over the {_format_bytes(violation.limit)} limit every run enforces. Remove some files first."
                )

    content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
    upload_url = client.create_file_upload_url(
        project_id, dest_path, content_type, size_bytes=file_size
    )
    client.upload_file_to_presigned_url(upload_url, local_path, content_type)
    if upload_is_staged(project_id, upload_url.key, requested=dest_path):
        # The bytes are in a holding area; the file only exists once the server has checked it.
        console.print("Checking the file...")
        try:
            client.complete_file_upload(project_id, dest_path, upload_url.key)
        except ApiError as exc:
            _fail(_upload_refusal(exc, dest_path))
    else:
        # The key looked final, so there was nothing to complete. That is true of an older
        # server. It is also what a newer one that changed its staging key would look like, and
        # then the file never appears, which is the bug `complete_file_upload` exists to prevent.
        # Look, rather than assume.
        _confirm_upload_landed(client, project_id, dest_path)
    console.print(
        f"[bold green]Uploaded[/bold green] {escape(_printable(local_path))} -> {escape(_printable(dest_path))} ({_format_bytes(file_size)}).",
        soft_wrap=True,
    )


class _FileNotThere(Exception):
    """`files rm --missing-ok` named a file that is not in the project: nothing to delete."""


def _find_project_file(
    client: Client, project_id: str, name: str, *, missing_ok: bool = False, script_hint: str = ""
) -> Optional[ProjectFile]:
    """The listed file called `name`, or a clear failure saying it is missing or is a folder.

    harumi-api tells a missing file from an existing one only for downloads (a 404); a delete
    answers "deleted: 1" for any path. So the listing is consulted first. A truncated listing
    cannot prove a name is absent, so then the caller goes ahead and lets the server decide.
    Returns None in that case.
    """
    listing = client.list_project_files(project_id)
    for f in listing.files:
        if f.name == name:
            return f
    inside = sorted(f.name for f in listing.files if f.name.startswith(name.rstrip("/") + "/"))
    if inside:
        shown = ", ".join(escape(_printable(n)) for n in inside[:3]) + (f" and {len(inside) - 3} more" if len(inside) > 3 else "")
        _fail(f"{escape(_printable(name))} is a folder, not a file. It holds {shown}.")
    if listing.is_truncated:
        return None
    if missing_ok:
        raise _FileNotThere()
    _fail(
        f"No file called {escape(_printable(name))} in this project. See what is there with [bold]harumi files ls[/bold]."
        + (f" {script_hint}" if script_hint else "")
    )


def _confirm_upload_landed(client: Client, project_id: str, dest_path: str) -> None:
    """Fail unless the uploaded file is listed. A truncated listing proves nothing, so it passes."""
    stored = posixpath.normpath(dest_path.strip("/"))  # how harumi-api stores the path it is given
    try:
        listing = client.list_project_files(project_id)
    except (ApiError, ValueError):
        # Cannot tell: an error status, or a body that is not what a listing looks like (pydantic's
        # ValidationError is a ValueError). The upload itself already succeeded; a safeguard that cannot
        # run must not turn it into a failure.
        return
    if listing.is_truncated or any(f.name == stored for f in listing.files):
        return
    _fail(
        f"{escape(_printable(dest_path))} was sent but does not appear in the project, so it may not have been added. "
        "Check with [bold]harumi files ls[/bold], and try again if it is missing."
    )


def _upload_refusal(exc: ApiError, dest_path: str) -> str:
    """Why an upload was not added, in words. The API gives the same text for an unsafe file and
    for a scanner that is down, so the status picks the sentence, not the API's detail."""
    shown = escape(_printable(dest_path))
    if isinstance(exc, ApiUnreachableError):
        # No answer at all (the connection dropped, or the scan outlasted the wait). The server may
        # still finish and promote the file, so "not added" would be a claim we cannot make.
        return (
            f"Did not hear back about {shown}: it may or may not have been added. "
            "Check with [bold]harumi files ls[/bold] before uploading it again."
        )
    if exc.status_code == 422:
        return f"{shown} could not be verified as safe, so it was not added."
    if exc.status_code == 413:
        return f"{shown} is too large to be checked, so it was not added."
    if exc.status_code == 503:
        return f"Harumi could not check this file right now, so {shown} was not added. Try again in a few minutes."
    if exc.status_code == 404:
        return f"The upload of {shown} did not arrive, so it was not added. Try again."
    return escape(str(exc))


@files_app.command("get")
@_handle_errors
def files_get(
    remote_path: str = typer.Argument(..., help="File path within the project's files."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Local path to write to (default: the remote file's name)."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Download a file to a local path."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    # A trailing slash (`files get data/`) leaves an empty basename, and
    # `Path("")` is `Path(".")` — writing the download into the cwd itself.
    # Refuse instead, since there's no name to default to.
    default_name = remote_path.rstrip("/").rsplit("/", 1)[-1]
    if output is None and not default_name:
        _fail(f"'{escape(_printable(remote_path))}' has no file name to save as. Pass --output, or name a file.")

    dest_path = output or Path(default_name)
    try:
        download_url = client.create_file_download_url(project_id, remote_path)
    except ApiError as exc:
        if exc.status_code == 404:
            # Only now spend a request on finding out why: a typo, or a folder name.
            _find_project_file(client, project_id, remote_path)
        raise
    client.download_file_from_presigned_url(download_url, dest_path)
    console.print(f"Downloaded {escape(_printable(remote_path))} -> {escape(_printable(dest_path))}", soft_wrap=True)


@files_app.command("rm")
@_handle_errors
def files_rm(
    remote_path: str = typer.Argument(..., help="File path within the project's files."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    missing_ok: bool = typer.Option(
        False,
        "--missing-ok",
        help="Exit 0 when the file is not there, for scripts that may run twice. A folder name is still an error.",
    ),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Delete an uploaded file. This cannot be undone."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    try:
        found = _find_project_file(
            client,
            project_id,
            remote_path,
            missing_ok=missing_ok,
            script_hint="In a script that may run twice, add [bold]--missing-ok[/bold].",  # `rm` only
        )
    except _FileNotThere:
        console.print(f"{escape(_printable(remote_path))} was not there, so there was nothing to delete.")
        return
    what = f"'{_printable(remote_path)}'" + (f" ({_format_bytes(found.size)})" if found else "")  # shown cleaned; sent as given
    if not yes and not typer.confirm(f"Delete {what} from this project's files? This cannot be undone."):
        console.print("Aborted.")
        return

    client.delete_project_file(project_id, remote_path)
    size = f" ({_format_bytes(found.size)})" if found else ""
    console.print(f"[bold red]Deleted[/bold red] {escape(_printable(remote_path))}{size}.")


# ---------------------------------------------------------------------------
# harumi dashboard
# ---------------------------------------------------------------------------

dashboard_app = typer.Typer(help="Reference and validate the project's dashboard spec widgets.")
app.add_typer(dashboard_app, name="dashboard")


@dashboard_app.command("widgets")
@_handle_errors
def dashboard_widgets(
    type_: Optional[str] = typer.Option(None, "--type", help="Only show this widget type."),
) -> None:
    """Print every dashboard widget type and its required/optional keys."""
    schemas = widget_schemas()
    types = [type_] if type_ else list(schemas)
    for t in types:
        schema = schemas.get(t)
        if schema is None:
            _fail(f'Unknown widget type "{t}". Known types: {", ".join(schemas)}')

        table = Table("key", "required", "kind", "values", title=f"[bold]{t}[/bold]")
        for field in schema:
            table.add_row(
                field.toml_key,
                "yes" if field.required else "",
                field.kind,
                ", ".join(field.values) if field.values else "",
            )
        console.print(table)


@dashboard_app.command("validate")
@_handle_errors
def dashboard_validate(
    path: Optional[Path] = typer.Argument(None, help="A single dashboard spec to validate (default: every ./dashboard/*.toml, else ./dashboard.toml)."),
    ref: Optional[str] = typer.Option(None, "--ref", help="Validate the repo's specs on this branch/commit instead of local files."),
    against: Optional[Path] = typer.Option(None, "--against", help="Check widget dot-paths against this local output.json."),
    run_id: Optional[str] = typer.Option(None, "--run", help="Check widget dot-paths against this run's output.json."),
    latest: bool = typer.Option(False, "--latest", help="Check widget dot-paths against the most recent run's output.json."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Validate the project's dashboard specs against the widget contract.

    A project can have several dashboards — every `dashboard/*.toml` plus the
    legacy root `dashboard.toml` — and each becomes an entry in the platform's
    dashboard picker, so all of them are validated unless PATH names one.

    Reports every widget the platform would drop (unknown type, missing
    required key) plus — when --against/--run/--latest is given — dot-paths
    that won't resolve against that output.json (the platform can't check
    this ahead of time; it just renders the widget empty).
    """
    if sum(bool(x) for x in (against, run_id, latest)) > 1:
        _fail("Pass at most one of --against, --run, --latest.")

    project_id = None
    client = None
    if run_id or latest or ref:
        project_id = _resolve_project(project)
        client = _get_client(api_url=api_url, org=org)

    # name -> raw TOML, in picker order.
    specs: list[tuple[str, str]] = []
    if path is not None:
        specs.append((str(path), path.read_text()))
    elif ref:
        assert client is not None and project_id is not None
        found = pick_dashboard_paths(f.path for f in client.list_repo_files(project_id, ref=ref))
        if not found:
            _fail(f"No dashboard specs (dashboard/*.toml or dashboard.toml) on {ref!r}.")
        for repo_path in found:
            file_content = client.get_repo_file(project_id, repo_path, ref=ref)
            specs.append(
                (
                    repo_path,
                    base64.b64decode(file_content.content).decode("utf-8")
                    if file_content.content
                    else "",
                )
            )
    else:
        found = local_dashboard_paths(Path("."))
        if not found:
            _fail(
                "No dashboard specs found. Add dashboard/<name>.toml (or dashboard.toml), "
                "pass a path, or use --ref to check the repo's copies."
            )
        specs.extend((local_path, Path(local_path).read_text()) for local_path in found)

    output: Optional[dict] = None
    if against:
        if not against.is_file():
            _fail(f"{against} not found.")
        try:
            output = json.loads(against.read_text())
        except json.JSONDecodeError as exc:
            _fail(f"{against} is not valid JSON: {exc}")
    elif run_id or latest:
        assert client is not None and project_id is not None
        run = client.get_run(project_id, run_id) if run_id else client.get_latest_run(project_id)
        if run is None:
            _fail("Project has no runs yet.")
        try:
            output = client.get_run_output(project_id, run.id)
        except HarumiError:
            _fail(f"Run {run.id!r} has no output.json to check against.")

    failed = False
    for name, raw in specs:
        if len(specs) > 1:
            console.print(f"[bold]{name}[/bold]")
        try:
            widgets, issues = validate_dashboard_toml(raw, output)
        except DashboardTomlError as exc:
            console.print(f"[bold red]invalid[/bold red] {name} is not valid TOML: {exc}")
            failed = True
            continue

        if widgets:
            table = Table("id", "type", "title")
            for w in widgets:
                table.add_row(w["id"], w["type"], w["title"])
            console.print(table)
        else:
            console.print("No widgets would render.")

        if not issues:
            console.print(
                "[bold green]OK[/bold green] — every widget, dataset, metric, and clock entry is valid"
                + (" and every dot-path resolves." if output is not None else ".")
            )
            continue

        for issue in issues:
            style = "red" if issue.dropped else "yellow"
            prefix = "dropped" if issue.dropped else issue.label
            console.print(f"[bold {style}]{prefix}[/bold {style}] {issue.message}")
        failed = True

    if failed:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# harumi share
# ---------------------------------------------------------------------------

share_app = typer.Typer(
    help="Manage a project's public, unauthenticated dashboard links (a project can have several)."
)
app.add_typer(share_app, name="share")


def _share_link_url(link: ProjectShareLink) -> str:
    return f"{active_platform_url()}/share/{link.token}"


def _share_reach(link: ProjectShareLink) -> str:
    """What a visitor can do with this link, in words: always the latest run's dashboard, plus
    whatever has been switched on."""
    extras = [
        label
        for label, on in (
            ("the deployed app", link.app_enabled),
            ("the assistant", link.chat_enabled),
            ("past runs", link.run_history_enabled),
            ("run the project", link.run_control_enabled),
        )
        if on
    ]
    return "view the latest dashboard" + (f", and use {', '.join(extras)}" if extras else " only")


def _share_short_reach(link: ProjectShareLink) -> str:
    names = [
        name
        for name, on in (
            ("app", link.app_enabled),
            ("assistant", link.chat_enabled),
            ("history", link.run_history_enabled),
            ("control", link.run_control_enabled),
        )
        if on
    ]
    return ", ".join(names) if names else "view only"


def _share_exposure(link: ProjectShareLink) -> str:
    """A warning when a link that anyone can open does more than show the dashboard."""
    if link.enabled and not link.password_set and (
        link.app_enabled or link.chat_enabled or link.run_history_enabled or link.run_control_enabled
    ):
        return "no password and more than view access: anyone with the link can use it"
    return ""


def _print_share_link(link: ProjectShareLink) -> None:
    label = escape(_printable(link.label)) if link.label else "untitled"
    console.print(f"Share link [bold]{escape(link.id)}[/bold] ({label})")
    if link.enabled:
        console.print(f"URL: {_share_link_url(link)}", soft_wrap=True)
        console.print(f"Anyone with this link can {_share_reach(link)}.")
        console.print(
            "It has a [bold]password[/bold]: visitors must enter it first."
            if link.password_set
            else "It has [bold]no password[/bold]: it opens for anyone who has the URL."
        )
        warning = _share_exposure(link)
        if warning:
            console.print(f"[yellow]Careful: {warning}.[/yellow]")
    else:
        console.print(
            "[bold]Disabled[/bold]: the URL does not work. Turn it back on with "
            f"[bold]harumi share update {escape(link.id)} --enable[/bold]."
        )
        console.print("It has a password." if link.password_set else "It has no password.")


def _pick_share_link(client: Client, project_id: str, action: str) -> Optional[str]:
    """Choose one of the project's share links; its id, or None if the user backed out."""
    links = client.list_share_links(project_id)
    if not links:
        _fail("This project has no share links. Create one with [bold]harumi share add[/bold].")
    options = [
        interactive.Option(
            _printable(link.label) if link.label else "untitled",
            link.id,
            hint=f"{'on' if link.enabled else 'off'}, {_share_short_reach(link)}, "
            f"{'password' if link.password_set else 'no password'}  {link.id}",
        )
        for link in links
    ]
    return interactive.select(  # type: ignore[return-value]
        f"Which share link do you want to {action}?", options, flag="the LINK_ID argument"
    )


_FULL_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _resolve_share_link(client: Client, project_id: str, link_id: str) -> str:
    """The full id for what the user typed: an exact id, or the start of exactly one.

    A table on a narrow terminal cuts ids, and what gets copied is often only the start. A full
    id is used as given, with no lookup: a wrong one gets the server's 404 (see `_share_not_found`).
    """
    if _FULL_ID.match(link_id):
        return link_id
    return _match_share_link(client.list_share_links(project_id), link_id).id


def _match_share_link(links: list[ProjectShareLink], link_id: str) -> ProjectShareLink:
    """The one link in `links` that `link_id` names, exactly or as the start of its id."""
    exact = next((link for link in links if link.id == link_id), None)
    if exact:
        return exact
    starts = [link for link in links if link_id and link.id.startswith(link_id)]
    if len(starts) == 1:
        return starts[0]
    if len(starts) > 1:
        _fail(f"'{escape(link_id)}' matches more than one share link. Give more of the id (see [bold]harumi share list[/bold]).")
    _fail(f"No share link '{escape(link_id)}' on this project. See them with [bold]harumi share list[/bold].")


def _share_not_found(exc: ApiError) -> None:
    """Turn the API's bare 404 for a full-looking but unknown link id into a pointer."""
    if exc.status_code == 404:
        _fail(f"{escape(str(exc))}\nSee this project's links with [bold]harumi share list[/bold].")


@share_app.command("list")
@_handle_errors
def share_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List a project's public dashboard links. Use `share get` for a link's full URL and permissions."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    links = client.list_share_links(project_id)
    if not links:
        console.print("No share links yet. Create one with [bold]harumi share add[/bold].")
        return

    # Only the start of the id is shown (8 characters, the first group of the UUID), because every
    # `share` command accepts the start of an id and the full 36 left no room for anything else.
    table = Table("id", "label", "state", "visitors can", "password")
    table.columns[0].no_wrap = True
    exposed = False
    for link in links:
        warn = bool(_share_exposure(link))
        exposed = exposed or warn
        table.add_row(
            escape(link.id[:8]),
            escape(_printable(link.label)) if link.label else "[dim]untitled[/dim]",
            "on" if link.enabled else "[dim]off[/dim]",
            _share_short_reach(link) + (" [yellow]![/yellow]" if warn else ""),
            "yes" if link.password_set else "no",
        )
    console.print(table)
    if exposed:
        console.print(
            "[yellow]![/yellow] no password and more than view access: anyone with the link can use it. "
            "Set one with [bold]harumi share update <id> --password[/bold]."
        )
    console.print("[dim]Open one with harumi share get <id>.[/dim]")


@share_app.command("get")
@_handle_errors
def share_get(
    link_id: Optional[str] = typer.Argument(None, help="Share link id (or the start of it). Pick from a list if omitted."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show one share link: its URL, who can do what with it, and whether it has a password."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    if link_id is None:
        link_id = _pick_share_link(client, project_id, "look at")
        if link_id is None:
            return
    # `get` needs the link itself, so it lists once and matches here (a full id too: a typo in
    # one hex digit must read as "no such link", not crash).
    _print_share_link(_match_share_link(client.list_share_links(project_id), link_id))


_IO_CONTROL_OPTION = typer.Option(
    None,
    "--io-control/--no-io-control",
    hidden=True,
    help="Removed. Accepted and ignored so existing scripts keep working.",
)


def _warn_io_control_removed(value: Optional[bool]) -> None:
    """`--io-control` was dropped when the server removed the permission. A script or
    CI job that still passes it should get a warning, not a usage error (exit 2)."""
    if value is not None:
        err_console.print(
            "[yellow]Warning:[/yellow] --io-control/--no-io-control no longer does anything "
            "and is ignored; share links can't grant input/output control."
        )


@share_app.command("add")
@_handle_errors
def share_add(
    label: Optional[str] = typer.Option(None, "--label", help="Optional name to tell links apart, e.g. 'Client dashboard'."),
    app: bool = typer.Option(False, "--app/--no-app", help="Let visitors open the project's deployed Streamlit app."),
    chat: bool = typer.Option(False, "--chat/--no-chat", help="Let signed-in visitors ask the read-only assistant about this project."),
    run_history: bool = typer.Option(False, "--run-history/--no-run-history", help="Let visitors browse past runs, not just the latest one."),
    run_control: bool = typer.Option(False, "--run-control/--no-run-control", help="Let signed-in visitors run now, override the kernel, and manage schedules."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
    io_control: Optional[bool] = _IO_CONTROL_OPTION,
) -> None:
    """Create a new public dashboard link. Every permission defaults to off."""
    _warn_io_control_removed(io_control)
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    body: dict = {
        "app_enabled": app,
        "chat_enabled": chat,
        "run_history_enabled": run_history,
        "run_control_enabled": run_control,
    }
    if label:
        body["label"] = label

    link = client.create_share_link(project_id, body)
    console.print("[bold green]Created[/bold green] share link.")
    _print_share_link(link)


@share_app.command("update")
@_handle_errors
def share_update(
    link_id: Optional[str] = typer.Argument(None, help="Share link id (or the start of it). Pick from a list if omitted."),
    label: Optional[str] = typer.Option(None, "--label", help="Rename the link."),
    enabled: Optional[bool] = typer.Option(None, "--enable/--disable", help="Turn the link on or off. The old URL stops working immediately when disabled."),
    app: Optional[bool] = typer.Option(None, "--app/--no-app", help="Let visitors open the project's deployed Streamlit app."),
    chat: Optional[bool] = typer.Option(None, "--chat/--no-chat", help="Let signed-in visitors ask the read-only assistant about this project."),
    run_history: Optional[bool] = typer.Option(None, "--run-history/--no-run-history", help="Let visitors browse past runs, not just the latest one."),
    run_control: Optional[bool] = typer.Option(None, "--run-control/--no-run-control", help="Let signed-in visitors run now, override the kernel, and manage schedules."),
    password: Optional[bool] = typer.Option(None, "--password/--no-password", help="--password sets or changes the link's password (prompted, hidden, min 8 characters); --no-password removes it. Previously unlocked viewers must re-enter it."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
    io_control: Optional[bool] = _IO_CONTROL_OPTION,
) -> None:
    """Partially update a share link. Only provided fields are changed."""
    _warn_io_control_removed(io_control)
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    body: dict = {}
    if label is not None:
        body["label"] = label
    if enabled is not None:
        body["enabled"] = enabled
    if app is not None:
        body["app_enabled"] = app
    if chat is not None:
        body["chat_enabled"] = chat
    if run_history is not None:
        body["run_history_enabled"] = run_history
    if run_control is not None:
        body["run_control_enabled"] = run_control

    if not body and password is None:
        _fail("No fields to update. Pass at least one flag (e.g. --label, --chat, --password).")

    if link_id is None:
        link_id = _pick_share_link(client, project_id, "change")
        if link_id is None:
            return
    link_id = _resolve_share_link(client, project_id, link_id)

    # Ask for the password before changing anything, and apply it last: setting or
    # removing it signs out every viewer who had unlocked the link, so it must not
    # happen if the other fields were rejected.
    new_password = None
    if password is True:
        new_password = typer.prompt(
            "Enter share link password (hidden, min 8 characters)",
            hide_input=True,
            confirmation_prompt=True,
        )

    if body:
        try:
            link = client.update_share_link(project_id, link_id, body)
        except ApiError as exc:
            _share_not_found(exc)
            raise
        console.print("[bold green]Updated[/bold green] share link.")
        _print_share_link(link)

    try:
        if password is True:
            client.set_share_link_password(project_id, link_id, new_password)
            console.print("[bold green]Set[/bold green] the share link password. Previously unlocked viewers must re-enter it.")
        elif password is False:
            client.remove_share_link_password(project_id, link_id)
            console.print("[bold red]Removed[/bold red] the share link password.")
    except HarumiError:
        if body:
            err_console.print(
                "[yellow]The other fields were already updated; only the password change failed.[/yellow]"
            )
        raise


@share_app.command("rm")
@_handle_errors
def share_rm(
    link_id: Optional[str] = typer.Argument(None, help="Share link id (or the start of it). Pick from a list if omitted."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Permanently delete a share link. The old URL stops working immediately."""
    if link_id is None and yes:
        # --yes removes the question, so nothing would stand between a slip in a list and a deleted link.
        _fail("Say which link to delete: [bold]harumi share rm <link id> --yes[/bold]. Find the id with [bold]harumi share list[/bold].")
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    if link_id is None:
        link_id = _pick_share_link(client, project_id, "delete")
        if link_id is None:
            return

    known: Optional[ProjectShareLink] = None
    if not yes:
        # One listing serves both the short-id match and the label in the question below.
        known = _match_share_link(client.list_share_links(project_id), link_id)
        link_id = known.id
    elif not _FULL_ID.match(link_id):
        link_id = _resolve_share_link(client, project_id, link_id)

    if not yes:
        # Name the link, not just its id: a prefix that matched the wrong one is then easy to spot.
        who = f"'{_printable(known.label) if known and known.label else 'untitled'}' ({link_id})"
        if not typer.confirm(f"Delete share link {who}? This cannot be undone."):  # plain text: not Rich markup
            console.print("Aborted.")
            return

    try:
        client.delete_share_link(project_id, link_id)
    except ApiError as exc:
        _share_not_found(exc)
        raise
    console.print(f"[bold red]Deleted[/bold red] share link [bold]{escape(link_id)}[/bold]. Its URL no longer works.")


@share_app.command("rotate")
@_handle_errors
def share_rotate(
    link_id: Optional[str] = typer.Argument(None, help="Share link id (or the start of it). Pick from a list if omitted."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Invalidate a share link's current token and mint a new one. Its flags are unchanged."""
    if link_id is None and yes:
        _fail("Say which link to rotate: [bold]harumi share rotate <link id> --yes[/bold]. Find the id with [bold]harumi share list[/bold].")
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    if link_id is None:
        link_id = _pick_share_link(client, project_id, "rotate")
        if link_id is None:
            return
    link_id = _resolve_share_link(client, project_id, link_id)

    if not yes and not typer.confirm("Rotate this share link? The current URL will stop working immediately."):
        console.print("Aborted.")
        return

    try:
        link = client.rotate_share_link(project_id, link_id)
    except ApiError as exc:
        _share_not_found(exc)
        raise
    console.print("[bold green]Rotated[/bold green] share link. Anyone using the old URL now needs the new one.")
    _print_share_link(link)


@share_app.command("set-password", hidden=True)
@_deprecated("harumi share set-password", "harumi share update <LINK_ID> --password")
@_handle_errors
def share_set_password(
    link_id: str = typer.Argument(..., help="Share link id."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Set or change a share link's password (min 8 characters, prompted, hidden input)."""
    project_id = _resolve_project(project)
    password = typer.prompt("Enter share link password (hidden, min 8 characters)", hide_input=True)
    client = _get_client(api_url=api_url, org=org)
    client.set_share_link_password(project_id, link_id, password)
    console.print("[bold green]Set[/bold green] the share link password. Previously unlocked viewers must re-enter it.")


@share_app.command("rm-password", hidden=True)
@_deprecated("harumi share rm-password", "harumi share update <LINK_ID> --no-password")
@_handle_errors
def share_rm_password(
    link_id: str = typer.Argument(..., help="Share link id."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Remove a share link's password. It becomes freely viewable."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)
    client.remove_share_link_password(project_id, link_id)
    console.print("[bold red]Removed[/bold red] the share link password.")


# ---------------------------------------------------------------------------
# harumi datasources

# ---------------------------------------------------------------------------

datasources_app = typer.Typer(help="Manage project datasources (database connections).")
app.add_typer(datasources_app, name="datasources")


def _prompt_credentials(current: str = "credentials") -> str:
    return typer.prompt(f"Enter {current} (hidden)", hide_input=True)


def _read_pem(path: Path, flag: str) -> str:
    """Read a certificate/key file as text, failing clearly if it can't be used."""
    try:
        text = path.read_text()
    except OSError as exc:
        _fail(f"Could not read {flag} from {str(path)!r}: {exc}")
        raise AssertionError("unreachable")  # _fail always raises
    if not text.strip():
        _fail(f"{str(path)!r} is empty — {flag} needs the PEM contents.")
    return text


def _proxy_tls_material(
    ca_cert: Optional[Path],
    client_cert: Optional[Path],
    client_key: Optional[Path],
) -> dict:
    """Turn the --proxy-tls-* file paths into the PEM strings the API stores in SSM.

    The flags take paths because these are multi-line PEM blobs; passing one
    inline would mean the certificate ends up in the user's shell history.
    """
    paths = {
        "proxy_tls_ca_cert": (ca_cert, "--proxy-tls-ca-cert"),
        "proxy_tls_client_cert": (client_cert, "--proxy-tls-client-cert"),
        "proxy_tls_client_key": (client_key, "--proxy-tls-client-key"),
    }
    return {
        field: _read_pem(path, flag)
        for field, (path, flag) in paths.items()
        if path is not None
    }


def _require_complete_proxy_config(
    proxy_host: Optional[str],
    proxy_port: Optional[int],
    material: dict,
) -> None:
    """Reject a half-specified --use-proxy before doing any work.

    The API requires the whole set (host, port and all three PEMs) whenever
    use_proxy is on, so checking here turns a wasted round trip — and a
    credentials prompt the user would have to answer again — into one message
    naming every flag that's still missing.
    """
    missing = [
        flag
        for flag, value in (
            ("--proxy-host", proxy_host),
            ("--proxy-port", proxy_port),
            ("--proxy-tls-ca-cert", material.get("proxy_tls_ca_cert")),
            ("--proxy-tls-client-cert", material.get("proxy_tls_client_cert")),
            ("--proxy-tls-client-key", material.get("proxy_tls_client_key")),
        )
        if not value
    ]
    if missing:
        _fail("--use-proxy also needs: " + ", ".join(missing) + ".")


def _reject_orphan_proxy_flags(
    use_proxy: bool,
    proxy_host: Optional[str],
    proxy_port: Optional[int],
    proxy_server_name: Optional[str],
    material: dict,
) -> None:
    """Catch the inverse mistake: proxy flags given without --use-proxy.

    Without this, `_proxy_tls_material` would already have read the PEM
    files for nothing and the datasource would be silently created as a
    plain, non-proxied connection with no warning that the proxy flags were
    ignored — easy to get wrong when scripting `datasources add`/`test`.
    """
    if use_proxy:
        return
    if proxy_host or proxy_port or proxy_server_name or material:
        _fail(
            "--proxy-host/--proxy-port/--proxy-server-name/--proxy-tls-* require --use-proxy."
        )


@datasources_app.command("list")
@_handle_errors
def datasources_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List datasources for a project."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    result = client.list_datasources(project_id)
    if not result.datasources:
        console.print("No datasources found.")
        return

    table = Table("name", "type", "host", "database", "use_proxy")
    for ds in result.datasources:
        table.add_row(ds.name, ds.type, ds.host or "", ds.database or "", "yes" if ds.use_proxy else "no")
    console.print(table)


@datasources_app.command("get")
@_handle_errors
def datasources_get(
    name: str = typer.Argument(..., help="Datasource name."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show details for one datasource (credentials are never returned)."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    ds = client.get_datasource(project_id, name)
    table = Table("field", "value")
    for field in ("id", "name", "type", "host", "port", "database", "username", "use_proxy", "proxy_host"):
        table.add_row(field, str(getattr(ds, field, "") or ""))
    console.print(table)


@datasources_app.command("add")
@_handle_errors
def datasources_add(
    name: str = typer.Argument(..., help="Datasource name (unique per project)."),
    type: str = typer.Option(..., "--type", help="postgresql | mysql | sqlserver | oracle"),
    host: Optional[str] = typer.Option(None, "--host", help="Database host."),
    port: Optional[int] = typer.Option(None, "--port", help="Database port."),
    database: Optional[str] = typer.Option(None, "--database", help="Database name."),
    username: Optional[str] = typer.Option(None, "--username", help="Database username."),
    use_proxy: bool = typer.Option(False, "--use-proxy", help="Route traffic via the mTLS proxy."),
    proxy_host: Optional[str] = typer.Option(None, "--proxy-host", help="Proxy target host. Used when --use-proxy is set."),
    proxy_port: Optional[int] = typer.Option(None, "--proxy-port", help="Proxy target port. Used when --use-proxy is set."),
    proxy_server_name: Optional[str] = typer.Option(None, "--proxy-server-name", help="Proxy TLS server name. Used when --use-proxy is set."),
    proxy_tls_ca_cert: Optional[Path] = typer.Option(None, "--proxy-tls-ca-cert", help="Path to the proxy CA certificate PEM. Required with --use-proxy."),
    proxy_tls_client_cert: Optional[Path] = typer.Option(None, "--proxy-tls-client-cert", help="Path to the client certificate PEM. Required with --use-proxy."),
    proxy_tls_client_key: Optional[Path] = typer.Option(None, "--proxy-tls-client-key", help="Path to the client private key PEM. Required with --use-proxy."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Create a new datasource. Credentials are prompted interactively (never a flag)."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    # Read and check the proxy material before prompting, so a missing flag
    # doesn't cost the user a credentials prompt they have to repeat.
    tls_material = _proxy_tls_material(proxy_tls_ca_cert, proxy_tls_client_cert, proxy_tls_client_key)
    if use_proxy:
        _require_complete_proxy_config(proxy_host, proxy_port, tls_material)
    else:
        _reject_orphan_proxy_flags(use_proxy, proxy_host, proxy_port, proxy_server_name, tls_material)

    credentials = _prompt_credentials()

    body: dict = {
        "name": name,
        "type": type,
        "host": host,
        "port": port,
        "database": database,
        "username": username,
        "credentials": credentials,
        "use_proxy": use_proxy,
    }
    if use_proxy:
        body["proxy_host"] = proxy_host
        body["proxy_port"] = proxy_port
        if proxy_server_name:
            body["proxy_server_name"] = proxy_server_name
        body.update(tls_material)

    ds = client.create_datasource(project_id, body)
    console.print(f"[bold green]Created[/bold green] datasource [bold]{ds.name}[/bold] ({ds.type}).")


@datasources_app.command("update")
@_handle_errors
def datasources_update(
    name: str = typer.Argument(..., help="Datasource name."),
    new_name: Optional[str] = typer.Option(None, "--name", help="Rename the datasource."),
    type: Optional[str] = typer.Option(None, "--type", help="postgresql | mysql | sqlserver | oracle"),
    host: Optional[str] = typer.Option(None, "--host", help="Database host."),
    port: Optional[int] = typer.Option(None, "--port", help="Database port."),
    database: Optional[str] = typer.Option(None, "--database", help="Database name."),
    username: Optional[str] = typer.Option(None, "--username", help="Database username."),
    set_credentials: bool = typer.Option(False, "--set-credentials", help="Prompt to replace the stored credentials."),
    use_proxy: Optional[bool] = typer.Option(None, "--use-proxy/--no-use-proxy", help="Route traffic via the mTLS proxy."),
    proxy_host: Optional[str] = typer.Option(None, "--proxy-host", help="Proxy target host. Used when --use-proxy is set."),
    proxy_port: Optional[int] = typer.Option(None, "--proxy-port", help="Proxy target port. Used when --use-proxy is set."),
    proxy_server_name: Optional[str] = typer.Option(None, "--proxy-server-name", help="Proxy TLS server name. Used when --use-proxy is set."),
    proxy_tls_ca_cert: Optional[Path] = typer.Option(None, "--proxy-tls-ca-cert", help="Path to a new proxy CA certificate PEM."),
    proxy_tls_client_cert: Optional[Path] = typer.Option(None, "--proxy-tls-client-cert", help="Path to a new client certificate PEM."),
    proxy_tls_client_key: Optional[Path] = typer.Option(None, "--proxy-tls-client-key", help="Path to a new client private key PEM."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Partially update a datasource. Only provided fields are changed."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    body: dict = {}
    if new_name:
        body["name"] = new_name
    if type:
        body["type"] = type
    if host:
        body["host"] = host
    if port is not None:
        body["port"] = port
    if database:
        body["database"] = database
    if username:
        body["username"] = username
    if use_proxy is not None:
        body["use_proxy"] = use_proxy
    if proxy_host:
        body["proxy_host"] = proxy_host
    if proxy_port is not None:
        body["proxy_port"] = proxy_port
    if proxy_server_name:
        body["proxy_server_name"] = proxy_server_name
    # Certificates are rotatable one at a time: the API keeps whichever parts of
    # the stored bundle this request doesn't mention, so only the flags actually
    # passed are sent. Completeness is the API's call, since it alone knows what
    # the datasource already has.
    body.update(_proxy_tls_material(proxy_tls_ca_cert, proxy_tls_client_cert, proxy_tls_client_key))
    if set_credentials:
        body["credentials"] = _prompt_credentials()

    if not body:
        _fail("No fields to update. Pass at least one flag (e.g. --host, --set-credentials).")

    ds = client.update_datasource(project_id, name, body)
    console.print(f"[bold green]Updated[/bold green] datasource [bold]{ds.name}[/bold].")


@datasources_app.command("rm")
@_handle_errors
def datasources_rm(
    name: str = typer.Argument(..., help="Datasource name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Delete a datasource (removes the DB row and its stored credentials)."""
    project_id = _resolve_project(project)

    if not yes and not typer.confirm(f"Delete datasource '{name}'? This cannot be undone."):
        console.print("Aborted.")
        return

    client = _get_client(api_url=api_url, org=org)
    ds = client.delete_datasource(project_id, name)
    console.print(f"[bold red]Deleted[/bold red] datasource [bold]{ds.name}[/bold].")


@datasources_app.command("test")
@_handle_errors
def datasources_test(
    type: str = typer.Option(..., "--type", help="postgresql | mysql | sqlserver | oracle"),
    host: str = typer.Option(..., "--host", help="Database host."),
    port: int = typer.Option(..., "--port", help="Database port."),
    database: str = typer.Option(..., "--database", help="Database name."),
    username: str = typer.Option(..., "--username", help="Database username."),
    use_proxy: bool = typer.Option(False, "--use-proxy", help="Route traffic via the mTLS proxy."),
    proxy_host: Optional[str] = typer.Option(None, "--proxy-host", help="Proxy target host. Used when --use-proxy is set."),
    proxy_port: Optional[int] = typer.Option(None, "--proxy-port", help="Proxy target port. Used when --use-proxy is set."),
    proxy_server_name: Optional[str] = typer.Option(None, "--proxy-server-name", help="Proxy TLS server name. Used when --use-proxy is set."),
    proxy_tls_ca_cert: Optional[Path] = typer.Option(None, "--proxy-tls-ca-cert", help="Path to the proxy CA certificate PEM. Required with --use-proxy."),
    proxy_tls_client_cert: Optional[Path] = typer.Option(None, "--proxy-tls-client-cert", help="Path to the client certificate PEM. Required with --use-proxy."),
    proxy_tls_client_key: Optional[Path] = typer.Option(None, "--proxy-tls-client-key", help="Path to the client private key PEM. Required with --use-proxy."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Test a connection without persisting it. Credentials are prompted interactively."""
    client = _get_client(api_url=api_url, org=org)

    tls_material = _proxy_tls_material(proxy_tls_ca_cert, proxy_tls_client_cert, proxy_tls_client_key)
    if use_proxy:
        _require_complete_proxy_config(proxy_host, proxy_port, tls_material)
    else:
        _reject_orphan_proxy_flags(use_proxy, proxy_host, proxy_port, proxy_server_name, tls_material)

    credentials = _prompt_credentials()

    body: dict = {
        "type": type,
        "host": host,
        "port": port,
        "database": database,
        "username": username,
        "credentials": credentials,
        "use_proxy": use_proxy,
    }
    if use_proxy:
        body["proxy_host"] = proxy_host
        body["proxy_port"] = proxy_port
        if proxy_server_name:
            body["proxy_server_name"] = proxy_server_name
        body.update(tls_material)

    result = client.test_datasource_connection(body)
    if result.success:
        console.print(f"[bold green]Success[/bold green]: {result.message}")
    else:
        _fail(result.message)


@datasources_app.command("query")
@_handle_errors
def datasources_query(
    name: str = typer.Argument(..., help="Datasource name."),
    sql: str = typer.Option(..., "--sql", help="SELECT/WITH-only SQL to run."),
    limit: int = typer.Option(10000, "--limit", help="Max rows to return (server-capped at 100000)."),
    csv: Optional[Path] = typer.Option(None, "--csv", help="Write results to this CSV path instead of printing a table."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Run a read-only query against a datasource (SELECT/WITH-only, row-capped server-side)."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    result = client.execute_datasource_query(project_id, name, sql, limit=limit)

    if csv:
        import csv as csv_module

        with open(csv, "w", newline="") as f:
            writer = csv_module.writer(f)
            writer.writerow(result.columns)
            writer.writerows(result.data)
        console.print(f"Wrote {result.row_count} rows to {csv}")
    else:
        table = Table(*result.columns)
        for row in result.data:
            table.add_row(*(str(v) for v in row))
        console.print(table)
        console.print(f"[dim]{result.row_count} row(s) returned.[/dim]")

    if result.was_limited:
        console.print(
            f"[yellow]Result was truncated at the server-side row cap "
            f"({result.max_rows}). Add --limit or narrow your query.[/yellow]"
        )


# ---------------------------------------------------------------------------
# harumi schedules
# ---------------------------------------------------------------------------

schedules_app = typer.Typer(help="Manage project cron schedules for git-ref runs.")
app.add_typer(schedules_app, name="schedules")


@schedules_app.command("list")
@_handle_errors
def schedules_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List cron schedules for a project."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    schedules = client.list_schedules(project_id)
    if not schedules:
        console.print("No schedules found.")
        return

    table = Table("id", "cron", "git_branch", "command", "kernel_spec", "last_executed_at")
    for s in schedules:
        table.add_row(
            s.id,
            s.cron,
            s.git_branch,
            s.command or "",
            s.kernel_spec,
            str(s.last_executed_at or ""),
        )
    console.print(table)


@schedules_app.command("get")
@_handle_errors
def schedules_get(
    schedule_id: str = typer.Argument(..., help="Schedule id."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show details for one schedule."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    s = client.get_schedule(project_id, schedule_id)
    table = Table("field", "value")
    for field in (
        "id",
        "cron",
        "start_at",
        "git_branch",
        "git_commit",
        "command",
        "kernel_spec",
        "output_format",
        "email_to",
        "status",
        "last_executed_at",
    ):
        table.add_row(field, str(getattr(s, field, "") or ""))
    console.print(table)


@schedules_app.command("add")
@_handle_errors
def schedules_add(
    cron: str = typer.Option(..., "--cron", help='Raw 5-field cron expression, interpreted in UTC (e.g. "0 9 * * *").'),
    start_at: Optional[str] = typer.Option(None, "--start-at", help="ISO-8601 datetime. Defaults to now (UTC)."),
    git_branch: str = typer.Option("main", "--git-branch", help="Branch to run on each fire."),
    git_commit: Optional[str] = typer.Option(None, "--git-commit", help="Pin to a specific commit instead of the branch tip."),
    command: Optional[str] = typer.Option(None, "--command", help="Override the harumi.toml command."),
    kernel: Optional[str] = typer.Option(None, "--kernel", help="Kernel to run on (see `harumi specs`), overriding the project's."),
    output_format: Optional[str] = typer.Option(None, "--output-format", help="Output format for scheduled runs."),
    email_to: Optional[str] = typer.Option(None, "--email-to", help="only-me | everyone | comma-separated emails."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Create a new cron schedule for a project.

    Cron is validated server-side (croniter) — an invalid expression returns
    a clear 400 error. The cron is interpreted in UTC.
    """
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    from datetime import datetime, timezone

    body: dict = {
        "cron": cron,
        "start_at": start_at or datetime.now(timezone.utc).isoformat(),
        "git_branch": git_branch,
    }
    if git_commit:
        body["git_commit"] = git_commit
    if command:
        body["command"] = command
    if kernel:
        body["kernel_spec"] = kernel
    if output_format:
        body["output_format"] = output_format
    if email_to:
        body["email_to"] = email_to

    schedule = client.create_schedule(project_id, body)
    console.print(
        f"[bold green]Created[/bold green] schedule [bold]{schedule.id}[/bold] "
        f"(cron=[bold]{schedule.cron}[/bold], UTC)."
    )


@schedules_app.command("update")
@_handle_errors
def schedules_update(
    schedule_id: str = typer.Argument(..., help="Schedule id."),
    cron: Optional[str] = typer.Option(None, "--cron", help='Raw 5-field cron expression, interpreted in UTC (e.g. "0 9 * * *").'),
    start_at: Optional[str] = typer.Option(None, "--start-at", help="ISO-8601 datetime."),
    git_branch: Optional[str] = typer.Option(None, "--git-branch", help="Branch to run on each fire."),
    git_commit: Optional[str] = typer.Option(None, "--git-commit", help="Pin to a specific commit instead of the branch tip."),
    command: Optional[str] = typer.Option(None, "--command", help="Override the harumi.toml command."),
    kernel: Optional[str] = typer.Option(None, "--kernel", help="Kernel to run on (see `harumi specs`), overriding the project's."),
    output_format: Optional[str] = typer.Option(None, "--output-format", help="Output format for scheduled runs."),
    email_to: Optional[str] = typer.Option(None, "--email-to", help="only-me | everyone | comma-separated emails."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Partially update a cron schedule. Only provided fields are changed."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    body: dict = {}
    if cron:
        body["cron"] = cron
    if start_at:
        body["start_at"] = start_at
    if git_branch:
        body["git_branch"] = git_branch
    if git_commit:
        body["git_commit"] = git_commit
    if command:
        body["command"] = command
    if kernel:
        body["kernel_spec"] = kernel
    if output_format:
        body["output_format"] = output_format
    if email_to:
        body["email_to"] = email_to

    if not body:
        _fail("No fields to update. Pass at least one flag (e.g. --cron, --start-at).")

    schedule = client.update_schedule(project_id, schedule_id, body)
    console.print(f"[bold green]Updated[/bold green] schedule [bold]{schedule.id}[/bold].")


@schedules_app.command("rm")
@_handle_errors
def schedules_rm(
    schedule_id: str = typer.Argument(..., help="Schedule id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Delete a cron schedule. This is the only way to stop it firing — there is no pause/enable flag."""
    project_id = _resolve_project(project)

    if not yes and not typer.confirm(f"Delete schedule '{schedule_id}'? This cannot be undone."):
        console.print("Aborted.")
        return

    client = _get_client(api_url=api_url, org=org)
    schedule = client.delete_schedule(project_id, schedule_id)
    console.print(f"[bold red]Deleted[/bold red] schedule [bold]{schedule.id}[/bold].")


# ---------------------------------------------------------------------------
# harumi secrets
# ---------------------------------------------------------------------------

secrets_app = typer.Typer(help="Manage project secrets (environment variables).")
app.add_typer(secrets_app, name="secrets")


@secrets_app.command("list")
@_handle_errors
def secrets_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List secret names for a project. Values are never printed by `list`."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    secrets = client.list_secrets(project_id)
    if not secrets:
        console.print("No secrets found.")
        return

    table = Table("name")
    for s in secrets:
        table.add_row(s.name)
    console.print(table)


@secrets_app.command("set")
@_handle_errors
def secrets_set(
    name: str = typer.Argument(..., help="Secret (env var) name."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Create or overwrite a secret. The value is prompted interactively (never a flag)."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    value = typer.prompt(f"Enter value for {name!r} (hidden)", hide_input=True)
    client.create_secret(project_id, name, value)
    console.print(f"[bold green]Set[/bold green] secret [bold]{name}[/bold].")


@secrets_app.command("rm")
@_handle_errors
def secrets_rm(
    name: str = typer.Argument(..., help="Secret name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Delete a secret."""
    project_id = _resolve_project(project)

    if not yes and not typer.confirm(f"Delete secret '{name}'? This cannot be undone."):
        console.print("Aborted.")
        return

    client = _get_client(api_url=api_url, org=org)
    client.delete_secret(project_id, name)
    console.print(f"[bold red]Deleted[/bold red] secret [bold]{name}[/bold].")


# ---------------------------------------------------------------------------
# harumi memories
# ---------------------------------------------------------------------------

memories_app = typer.Typer(help="Manage project memories (notes the project's chat remembers).")
app.add_typer(memories_app, name="memories")

_MEMORY_MODES = ("always", "on_demand")
_MEMORY_PREVIEW_CHARS = 80
# Mirrors MemoryContent in harumi-api/src/api/memories/schemas.py: stripped, 1-4000 chars.
_MEMORY_MAX_CHARS = 4000


def _check_memory_mode(mode: Optional[str]) -> Optional[str]:
    if mode is not None and mode not in _MEMORY_MODES:
        _fail(f"Invalid --mode {mode!r}. Use one of: {', '.join(_MEMORY_MODES)}.")
    return mode


def _check_memory_content(content: str) -> str:
    """Reject text the API would 422 on, before spending a request on it."""
    if not content.strip():
        _fail("Memory content can't be empty.")
    if len(content.strip()) > _MEMORY_MAX_CHARS:
        _fail(f"Memory content is too long ({len(content.strip())} characters; the limit is {_MEMORY_MAX_CHARS}).")
    return content


def _memory_preview(content: str) -> str:
    flat = " ".join(content.split())
    if len(flat) <= _MEMORY_PREVIEW_CHARS:
        return flat
    return flat[: _MEMORY_PREVIEW_CHARS - 1] + "…"


_MODE_HELP = "'always' loads the memory into every chat; 'on_demand' (default) only when retrieved."


@memories_app.command("list")
@_handle_errors
def memories_list(
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Full-text search over memory content."),
    mode: Optional[str] = typer.Option(None, "--mode", help="Only 'always' or only 'on_demand' memories."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List a project's memories, plus those inherited from its workspace.

    Inherited rows (scope 'organization' or 'user') are read-only here: edit
    or remove them from the workspace's own settings.
    """
    _check_memory_mode(mode)
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    memories = client.list_memories(project_id, query=query, retrieval_mode=mode)
    if not memories:
        console.print("No memories found.")
        return

    table = Table("id", "scope", "mode", "content", "updated")
    for m in memories:
        table.add_row(
            m.id,
            m.scope,
            m.retrieval_mode,
            escape(_memory_preview(m.content)),
            str(m.updated_at or ""),
        )
    console.print(table)


@memories_app.command("add")
@_handle_errors
def memories_add(
    content: Optional[str] = typer.Argument(None, help="The memory text (1-4000 characters). Prompted for if omitted."),
    mode: str = typer.Option("on_demand", "--mode", help=_MODE_HELP),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Add a memory to the project, visible to every member with access to it."""
    _check_memory_mode(mode)
    project_id = _resolve_project(project)

    if content is None:
        content = typer.prompt("Memory")
    _check_memory_content(content)

    client = _get_client(api_url=api_url, org=org)
    memory = client.create_memory(project_id, content, retrieval_mode=mode)
    console.print(f"[bold green]Added[/bold green] memory [bold]{memory.id}[/bold].")


@memories_app.command("edit")
@_handle_errors
def memories_edit(
    memory_id: str = typer.Argument(..., help="Memory id (see `harumi memories list`)."),
    content: Optional[str] = typer.Option(None, "--content", help="New memory text."),
    mode: Optional[str] = typer.Option(None, "--mode", help="New retrieval mode: 'always' or 'on_demand'."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Edit a project memory's text and/or retrieval mode. Only provided fields change."""
    _check_memory_mode(mode)
    project_id = _resolve_project(project)

    if content is None and mode is None:
        _fail("No fields to update. Pass --content and/or --mode.")
    if content is not None:
        _check_memory_content(content)

    client = _get_client(api_url=api_url, org=org)
    memory = client.update_memory(project_id, memory_id, content=content, retrieval_mode=mode)
    console.print(f"[bold green]Updated[/bold green] memory [bold]{memory.id}[/bold].")


@memories_app.command("rm")
@_handle_errors
def memories_rm(
    memory_id: str = typer.Argument(..., help="Memory id (see `harumi memories list`)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Delete a project memory."""
    project_id = _resolve_project(project)

    if not yes and not typer.confirm(f"Delete memory '{memory_id}'? This cannot be undone."):
        console.print("Aborted.")
        return

    client = _get_client(api_url=api_url, org=org)
    memory = client.delete_memory(project_id, memory_id)
    console.print(f"[bold red]Deleted[/bold red] memory [bold]{memory.id}[/bold].")


# ---------------------------------------------------------------------------
# harumi org
# ---------------------------------------------------------------------------

org_app = typer.Typer(help="Manage organizations and their members.")
app.add_typer(org_app, name="org")


org_app.command("use")(org_use)


@org_app.command("list")
@_handle_errors
def org_list(
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
) -> None:
    """List organizations you belong to."""
    client = _get_client(api_url=api_url)
    orgs = client.list_organizations()
    if not orgs:
        console.print("No organizations found.")
        return

    table = Table("id", "business_name", "role")
    for o in orgs:
        table.add_row(o.id, o.business_name, o.role or o.role_name or "")
    console.print(table)


@org_app.command("create")
@_handle_errors
def org_create(
    business_name: str = typer.Argument(..., help="Organization name."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
) -> None:
    """Create a new organization."""
    client = _get_client(api_url=api_url)
    org = client.create_organization(business_name)
    console.print(f"[bold green]Created[/bold green] organization [bold]{org.business_name}[/bold] (id={org.id}).")


@org_app.command("rename")
@_handle_errors
def org_rename(
    organization_id: str = typer.Argument(..., help="Organization id."),
    business_name: str = typer.Argument(..., help="New organization name."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
) -> None:
    """Rename an organization."""
    client = _get_client(api_url=api_url)
    org = client.update_organization(organization_id, business_name)
    console.print(f"[bold green]Renamed[/bold green] organization to [bold]{org.business_name}[/bold].")


@org_app.command("rm")
@_handle_errors
def org_rm(
    organization_id: str = typer.Argument(..., help="Organization id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
) -> None:
    """Delete an organization. This cannot be undone."""
    if not yes and not typer.confirm(f"Delete organization '{organization_id}'? This cannot be undone."):
        console.print("Aborted.")
        return

    client = _get_client(api_url=api_url)
    client.delete_organization(organization_id)
    console.print(f"[bold red]Deleted[/bold red] organization [bold]{organization_id}[/bold].")


@org_app.command("members")
@_handle_errors
def org_members(
    organization_id: str = typer.Argument(..., help="Organization id."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
) -> None:
    """List an organization's members."""
    client = _get_client(api_url=api_url)
    members = client.list_organization_members(organization_id)
    if not members:
        console.print("No members found.")
        return

    table = Table("user_id", "email", "role", "pending")
    for m in members:
        table.add_row(m.user_id, m.email or "", m.role, "yes" if m.pending else "")
    console.print(table)


@org_app.command("invite")
@_handle_errors
def org_invite(
    organization_id: str = typer.Argument(..., help="Organization id."),
    email: str = typer.Option(..., "--email", help="Email of the person to invite."),
    role: str = typer.Option("member", "--role", help="owner | admin | member | viewer"),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
) -> None:
    """Invite a member to an organization."""
    client = _get_client(api_url=api_url)
    member = client.invite_organization_member(organization_id, email, role)
    console.print(f"[bold green]Invited[/bold green] {email} as [bold]{member.role}[/bold].")


@org_app.command("role")
@_handle_errors
def org_role(
    organization_id: str = typer.Argument(..., help="Organization id."),
    user_id: str = typer.Argument(..., help="User id."),
    role: str = typer.Option(..., "--role", help="owner | admin | member | viewer"),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
) -> None:
    """Change a member's role."""
    client = _get_client(api_url=api_url)
    member = client.update_organization_member_role(organization_id, user_id, role)
    console.print(f"[bold green]Updated[/bold green] role to [bold]{member.role}[/bold].")


@org_app.command("rm-member")
@_handle_errors
def org_rm_member(
    organization_id: str = typer.Argument(..., help="Organization id."),
    user_id: str = typer.Argument(..., help="User id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
) -> None:
    """Remove a member from an organization."""
    if not yes and not typer.confirm(f"Remove user '{user_id}' from the organization?"):
        console.print("Aborted.")
        return

    client = _get_client(api_url=api_url)
    client.remove_organization_member(organization_id, user_id)
    console.print(f"[bold red]Removed[/bold red] user [bold]{user_id}[/bold].")


# ---------------------------------------------------------------------------
# Deprecated names — hidden, warn on stderr, removed in 0.12.0
# ---------------------------------------------------------------------------
# Renamed in 0.11. Each runs the new command's code unchanged. (Aliases whose
# behaviour differs are declared next to the command they replace.)

app.command("push", hidden=True)(_deprecated("harumi push", "harumi import")(import_project))
skill_app.command("install")(_deprecated("harumi skill install", "harumi agent-skills install")(agent_skills_install))
_hidden_alias(projects_app, "delete", projects_rm, "harumi projects delete", "harumi projects rm")
_hidden_alias(share_app, "remove", share_rm, "harumi share remove", "harumi share rm")
_hidden_alias(datasources_app, "remove", datasources_rm, "harumi datasources remove", "harumi datasources rm")
_hidden_alias(schedules_app, "remove", schedules_rm, "harumi schedules remove", "harumi schedules rm")
_hidden_alias(org_app, "delete", org_rm, "harumi org delete", "harumi org rm")
_hidden_alias(org_app, "remove", org_rm_member, "harumi org remove", "harumi org rm-member")


def main() -> None:
    from harumi.update import UpdateCheck

    _print_banner_if_bare_entrypoint()
    check = UpdateCheck(sys.argv[1:])
    check.start()
    try:
        app()
    finally:
        # Runs after the command (including after `typer.Exit`/SystemExit), so a
        # notice never lands in the middle of a command's own output.
        check.finish()


if __name__ == "__main__":
    main()
