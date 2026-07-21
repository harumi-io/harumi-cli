"""`harumi` command-line interface.

    harumi login
    harumi logout
    harumi specs
    harumi notebooks [--project <id>]
    harumi run <file-or-dir> --notebook <id> [--mode interactive|job] ...
    harumi outputs --notebook <id> [--latest] [--download <output_id>]
"""

from __future__ import annotations

import base64
import functools
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from harumi import auth
from harumi.client import Client
from harumi.config import Config
from harumi.errors import ApiError, HarumiError, NotAuthenticatedError
from harumi.sse import SSEEvent

app = typer.Typer(
    name="harumi",
    help="Run local code on Harumi's infrastructure and fetch the results.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


def _get_client(api_url: Optional[str] = None, org: Optional[str] = None) -> Client:
    return Client(api_url=api_url, org_id=org)


def _fail(message: str) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code=1)


def _handle_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except NotAuthenticatedError as exc:
            _fail(str(exc))
        except ApiError as exc:
            _fail(str(exc))
        except HarumiError as exc:
            _fail(str(exc))
        except FileNotFoundError as exc:
            _fail(str(exc))

    return wrapper


@app.command()
@_handle_errors
def login(
    email: Optional[str] = typer.Option(None, help="Account email. Prompted if omitted."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Override harumi-api base URL."),
) -> None:
    """Log in via one-time email code and store the session locally."""
    config = Config.load(api_url=api_url)
    email = email or typer.prompt("Email")

    auth.request_otp(config, email)
    console.print(f"A login code was sent to [bold]{email}[/bold].")
    code = typer.prompt("Enter the code")

    user = auth.verify_otp(config, email, code)
    console.print(f"[bold green]Logged in[/bold green] as {user.email or email}.")

    _resolve_and_store_org(config)


def _resolve_and_store_org(config: Config) -> None:
    """Best-effort: if the user belongs to exactly one org, store it so
    `X-Organization` doesn't need to be passed on every command.
    """
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


@app.command()
def logout() -> None:
    """Clear the stored local session."""
    auth.logout()
    console.print("Logged out.")


config_app = typer.Typer(help="Manage local CLI configuration.")
app.add_typer(config_app, name="config")


@config_app.command("set-org")
def config_set_org(org_id: str) -> None:
    """Persist the organization id sent as X-Organization on every request."""
    config = Config.load()
    config.save_org_id(org_id)
    console.print(f"Organization set to [bold]{org_id}[/bold].")


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
    """List projects and their notebooks, to find a --notebook id to run against."""
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


@app.command()
@_handle_errors
def run(
    path: Path = typer.Argument(..., help="Local file or directory to run."),
    notebook: str = typer.Option(..., "--notebook", "-n", help="Target notebook id."),
    mode: str = typer.Option(
        "job", "--mode", "-m", help="'interactive' streams live output; 'job' queues an async run."
    ),
    kernel: Optional[str] = typer.Option(
        None, "--kernel", "-k", help="Kernel spec, e.g. or_python_small, gurobi_python_medium."
    ),
    project: Optional[str] = typer.Option(None, "--project", help="Project id (job mode; auto-detected if omitted)."),
    watch: bool = typer.Option(False, "--watch", "-w", help="(job mode) Block until the run finishes."),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o", help="(job mode + --watch) Download output artifacts here."
    ),
    scenario_id: Optional[str] = typer.Option(None, "--scenario-id"),
    scenario_name: Optional[str] = typer.Option(None, "--scenario-name"),
    email_to: Optional[str] = typer.Option(None, "--email-to", help="(job mode) Email results when finished."),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
) -> None:
    """Run local code on Harumi's infrastructure.

    interactive: sends the file's code to the notebook's live sandbox kernel
    and streams stdout/results back in real time.

    job: uploads the path to the notebook's project, then queues the
    notebook's live version on the async job queue (for long/heavy runs).
    """
    client = _get_client(api_url=api_url, org=org)

    if mode == "interactive":
        if path.is_dir():
            _fail("interactive mode requires a single Python file, not a directory.")
        code = path.read_text()
        console.print(f"[bold]Running[/bold] {path} on notebook {notebook} (interactive)...")
        result = client.run_interactive(
            code, notebook_id=notebook, kernel_spec=kernel, on_event=_print_sse_event
        )
        if output_dir:
            _save_rich_results(result, Path(output_dir))
        if not result.ok:
            _print_error(result.error)
            raise typer.Exit(code=1)
        return

    if mode != "job":
        _fail(f"Unknown --mode {mode!r}. Use 'interactive' or 'job'.")

    console.print(f"[bold]Uploading[/bold] {path} and queuing a run on notebook {notebook}...")
    response = client.run_job(
        path,
        notebook_id=notebook,
        project_id=project,
        kernel_spec=kernel,
        scenario_id=scenario_id,
        scenario_name=scenario_name,
        email_to=email_to,
        watch=False,
    )
    console.print(
        f"Queued (task_id={response.task_id}, output_id={response.output_id}). "
        f"{response.message}"
    )

    if not watch:
        console.print(
            f"Run [bold]harumi outputs --notebook {notebook} --latest[/bold] to check on it later."
        )
        return

    if not response.output_id:
        console.print("[yellow]No output_id returned; cannot watch this run.[/yellow]")
        return

    from harumi.execution import wait_for_output

    console.print("Waiting for the run to finish...")
    output = wait_for_output(
        client.api,
        notebook,
        response.output_id,
        on_poll=lambda o: console.print(f"  status: {o.status}"),
    )

    if output.succeeded:
        console.print(f"[bold green]Run finished[/bold green]: {output.status}")
        if output_dir:
            from harumi.execution import download_output

            zip_path = download_output(client.api, notebook, output.id, Path(output_dir))
            console.print(f"Downloaded output to {zip_path}")
    else:
        console.print(f"[bold red]Run ended with status[/bold red]: {output.status}")
        if output.log_url:
            console.print(f"Logs: {output.log_url}")
        raise typer.Exit(code=1)


