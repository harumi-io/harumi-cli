"""`harumi` command-line interface.

    harumi login [--signup]
    harumi logout
    harumi whoami
    harumi profile show|set
    harumi specs
    harumi templates
    harumi init --project <id> [--api-url <url>] [--git-url <url>]
    harumi import [path] [--from-git <url>] [--project-name <name>]
    harumi run [--branch <b>] [--commit <sha>] [--command <c>] [--kernel <k>]
               [--watch] [--output-dir <dir>]
    harumi runs list|get|cancel [--project <id>]
    harumi outputs --project <id> [--latest] [--download <output_id>]
    harumi config set-org <ORG_ID>
    harumi skill install|path
    harumi projects create|list|get|rename|delete
    harumi repo ls|cat|put|rm|mv|download|branches|branch|promote|dir
    harumi dashboard widgets|validate [--project <id>]
    harumi share list|get|add|update|remove|rotate|set-password|rm-password [--project <id>]
    harumi datasources list|get|add|update|remove|test|query [--project <id>]
    harumi schedules list|get|add|update|remove [--project <id>]
    harumi secrets list|set|rm [--project <id>]
    harumi org list|create|rename|delete|members|invite|role|remove

Git-ref execution model
-----------------------
Every run goes through the project's Harumi Git (Gitea) repo.  If the
working tree is dirty or has unpushed commits, the CLI auto-pushes a
throwaway scratch branch so the run still executes without forcing the
user to commit manually.

Requires `harumi init` to have been run in (or above) the current directory.

Repo files
----------
`harumi repo` reads and writes the project's Gitea repo directly through
harumi-api (no local git credential needed for file operations — only
`git push`/`harumi run`'s scratch-branch path uses the Gitea token). Writes
always land in a single commit via the batch `repo/changes` endpoint.

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
before `repo put` is the only way to catch a typo up front.

Sharing
-------
`harumi share` manages a project's public, unauthenticated dashboard links —
a project can have several, each independently revocable and optionally
password-protected. Every permission (assistant, run history, run control,
inputs/outputs) defaults to off on `add`, so creating a link never silently
grants more than a bare read-only, latest-run-only dashboard view.
`run-control` capabilities additionally require the viewer to be signed in.
`rotate`/`set-password` invalidate previously issued viewer sessions for
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
`harumi projects create` calls `POST /projects`, which provisions the
project's Gitea repo server-side, then binds the current directory the
same way `harumi init` does (skip with `--no-bind`).
"""

from __future__ import annotations

import base64
import functools
import json
import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from harumi import __version__, auth
from harumi import skills as skills_mod
from harumi.client import Client
from harumi.config import (
    ENVIRONMENTS,
    Config,
    ProjectBinding,
    active_environment,
    active_platform_url,
    load_git_token,
    load_git_username,
    resolve_environment,
    save_environment,
    save_git_token,
)
from harumi.dashboard import (
    DashboardSchemaError,
    DashboardTomlError,
    local_dashboard_paths,
    pick_dashboard_paths,
    validate_dashboard_toml,
    widget_schemas,
)
from harumi.errors import ApiError, HarumiError, NotAuthenticatedError
from harumi.git import (
    GitError,
    NotAHarumiRepoError,
    current_branch,
    delete_remote_scratch,
    ensure_remote,
    has_unpushed_commits,
    is_dirty,
    push_folder,
    push_scratch,
    repo_root,
)
from harumi.models import ProjectShareLink

app = typer.Typer(
    name="harumi",
    help="Run local optimization code on Harumi's infrastructure via Harumi Git.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


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
        help="Backend environment to target for this command (e.g. production, staging). "
        "Overrides the persisted default set by `harumi env use`.",
    ),
) -> None:
    pass


def _get_client(
    api_url: Optional[str] = None,
    git_url: Optional[str] = None,
    org: Optional[str] = None,
) -> Client:
    return Client(api_url=api_url, git_url=git_url, org_id=org)


