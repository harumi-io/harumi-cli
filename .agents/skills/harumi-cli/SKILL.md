---
name: harumi-cli
description: Guide for using the `harumi` CLI to run local optimization/solver code (Gurobi, OR-Tools, plain Python) on Harumi's infrastructure via the project's self-hosted Gitea repo, to select the backend environment (production vs internal staging), create/manage projects, import a downloaded project export as a new project, browse and edit the project's git repo, inspect and cancel runs, manage datasources, schedules, secrets, organizations, and your profile. Use when the user wants to run, push, or debug a local script against a Harumi project, mentions `harumi init`, `harumi import`, `harumi run`, `harumi runs`, `harumi repo`, `harumi env`, `harumi projects`, `harumi datasources`, `harumi schedules`, `harumi secrets`, `harumi org`, `harumi profile`, asks about switching between production and staging, creating a new Harumi project, importing/uploading a downloaded project zip to the CLI, Harumi kernel specs, Gitea remotes, scratch branches, database connections, running SQL queries against a project datasource, scheduling/cron runs, managing environment variables/secrets, organization members, or needs to fetch/download results or files from a Harumi run/repo.
---

# Harumi CLI

Drives the `harumi` CLI (source: `harumi-cli`, package `harumi`). Every run is git-ref based: code lives in a per-project Harumi Git (Gitea) repo. The CLI auto-manages a scratch branch so users can iterate without manually committing.

