---
name: harumi-cli
description: Guide for using the `harumi` CLI to run local optimization/solver code (Gurobi, OR-Tools, plain Python) on Harumi's infrastructure from the terminal instead of the platform's notebook editor. Use when the user wants to run, upload, or debug a local script against a Harumi notebook, mentions the `harumi` command, `harumi run`/`harumi notebooks`/`harumi specs`/`harumi outputs`, asks about Harumi kernel specs, or needs to fetch/download results from a Harumi run.
---

# Harumi CLI

Drives the `harumi` CLI (source: `harumi-dev-cli`, package `harumi`, entry point `harumi = "harumi.cli:app"`). It uploads/streams local code to Harumi's sandboxes and fetches results — same backend the web app and AI agent use.

For the full flag-by-flag reference and troubleshooting table, see [references/commands.md](references/commands.md).

## Preflight

1. Check the CLI is installed: `harumi --version`. If missing, install from this repo with `pip install -e .` (or `pip install harumi` once published).
2. Check the user is authenticated. Any command will raise `Not logged in. Run harumi login first.` if not. **Never try to run `harumi login` for the user or answer the OTP prompt yourself** — it emails a one-time code and needs interactive input. Ask the user to run it themselves:
   - `harumi login` for an existing account.
   - `harumi login --signup` for a brand-new email (plain `harumi login` on a new email 422s with "Signups not allowed for otp").
3. If they belong to multiple orgs, `harumi login` prints a table and asks them to run `harumi config set-org <ORG_ID>` (or pass `--org` per command).

## Discover before running

Never guess a `--notebook` id or `--kernel` name — always look them up first:

- `harumi notebooks` — lists projects and their notebooks; grab a `notebook_id` from the table.
- `harumi notebooks --project <id>` — scope to one project.
- `harumi specs` — lists available kernel specs (e.g. `or_python_small`, `gurobi_python_medium`) with CPU/RAM and whether a subscription is required.

## Pick the right execution mode

This is the most important — and most easily misused — decision. `harumi run <path>` has two modes that behave very differently:

| | `--mode interactive` | `--mode job` (default) |
|---|---|---|
| Runs | The **actual local file's code**, sent as-is to the notebook's live sandbox kernel | The notebook's **own saved live version** in the Harumi web app — NOT the local file's code |
| Path | Must be a single `.py` file | File or directory |
| What happens to `path` | Sent inline, not uploaded | Uploaded to the notebook's project first (so imports/data files stay in sync) |
| Output | Streams stdout/stderr/results live | Queues an async job; poll or `--watch` |
| Best for | Fast iteration, "run my file as written" | Long/heavy runs; runs that must match the notebook's configured live version |

If the user says "run my script"/"run this file", default to `--mode interactive`. Only use `--mode job` when they want the notebook's saved live version executed, or the run is long enough to need the async queue (and mention that job mode won't execute arbitrary local code — the file must also be saved as the notebook's live version in the web app for its logic to actually run).

## Run

Interactive:

```bash
harumi run ./solver.py --notebook <NOTEBOOK_ID> --mode interactive --kernel or_python_small
```

Job, fire-and-forget:

```bash
harumi run ./project_dir --notebook <NOTEBOOK_ID> --kernel gurobi_python_medium
```

Job, blocking until done and downloading artifacts:

```bash
harumi run ./project_dir --notebook <NOTEBOOK_ID> --watch --output-dir ./out
```

## Retrieve outputs later

```bash
harumi outputs --notebook <NOTEBOOK_ID> --latest
harumi outputs --notebook <NOTEBOOK_ID> --download <OUTPUT_ID> --output-dir ./out
```

## Local backend / org config

- Point at a local `harumi-api`: `--api-url http://localhost:8000/api` or `export HARUMI_API_URL=http://localhost:8000/api`.
- Precedence everywhere: CLI flags > env vars (`HARUMI_API_URL`, `HARUMI_ORG`) > `~/.harumi/config.json` > defaults.

## Try it risk-free with the bundled demos

If the user just wants to smoke-test the CLI, point them at `demos/` in this repo (see `demos/README.md`):

- `demos/hello_world.py` — zero-dependency smoke test, any kernel.
- `demos/or_tools_lp.py` — OR-Tools LP, kernel `or_python_small`.
- `demos/gurobi_knapsack.py` — Gurobi MIP, kernel `gurobi_python_medium`.
- `demos/project_demo/` — multi-file directory (tests directory uploads in job mode).