def _fail(message: str) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code=1)


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
    _fail("Provide --project or run from a directory with a .harumi binding (see `harumi init`).")
    raise AssertionError("unreachable")  # _fail always raises


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
        except ApiError as exc:
            _fail(str(exc))
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
        except FileNotFoundError as exc:
            _fail(str(exc))

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

    First time logging in with a given email? Pass --signup to create the
    account first — otherwise harumi-api rejects the OTP request with
    'Signups not allowed for otp'.
    """
    config = Config.load(api_url=api_url, git_url=git_url)
    env = ENVIRONMENTS[config.environment]
    console.print(
        f"Environment: [bold]{config.environment}[/bold] "
        f"([dim]{config.api_url}[/dim])"
        + (" [yellow](internal — VPN required)[/yellow]" if env.internal else "")
    )
    email = email or typer.prompt("Email")

    try:
        auth.request_otp(config, email, sign_up=signup)
    except ApiError as exc:
        if not signup and exc.status_code == 422:
            _fail(
                f"{exc} — this looks like a new account. Retry with "
                f"[bold]harumi login --signup[/bold]."
            )
        raise
    console.print(f"A login code was sent to [bold]{email}[/bold].")
    code = typer.prompt("Enter the code")

    user = auth.verify_otp(config, email, code)
    console.print(
        f"[bold green]Logged in[/bold green] as {user.email or email} "
        f"on [bold]{config.environment}[/bold]."
    )

    _resolve_and_store_org(config)
    _provision_git_token(config)


def _resolve_and_store_org(config: Config) -> None:
    try:
        client = _get_client(api_url=config.api_url)
        orgs = client.list_organizations()
    except HarumiError:
        return

    if not orgs:
        return
    if len(orgs) == 1:
        config.save_org_id(orgs[0].id)
        console.print(f"Using organization: [bold]{orgs[0].business_name}[/bold]")
    else:
        console.print(
            "You belong to multiple organizations. Set one with:\n"
            "  [bold]harumi config set-org <ORG_ID>[/bold]\n"
            "or pass [bold]--org[/bold] on each command."
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
        console.print(
            f"[dim]Gitea user [bold]{creds.username}[/bold] provisioned.[/dim]"
        )
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
    """Clear the stored local session for the active environment."""
    # Resolve the active env (honors --env / HARUMI_ENV / saved default).
    Config.load()
    auth.logout()
    console.print(f"Logged out of [bold]{active_environment()}[/bold].")


@app.command()
@_handle_errors
def whoami(
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show the currently logged-in user."""
    client = _get_client(api_url=api_url, org=org)
    profile = client.get_profile()
    console.print(
        f"[bold]{profile.email or '?'}[/bold]  (id: {profile.id or '?'})  "
        f"[dim]env: {client.config.environment}[/dim]"
    )


# ---------------------------------------------------------------------------
# harumi env
# ---------------------------------------------------------------------------

env_app = typer.Typer(help="Select and inspect the backend environment (production/staging).")
app.add_typer(env_app, name="env")


def _internal_visible() -> bool:
    """Whether internal (VPN-only) environments should be listed. Off by
    default so regular users only ever see production; internal devs opt in
    with HARUMI_INTERNAL=1 (or `harumi env list --all`)."""
    return os.environ.get("HARUMI_INTERNAL", "").lower() in {"1", "true", "yes"}


@env_app.command("list")
def env_list(
    all_: bool = typer.Option(False, "--all", "-a", help="Include internal (VPN-only) environments."),
) -> None:
    """List selectable environments (the active one is flagged)."""
    current = active_environment()
    show_internal = all_ or _internal_visible()

    table = Table("", "name", "api_url", "git_url", "access")
    for name, env in ENVIRONMENTS.items():
        if env.internal and not show_internal:
            continue
        table.add_row(
            "*" if name == current else "",
            name,
            env.api_url,
            env.git_url,
            "internal (VPN)" if env.internal else "public",
        )
    console.print(table)


@env_app.command("current")
def env_current() -> None:
    """Show the active environment and its endpoints."""
    name = active_environment()
    env = ENVIRONMENTS[name]
    console.print(
        f"[bold]{name}[/bold]\n"
        f"  api: {env.api_url}\n"
        f"  git: {env.git_url}\n"
        f"  access: {'internal (VPN required)' if env.internal else 'public'}"
    )


@env_app.command("use")
def env_use(
    name: str = typer.Argument(..., help="Environment to make the default (e.g. production, staging)."),
) -> None:
    """Persist the default environment for future commands.

    Each environment keeps its own stored session, so switching does not log
    you out of the other one — but you must `harumi login` at least once per
    environment (they use separate Supabase backends).
    """
    try:
        save_environment(name)
    except ValueError as exc:
        _fail(str(exc))

    env = ENVIRONMENTS[name]
    console.print(f"Default environment set to [bold]{name}[/bold] ([dim]{env.api_url}[/dim]).")
    if env.internal:
        console.print(
            "[yellow]This is an internal environment — it requires the VPN and an "
            "account in its Supabase.[/yellow]"
        )
    from harumi.config import load_credentials

    if not load_credentials():
        console.print(f"You're not logged in on [bold]{name}[/bold] yet — run [bold]harumi login[/bold].")


