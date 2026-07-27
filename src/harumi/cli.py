"""`harumi` command-line interface.

    harumi login [--signup]
    harumi logout
    harumi whoami
    harumi specs
    harumi notebooks [--project <id>]
    harumi projects create <NAME> [--customer-id <id>] [--template-id <id>] [--no-bind]
    harumi init --project <id> [--api-url <url>] [--git-url <url>]
    harumi run [--branch <b>] [--commit <sha>] [--command <c>] [--kernel <k>]
               [--watch] [--output-dir <dir>]
    harumi outputs --project <id> [--latest] [--download <output_id>]
    harumi config set-org <ORG_ID>
    harumi datasources list|get|add|update|remove|test|query [--project <id>]
    harumi schedules list|get|add|update|remove [--project <id>]

Git-ref execution model
-----------------------
Every run goes through the project's Harumi Git (Gitea) repo.  If the
working tree is dirty or has unpushed commits, the CLI auto-pushes a
throwaway scratch branch so the run still executes without forcing the
user to commit manually.

Requires `harumi init` to have been run in (or above) the current directory.

Datasources
-----------
`harumi datasources` manages project-scoped database connections (real
endpoints, live today). Credentials are only ever prompted interactively
(hidden input) — never accepted as a flag — and are stored server-side in
AWS SSM. `datasources query` runs a SELECT/WITH-only, row-capped proxy
query so users can validate SQL before wiring it into solver code.

Schedules
---------
`harumi schedules` manages project-scoped cron schedules (ASSUMED
endpoints — the git-first pivot re-keys these from notebook_id to
project_id; calls no-op with a clear error until the backend lands).
Cron is a raw 5-field expression interpreted in UTC; there is no
pause/enable flag — delete the schedule to stop it firing.

Creating projects
-----------------
`harumi projects create` calls the real `POST /projects` endpoint. Repo
provisioning on create is an ASSUMED contract (part of the git-first
pivot) — the command errors clearly if harumi-api doesn't yet return repo
metadata, instead of leaving you with an unusable project. On success it
binds the current directory the same way `harumi init` does (skip with
`--no-bind`).
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from harumi import __version__, auth
from harumi.client import Client
from harumi.config import (
    Config,
    ProjectBinding,
    load_git_token,
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


@app.callback()
def _main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the harumi CLI version and exit.",
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
    console.print(f"[bold green]Logged in[/bold green] as {user.email or email}.")

    _resolve_and_store_org(config)
    _provision_git_token(config)


def _resolve_and_store_org(config: Config) -> None:
    try:
        client = _get_client(api_url=config.api_url)
        response = client.api.request("GET", "/users/organizations")
        orgs = response.json()
    except HarumiError:
        return

    if not orgs:
        return
    if len(orgs) == 1:
        config.save_org_id(orgs[0]["id"])
        console.print(f"Using organization: [bold]{orgs[0].get('business_name', orgs[0]['id'])}[/bold]")
    else:
        console.print(
            "You belong to multiple organizations. Set one with:\n"
            "  [bold]harumi config set-org <ORG_ID>[/bold]\n"
            "or pass [bold]--org[/bold] on each command."
        )
        table = Table("id", "business_name")
        for org in orgs:
            table.add_row(org.get("id", ""), org.get("business_name", ""))
        console.print(table)


def _provision_git_token(config: Config) -> None:
    """Best-effort: request a per-user Gitea token from harumi-api."""
    try:
        client = _get_client(api_url=config.api_url)
        git_user = client.get_git_token()
        save_git_token(git_user.token)
        console.print(
            f"[dim]Gitea user [bold]{git_user.username}[/bold] provisioned.[/dim]"
        )
    except HarumiError:
        console.print(
            "[dim]Gitea token provisioning skipped "
            "(backend not ready — run [bold]harumi login[/bold] again after the git pivot lands).[/dim]"
        )


@app.command()
def logout() -> None:
    """Clear the stored local session."""
    auth.logout()
    console.print("Logged out.")


@app.command()
@_handle_errors
def whoami(
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
) -> None:
    """Show the currently logged-in user."""
    client = _get_client(api_url=api_url, org=org)
    response = client.api.request("GET", "/users/me")
    data = response.json()
    console.print(f"[bold]{data.get('email', '?')}[/bold]  (id: {data.get('id', '?')})")


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

def _bind_and_configure_remote(project_id: str, repo) -> None:
    """Write .harumi/config.json for `project_id`/`repo` and configure the
    `harumi` git remote in the current directory, if possible.

    Shared by `init` (binding an existing project) and `projects create`
    (binding a just-created project). `repo` is a `ProjectRepo`-shaped object
    (owner/name/clone_url/default_branch).
    """
    from harumi.config import ProjectBinding, RepoBinding

    binding = ProjectBinding.write(
        Path.cwd(),
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
    if repo_root() is None:
        console.print(
            "[yellow]Not inside a git repo — skipping remote setup.[/yellow]\n"
            "Run [bold]git init[/bold] then [bold]harumi init --project ...[/bold] again."
        )
        return

    git_token = load_git_token()
    if not git_token:
        console.print(
            "[yellow]No Gitea token found — skipping remote setup.[/yellow]\n"
            "Log in again with [bold]harumi login[/bold] once the git backend is live."
        )
        return

    creds = auth.current_credentials()
    username = (creds or {}).get("email", "harumi-user")

    ensure_remote(
        clone_url=repo.clone_url,
        username=username,
        token=git_token,
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
# harumi projects
# ---------------------------------------------------------------------------

projects_app = typer.Typer(help="Create Harumi projects.")
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
    """Create a new Harumi project and its Gitea repo, then bind this directory to it.

    ASSUMED CONTRACT: `POST /projects` is real today but only creates a bare
    project row — it does not yet provision a Gitea repo. Under the
    git-first pivot (one project <-> one notebook/repo), project creation is
    expected to provision the repo atomically. This command errors clearly
    if harumi-api doesn't return repo metadata yet, rather than silently
    leaving you with a project you can't `harumi init` into.
    """
    client = _get_client(api_url=api_url, git_url=git_url, org=org)

    console.print(f"Creating project [bold]{name}[/bold]...")
    project = client.create_project(name, customer_id=customer_id, template_id=template_id)
    console.print(f"[bold green]Created[/bold green] project [bold]{project.name}[/bold] (id={project.id}).")

    if not bind:
        return

    _bind_and_configure_remote(project.id, project.repo)


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

            creds = auth.current_credentials()
            username = (creds or {}).get("email", "harumi-user")

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
            f"Queued (task_id={response.task_id}, output_id={response.output_id}). "
            f"{response.message}"
        )

        if not watch:
            if response.output_id:
                console.print(
                    f"Run [bold]harumi outputs --project {project_id} --latest[/bold] "
                    "to check on it later."
                )
            return

        if not response.output_id:
            console.print("[yellow]No output_id returned; cannot watch this run.[/yellow]")
            return

        from harumi.execution import wait_for_output, download_output

        console.print("Waiting for the run to finish...")
        output = wait_for_output(
            client.api,
            project_id,
            response.output_id,
            on_poll=lambda o: console.print(f"  status: {o.status}"),
        )

        if output.succeeded:
            console.print(f"[bold green]Run finished[/bold green]: {output.status}")
            if output_dir:
                zip_path = download_output(client.api, project_id, output.id, output_dir)
                console.print(f"Downloaded output to {zip_path}")
        else:
            console.print(f"[bold red]Run ended with status[/bold red]: {output.status}")
            if output.log_url:
                console.print(f"Logs: {output.log_url}")
            raise typer.Exit(code=1)

    finally:
        if scratch_branch:
            delete_remote_scratch(scratch_branch)
            console.print(f"[dim]Scratch branch {scratch_branch} cleaned up.[/dim]")


# ---------------------------------------------------------------------------
# harumi outputs
# ---------------------------------------------------------------------------

@app.command()
@_handle_errors
def outputs(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project id (uses .harumi binding if omitted)."),
    latest: bool = typer.Option(False, "--latest", help="Show only the most recent output."),
    download: Optional[str] = typer.Option(None, "--download", help="Download this output id's files."),
    output_dir: Path = typer.Option(Path("."), "--output-dir", "-o"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
) -> None:
    """List or download outputs for a project."""
    resolved_project = _resolve_project(project)

    client = _get_client(api_url=api_url, org=org)

    if download:
        from harumi.execution import download_output

        zip_path = download_output(client.api, resolved_project, download, output_dir)
        console.print(f"Downloaded to {zip_path}")
        return

    from harumi.execution import get_latest_output

    if latest:
        output = get_latest_output(client.api, resolved_project)
        results = [output] if output else []
    else:
        results = client.list_outputs(resolved_project)

    if not results:
        console.print("No outputs found.")
        return

    table = Table("id", "status", "started", "ended", "scenario")
    for o in results:
        table.add_row(
            o.id,
            o.status or "",
            str(o.started or ""),
            str(o.ended or ""),
            o.scenario_name or "",
        )
    console.print(table)


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

schedules_app = typer.Typer(help="Manage project cron schedules (assumed endpoints — git-first pivot).")
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

    table = Table("id", "cron", "start_at", "kernel_spec", "scenario_name", "last_executed_at")
    for s in schedules:
        table.add_row(
            s.id,
            s.cron,
            str(s.start_at or ""),
            s.kernel_spec,
            s.scenario_name or "",
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
        "kernel_spec",
        "scenario_id",
        "scenario_name",
        "output_format",
        "email_to",
        "last_executed_at",
    ):
        table.add_row(field, str(getattr(s, field, "") or ""))
    console.print(table)


@schedules_app.command("add")
@_handle_errors
def schedules_add(
    cron: str = typer.Option(..., "--cron", help='Raw 5-field cron expression, interpreted in UTC (e.g. "0 9 * * *").'),
    start_at: Optional[str] = typer.Option(None, "--start-at", help="ISO-8601 datetime. Defaults to now (UTC)."),
    kernel: Optional[str] = typer.Option(None, "--kernel", help="Kernel spec (default: or_python_small)."),
    scenario_id: Optional[str] = typer.Option(None, "--scenario-id"),
    scenario_name: Optional[str] = typer.Option(None, "--scenario-name"),
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
    }
    if kernel:
        body["kernel_spec"] = kernel
    if scenario_id:
        body["scenario_id"] = scenario_id
    if scenario_name:
        body["scenario_name"] = scenario_name
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
    kernel: Optional[str] = typer.Option(None, "--kernel"),
    scenario_id: Optional[str] = typer.Option(None, "--scenario-id"),
    scenario_name: Optional[str] = typer.Option(None, "--scenario-name"),
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
    if kernel:
        body["kernel_spec"] = kernel
    if scenario_id:
        body["scenario_id"] = scenario_id
    if scenario_name:
        body["scenario_name"] = scenario_name
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
