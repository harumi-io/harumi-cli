"""`harumi` command-line interface.

    harumi login [--signup]
    harumi logout
    harumi whoami
    harumi specs
    harumi notebooks [--project <id>]
    harumi init --project <id> [--api-url <url>] [--git-url <url>]
    harumi run [--branch <b>] [--commit <sha>] [--command <c>] [--kernel <k>]
               [--watch] [--output-dir <dir>]
    harumi outputs --project <id> [--latest] [--download <output_id>]
    harumi config set-org <ORG_ID>

Git-ref execution model
-----------------------
Every run goes through the project's Harumi Git (Gitea) repo.  If the
working tree is dirty or has unpushed commits, the CLI auto-pushes a
throwaway scratch branch so the run still executes without forcing the
user to commit manually.

Requires `harumi init` to have been run in (or above) the current directory.
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

    from harumi.config import ProjectBinding, RepoBinding

    binding = ProjectBinding.write(
        Path.cwd(),
        project_id=project,
        repo=RepoBinding(
            owner=repo.owner,
            name=repo.name,
            clone_url=repo.clone_url,
            default_branch=repo.default_branch,
        ),
    )
    console.print(
        f"Wrote [bold]{binding.config_path}[/bold] "
        f"(project={project}, repo={repo.owner}/{repo.name})."
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
    resolved_project = project
    if not resolved_project:
        binding = ProjectBinding.load()
        if binding:
            resolved_project = binding.project_id
    if not resolved_project:
        _fail("Provide --project or run from a directory with a .harumi binding.")

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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