# ---------------------------------------------------------------------------
# Profile sub-commands
# ---------------------------------------------------------------------------

profile_app = typer.Typer(help="View and update your account profile.")
app.add_typer(profile_app, name="profile")


@profile_app.command("show")
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

config_app = typer.Typer(help="Manage local CLI configuration.")
app.add_typer(config_app, name="config")


@config_app.command("set-org")
def config_set_org(org_id: str = typer.Argument(..., help="Organization id to persist as X-Organization.")) -> None:
    """Persist the organization id sent as X-Organization on every request."""
    config = Config.load()
    config.save_org_id(org_id)
    console.print(f"Organization set to [bold]{org_id}[/bold].")


# ---------------------------------------------------------------------------
# harumi skill — seed the bundled agent skills onto this machine
# ---------------------------------------------------------------------------

skill_app = typer.Typer(help="Install the bundled harumi-cli agent skills (Cursor, Claude Code, etc.).")
app.add_typer(skill_app, name="skill")


@skill_app.command("install")
def skill_install(
    agent: Optional[list[str]] = typer.Option(
        None,
        "--agent",
        "-a",
        help=f"Target agent(s): {', '.join(skills_mod.AGENT_KEYS)}. Default: auto-detect.",
    ),
    project: bool = typer.Option(
        False, "--project", help=f"Write to ./{skills_mod.PROJECT_SKILLS_DIR} instead of a global agent directory."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be written without writing it."),
    force: bool = typer.Option(False, "--force", help="Overwrite a destination even if it doesn't look like a skill."),
) -> None:
    """Copy the harumi-cli and harumi-cli-setup skills onto this machine.

    With no flags, auto-detects installed agents (Cursor/Claude Code/Codex)
    by their config directory and writes to each one's global skills folder.
    """
    try:
        written = skills_mod.install(agent_keys=agent, project=project, dry_run=dry_run, force=force)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        _fail(str(exc))
        return
    verb = "Would write" if dry_run else "Wrote"
    for path in written:
        console.print(f"[dim]{verb}:[/dim] {path}")
    if not dry_run:
        console.print(f"[bold green]Installed[/bold green] {len(written)} skill director{'y' if len(written) == 1 else 'ies'}.")


@skill_app.command("path")
def skill_path() -> None:
    """Print the local directory containing the bundled skills."""
    for skill_dir in skills_mod.bundled_skill_dirs():
        console.print(str(skill_dir))


# ---------------------------------------------------------------------------
# Discovery commands
# ---------------------------------------------------------------------------

@app.command()
@_handle_errors
def specs(
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List available kernel specs (sizes/images) for running code."""
    client = _get_client(api_url=api_url, org=org)
    table = Table("name", "display_name", "cpu", "memory", "subscription_required")
    for spec in client.get_specs():
        table.add_row(
            spec.name,
            spec.display_name,
            str(spec.size.cpu),
            spec.size.memory,
            "yes" if spec.subscription_required else "no",
        )
    console.print(table)


@app.command()
@_handle_errors
def templates(
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List project templates. Pass a template's id as `projects create --template-id`."""
    client = _get_client(api_url=api_url, org=org)
    items = client.list_templates()
    if not items:
        console.print("No templates found.")
        return

    table = Table("id", "slug", "name", "description")
    for t in items:
        table.add_row(t.id, t.slug, t.name, t.description)
    console.print(table)


# ---------------------------------------------------------------------------
# harumi init
# ---------------------------------------------------------------------------

def _bind_and_configure_remote(project_id: str, repo, cwd: Optional[Path] = None) -> None:
    """Write .harumi/config.json for `project_id`/`repo` and configure the
    `harumi` git remote in the target directory (defaults to cwd), if possible.

    Shared by `init` (binding an existing project), `projects create`
    (binding a just-created project), and `import` (binding an imported folder).
    `repo` is a `RepoInfo`-shaped object (owner/name/clone_url/default_branch).
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
            "Run [bold]git init[/bold] then [bold]harumi init --project ...[/bold] again."
        )
        return

    git_token = load_git_token()
    if not git_token:
        console.print(
            "[yellow]No Gitea token found — skipping remote setup.[/yellow]\n"
            "Run [bold]harumi login[/bold] to provision one."
        )
        return

    username = load_git_username()
    if not username:
        console.print(
            "[yellow]No Gitea username on file — skipping remote setup.[/yellow]\n"
            "Run [bold]harumi login[/bold] again to re-provision your Gitea credentials."
        )
        return

    ensure_remote(
        clone_url=repo.clone_url,
        username=username,
        token=git_token,
        cwd=target,
    )
    console.print(
        "[bold green]Remote `harumi` configured.[/bold green]\n"
        f"Push your code:  git push harumi {repo.default_branch}"
    )


@app.command()
@_handle_errors
def init(
    project: str = typer.Option(..., "--project", "-p", help="Harumi project id to bind this directory to."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Override the Harumi Git base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Bind the current directory to a Harumi project and configure the git remote.

    Fetches the project's Gitea repo from harumi-api, writes .harumi/config.json,
    and configures the `harumi` git remote for HTTPS+token pushes.

    Run this once per project directory before using `harumi run`.
    """
    client = _get_client(api_url=api_url, git_url=git_url, org=org)

    console.print(f"Fetching repo for project [bold]{project}[/bold]...")
    repo = client.get_project_repo(project)

    _bind_and_configure_remote(project, repo)


# ---------------------------------------------------------------------------
# harumi import
# ---------------------------------------------------------------------------

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
            "[yellow]Kept the exported version of "
            f"{len(collisions)} file(s) that also exist in the repo "
            f"(e.g. {', '.join(collisions[:3])}); merge manually if needed.[/yellow]"
        )


@app.command(name="import")
@_handle_errors
def import_project(
    path: Path = typer.Argument(
        Path("."),
        help="Folder to import (an unzipped project export). Defaults to the current directory.",
    ),
    project_name: Optional[str] = typer.Option(
        None, "--project-name", help="Name for the new project (defaults to the folder name)."
    ),
    from_git: Optional[str] = typer.Option(
        None,
        "--from-git",
        help="Also clone this git URL (the project's old GitHub repo) flat into the folder before importing.",
    ),
    bind: bool = typer.Option(
        True, "--bind/--no-bind", help="Bind the folder to the new project (like `harumi init`)."
    ),
    personal: bool = typer.Option(
        False,
        "--personal",
        help="Create in your personal workspace, ignoring the configured org.",
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Override the Harumi Git base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Turn a downloaded project folder into a new git-based Harumi project.

    Creates a project (which provisions a Harumi Git repo), optionally clones the
    project's old GitHub repo flat into the folder, then commits and pushes the
    whole folder as the repo's initial content. Read `HARUMI_IMPORT.md` in the
    export for follow-ups (datasource credentials, the GitHub repo URL).
    """
    folder = path.resolve()
    if not folder.is_dir():
        _fail(f"Not a directory: {folder}")

    client = _get_client(api_url=api_url, git_url=git_url, org=org)

    if from_git:
        _merge_git_repo_flat(from_git, folder)

    name = project_name or folder.name
    console.print(f"Creating project [bold]{name}[/bold]...")
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

    console.print("Pushing project files...")
    push_folder(
        folder,
        clone_url=repo.clone_url,
        username=username,
        token=git_token,
        branch=repo.default_branch,
        message="Import project",
    )
    console.print(
        f"[bold green]Pushed[/bold green] to Harumi ({repo.default_branch})."
    )
    if not bind:
        console.print(f"View project: {active_platform_url()}/projects/{project.id}")

    notes = folder / "HARUMI_IMPORT.md"
    if notes.exists():
        console.print(
            "[yellow]•[/yellow] See [bold]HARUMI_IMPORT.md[/bold] for follow-ups "
            "(datasource credentials, GitHub repo)."
        )

    if bind:
        _bind_and_configure_remote(project.id, repo, cwd=folder)


# ---------------------------------------------------------------------------
# harumi projects
# ---------------------------------------------------------------------------

projects_app = typer.Typer(help="Create and manage Harumi projects.")
app.add_typer(projects_app, name="projects")


@projects_app.command("create")
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
    template_id: Optional[str] = typer.Option(None, "--template-id", help="Template id to pre-configure the project (optional)."),
    bind: bool = typer.Option(
        True, "--bind/--no-bind", help="Bind the current directory to the new project (like `harumi init`)."
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Override the Harumi Git base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Create a new Harumi project and its Gitea repo, then bind this directory to it."""
    if personal and customer_id:
        _fail("--personal and --customer-id are mutually exclusive. Pass only one.")

    client = _get_client(api_url=api_url, git_url=git_url, org=org)

    console.print(f"Creating project [bold]{name}[/bold]...")
    project = client.create_project(
        name, customer_id=customer_id, template_id=template_id, personal=personal
    )
    console.print(f"[bold green]Created[/bold green] project [bold]{project.name}[/bold] (id={project.id}).")
    _print_project_workspace(project.customer_id)

    if project.repo is None:
        console.print(
            "[yellow]No Gitea repo was provisioned for this project "
            "(Harumi Git may not be configured on this backend).[/yellow]"
        )
        return

    if not bind:
        return

    _bind_and_configure_remote(project.id, project.repo)


@projects_app.command("list")
@_handle_errors
def projects_list(
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List your projects."""
    client = _get_client(api_url=api_url, org=org)
    projects = client.list_projects()
    if not projects:
        console.print("No projects found.")
        return

    table = Table("id", "name", "kernel_spec", "role")
    for p in projects:
        table.add_row(p.id, p.name, p.kernel_spec or "", p.role_name or "")
    console.print(table)


@projects_app.command("get")
@_handle_errors
def projects_get(
    project_id: str = typer.Argument(..., help="Project id."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show details for one project."""
    client = _get_client(api_url=api_url, org=org)
    p = client.get_project(project_id)
    table = Table("field", "value")
    for field in ("id", "name", "customer_id", "kernel_spec", "role_name", "created_at", "updated_at"):
        table.add_row(field, str(getattr(p, field, "") or ""))
    console.print(table)


@projects_app.command("rename")
@_handle_errors
def projects_rename(
    project_id: str = typer.Argument(..., help="Project id."),
    name: str = typer.Argument(..., help="New project name."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Rename a project."""
    client = _get_client(api_url=api_url, org=org)
    p = client.update_project(project_id, {"name": name})
    console.print(f"[bold green]Renamed[/bold green] project to [bold]{p.name}[/bold].")


@projects_app.command("delete")
@_handle_errors
def projects_delete(
    project_id: str = typer.Argument(..., help="Project id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Delete a project. This cannot be undone."""
    client = _get_client(api_url=api_url, org=org)

    if not yes:
        p = client.get_project(project_id)
        typed = typer.prompt(
            f"Type the project name ({p.name!r}) to confirm deletion"
        )
        if typed != p.name:
            console.print("Aborted (name did not match).")
            return

    deleted = client.delete_project(project_id)
    console.print(f"[bold red]Deleted[/bold red] project [bold]{deleted.name}[/bold].")


# ---------------------------------------------------------------------------
# harumi run
# ---------------------------------------------------------------------------

@app.command()
@_handle_errors
def run(
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="Run a specific branch."),
    commit: Optional[str] = typer.Option(None, "--commit", help="Run a specific commit SHA."),
    command: Optional[str] = typer.Option(None, "--command", "-c", help="Override the harumi.toml command."),
    kernel: Optional[str] = typer.Option(None, "--kernel", "-k", help="Override the kernel spec."),
    watch: bool = typer.Option(False, "--watch", "-w", help="Block until the run finishes."),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o", help="Download output artifacts here (requires --watch)."
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    git_url: Optional[str] = typer.Option(None, "--git-url", help="Override the Harumi Git base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Run the project via its Harumi Git repo.

    By default, automatically pushes the working tree to a scratch branch so
    you can iterate without committing manually.  Pass --branch or --commit
    to run a specific ref instead.

    Requires `harumi init` to have been run in this directory (or a parent).
    """
    binding = ProjectBinding.load()
    if binding is None:
        _fail(
            "No Harumi project found. Run [bold]harumi init --project <PROJECT_ID>[/bold] first."
        )

    client = _get_client(api_url=api_url, git_url=git_url, org=org)
    project_id = binding.project_id  # type: ignore[union-attr]
    repo = binding.repo  # type: ignore[union-attr]

    scratch_branch: Optional[str] = None

    if branch or commit:
        # Explicit ref — run it directly, no scratch needed.
        run_branch = branch
        run_commit = commit
        console.print(
            f"[bold]Running[/bold] project [bold]{project_id}[/bold] "
            + (f"@ branch [bold]{run_branch}[/bold]" if run_branch else "")
            + (f" commit [bold]{run_commit[:8]}[/bold]" if run_commit else "")
        )
    else:
        # No explicit ref — inspect the working tree.
        dirty = is_dirty()
        unpushed = has_unpushed_commits()

        if not dirty and not unpushed:
            # Clean and pushed: run the current branch directly.
            run_branch = current_branch() or repo.default_branch
            run_commit = None
            console.print(
                f"[bold]Running[/bold] project [bold]{project_id}[/bold] "
                f"@ branch [bold]{run_branch}[/bold]"
            )
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

            status_parts = []
            if dirty:
                status_parts.append("uncommitted changes")
            if unpushed:
                status_parts.append("unpushed commits")
            console.print(
                f"[dim]Working tree has {' and '.join(status_parts)}. "
                f"Pushing scratch branch...[/dim]"
            )

            scratch_branch, _ = push_scratch(username=username, token=git_token)  # type: ignore[arg-type]
            run_branch = scratch_branch
            run_commit = None
            console.print(f"[dim]Scratch branch: [bold]{scratch_branch}[/bold][/dim]")

    try:
        response = client.execute_project(
            project_id,
            branch=run_branch,
            commit=run_commit,
            command=command,
            kernel_spec=kernel,
        )
        console.print(
            f"Queued (execution_log_id={response.execution_log_id}, "
            f"run_id={response.project_run_id}). status={response.status}"
        )

        if not watch:
            if response.project_run_id:
                console.print(
                    f"Run [bold]harumi runs get {response.project_run_id} "
                    f"--project {project_id}[/bold] to check on it later."
                )
            return

        if not response.project_run_id:
            console.print("[yellow]No run id returned; cannot watch this run.[/yellow]")
            return

        from harumi.execution import download_run_output, wait_for_run

        console.print("Waiting for the run to finish...")
        result = wait_for_run(
            client.api,
            project_id,
            response.project_run_id,
            on_poll=lambda r: console.print(f"  status: {r.status}"),
        )

        if result.succeeded:
            console.print(f"[bold green]Run finished[/bold green]: {result.status}")
            if output_dir:
                try:
                    zip_path = download_run_output(client.api, project_id, result, output_dir)
                    console.print(f"Downloaded output to {zip_path}")
                except HarumiError as exc:
                    console.print(f"[yellow]{exc}[/yellow]")
        else:
            console.print(f"[bold red]Run ended with status[/bold red]: {result.status}")
            if result.error:
                console.print(f"Error: {result.error}")
            if result.stderr:
                console.print(f"[dim]stderr:[/dim]\n{result.stderr}")
            raise typer.Exit(code=1)

    finally:
        if scratch_branch:
            delete_remote_scratch(scratch_branch)
            console.print(f"[dim]Scratch branch {scratch_branch} cleaned up.[/dim]")


# ---------------------------------------------------------------------------
# harumi runs
# ---------------------------------------------------------------------------

runs_app = typer.Typer(help="List, inspect, and cancel project runs.")
app.add_typer(runs_app, name="runs")


@runs_app.command("list")
@_handle_errors
def runs_list(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """List runs for a project, newest first."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    runs = client.list_runs(project_id)
    if not runs:
        console.print("No runs found.")
        return

    table = Table("id", "status", "source", "git_branch", "started", "ended")
    for r in runs:
        table.add_row(
            r.id,
            r.status,
            r.source or "",
            r.git_branch or "",
            str(r.started or ""),
            str(r.ended or ""),
        )
    console.print(table)


@runs_app.command("get")
@_handle_errors
def runs_get(
    run_id: str = typer.Argument(..., help="Run id."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show details (including captured stdout/stderr) for one run."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    r = client.get_run(project_id, run_id)
    table = Table("field", "value")
    for field in (
        "id",
        "status",
        "source",
        "git_branch",
        "git_commit",
        "command",
        "kernel_spec",
        "exit_code",
        "output_url",
        "triggered_by",
        "started",
        "ended",
    ):
        table.add_row(field, str(getattr(r, field, "") or ""))
    console.print(table)

    if r.stdout:
        console.print("\n[bold]stdout:[/bold]")
        console.print(r.stdout)
    if r.stderr:
        console.print("\n[bold]stderr:[/bold]")
        console.print(r.stderr)
    if r.error:
        console.print(f"\n[bold red]error:[/bold red] {r.error}")


@runs_app.command("cancel")
@_handle_errors
def runs_cancel(
    run_id: str = typer.Argument(..., help="Run id."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Cancel an in-flight run."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    r = client.cancel_run(project_id, run_id)
    console.print(f"[bold]Run {r.id}[/bold] status is now [bold]{r.status}[/bold].")


# ---------------------------------------------------------------------------
# harumi outputs (thin wrapper over `runs`, kept for backwards compatibility)
# ---------------------------------------------------------------------------

@app.command()
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

    Deprecated alias for `harumi runs`/`harumi run --output-dir`; kept for
    backwards compatibility.
    """
    resolved_project = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    if download:
        from harumi.execution import download_run_output

        run = client.get_run(resolved_project, download)
        zip_path = download_run_output(client.api, resolved_project, run, output_dir)
        console.print(f"Downloaded to {zip_path}")
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

repo_app = typer.Typer(help="Read and write the project's Harumi Git (Gitea) repo.")
app.add_typer(repo_app, name="repo")


@repo_app.command("ls")
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

    if not yes and not typer.confirm(f"Delete '{path}' from the repo? This cannot be undone."):
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
    console.print(f"Downloaded to {zip_path}")


@repo_app.command("branches")
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
            console.print("[bold green]OK[/bold green] — every widget is valid" + (" and every dot-path resolves." if output is not None else "."))
            continue

        for issue in issues:
            style = "red" if issue.dropped else "yellow"
            prefix = "dropped" if issue.dropped else "empty"
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


def _print_share_link(link: ProjectShareLink) -> None:
    state = "[bold green]enabled[/bold green]" if link.enabled else "[bold]disabled[/bold]"
    console.print(f"Link [bold]{link.id}[/bold] ({link.label or 'untitled'}) is {state}.")
    console.print(f"URL: {_share_link_url(link)}")
    console.print(f"Password protected: {'yes' if link.password_set else 'no'}")
    console.print(
        "Permissions: "
        f"assistant={'on' if link.chat_enabled else 'off'}, "
        f"run history={'on' if link.run_history_enabled else 'off'}, "
        f"run control={'on' if link.run_control_enabled else 'off'}, "
        f"inputs/outputs={'on' if link.io_control_enabled else 'off'}"
    )


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
        console.print("No share links found.")
        return

    table = Table("id", "label", "enabled", "permissions", "password", "token")
    for link in links:
        permissions = ", ".join(
            name
            for name, on in (
                ("chat", link.chat_enabled),
                ("run_history", link.run_history_enabled),
                ("run_control", link.run_control_enabled),
                ("io_control", link.io_control_enabled),
            )
            if on
        )
        table.add_row(
            link.id,
            link.label or "",
            "yes" if link.enabled else "no",
            permissions or "-",
            "yes" if link.password_set else "no",
            link.token,
        )
    console.print(table)


@share_app.command("get")
@_handle_errors
def share_get(
    link_id: str = typer.Argument(..., help="Share link id."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Show one share link's full URL and permissions."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    links = client.list_share_links(project_id)
    link = next((sl for sl in links if sl.id == link_id), None)
    if link is None:
        _fail(f"No share link '{link_id}' found on this project.")

    _print_share_link(link)


@share_app.command("add")
@_handle_errors
def share_add(
    label: Optional[str] = typer.Option(None, "--label", help="Optional name to tell links apart, e.g. 'Client dashboard'."),
    chat: bool = typer.Option(False, "--chat/--no-chat", help="Let signed-in visitors ask the read-only assistant about this project."),
    run_history: bool = typer.Option(False, "--run-history/--no-run-history", help="Let visitors browse past runs, not just the latest one."),
    run_control: bool = typer.Option(False, "--run-control/--no-run-control", help="Let signed-in visitors run now, override the kernel, and manage schedules."),
    io_control: bool = typer.Option(False, "--io-control/--no-io-control", help="Let visitors control/edit this project's inputs and outputs."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Create a new public dashboard link. Every permission defaults to off."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    body: dict = {
        "chat_enabled": chat,
        "run_history_enabled": run_history,
        "run_control_enabled": run_control,
        "io_control_enabled": io_control,
    }
    if label:
        body["label"] = label

    link = client.create_share_link(project_id, body)
    console.print("[bold green]Created[/bold green] share link.")
    _print_share_link(link)


@share_app.command("update")
@_handle_errors
def share_update(
    link_id: str = typer.Argument(..., help="Share link id."),
    label: Optional[str] = typer.Option(None, "--label", help="Rename the link."),
    enabled: Optional[bool] = typer.Option(None, "--enable/--disable", help="Turn the link on or off. The old URL stops working immediately when disabled."),
    chat: Optional[bool] = typer.Option(None, "--chat/--no-chat", help="Let signed-in visitors ask the read-only assistant about this project."),
    run_history: Optional[bool] = typer.Option(None, "--run-history/--no-run-history", help="Let visitors browse past runs, not just the latest one."),
    run_control: Optional[bool] = typer.Option(None, "--run-control/--no-run-control", help="Let signed-in visitors run now, override the kernel, and manage schedules."),
    io_control: Optional[bool] = typer.Option(None, "--io-control/--no-io-control", help="Let visitors control/edit this project's inputs and outputs."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Partially update a share link. Only provided fields are changed."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    body: dict = {}
    if label is not None:
        body["label"] = label
    if enabled is not None:
        body["enabled"] = enabled
    if chat is not None:
        body["chat_enabled"] = chat
    if run_history is not None:
        body["run_history_enabled"] = run_history
    if run_control is not None:
        body["run_control_enabled"] = run_control
    if io_control is not None:
        body["io_control_enabled"] = io_control

    if not body:
        _fail("No fields to update. Pass at least one flag (e.g. --label, --chat).")

    link = client.update_share_link(project_id, link_id, body)
    console.print("[bold green]Updated[/bold green] share link.")
    _print_share_link(link)


@share_app.command("remove")
@_handle_errors
def share_remove(
    link_id: str = typer.Argument(..., help="Share link id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Permanently delete a share link. The old URL stops working immediately."""
    project_id = _resolve_project(project)

    if not yes and not typer.confirm(f"Delete share link '{link_id}'? This cannot be undone."):
        console.print("Aborted.")
        return

    client = _get_client(api_url=api_url, org=org)
    client.delete_share_link(project_id, link_id)
    console.print(f"[bold red]Deleted[/bold red] share link [bold]{link_id}[/bold].")


@share_app.command("rotate")
@_handle_errors
def share_rotate(
    link_id: str = typer.Argument(..., help="Share link id."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id. Uses the .harumi binding if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override the harumi-api base URL."),
    org: Optional[str] = typer.Option(None, "--org", help="Override the organization sent as X-Organization."),
) -> None:
    """Invalidate a share link's current token and mint a new one. Its flags are unchanged."""
    project_id = _resolve_project(project)

    if not yes and not typer.confirm("Rotate this share link? The current URL will stop working immediately."):
        console.print("Aborted.")
        return

    client = _get_client(api_url=api_url, org=org)
    link = client.rotate_share_link(project_id, link_id)
    console.print("[bold green]Rotated[/bold green] share link.")
    _print_share_link(link)


@share_app.command("set-password")
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


@share_app.command("rm-password")
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


def _read_pem(path: Path, field: str) -> str:
    """Read a certificate/key file as text, failing clearly if it can't be used."""
    try:
        text = path.read_text()
    except OSError as exc:
        _fail(f"Could not read {field} from {str(path)!r}: {exc}")
        raise AssertionError("unreachable")  # _fail always raises
    if not text.strip():
        _fail(f"{str(path)!r} is empty — {field} needs the PEM contents.")
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
        "proxy_tls_ca_cert": ca_cert,
        "proxy_tls_client_cert": client_cert,
        "proxy_tls_client_key": client_key,
    }
    return {field: _read_pem(path, field) for field, path in paths.items() if path is not None}


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


@datasources_app.command("remove")
@_handle_errors
def datasources_remove(
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
    kernel: Optional[str] = typer.Option(None, "--kernel", help="Override the kernel spec."),
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
    kernel: Optional[str] = typer.Option(None, "--kernel", help="Override the kernel spec."),
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


@schedules_app.command("remove")
@_handle_errors
def schedules_remove(
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
# harumi org
# ---------------------------------------------------------------------------

org_app = typer.Typer(help="Manage organizations and their members.")
app.add_typer(org_app, name="org")


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


@org_app.command("delete")
@_handle_errors
def org_delete(
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


@org_app.command("remove")
@_handle_errors
def org_remove(
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
