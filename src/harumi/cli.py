"""`harumi` command-line interface.

    harumi login [--signup]
    harumi logout
    harumi whoami
    harumi profile show|set
    harumi specs
    harumi notebooks [--project <id>]
    harumi init --project <id> [--api-url <url>] [--git-url <url>]
    harumi import [path] [--from-git <url>] [--project-name <name>]
    harumi run [--branch <b>] [--commit <sha>] [--command <c>] [--kernel <k>]
               [--watch] [--output-dir <dir>]
    harumi runs list|get|cancel [--project <id>]
    harumi outputs --project <id> [--latest] [--download <output_id>]
    harumi config set-org <ORG_ID>
    harumi projects create|list|get|rename|delete
    harumi repo ls|cat|put|rm|mv|download|branches|branch|promote
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
import os
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from harumi import __version__, auth
from harumi.client import Client
from harumi.config import (
    ENVIRONMENTS,
    Config,
    ProjectBinding,
    active_environment,
    load_git_token,
    load_git_username,
    resolve_environment,
    save_environment,
    save_git_token,
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    first_name: Optional[str] = typer.Option(None, "--first-name"),
    last_name: Optional[str] = typer.Option(None, "--last-name"),
    bio: Optional[str] = typer.Option(None, "--bio"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
def config_set_org(org_id: str) -> None:
    """Persist the organization id sent as X-Organization on every request."""
    config = Config.load()
    config.save_org_id(org_id)
    console.print(f"Organization set to [bold]{org_id}[/bold].")


# ---------------------------------------------------------------------------
# Discovery commands
# ---------------------------------------------------------------------------

@app.command()
@_handle_errors
def specs(
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
def notebooks(
    project: Optional[str] = typer.Option(None, "--project", help="Only list notebooks for this project."),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
) -> None:
    """List projects and their notebooks."""
    client = _get_client(api_url=api_url, org=org)

    projects = [p for p in client.list_projects() if not project or p.id == project]
    if not projects:
        console.print("No projects found.")
        return

    for proj in projects:
        console.print(f"\n[bold]{proj.name}[/bold] (project: {proj.id})")
        notebook_list = client.list_notebooks(proj.id)
        if not notebook_list:
            console.print("  (no notebooks)")
            continue
        table = Table("notebook_id", "name")
        for nb in notebook_list:
            table.add_row(nb.id, nb.name or "")
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
        f"[bold green]Remote `harumi` configured[/bold green] → {repo.clone_url}\n"
        f"Push your code:  git push harumi {repo.default_branch}"
    )


@app.command()
@_handle_errors
def init(
    project: str = typer.Option(..., "--project", "-p", help="Harumi project id to bind this directory to."),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    git_url: Optional[str] = typer.Option(None, "--git-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    git_url: Optional[str] = typer.Option(None, "--git-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project = client.create_project(name)
    console.print(
        f"[bold green]Created[/bold green] project [bold]{project.name}[/bold] (id={project.id})."
    )

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
        f"[bold green]Pushed[/bold green] to {repo.clone_url} ({repo.default_branch})."
    )

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
    customer_id: Optional[str] = typer.Option(None, "--customer-id", help="Customer/organization id (optional)."),
    template_id: Optional[str] = typer.Option(None, "--template-id", help="Template id to pre-configure the project (optional)."),
    bind: bool = typer.Option(
        True, "--bind/--no-bind", help="Bind the current directory to the new project (like `harumi init`)."
    ),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    git_url: Optional[str] = typer.Option(None, "--git-url"),
    org: Optional[str] = typer.Option(None, "--org"),
) -> None:
    """Create a new Harumi project and its Gitea repo, then bind this directory to it."""
    client = _get_client(api_url=api_url, git_url=git_url, org=org)

    console.print(f"Creating project [bold]{name}[/bold]...")
    project = client.create_project(name, customer_id=customer_id, template_id=template_id)
    console.print(f"[bold green]Created[/bold green] project [bold]{project.name}[/bold] (id={project.id}).")

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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    git_url: Optional[str] = typer.Option(None, "--git-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    output_dir: Path = typer.Option(Path("."), "--output-dir", "-o"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
) -> None:
    """Download the repo (or a folder within it) as a zip."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    zip_path = client.download_repo_archive(project_id, output, path=path, ref=ref)
    console.print(f"Downloaded to {zip_path}")


