# harumi

Run your local optimization code on Harumi's infrastructure — straight from your terminal or IDE — via the project's self-hosted Gitea repo, instead of pasting it into the platform's notebook editor.

Optimization/solver code (Gurobi, OR-Tools, etc.) is often too heavy to run on a laptop. `harumi` binds a local directory to a Harumi project, runs your code (from a git ref) on Harumi's infrastructure, and lets you inspect/download the results — reusing the exact same backend endpoints the web app and AI agent already use. It can also manage the project's repo, datasources, schedules, secrets, and organizations end to end.

## Install

```bash
pip install harumi
```

This installs the `harumi` CLI and the `harumi` Python package (`import harumi`).

Installing from source instead (for contributing, or an unreleased fix):

```bash
pip install -e .
```

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

# 7. Check the project's dashboard.toml renders the widgets you expect
harumi dashboard validate --latest
```

## Everything else the CLI can do

- `harumi repo` — browse, read, write, delete, move, and download files in the project's Gitea repo; create/delete/promote branches (versions); `repo dir` for a GitHub-style folder-at-a-time browse.
- `harumi dashboard` — look up the `dashboard.toml` widget reference (`widgets`) and validate a project's dashboard, including its `output.json` dot-paths, before pushing (`validate`).
- `harumi share` — turn the project's public, unauthenticated dashboard link on/off, rotate it, and password-protect it.
- `harumi templates` — list project templates to pass as `projects create --template-id`.
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
| `HARUMI_ORG` | Organization ID sent as `X-Organization`, and the workspace new projects are created in (`projects create --personal` opts out) |
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

### Checking the CLI against a real backend

The offline suite proves the CLI *builds* the right request; it cannot prove a
deployed backend accepts it. `scripts/live_check.py` closes that gap by driving
the real binary through a dependency-ordered lifecycle against a disposable
"canary" project it creates and then deletes.

```bash
python scripts/live_check.py --plan          # print the plan + coverage ledger, run nothing
python scripts/live_check.py                 # staging (default)
python scripts/live_check.py --include-run    # also queue a real solver run (costs compute)
python scripts/live_check.py --env production --allow-prod
```

Every run ends with a ledger: which of the CLI's commands it covered, and which
it did not *with the reason*. Commands that cannot be driven against a live
backend at all — `login` needs a code from a real mailbox, `org invite` emails a
real person, the `datasources` group needs a reachable customer database — are
classified in `TIERS` with a stated reason, so an untested command is a recorded
decision rather than an oversight. Adding a CLI command without classifying it
fails `tests/test_live_check.py`.

Authentication is non-interactive: the harness copies your existing session for
the target environment into a throwaway `HARUMI_HOME`, or mints one from
`HARUMI_LIVE_REFRESH_TOKEN` / `HARUMI_LIVE_ACCESS_TOKEN` (prefer the refresh
token in CI — a stale access token self-heals). It works in a copy on purpose:
any request may refresh and rewrite `credentials.json`, and a harness run should
not rotate your own session.

There is also a thin pytest entry point, deselected by default:

```bash
pytest -m live
```

Staging requires the VPN. Production needs the second `--allow-prod` opt-in
because the run creates real rows in a real workspace.