@app.command()
@_handle_errors
def outputs(
    notebook: str = typer.Option(..., "--notebook", "-n"),
    latest: bool = typer.Option(False, "--latest", help="Show only the most recent output."),
    download: Optional[str] = typer.Option(None, "--download", help="Download this output id's files."),
    output_dir: Path = typer.Option(Path("."), "--output-dir", "-o"),
    api_url: Optional[str] = typer.Option(None, "--api-url"),
    org: Optional[str] = typer.Option(None, "--org"),
) -> None:
    """List or download outputs for a notebook."""
    client = _get_client(api_url=api_url, org=org)

    if download:
        from harumi.execution import download_output

        zip_path = download_output(client.api, notebook, download, output_dir)
        console.print(f"Downloaded to {zip_path}")
        return

    from harumi.execution import get_latest_output

    if latest:
        output = get_latest_output(client.api, notebook)
        results = [output] if output else []
    else:
        results = client.list_outputs(notebook)

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


def _print_sse_event(event: SSEEvent) -> None:
    if event.type == "stream":
        text = event.data.get("text", "")
        if event.data.get("name") == "stderr":
            err_console.print(text, end="")
        else:
            console.print(text, end="")
    elif event.type == "error":
        _print_error(
            {
                "ename": event.data.get("ename", ""),
                "evalue": event.data.get("evalue", ""),
                "traceback": event.data.get("traceback", []),
            }
        )
    elif event.type == "result":
        data = event.data.get("data", {})
        text_plain = data.get("text/plain")
        if text_plain:
            console.print(text_plain)
        rich_keys = [k for k in data if k != "text/plain"]
        if rich_keys:
            console.print(f"[dim](result includes: {', '.join(rich_keys)} — use --output-dir to save)[/dim]")
    elif event.type == "execution_complete":
        ms = event.data.get("execution_time_ms")
        if ms is not None:
            console.print(f"\n[dim]Finished in {ms} ms[/dim]")


def _print_error(error: Optional[dict]) -> None:
    if not error:
        return
    err_console.print(f"[bold red]{error.get('ename', 'Error')}[/bold red]: {error.get('evalue', '')}")
    for line in error.get("traceback", []):
        err_console.print(line)


def _save_rich_results(result, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for idx, data in enumerate(result.results):
        for mime, payload in data.items():
            if mime == "text/plain":
                continue
            if mime == "image/png":
                (output_dir / f"result_{idx}.png").write_bytes(base64.b64decode(payload))
            elif mime in ("text/html", "image/svg+xml"):
                ext = "html" if mime == "text/html" else "svg"
                (output_dir / f"result_{idx}.{ext}").write_text(payload)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