The CLI targets one of two environments (see [Config & environments](#config--environments)): `production` (default) and `staging` (internal, VPN-only, `git.dev.harumi.io`). Each has its own Supabase, so each has its own login. The git-first `run`/`repo` flow currently only works on `staging` — production Gitea (`git.harumi.io`) is not live yet, so on production only the non-git commands (`projects`/`datasources`/`secrets`/`org`/`profile`) work.

For the full flag-by-flag reference and troubleshooting table, see [references/commands.md](references/commands.md).

## VPN requirement

The staging endpoints (`api.dev.harumi.io`, `git.dev.harumi.io`) are internal ALBs — anything on the `staging` environment (login, `harumi init`, git push, runs) only works over VPN. Surface a clear network error if the user isn't on VPN.

## Preflight

1. Check CLI is installed: `harumi --version`. Install with `pip install harumi` (or `pip install -e .` from this repo for an unreleased fix).
2. Check the user is authenticated. Any command raises `Not logged in. Run harumi login first.` if not. **Never try to automate the OTP flow** — it emails a one-time code and needs interactive input. Ask the user to run it:
   - `harumi login` for an existing account.
   - `harumi login --signup` for a brand-new email (plain login 422s with "Signups not allowed for otp" on new emails).
3. `harumi login` also provisions a per-user Gitea token via `POST /api/git/credentials`. The token is stored in `~/.harumi/credentials.json`.

## The always-bound-repo invariant

Every `harumi run` (and every command that accepts `--project`) requires either an explicit `--project <ID>`, or the working directory (or a parent) to be bound to a Harumi project via `harumi init`. Without either, the command exits with a clear "provide --project or run `harumi init`" error.

## Workflow

### 1. Bind a directory to a project

Run once per project directory:

```bash
harumi init --project <PROJECT_ID>
```

This fetches the Gitea repo metadata (`GET /projects/{id}/repo`), writes `.harumi/config.json`, and configures the `harumi` git remote for authenticated HTTPS pushes.

Find project IDs with: `harumi projects list` (or `harumi notebooks` for the legacy notebook-centric view).

**Creating a brand-new project instead of binding to an existing one:**

```bash
harumi projects create "My Project" [--customer-id ID] [--template-id ID]
```

Calls `POST /projects`, then fetches its repo and binds the current directory the same way `harumi init` does (pass `--no-bind` to skip that). If the backend hasn't provisioned a Gitea repo for the project, the CLI still creates the bare project and prints a warning instead of failing.

**Importing a downloaded project export (e.g. from the web app's "Download project" button) as a new project:**

```bash
harumi import [PATH] [--project-name NAME] [--from-git URL]
```

`PATH` must be an **unzipped folder** (default: current directory) — unzip the export first. Creates a project, then pushes the whole folder as the repo's initial commit and binds the directory, same as `projects create` above. `--from-git URL` additionally clones an old GitHub repo flat into the folder before pushing (exported files win on any filename collision). See [references/commands.md#import](references/commands.md#import) for the full flag/behavior breakdown.

### 2. Run code

**Default — scratch branch (for uncommitted/unpushed work):**

```bash
harumi run
```

The CLI detects a dirty or unpushed tree, transparently pushes a throwaway branch (`harumi-scratch/<user>/<timestamp>`) to Gitea, queues the run against that ref, and cleans up the scratch branch when done. The user's real branches are never touched.

If the tree is clean and fully pushed, it runs the current branch directly — no scratch branch needed.

**Run a specific branch or commit:**

```bash
harumi run --branch feature/solver-v2
harumi run --commit abc123f
```

**Override the `harumi.toml` command or kernel:**

```bash
harumi run --command "python solver.py" --kernel gurobi_python_medium
```

**Block until done and download output artifacts:**

```bash
harumi run --watch --output-dir ./out
```

### 3. Inspect and manage runs

```bash
harumi runs list                          # table of runs for the bound project, newest first
harumi runs get <RUN_ID>                  # status, git ref, exit code, stdout/stderr/error
harumi runs cancel <RUN_ID>               # cancel an in-flight run
```

`harumi outputs` still works as a thin backwards-compatible wrapper (`--latest`, `--download <RUN_ID>`), but prefer `harumi runs` for new usage.

## Manage the project's repo directly

`harumi repo` reads and writes the project's Gitea repo through harumi-api's git router — no local git clone required for file-level edits. Every write lands in a single commit via the batch changes endpoint.

```bash
harumi repo ls [--ref BRANCH]                          # list every file (flat, recursive)
harumi repo cat <path> [--ref BRANCH] [--output FILE]  # print or save a file's content
harumi repo put <local_file> <repo_path> [-m MSG] [--branch B]   # create/update as one commit
harumi repo rm <path> [-m MSG] [--branch B]            # delete a file or folder, one commit
harumi repo mv <from> <to> [-m MSG] [--branch B]       # rename/move, one commit
harumi repo download -o out.zip [--path DIR] [--ref REF]  # download repo/folder as a zip
harumi repo branches                                    # list versions; live branch flagged
harumi repo branch-create <name> [--from BRANCH]
harumi repo branch-rm <name>
harumi repo promote <name> [--title T] [--delete-after]  # merge a version into live
```

All `repo` subcommands accept `--project` to override the `.harumi` binding.

## Manage datasources

`harumi datasources` manages project-scoped database connections. Credentials are **always prompted interactively with hidden input** — the CLI never accepts them as a flag, and the server never returns them back (stored in AWS SSM).

```bash
harumi datasources list                                   # table of datasources for the bound project
harumi datasources get <name>                              # detail view (no credentials)
harumi datasources add <name> --type postgresql --host ... --port 5432 --database ... --username ...
harumi datasources update <name> --host newhost --set-credentials
harumi datasources remove <name>
harumi datasources test --type postgresql --host ... --port 5432 --database ... --username ...
harumi datasources query <name> --sql "SELECT * FROM orders LIMIT 10"
```

`query` is the most useful command for iteration: it runs SQL against the real datasource through a server-side proxy that **only allows `SELECT`/`WITH`** (any destructive keyword is rejected with a 403) and **caps rows** (default limit 10000, server max 100000). Use it to validate a query before hardcoding it into solver code. Add `--csv <path>` to save results instead of printing a table.

All `datasources` subcommands accept `--project` to override the `.harumi` binding.

## Schedule runs

`harumi schedules` manages project-scoped cron schedules for git-ref runs (`/projects/{project_id}/schedules`).

```bash
harumi schedules list                                       # table of schedules for the bound project
harumi schedules get <SCHEDULE_ID>                           # detail view
harumi schedules add --cron "0 9 * * *" --git-branch main [--start-at ISO] [--kernel ...] [--email-to ...]
harumi schedules update <SCHEDULE_ID> --cron "0 */6 * * *"
harumi schedules remove <SCHEDULE_ID>
```

Key semantics:

- **Cron is a raw 5-field expression, interpreted in UTC.** The CLI does not validate it client-side — the server validates with `croniter` and returns a clear 400 on a bad expression. Don't try to build a calendar/human-friendly UI; just pass the cron string.
- **No pause/enable flag exists.** The only way to stop a schedule from firing is `harumi schedules remove`.
- **No separate "run now" for a schedule.** Immediate execution is already covered by `harumi run` (git-ref based); schedules only control recurring runs.

All `schedules` subcommands accept `--project` to override the `.harumi` binding.

## Manage secrets

`harumi secrets` manages project-scoped environment variables, injected into kernels/apps. Values are stored as SSM SecureStrings; there is no update endpoint — `set` on an existing name overwrites it.

```bash
harumi secrets list                 # names only, never values
harumi secrets set <NAME>           # prompts for the value (hidden input)
harumi secrets rm <NAME>
```

## Manage organizations and your profile

```bash
harumi org list
harumi org create <BUSINESS_NAME>
harumi org rename <ORG_ID> <NEW_NAME>
harumi org delete <ORG_ID>
harumi org members <ORG_ID>
harumi org invite <ORG_ID> --email a@b.com --role member
harumi org role <ORG_ID> <USER_ID> --role admin
harumi org remove <ORG_ID> <USER_ID>

harumi profile show
harumi profile set --first-name Ana --bio "..."
```

## Config & environments

There are two built-in environments, selectable in the CLI:

| Env | API | Gitea | Access |
|---|---|---|---|
| `production` (default) | `https://api.harumi.io/api` | `https://git.harumi.io` | public |
| `staging` | `https://api.dev.harumi.io/api` | `https://git.dev.harumi.io` | internal, VPN-only |

Auth flows through harumi-api (`/users/otp`, `/users/refresh`), and each environment has its own Supabase — so **each environment has its own stored session**. You must `harumi login` once per environment; switching does not log you out of the other.

```bash
harumi env list            # production only (staging is hidden by default)
harumi env list --all      # include internal/VPN-only envs (or set HARUMI_INTERNAL=1)
harumi env current         # show the active env + endpoints
harumi env use staging     # persist the default (internal devs, VPN required)
harumi --env staging run   # override for a single command
```

Staging is **internal-only**: it's hidden from `env list`/help for regular users, but the real gate is needing a staging Supabase account and the VPN — anyone internal can `harumi env use staging` (or pass `--env staging`).

Environment selection precedence: `--env` > `HARUMI_ENV` > `harumi env use` (saved default) > `production`.

- Per-command URL overrides: `--api-url` / `HARUMI_API_URL`, `--git-url` / `HARUMI_GIT_URL` (e.g. for a local harumi-api). These override the active environment's endpoints without changing which env you're on.
- Org: `harumi config set-org <ORG_ID>` / `--org` / `HARUMI_ORG` (scoped per environment).
- Stored under `~/.harumi/`: global `config.json` (just the selected environment) + per-env `environments/<env>/{credentials,config}.json`. Override the home dir with `HARUMI_HOME`.
