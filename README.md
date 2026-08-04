# harumi

Run your local optimization code on Harumi's infrastructure — straight from your terminal or IDE — via the project's self-hosted Gitea repo, instead of pasting it into the platform's notebook editor.

Optimization/solver code (Gurobi, OR-Tools, etc.) is often too heavy to run on a laptop. `harumi` binds a local directory to a Harumi project, runs your code (from a git ref) on Harumi's infrastructure, and lets you inspect/download the results — reusing the exact same backend endpoints the web app and AI agent already use. It can also manage the project's repo, datasources, schedules, secrets, and organizations end to end.

## Install

```bash
pip install -e .
# or, once published:
pip install harumi
```

This installs the `harumi` CLI and the `harumi` Python package (`import harumi`).

## Quick start

```bash
# 1. Log in (Supabase OTP — check your email for the code)
harumi login

# 2. Create a new project, or find an existing one
harumi projects create "My Project"
harumi projects list

# 3. Bind the current directory to a project (skip if `projects create` already bound it)
harumi init --project <PROJECT_ID>

# 4. See available kernel sizes (CPU/RAM, Gurobi vs plain Python)
harumi specs

# 5. Run the bound directory's code on the infra
harumi run --watch --output-dir ./out

# 6. Inspect runs later
harumi runs list
harumi runs get <RUN_ID>
```

## Everything else the CLI can do

- `harumi repo` — browse, read, write, delete, move, and download files in the project's Gitea repo; create/delete/promote branches (versions).
- `harumi datasources` — CRUD project database connections, test them, and run read-only SQL queries against them.
- `harumi schedules` — CRUD cron schedules that trigger git-ref runs.
- `harumi secrets` — CRUD project-scoped environment variables.
- `harumi org` — CRUD organizations and manage their members.
- `harumi profile` — view/update your account profile.

Run `harumi --help` or any subcommand with `--help` for the full flag reference, or see the [command reference](.agents/skills/harumi-cli/references/commands.md) for endpoint-level detail.

## Execution model

Every run is git-ref based: code lives in the project's Harumi Git (Gitea) repo. If your working tree is dirty or has unpushed commits when you run `harumi run`, the CLI transparently pushes a throwaway scratch branch so you can iterate without committing manually — your real branches are never touched. Pass `--branch`/`--commit` to run a specific ref instead.

## Configuration & environments

The CLI targets one of two environments (each with its own Supabase, so each has its own login):

| Env | API | Gitea | Access |
|---|---|---|---|
| `production` (default) | `https://api.harumi.io/api` | `https://git.harumi.io` | public |
| `staging` | `https://api.dev.harumi.io/api` | `https://git.dev.harumi.io` | internal, VPN-only |

```bash
harumi env list          # production only (staging hidden unless --all / HARUMI_INTERNAL=1)
harumi env use staging   # internal devs; requires VPN + a staging account
harumi --env staging run # override for a single command
```

Selection precedence: `--env` > `HARUMI_ENV` > `harumi env use` (saved default) > `production`. Within an environment you can still override endpoints for local development:

| Env var | Purpose |
|---|---|
| `HARUMI_API_URL` | Override `harumi-api` base URL (e.g. `http://localhost:8000/api`) |
| `HARUMI_GIT_URL` | Override the Harumi Git (Gitea) base URL |
| `HARUMI_ORG` | Organization ID sent as `X-Organization` |
| `HARUMI_INTERNAL` | Set to `1` to reveal internal environments in `harumi env list` |

Credentials (JWT + refresh token + Gitea token) are stored per-environment under `~/.harumi/environments/<env>/credentials.json` (mode `0600`) after `harumi login`. An older flat `~/.harumi/credentials.json` is migrated into `production` automatically on first run.

## Library usage

```python
from harumi import Client
from harumi.config import ProjectBinding

binding = ProjectBinding.load()  # reads .harumi/config.json in cwd (or a parent)
client = Client()  # loads stored credentials

response = client.execute_project(binding.project_id, branch="main")
```

See the [command reference](.agents/skills/harumi-cli/references/commands.md#python-library-client-alternative) for more examples (polling, repo edits, datasources, schedules, secrets, orgs).

## Development

```bash
pip install -e ".[dev]"
pytest
```

All tests are offline (SSE parser + mocked HTTP transport) — no live backend required.
