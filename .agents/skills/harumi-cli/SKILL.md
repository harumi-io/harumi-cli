---
name: harumi-cli
description: Guide for using the `harumi` CLI to run local optimization/solver code (Gurobi, OR-Tools, plain Python) on Harumi's infrastructure via the project's self-hosted Gitea repo. Use when the user wants to run, push, or debug a local script against a Harumi project, mentions `harumi init`, `harumi run`, `harumi notebooks`, `harumi specs`, `harumi outputs`, asks about Harumi kernel specs, Gitea remotes, scratch branches, or needs to fetch/download results from a Harumi run.
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

## Config

- Gitea URL: `--git-url` or `HARUMI_GIT_URL` (default `https://git.dev.harumi.io`).
- API URL: `--api-url` or `HARUMI_API_URL` (default `https://api.harumi.io/api`).
- Org: `harumi config set-org <ORG_ID>` / `--org` / `HARUMI_ORG`.
- All settings: flags > env vars > `~/.harumi/config.json` > defaults.
