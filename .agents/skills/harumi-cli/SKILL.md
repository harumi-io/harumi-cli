---
name: harumi-cli
description: Guide for using the `harumi` CLI to run local optimization/solver code (Gurobi, OR-Tools, plain Python) on Harumi's infrastructure via the project's self-hosted Gitea repo, and to manage project datasources (database connections). Use when the user wants to run, push, or debug a local script against a Harumi project, mentions `harumi init`, `harumi run`, `harumi notebooks`, `harumi specs`, `harumi outputs`, `harumi datasources`, asks about Harumi kernel specs, Gitea remotes, scratch branches, database connections, running SQL queries against a project datasource, or needs to fetch/download results from a Harumi run.
---

# Harumi CLI

Drives the `harumi` CLI (source: `harumi-dev-cli`, package `harumi`). Every run is git-ref based: code lives in a per-project Harumi Git (Gitea) repo at `https://git.dev.harumi.io` (staging, VPN-only). The CLI auto-manages a scratch branch so users can iterate without manually committing.

For the full flag-by-flag reference and troubleshooting table, see [references/commands.md](references/commands.md).

## VPN requirement

`git.dev.harumi.io` is an internal ALB — git push and `harumi init` only work over VPN. Surface a clear network error if the user isn't on VPN.

## Preflight

1. Check CLI is installed: `harumi --version`. Install from this repo: `pip install -e .` (or `pip install harumi` once published).
2. Check the user is authenticated. Any command raises `Not logged in. Run harumi login first.` if not. **Never try to automate the OTP flow** — it emails a one-time code and needs interactive input. Ask the user to run it:
   - `harumi login` for an existing account.
   - `harumi login --signup` for a brand-new email (plain login 422s with "Signups not allowed for otp" on new emails).
3. `harumi login` also provisions a per-user Gitea token (best-effort — prints a notice if the backend isn't ready yet). The token is stored in `~/.harumi/credentials.json`.

## The always-bound-repo invariant

Every `harumi run` requires the working directory (or a parent) to be bound to a Harumi project via `harumi init`. This is the single prerequisite — without it, `run` exits immediately with "run `harumi init` first."

## Workflow

### 1. Bind a directory to a project

Run once per project directory:

```bash
harumi init --project <PROJECT_ID>
```

This fetches the Gitea repo metadata from harumi-api, writes `.harumi/config.json`, and configures the `harumi` git remote for authenticated HTTPS pushes.

Find project IDs with: `harumi notebooks`

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

### 3. Check outputs

```bash
harumi outputs --latest
harumi outputs --download <OUTPUT_ID> --output-dir ./out
```

`--project` is optional if run from a bound directory.

## Manage datasources

`harumi datasources` manages project-scoped database connections against real, live endpoints (no assumed contract here — unlike `run`/`init`). Credentials are **always prompted interactively with hidden input** — the CLI never accepts them as a flag, and the server never returns them back (stored in AWS SSM).

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

## Not yet supported: scheduled runs

Harumi supports cron-based scheduled runs, but they are still keyed by **notebook id** in harumi-api (`/notebooks/{notebook_id}/schedules`), which conflicts with this CLI's project + git-ref model. This CLI intentionally does not manage schedules yet — wait until the backend re-keys schedules to `project_id`/`branch` (part of the git-first pivot) before adding CLI support.

## Config

- Gitea URL: `--git-url` or `HARUMI_GIT_URL` (default `https://git.dev.harumi.io`).
- API URL: `--api-url` or `HARUMI_API_URL` (default `https://api.harumi.io/api`).
- Org: `harumi config set-org <ORG_ID>` / `--org` / `HARUMI_ORG`.
- All settings: flags > env vars > `~/.harumi/config.json` > defaults.
