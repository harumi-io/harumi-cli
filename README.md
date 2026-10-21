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

# 2. Pick your onboarding journey — nothing yet, an existing folder, or an
#    existing Harumi project — and run its command (or let `harumi start`
#    ask and dispatch for you)
harumi new "My Project"          # nothing yet: creates + clones a fresh project
# harumi import                  # or: turn the current folder into a new project
# harumi clone <PROJECT_ID>      # or: fetch a project that already exists

# 3. See available kernel sizes (CPU/RAM, Gurobi vs plain Python)
harumi specs

# 4. Run the bound directory's code on the infra
harumi run --watch
harumi runs download -o ./out

# 5. Inspect runs later
harumi runs list
harumi runs get <RUN_ID>

# Compare 2-4 runs (the first is the baseline): KPIs, winner, same inputs?
harumi compare                      # pick the runs from a list
harumi compare <RUN_ID> <RUN_ID>

# Input files (land in inputs/ for every run; 500 files / 500 MB per project)
harumi files put data.csv
harumi files ls

# 6. Check the project's dashboard.toml renders the widgets you expect
harumi dashboard validate --latest
```

## Everything else the CLI can do

- `harumi new` / `harumi import` / `harumi clone` / `harumi link` / `harumi start` — the onboarding verbs; see Quick start above.
- Editing the project's repo is plain git in your clone (`harumi clone` sets up a `harumi` remote with your token embedded in its URL, so don't share `git remote -v` output or `.git/config`): `git push harumi <branch>`. `harumi check` reports what blocks the project from running; `harumi skills library|add` installs preset skills; `harumi agent-skills install` teaches Cursor/Claude Code/Codex this CLI.
- `harumi dashboard` — look up the `dashboard.toml` widget reference (`widgets`) and validate a project's dashboard, including its `output.json` dot-paths, before pushing (`validate`).
- `harumi share` — manage the project's public, unauthenticated dashboard links (several per project): add, enable/disable, rotate, and password-protect them.
- `harumi whats-new` — release notes for the installed version (`--all` for the history). harumi also tells you, at most once a day and only in an interactive terminal, when a newer version is on PyPI.
- `harumi blueprints` — list project blueprints to pass as `harumi new --blueprint`.
- `harumi datasources` — CRUD project database connections, test them, and run read-only SQL queries against them.
- `harumi schedules` — CRUD cron schedules that trigger git-ref runs.
- `harumi secrets` — CRUD project-scoped environment variables.
- `harumi memories` — list/add/edit/remove the project's memories. (Project skills are files in the repo under `skills/<slug>/SKILL.md`; run `harumi skills` for how to edit them.)
- `harumi org` — manage organizations and their members, by name or email.
- `harumi profile` — view/update your account profile.

In scripts and CI, read commands take `--json` (one JSON document on stdout, nothing else); `harumi run --watch --json` gives the result as data and exits 1 if the run fails. See [Scripts and CI](.agents/skills/harumi-cli/references/commands.md#scripts-and-ci---json).

Run `harumi --help` or any subcommand with `--help` for the full flag reference, or see the [command reference](.agents/skills/harumi-cli/references/commands.md) for endpoint-level detail.

## Execution model

Every run is git-ref based: code lives in the project's Harumi Git (Gitea) repo. If your working tree is dirty or has unpushed commits when you run `harumi run`, the CLI transparently pushes a throwaway scratch branch so you can iterate without committing manually — your real branches are never touched. Pass `--branch`/`--commit` to run a specific ref instead.

## Configuration & environments

*Most people can skip this section: `harumi` uses production, and you are never asked to choose. It is for Harumi staff and for developers pointing the CLI at their own API.*

The CLI targets one of two environments (each with its own Supabase, so each has its own login):

| Env | API | Gitea | Access |
|---|---|---|---|
| `production` (default) | `https://api.harumi.io/api` | `https://git.harumi.io` | public |
| `staging` | `https://api.dev.harumi.io/api` | `https://git.dev.harumi.io` | internal, VPN-only |

Clients only ever use `production`, and are never asked about it. Harumi staff (a `@harumi.io` address) are asked which environment to use when they run `harumi login`, and that choice becomes their default. Run `harumi login` again and pick the other one to switch: if you are already logged in there with the same email, no new code is sent.

```bash
harumi login               # staff: pick production or staging; remembered
harumi --env staging run   # run one command against another environment (not saved)
```

Selection precedence: `--env` > `HARUMI_ENV` > the choice made at `harumi login` > `production`. Within an environment you can still override endpoints for local development:

| Env var | Purpose |
|---|---|
| `HARUMI_API_URL` | Override `harumi-api` base URL (e.g. `http://localhost:8000/api`) |
| `HARUMI_GIT_URL` | Override the Harumi Git (Gitea) base URL |
| `HARUMI_ORG` | Organization ID sent as `X-Organization`, and the workspace new projects are created in (`--personal` on `new`/`import` opts out) |
| `HARUMI_PLATFORM_URL` | Override the web-app URL used in printed project links |
| `HARUMI_NO_INTERACTIVE` | Set to `1` to never show an arrow-key menu (menus are also off under `CI`, with `TERM=dumb`, and whenever stdin or stdout isn't a terminal) |
| `HARUMI_NO_UPDATE_CHECK` | Set to `1` to turn off the "a newer version is available" notice (it is also off under `CI` and whenever stderr isn't a terminal) |
| `HARUMI_STATUS_URL` | Override the status page used by `harumi status` and the unreachable-API hint (the hint is skipped when the API URL is overridden, unless this is set) |

Credentials (JWT + refresh token + Gitea token) are stored per-environment under `~/.harumi/environments/<env>/credentials.json` (mode `0600`) after `harumi login`. The file is written atomically, and a session refresh keeps everything else in it (the git token included). Several `harumi` commands running at once take turns on a lock (`credentials.json.lock` beside it), so they share one refresh instead of logging each other out. An older flat `~/.harumi/credentials.json` is migrated into `production` automatically on first run.

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