@repo_app.command("branches")
@_handle_errors
def repo_branches(
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
) -> None:
    """Promote a version into the live branch via a merge commit."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

    result = client.promote_repo_branch(project_id, name, title=title, delete_after=delete_after)
    if result.conflict:
        _fail(result.message or f"Version {name!r} could not be merged (conflicts).")
    console.print(f"[bold green]Promoted[/bold green] [bold]{name}[/bold] into live.")


# ---------------------------------------------------------------------------
# harumi datasources
# ---------------------------------------------------------------------------

datasources_app = typer.Typer(help="Manage project datasources (database connections).")
app.add_typer(datasources_app, name="datasources")


def _prompt_credentials(current: str = "credentials") -> str:
    return typer.prompt(f"Enter {current} (hidden)", hide_input=True)


@datasources_app.command("list")
@_handle_errors
def datasources_list(
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
    database: Optional[str] = typer.Option(None, "--database"),
    username: Optional[str] = typer.Option(None, "--username"),
    use_proxy: bool = typer.Option(False, "--use-proxy", help="Route traffic via the mTLS proxy."),
    proxy_host: Optional[str] = typer.Option(None, "--proxy-host"),
    proxy_port: Optional[int] = typer.Option(None, "--proxy-port"),
    proxy_server_name: Optional[str] = typer.Option(None, "--proxy-server-name"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
) -> None:
    """Create a new datasource. Credentials are prompted interactively (never a flag)."""
    project_id = _resolve_project(project)
    client = _get_client(api_url=api_url, org=org)

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

    ds = client.create_datasource(project_id, body)
    console.print(f"[bold green]Created[/bold green] datasource [bold]{ds.name}[/bold] ({ds.type}).")


@datasources_app.command("update")
@_handle_errors
def datasources_update(
    name: str = typer.Argument(..., help="Datasource name."),
    new_name: Optional[str] = typer.Option(None, "--name", help="Rename the datasource."),
    type: Optional[str] = typer.Option(None, "--type"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
    database: Optional[str] = typer.Option(None, "--database"),
    username: Optional[str] = typer.Option(None, "--username"),
    set_credentials: bool = typer.Option(False, "--set-credentials", help="Prompt to replace the stored credentials."),
    use_proxy: Optional[bool] = typer.Option(None, "--use-proxy/--no-use-proxy"),
    proxy_host: Optional[str] = typer.Option(None, "--proxy-host"),
    proxy_port: Optional[int] = typer.Option(None, "--proxy-port"),
    proxy_server_name: Optional[str] = typer.Option(None, "--proxy-server-name"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    host: str = typer.Option(..., "--host"),
    port: int = typer.Option(..., "--port"),
    database: str = typer.Option(..., "--database"),
    username: str = typer.Option(..., "--username"),
    use_proxy: bool = typer.Option(False, "--use-proxy"),
    proxy_host: Optional[str] = typer.Option(None, "--proxy-host"),
    proxy_port: Optional[int] = typer.Option(None, "--proxy-port"),
    proxy_server_name: Optional[str] = typer.Option(None, "--proxy-server-name"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
) -> None:
    """Test a connection without persisting it. Credentials are prompted interactively."""
    client = _get_client(api_url=api_url, org=org)
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    output_format: Optional[str] = typer.Option(None, "--output-format"),
    email_to: Optional[str] = typer.Option(None, "--email-to", help="only-me | team | everyone | comma-separated emails."),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    cron: Optional[str] = typer.Option(None, "--cron"),
    start_at: Optional[str] = typer.Option(None, "--start-at", help="ISO-8601 datetime."),
    git_branch: Optional[str] = typer.Option(None, "--git-branch"),
    git_commit: Optional[str] = typer.Option(None, "--git-commit"),
    command: Optional[str] = typer.Option(None, "--command"),
    kernel: Optional[str] = typer.Option(None, "--kernel"),
    output_format: Optional[str] = typer.Option(None, "--output-format"),
    email_to: Optional[str] = typer.Option(None, "--email-to"),
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
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
    api_url: Optional[str] = typer.Option(None, "--api-url"),
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
