# harumi CLI — Command Reference

Detailed flag reference, config/credential storage, troubleshooting, and the Python `Client` SDK. Loaded on demand from [SKILL.md](../SKILL.md).

`harumi help` is `harumi --help`, and `harumi help <command>` is `harumi <command> --help` (it is not listed in the command list). Running `harumi` with no arguments opens the quick start (a short menu of what to do next).

## Contents

- [Auth commands](#auth-commands)
- [Environments](#environments)
- [profile](#profile)
- [usage](#usage)
- [specs](#specs)
- [examples](#examples)
- [projects](#projects)
- [new](#new)
- [import](#import)
- [clone](#clone)
- [link](#link)
- [start](#start)
- [run](#run)
- [runs](#runs)
- [tags](#tags)
- [Editing the repo (git)](#editing-the-repo-git)
- [check](#check)
- [files](#files)
- [dashboard](#dashboard)
- [share](#share)
- [datasources](#datasources)
- [schedules](#schedules)
- [secrets](#secrets)
- [memories and skills](#memories-and-skills)
- [agent-skills](#agent-skills)
- [org](#org)
- [Scripts and CI (`--json`)](#scripts-and-ci---json)
- [Confirmations (`--yes`)](#confirmations---yes)
- [Command grammar](#command-grammar)
- [Config & credential files](#config--credential-files)
- [Troubleshooting](#troubleshooting)
- [Python library (`Client`) alternative](#python-library-client-alternative)

## Auth commands

### `harumi login`

```
harumi login [--email EMAIL] [--signup] [--api-url URL] [--git-url URL]
```

Logs into the **active environment** (see [Environments](#environments)). Each environment has its own Supabase and its own stored session.

- **Harumi staff** (a `@harumi.io` email) at a terminal are asked, after typing their email, whether to log in to `production` or `staging`; the choice is saved as their default once the login succeeds. Everyone else is never asked. `harumi --env staging login` (or `HARUMI_ENV`) skips the question and is not saved.
- Already logged in to that environment with the same email? No code is sent: `login` just switches (and re-provisions the Gitea token). Run `harumi logout` first to force a fresh code.

- Prompts for email if `--email` omitted, then prompts for the OTP code emailed by Supabase. Spaces in a pasted code are ignored. At a terminal a code the server refuses can be retyped (three tries); with no terminal it fails once, so piped input is never consumed by retries. A refused code is reported as "That code was not accepted" (never as an HTTP status); a 429 says to wait a minute and stops asking. An email with no account says so and offers `--signup`.
- `--signup`: creates the Supabase account first. Required the *first* time a new email logs in.
- On success, stores `access_token`/`refresh_token` at `~/.harumi/credentials.json` (mode `0600`, written atomically; a new login replaces the whole file, so another account's git token never survives), then calls `POST /git/credentials` to provision a per-user Gitea personal access token (`git_token` + `git_url`), used by `harumi new`/`import`/`clone`/`link` and `harumi run` for git-over-HTTPS.
- Best-effort workspace resolution (skipped when `HARUMI_ORG` is set). No organizations: the personal workspace, automatically. A saved org you still belong to: kept. Otherwise, at a terminal, a menu of your organizations plus "Personal workspace" (the choice is saved). With no terminal it never prompts: exactly one org is stored, several mean the personal workspace plus a printed table and the `harumi org use` hint.
- At a terminal, a successful login ends with the quick-start menu (new project / import / clone, or run inside a project folder); scripts and agents get no menu.

### `harumi logout`

Clears `~/.harumi/credentials.json`. No flags. Says whether anything was cleared (`You were not logged in; there was nothing to clear.` otherwise). Both lines add `on staging` when the active environment is not production.

### `harumi whoami`

```
harumi whoami [--api-url URL] [--org ORG]
```

Prints the email and id of the logged-in user (`GET /users/profile`), the environment only when it is not production (`env: staging`; no `env:` means production), and — when set — the profile's name and bio and the active organization (`--org` or the one from `org use`). Replaces `profile show`.

## `whats-new` and update notices

```
harumi whats-new [--all]
```

Prints the bundled release notes for the installed version (`--all`: every release, newest first). It works offline; the notes ship inside the package.

Separately, real invocations may print a one-line notice on **stderr** after the command: a newer version exists on PyPI (with the upgrade command for pipx / uv / pip), or "harumi updated X -> Y, see `harumi whats-new`" once after an upgrade. PyPI is asked at most once a day, in the background with a 2 s timeout, and the same notice repeats at most once a day (a newer release is announced immediately). Nothing is printed unless stderr is a terminal, under `CI`, with `HARUMI_NO_UPDATE_CHECK=1`, or for `--version`. State: `~/.harumi/update-check.json`.

## Environments

There is no `env` command (a hidden stub points at `harumi login`). Two environments are built in:

| Env | API | Gitea | Access |
|---|---|---|---|
| `production` (default) | `https://api.harumi.io/api` | `https://git.harumi.io` | public |
| `staging` | `https://api.dev.harumi.io/api` | `https://git.dev.harumi.io` | internal, VPN-only |

- **Choosing one:** Harumi staff pick at `harumi login` (see above); the choice is saved in `~/.harumi/config.json`. To switch, run `harumi login` and pick the other one.
- **One command elsewhere:** `harumi --env staging <command>` (hidden flag) or `HARUMI_ENV=staging`. Not saved. This is what agents and scripts should use, since they are never shown a menu.

Selection precedence: `--env` (top-level flag) > `HARUMI_ENV` > the choice made at `harumi login` > `production`.

**Each environment has its own Supabase, so each has its own stored session** (`~/.harumi/environments/<env>/credentials.json`). Switching never logs you out of the other. `--api-url`/`--git-url` (and `HARUMI_API_URL`/`HARUMI_GIT_URL`) still override the active environment's endpoints for local development without changing which environment you're on.

Staging is only offered to `@harumi.io` emails as a convenience; the real access gate is an account in staging's Supabase plus the VPN.

## `profile`

```
harumi profile set [--first-name N] [--last-name N] [--bio TEXT] [--api-url URL] [--org ORG]
```

`set` sends only the flags you pass as a partial update (`POST /users/profile`); errors locally with "No fields to update" if none are given, and with an empty `--first-name` (the profile requires one). `--last-name ""` and `--bio ""` clear those fields.

## `usage`

```
harumi usage [--org ORG] [--json] [--api-url URL]
```

`--json` prints `{workspace, active, plan_code, tier, weekly_used_pct, monthly_used_pct, weekly_resets_at, monthly_resets_at, overage_*, ...}`; with no plan it is `{workspace, active: false, reason}`. Shows the plan, the weekly and monthly allowance as a percentage used (with when each resets), and overage. `--org` shows an organization's pooled allowance instead of your personal one. States it can print: `no active plan` with the reason and what to do; `Harumi team, unlimited`; `no limit` for a window the plan does not cap; `Overage: not available on this plan`, `off`, `on` (dollars spent of the cap) or `paused` (an invoice is unpaid). When a limit is reached it ends with the billing link, and offers overage only if the plan has it. A plan the CLI has no label for is shown as words (`trial` becomes `Trial`). It exits 1 with the usual platform-down message when the API fails. This is the same allowance a `402` from `run` or chat is about.

## `specs`

```
harumi specs [--json] [--api-url URL] [--org ORG]
```

`--json` is an array of `{name, display_name, language, description, cpu, memory, gpu, subscription_required}`. Lists kernel specs from `GET /sandbox/specs` as a table of `name`, `cpu`, `memory`, `gpu` (yes/no), `plan` (`any` = every plan includes it, `paid` = it needs a subscription) and `label`. `name` is what you pass to `run --kernel` and `schedules add/update --kernel`; it is never shortened, whatever the terminal width. It ends with a line saying how to use one. An empty list prints `No kernels are available to this workspace.`

## `examples`

```
harumi examples [--json] [--api-url URL] [--org ORG]
```

`--json` is an array of those three fields. Lists example projects (`slug`, `name`, `description`): finished Harumi projects you can start a copy of. Pass an example's `slug` as `new --example`. They were called blueprints until 1.0.0 and the API still calls them that (`GET /blueprints`), so that is what is asked for. `harumi blueprints` and `new --blueprint` still work, hidden, with a `deprecated:` line on stderr, and are removed in 1.1.0.

## `projects`

```
harumi projects list [--limit N] [--api-url URL] [--org ORG]
harumi projects get [PROJECT_ID] [--api-url URL] [--org ORG]
harumi projects rename [PROJECT_ID] [NAME] [--api-url URL] [--org ORG]
harumi projects rm [PROJECT_ID] [--yes] [--api-url URL] [--org ORG]
```

With the id omitted, `get`, `rename` and `rm` open the project picker (most recently updated first); `rename` also asks for the new name, offering the current one; `rm` still requires typing the project's name afterwards, because choosing a row is not consent to delete it. With no terminal they fail listing the choices. Creating a project is `harumi new` (`--no-clone` to only create it; see [new](#new)).

- **`list`**: the **id is always whole** (it goes to `get`, `clone`, `link` and `--project`). A table needs about 87 columns for that, so on a narrower terminal each project is two lines instead, `<id>  <name>` then `updated: ... kernel_spec: ... role: ...`; `org list` does the same. Nothing is cut at any width, and `--json` is the form for a script. `GET /projects`, following pages (200 at a time) until the server's `total_count` is reached, so accounts with hundreds of projects see all of them. Sorted **most recently updated first** (the server's own order is creation date; the CLI re-sorts). Table of `id`, `name`, `updated` ("3 days ago"), `workspace` (only when the projects span more than one workspace), `kernel_spec`, `role`. `--limit N` shows only the first N; any time rows are left out it says "Showing N of M projects". The project pickers (`clone` and others) use the same order and show age and workspace beside each name.
- **`get`**: `GET /projects/{id}` → detail view.
- **`rename`**: `PUT /projects/{id}` with `{name}`.
- **`rm`**: `DELETE /projects/{id}`, which **moves the project to the Trash; it is not erased**. It stops at once (schedules cancelled, a deployed app shut down) and its share links are removed for good: restoring does not bring them back. It can be restored for **7 days** (harumi-api's `project_trash_retention_days`, default 7), after which a daily sweep deletes it permanently. **The CLI has no restore command**: restore from the Trash page in the web app (or `POST /projects/{id}/restore`). So when asked to undo a `projects rm`, do not say it is gone; point at the Trash and the 7-day window. It shows what will stop, then prompts you to type the exact project name unless `--yes`. With `--yes` the project id is required (no picker), so a slip in a list cannot trash a project. Contrast `org rm`, which really is permanent.

## Onboarding: `new` / `import` / `clone` / `link` / `start`

Three journeys, three commands — plus the narrower `link` primitive and the interactive `start` entry point. All of `new`/`import`/`clone` leave the target directory bound (`.harumi/config.json` written, `harumi` git remote configured) and ready for `harumi run`.

### `new`

```
harumi new [NAME] [--dir PATH] [--customer-id ID] [--personal] [--example SLUG] [--no-clone]
          [--api-url URL] [--git-url URL] [--org ORG]
```

Use when there's nothing yet. With `NAME` omitted it asks for one at a terminal; with no terminal it fails showing `harumi new "My Project"`. When it finishes it prints the exact next step (`Next: cd my-project then harumi run --watch`, with a relative path when that is shorter).

1. `POST /projects` to create the project (seeds a runnable `harumi.toml` + `main.py` + `dashboard.toml` server-side, or a copy of an example if `--example` is given — see `harumi examples`; a slug that is not in the list is refused before anything is created, with the nearest one suggested).
2. `GET /projects/{id}/repo` to fetch the Gitea repo.
3. Clones that repo into `--dir` (default: `./<slugified-name>`, refuses to clone into an existing non-empty directory).
4. Binds the clone the same way `link` does (writes `.harumi/config.json`, configures the `harumi` git remote).

`--no-clone` stops after step 1: it only creates the project (no clone, no binding, so `--dir` is refused with it) and prints a `harumi clone <id>` hint. Use it for scripting. The project is created in the configured org (`org use` / `--org` / `HARUMI_ORG`) unless you pass `--customer-id` to pick a different one or `--personal` for your personal workspace; the command prints which workspace it landed in. Replaces `projects create`.

### `import`

```
harumi import [PATH] [--project-name NAME] [--from-git URL] [--bind/--no-bind] [--personal] [--yes] [--include-tracked-secrets]
           [--api-url URL] [--git-url URL] [--org ORG]
```

Use when you already have code on your machine — from anywhere: an old GitHub repo, a colleague's zip, a script you've been iterating on locally. `PATH` defaults to the current directory.

| Flag | Meaning |
|---|---|
| `PATH` | Folder to push (positional). Default: current directory. |
| `--project-name` | Name for the new project. Default: the folder's name. |
| `--from-git` | Also clone this git URL (e.g. an old GitHub repo) flat into the folder — `.git` stripped, files copied alongside the existing ones — before pushing. On a filename collision the existing (folder's own) file wins; the CLI warns and lists the first few colliding paths. |
| `--bind / --no-bind` | Bind the folder to the new project afterward. Default: `--bind`. |
| `--personal` | Create the project in your personal workspace, ignoring the configured org. |
| `--yes, -y` | Don't ask for a name or confirmation (use the folder name and go). Does **not** override the secret check below. |
| `--include-tracked-secrets` | Upload secret-looking files that git already tracks or has in its history. Without it, a non-interactive import (no terminal, or `--yes`) stops with exit 1 instead. |

**Before anything is created** `import` prints what it will upload (`Importing <folder>: 5 files, 2.9 MB`), then what it leaves out: virtual environments (`.venv`, `.env/`, `venv`), `node_modules`, `__pycache__` and similar, and files that look like secrets (`.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `id_rsa*` ... in any letter case), with a pointer to `harumi secrets set`. Templates such as `.env.example` are uploaded. The exclusions are written to the repo's local `.git/info/exclude` and stay there after the import (they are not removed if a later step fails, and an older block is not updated); the user's own `.gitignore` is never edited, and `.gitignore` rules are honoured. A secret-looking file that git **already tracks**, or that exists anywhere in the folder's **history** (even if deleted since), is uploaded, because the whole history is pushed as it is. At a terminal that gets a loud warning and the confirmation defaults to No; **without a terminal, or with `--yes`, the import refuses (exit 1) unless `--include-tracked-secrets` is given.** Detection is by file name only. Large files (50 MB and up) are called out. At a terminal it then asks for the project name (Enter accepts the folder name) and `Create the project and upload these files?`; without a terminal, or with `--yes`, it asks nothing (but see the secret refusal above). A folder that already has a `.harumi` binding is refused, because `import` always makes a *new* project: use `git push harumi <branch>` instead.

Sequence:

1. `POST /projects` to create the project (`name` = `--project-name` or the folder name), in the configured org unless `--personal` is passed.
2. If `--from-git` is set, shallow-clones that URL into a temp dir and copies its tree (minus `.git`) flat into the folder first.
3. If the folder has no `harumi.toml`, writes a minimal one (`command = "python main.py"`, `kernel = "or_python_small"`) — `import` force-pushes over the server-seeded scaffold, so without this the first `harumi run` afterward would fail with a missing manifest.
4. If the backend didn't provision a Gitea repo for the project (`project.repo is None`), prints a warning and stops — nothing is pushed, no binding happens.
5. Otherwise, requires a Gitea token from `harumi login` (prints a warning and stops if missing — never fails hard), then force-pushes the **entire folder** as one commit ("Push local project") to the new repo's default branch, overwriting the seeded scaffold.
6. Unless `--no-bind`, binds the folder the same way `link` does.

### `clone`

```
harumi clone PROJECT_ID [--dir PATH] [--api-url URL] [--git-url URL] [--org ORG]
```

Use for a project that already exists in Harumi — created via the web app, by a teammate, or in an earlier session.

1. `GET /projects/{id}/repo` and `GET /projects/{id}` to fetch the repo and project name.
2. Clones the repo into `--dir` (default: `./<slugified-project-name>`, refuses to clone into an existing non-empty directory).
3. Binds the clone the same way `link` does.

### `link`

```
harumi link [--project PROJECT_ID] [--api-url URL] [--git-url URL] [--org ORG]
```

The narrower primitive underneath `clone` — binds an **already-checked-out** directory to a project, without cloning or fetching anything. For the rare case where the code is already on disk by hand (or you're re-binding after moving the folder).

1. Calls `GET /projects/{id}/repo`. With `--project` omitted, a project picker opens at a terminal (with no terminal it fails listing the projects and naming `--project`).
2. Writes `.harumi/config.json` in the current directory with `project_id` + repo metadata.
3. Configures the `harumi` git remote with an authenticated HTTPS URL (requires a `git_token` in credentials from `harumi login` and the current directory to already be a git working tree — `link` warns and skips remote setup otherwise).

After binding (via `new`/`import`/`clone`/`link`), `harumi run`, `harumi runs`, `harumi check`, `harumi skills add`, `harumi datasources`, `harumi schedules`, and `harumi secrets` all work without any `--project` flag. `.harumi/config.json` is searched upward from cwd, so these commands work from subdirectories too.

### `start`

```
harumi start
```

Interactive entry point for a confused or new user. It checks that you are logged in before it asks anything. It asks "What do you have?" (nothing yet / a local folder / an existing Harumi project) and dispatches to `new`/`import`/`clone` accordingly. Equivalent to picking the right one yourself.

## `run`

```
harumi run [--branch B | --tag T] [--commit SHA] [--command C] [--kernel K]
           [--watch] [--json]
           [--api-url URL] [--git-url URL] [--org ORG]
```

Requires the directory (or a parent) to be bound — via `harumi new`, `harumi import`, `harumi clone`, or `harumi link`.

| Flag | Meaning |
|---|---|
| `--branch, -b` | Run a specific branch. Default: current branch (or scratch branch if dirty/unpushed). |
| `--commit` | Run a specific commit SHA. |
| `--tag, -t` | Run the commit a tag points at (`harumi tags list`). Cannot be combined with `--branch` or `--commit`. Nothing local is pushed: the tag is a label on the server. |
| `--command, -c` | Override the command in `harumi.toml`. |
| `--kernel, -k` | Override the kernel spec (e.g. `or_python_small`, `gurobi_python_medium`). |
| `--watch, -w` | Block until the run reaches a terminal status. |
| `--json` | Print the result as one JSON document on stdout; progress and the failure summary go to stderr. See [Scripts and CI](#scripts-and-ci---json). |

`run` prints `Running project <id> on branch <ref>` (or `on tag <name>`) and then `Queued run <run-id>` (the run id is the UUID on that line; the internal execution-log id is no longer printed). With `--watch`: at a terminal one live line shows the status and the elapsed time; piped or logged it prints one line each time the status *changes*. Ctrl-C stops watching, never the run: it exits 130 and prints how to follow or cancel it. A succeeded run ends with `Run completed in 1m 12s.` and `harumi runs download <run-id>`; a failed, cancelled or timed-out run exits 1 and prints the cause first (error, exit code, the last 20 lines of stderr and how many were left out) and `harumi runs get <run-id>` for everything. If the server refuses the run for a reason about the project (HTTP 400, 404, 409 or 422), the message ends with `Run harumi check`. (`--output-dir` is deprecated: it still downloads, with a warning.)

**Scratch-branch flow (default when tree is dirty or has unpushed commits):**

The CLI detects local changes, creates a temporary branch `harumi-scratch/<user>/<yyyymmdd-HHMMSS>` from HEAD, commits the full working tree to it using a throwaway git index (the user's real index/HEAD are untouched), pushes it to the `harumi` remote, queues the run, then deletes the remote scratch branch when finished (best-effort cleanup). The user never has to commit manually for a quick iteration.

**Calls:** `POST /projects/{id}/execute` with `{ branch, commit?, tag?, command?, kernel_spec? }`, which returns `{execution_log_id, status, workflow_run_id?, project_run_id?}`. With `--watch`, the CLI then polls `GET /projects/{id}/runs/{run_id}` until it reaches a terminal status.

## `runs`

```
harumi runs list [--project ID] [--limit N | --all] [--api-url URL] [--org ORG]
harumi runs get [RUN_ID] [--project ID] [--api-url URL] [--org ORG]
harumi runs cancel [RUN_ID] [--project ID] [--api-url URL] [--org ORG]
harumi runs download [RUN_ID] [--output-dir DIR] [--project ID] [--api-url URL] [--org ORG]
```

- **Ids**: `list` shows the **first 8 characters** of each run id, and `get`, `cancel`, `download` and `compare` take that or the whole id (a whole id goes straight to the run; a start is matched against the project's runs and refused if it matches none or more than one, pointing at `runs list`). `run` still prints the whole id.
- **`list`**: `GET /projects/{id}/runs` → table of `id` (first 8 characters), `status` (coloured), `source`, `branch`, `started` ("3 hours ago") and `took` ("1m 12s"), newest first. Shows the latest 25 by default and says "Showing the latest 25 of N runs" when there are more; `--limit N` changes the count and `--all` shows every run.
- **`get`**: `GET /projects/{id}/runs/{run_id}` (no `RUN_ID`: the latest run) → a short summary (status and exit code, how long, code ref, command, kernel), then `stdout`, `stderr` and `error` if present, then the next step (`runs cancel` if it is still going, `runs download` if it left output). Output is printed literally, never read as markup.
- **`cancel`**: `POST /projects/{id}/runs/{run_id}/cancel` on an in-flight run. With no `RUN_ID`, at a terminal, a picker offers only the runs still going (picking one is the confirmation); with no terminal it fails listing them and naming the `RUN_ID` argument. Cancelling a run that already finished says so instead of claiming a cancellation.
- **`download`**: refuses a run that has not finished ("still running") and one that finished without output, each pointing at `runs get`; otherwise prints the zip's path and size. Downloads the run's output artifacts as a zip via `GET /projects/{id}/runs/{run_id}/output/archive` (proxied by the API from S3 for current runs, or Gitea for pre-migration runs). With no `RUN_ID` it takes the latest run (`GET /projects/{id}/runs`, newest first); errors if the project has no runs. `--output-dir` defaults to the current directory. Replaces `outputs --download` and `run --output-dir`.

`--project` on every subcommand overrides the `.harumi` binding.

## `tags`

```
harumi tags list [--json] [--project ID] [--api-url URL] [--org ORG]
harumi tags set NAME [--to BRANCH_OR_SHA] [--message TEXT] [--project ID] [--api-url URL] [--org ORG]
harumi tags rm NAME [--yes] [--project ID] [--api-url URL] [--org ORG]
```

A tag is a **movable label** on one commit, such as `prod`. Run it with `harumi run --tag prod` or follow it from a schedule with `harumi schedules add --git-tag prod`. Tags are real git tags in the project's Gitea repo, so `git fetch harumi --tags --force` brings them into a clone (`--force` because a moved tag differs from the one you have).

- **`list`**: `GET /projects/{id}/repo/tags` → table of `tag`, `commit` (first 8 characters) and `message`. `--json` is an array of `{name, commit_sha, message, created_at}`. With none: `No tags yet. Add one with harumi tags set prod.`
- **`set`**: `PUT /projects/{id}/repo/tags/{name}` with `{target, message?}`. **Creates the tag, or moves it** if it exists (moving prints the same `Tagged` line). `--to` takes a branch or a commit SHA; without it the tag labels the branch you are on, and that branch **must be pushed** (it refuses when `git` has commits the server lacks, since the tag would label older code; push with `git push harumi <branch>` or pass `--to`). A tag cannot share a name with a branch (HTTP 409).
- **`rm`**: `DELETE /projects/{id}/repo/tags/{name}`, asks first unless `--yes`. Refused (409) while a schedule follows the tag; change or remove the schedule first. The commits are untouched.
- **What moving a tag changes:** runs and schedules that follow the tag use the new commit from then on. A run already started keeps the commit it resolved (`runs get` shows `tag @ commit`); a run queued from a tag is fixed at the moment it was queued.
- A tag that does not exist is HTTP 404 (`Tag 'x' does not exist`) on `run --tag`, and a failed run for a schedule whose tag was deleted.

## `compare`

```
harumi compare [RUN_ID...] [--json] [--project ID] [--api-url URL] [--org ORG]
```

- Compares 2–4 distinct runs (the first is the baseline): `GET /projects/{id}/compare?runs=a,b,c`. The count and duplicates are checked before the comparison request; a wrong count says to find ids with `harumi runs list`.
- **No `RUN_ID`s**: at a terminal it asks for the baseline, then ticks the runs to compare it with (space ticks, Enter confirms; baseline + 1 to 3 others). Only runs that **succeeded** are offered (the latest 25, newest first): one still going has no output yet and a failed one never will. It needs at least two, else it says so and points at `harumi run`. **Without a terminal (agents, CI, pipes) it never prompts**: it exits 1 listing the runs and telling you to pass them as `RUN_ID` arguments. An agent should pass the ids itself.
- An unknown run id (HTTP 404) adds a pointer to `harumi runs list`.
- Prints the project's `[[output.kpi]]` values with the change against the baseline, runtimes, the winner (best value of the primary KPI; or why there is none) followed by `harumi runs download <winner id>`, whether the runs used the same inputs (`same` / `differs` with the files / `unknown`), and the server's warnings.
- A project with no KPIs declared says so and how to declare them (`[[output.kpi]]` tables in `harumi.toml`), instead of "no KPI is marked primary" under a table that has only runtime.
- **`--json`**: prints the comparison for scripts: `columns` (`run_id`, `branch`, `commit`, `status`, `runtime_s`, `source`), `kpis` (`key`, `label`, `unit`, `direction`, `primary`, `values`, `deltas`, aligned with `columns`), `winner_run_id`, `no_winner_reason`, `warnings` and `fairness` (`verdict`: `same`, `differs` or `unknown`; `files`). Only these fields: one the API adds later is not printed. A field with no value is `null`, never missing; `warnings` and `files` are always lists.

## Editing the repo (git)

There is no `harumi repo`. A project's repo is a plain git repo; `harumi clone` leaves a remote named `harumi` with a plain URL, and git asks `harumi git-credential` for the password when it needs it (set for this repository and for Harumi's host only), so `git fetch harumi` / `git push harumi <branch>` work with no further setup and no token in `.git/config`. Each of these was a `harumi repo` subcommand until 1.0.0 removed them; running one now exits 2 and prints the git command to use.

| Task | Command in the clone |
|---|---|
| List files | `git ls-files` (another branch: `git fetch harumi && git ls-tree -r --name-only harumi/<ref>`) |
| One folder level | `ls <path>` |
| Print a file | `cat <path>` (another branch: `git show harumi/<ref>:<path>`) |
| Create or update a file | edit it, then `git add <path> && git commit -m MSG && git push harumi <branch>` |
| Delete / move | `git rm -r <path>` / `git mv <from> <to>`, then commit and push |
| Zip the repo or a folder | `git archive --format=zip -o repo.zip harumi/<ref> [<path>]` |
| History | `git log --oneline [-- <path>]` |
| List branches ("versions") | `git fetch harumi && git branch -r` |
| Create a branch | `git switch -c NAME harumi/main && git push -u harumi NAME` |
| Delete a branch | `git push harumi --delete NAME` (the old command refused when a schedule pinned the branch: check `harumi schedules list` first) |
| Tag a version (movable label such as `prod`) | `harumi tags set NAME [--to BRANCH_OR_SHA]` (see [tags](#tags)); `git fetch harumi --tags --force` brings tags into the clone |
| Promote into live | `git fetch harumi && git switch main && git merge --ff-only harumi/main && git merge --no-ff harumi/NAME && git push harumi main` |

The remote's URL has no token. The token stays in `~/.harumi/environments/<env>/credentials.json` (mode 0600), which `harumi git-credential` reads: never paste that file into chat, logs or issues. A clone made before 1.0 had the token in the URL; the first `harumi` command run inside it takes it out. `git-credential` is run by git, not by people, and answers only for Harumi's own git host over https.

Commit once per change and push once; the old file-by-file commands made one server commit per file and never updated the local clone.

## `check`

```
harumi check [--ref REF] [--project ID] [--api-url URL] [--org ORG]
```

`GET /projects/{id}/readiness?ref=` → everything blocking this project from running (repo exists, `harumi.toml` present and valid, kernel allowed), answered up front instead of surfacing as a 4xx from `run`/`schedules add`. Each check is shown by a readable name ("harumi.toml is valid"), then `Ready to run` with the next step, or `Not ready ...: N problems` and **exit code 1**, so a script can stop before `harumi run`. Replaces `repo readiness`.

## `files`

```
harumi files ls  [--project ID] [--api-url URL] [--org ORG]
harumi files put LOCAL_PATH [REMOTE_PATH] [--project ID] [...]
harumi files get REMOTE_PATH [-o LOCAL_PATH] [--project ID] [...]
harumi files rm  REMOTE_PATH [--yes] [--missing-ok] [--project ID] [...]
```

The project's non-git file storage. A run finds these files under `inputs/` in its sandbox. A project may hold at most **500 files and 500 MB** in total (the limit every run enforces); `put` refuses an upload that would pass either, before sending anything, and replacing an existing path does not count twice.

- **`put`** asks harumi-api for a presigned URL (`POST /projects/{id}/files/upload-url`), uploads the bytes to it, and then, **if the server staged the upload** (its key is `{project}/.quarantine/<id>`), calls `POST /projects/{id}/files/complete-upload`, which virus-scans the file (up to about two minutes) and only then makes it a project file. It prints `Checking the file...` while waiting and `Uploaded ...` only after the server accepts it. A file the server refuses exits 1 with `... was not added`: 422 = could not be verified as safe, 413 = too large to be checked, 503 = the scanner is unavailable right now (retry later), 404 = the upload did not arrive (retry). **If the answer never comes** (the connection drops, or the scan outlasts the wait of about three minutes) it exits 1 with "may or may not have been added", because the server can still finish and promote the file: run `harumi files ls` before uploading again, and do not assume it failed. Older servers that do not stage uploads return the file's final key, and `put` then makes no second call; instead it lists the project once to confirm the file is there, and exits 1 with "does not appear in the project" if it is not (a truncated listing, or a listing that fails, is not taken as proof, so it never turns a good upload into an error). The path you asked for settles it first: a key that is exactly `{project}/{your path}` is the file's own key and final, even if the path contains `.quarantine` (`put x .quarantine/x.csv` is a plain upload on an older server). Only a key that differs from your path is judged by its shape: `.quarantine` as its first path segment or directly after the project id counts as staging, so a change in the server's exact key shape does not silently skip the completion; a user's own folder of that name deeper in the project does not. The landing check is not guaranteed read-after-write on every backend, so "does not appear" says the file *may* not have been added: look again with `harumi files ls` before uploading it a second time. `REMOTE_PATH` defaults to the local file's name.
- **`get`** downloads through a presigned URL to `REMOTE_PATH`'s own name, or `-o`. A name that is not in the project, or is a folder, exits 1 saying so and pointing at `files ls`. The file is written to `<name>.part` and renamed only when complete, so a dropped connection leaves nothing half-written.
- **`rm`** is a hard delete (no trash): it prompts unless `--yes`, and the question and the result name the file and its size. harumi-api answers `deleted: 1` for **any** path, so the CLI reads the project's file listing first (one request, up to 500 files) and refuses a name that is not there, or is a folder, with exit 1 and a reason. **This changed `files rm <missing> --yes` from exit 0 to exit 1.** A cleanup script that may run twice should pass **`--missing-ok`**: it exits 0 with "was not there" for a missing file, and does not excuse a folder name.
- **`ls`** lists every file (`name`, `size` as `1.4 MB`, `modified` as `3 days ago`; names are never cut) and ends with the total against the limits (`3 files, 87.3 MB (of 500 files and 500.0 MB every run can use)`), with a warning at 90%. If the listing is truncated it says the figures are a minimum. File names are shown without terminal control characters.

## `dashboard`

```
harumi dashboard widgets [--type TYPE]
harumi dashboard validate [PATH] [--ref REF] [--against FILE | --run RUN_ID | --latest] [--project ID]
```

No backend endpoint — a dashboard spec is a plain file in the project's Gitea repo (edit it in the clone and `git push harumi` like any other file). A project can have several: every `dashboard/<name>.toml` (alphabetical) plus the legacy root `dashboard.toml` (last), each an entry in the platform's dashboard picker. This command group only helps you get their contents right. Full per-type reference and folder layout: [dashboard.md](dashboard.md).

- **`widgets`**: prints the current widget-type contract (required/optional keys, enum values) for all 5 types, or one with `--type`. Sourced from `harumi.dashboard.WIDGET_SCHEMAS`, a hand-maintained mirror of harumi-platform's `schema.ts` (see the `ponytail:` comment in that module) — always current with this CLI version, but can drift from the platform between CLI releases if a new widget type ships there first.
- **`validate`**: parses each dashboard spec the same way the platform's `parseDashboardConfig` does, and reports every widget that would be **dropped** (unknown `type` (the message lists the valid types and suggests the nearest one), missing/invalid required key — e.g. a `valueKey` typo for `value_key`). Exits 1 if any spec drops a widget or isn't valid TOML.
  - With no `PATH`: every `./dashboard/*.toml`, else `./dashboard.toml`. Each filename is printed as a heading when there's more than one.
  - `PATH`: just that one file, wherever it lives.
  - `--ref BRANCH`: the repo's copies instead of local files — lists the tree via `GET /repo/files`, then reads each spec via `GET /repo/file-content`. Never guesses a filename.
  - `--against FILE`: additionally resolves every widget's dot-path keys (`value_key`, `rows_key`, `data_key`, `tasks_key`, etc.) against a local `output.json` and reports any that don't resolve (widget renders empty on the platform, not an error there).
  - `--run RUN_ID` / `--latest`: same dot-path check, but fetches the run's structured output from `GET /projects/{id}/runs/{run_id}/output` (S3-backed for current runs, Gitea for pre-migration runs — resolved transparently by the API) instead of a local file.
  - At most one of `--against`/`--run`/`--latest` may be passed.

## `share`

```
harumi share list [--project ID]
harumi share get [LINK_ID] [--project ID]
harumi share add [--label TEXT] [--app/--no-app] [--chat/--no-chat] [--run-history/--no-run-history]
                  [--run-control/--no-run-control] [--project ID]
harumi share update [LINK_ID] [--label TEXT] [--enable/--disable] [--app/--no-app] [--chat/--no-chat]
                     [--run-history/--no-run-history] [--run-control/--no-run-control]
                     [--password/--no-password] [--yes] [--project ID]
harumi share rm [LINK_ID] [--yes] [--project ID]
harumi share rotate [LINK_ID] [--yes] [--project ID]
```

Manages `/projects/{id}/share-links*` — a project's public, unauthenticated dashboard links (read-only view of the project's dashboard specs, with the same picker when there are several, + a chosen run's `output.json`; no login required to view). A project can have several links, each independently revocable, each with its own permissions and optional password.

- **Ids**: every command takes the full link id **or the start of it** (as long as it matches one link). `list` shows the first 8 characters, so what it prints can be pasted into the next command. A full id is sent as given, with no listing request, for `update`, `rotate` and `rm --yes`; a wrong one gets the API's 404 plus a pointer to `harumi share list`. `get` and `rm` **without** `--yes` read the listing once (to describe the link, or to name it in the question), so a full id that is not in it is reported as "No share link" before the question and nothing is sent.
- **No id**: at a terminal `get`, `update`, `rm` and `rotate` open a picker of the project's links. **Without a terminal (agents, CI, pipes) they never prompt**: they exit 1 listing the links and naming `LINK_ID`. `rm` and `rotate` refuse `--yes` without an id, because `--yes` removes the only question and a mis-pick would then delete or rotate a link; picking a link still asks for confirmation.
- **`list`**: `GET /projects/{id}/share-links` → `ProjectShareLinkList {links: [ProjectShareLink]}`. A table of `id` (first 8), `label`, `state` (on/off), `visitors can` (`view only`, or any of `app`, `assistant`, `history`, `control`) and `password`. A link that is on, has **no password** and allows more than viewing is marked `!`, with a line saying anyone with the URL can use it. **The token is not printed**: it is the secret in the public URL; `get` shows the URL.
- **`get`**: same list call, filtered to one link. For an enabled link it prints the full viewer URL (built client-side as `{platform_url}/share/{token}`, since the API doesn't know its own public origin), then in words what anyone with the link can do and whether it has a password, with a warning in the exposed case above. A disabled link says its URL does not work and how to turn it back on.
- **`add`**: `POST /projects/{id}/share-links` → `ProjectShareLink`. Every permission flag (`--app`, `--chat`, `--run-history`, `--run-control`) defaults to off, so creating a link never silently grants more than a bare read-only, latest-run-only dashboard view.
  - `--app`: let visitors open the project's deployed Streamlit app.
  - `--chat`: read-only assistant for signed-in visitors.
  - `--run-history`: browse past runs instead of only ever the latest.
  - `--run-control`: signed-in visitors can run now, override the kernel, and manage schedules.
- **`update`**: `PATCH /projects/{id}/share-links/{link_id}`. Only the flags you pass are changed; `--enable`/`--disable` toggles the link without touching its permissions.
- **`rm`**: `DELETE /projects/{id}/share-links/{link_id}`. The old URL stops working immediately. Prompts for confirmation unless `--yes`, and the question names the link by its label so a prefix that matched the wrong one is noticed. A hard delete: there is no way to bring the link back (trashing a project removes its links for good too).
- **`rotate`**: `POST /projects/{id}/share-links/{link_id}/rotate` — invalidates the current token and mints a new one; permission flags are unchanged. Prompts for confirmation unless `--yes`.
- **`update --password`**: prompts for a password (hidden input, server-enforced 8–200 chars), then `PUT /projects/{id}/share-links/{link_id}/password`. Changing the password invalidates every previously issued unlock session for that link — viewers must re-enter it. May be combined with the other `update` flags, or used alone.
- **`update --no-password`**: `DELETE /projects/{id}/share-links/{link_id}/password`. That link becomes freely viewable (no password prompt).

`--project` on every subcommand overrides the `.harumi` binding.


## `datasources`

Real endpoints, live today (`harumi-api/src/api/datasources/router.py`). Scoped per-project by `(project_id, name)`.

```
harumi datasources list [--project ID] [--api-url URL] [--org ORG]
harumi datasources get NAME [--project ID]
harumi datasources add NAME --type TYPE [--host H] [--port P] [--database D] [--username U]
                            [--use-proxy] [--proxy-host H] [--proxy-port P] [--proxy-server-name N]
                            [--proxy-tls-ca-cert PATH] [--proxy-tls-client-cert PATH]
                            [--proxy-tls-client-key PATH]
                            [--project ID]
harumi datasources update NAME [--name NEW_NAME] [--type T] [--host H] [--port P] [--database D]
                               [--username U] [--set-credentials] [--use-proxy/--no-use-proxy]
                               [--proxy-host H] [--proxy-port P] [--proxy-server-name N]
                               [--proxy-tls-ca-cert PATH] [--proxy-tls-client-cert PATH]
                               [--proxy-tls-client-key PATH] [--project ID]
harumi datasources rm NAME [--yes] [--project ID]
harumi datasources test --type TYPE --host H --port P --database D --username U
                        [--use-proxy] [--proxy-host H] [--proxy-port P] [--proxy-server-name N]
                        [--proxy-tls-ca-cert PATH] [--proxy-tls-client-cert PATH]
                        [--proxy-tls-client-key PATH]
harumi datasources query NAME --sql "SELECT ..." [--limit N] [--csv PATH] [--project ID]
```

`--project` on every subcommand overrides the `.harumi` binding.

**Credentials are always prompted, never a flag.** `add`, `update --set-credentials`, and `test` each prompt with `typer.prompt(hide_input=True)`. This is deliberate — secrets must never land in shell history, process listings, or `--help` output. The server stores credentials in AWS SSM (SecureString) and never returns them; `get`/`list` responses have no credential field.

**`type`** must be one of `postgresql | mysql | sqlserver | oracle`. Anything else is refused before the password prompt, with a "did you mean" for a near miss (`postgres` -> `postgresql`).

**Proxy mTLS.** A datasource only reachable through the mTLS proxy needs `--use-proxy` together with the complete set: `--proxy-host`, `--proxy-port`, and all three of `--proxy-tls-ca-cert`, `--proxy-tls-client-cert`, `--proxy-tls-client-key`. Those three take **file paths**, not inline values, so a PEM never lands in shell history; the CLI reads each file and sends the contents, which the server stores in the same SSM bundle as the password. `--proxy-server-name` defaults to `vpnproxy.harumi.io` server-side. `add` and `test` reject an incomplete set locally, before prompting for credentials, listing each missing flag. `update` sends only the certs you pass, so any one of them can be rotated on its own without resupplying the others.

**`add`** calls `POST /datasources/{project_id}` — the backend tests the connection before persisting, so a bad host/credentials fails the `add` with the same error `test` would surface.

**`update`** calls `PUT /datasources/{project_id}/{name}` with only the fields you pass (partial update). Passing `--name` renames the datasource (and its SSM parameter, server-side). Omit `--set-credentials` to leave the stored credentials untouched.

**`rm`** calls `DELETE /datasources/{project_id}/{name}`, prompting for confirmation unless `--yes`. Deletes the DB row and the SSM parameter.

**`test`** calls `POST /datasources/test-connection` — validates without persisting. Useful to sanity-check credentials before `add`.

**`query`** calls `POST /datasources/{project_id}/{name}/execute`, the read-only proxy:

- Server validates the SQL is **SELECT/WITH-only**; any of `INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|EXEC|EXECUTE|CALL|MERGE|UPSERT` (as a whole word, case-insensitive) → **403** with a message naming the forbidden keyword.
- Rows are capped server-side at `--limit` (default 10000, hard max 100000). If actual rows exceed the cap, the response sets `wasLimited=true` and the CLI prints a yellow warning.
- Response shape: `{ columns: string[], data: any[][], rowCount, wasLimited, maxRows, dataframe_name }`. The CLI renders `columns`/`data` as a Rich table, or writes them as CSV with `--csv <path>`.
- Datasource not found → 404. Query execution error (bad SQL, connection issue) → 400.

## `schedules`

Real, project-scoped endpoints (`/projects/{project_id}/schedules`).

```
harumi schedules list [--project ID] [--api-url URL] [--org ORG]
harumi schedules get [SCHEDULE_ID] [--project ID]
harumi schedules add --cron CRON [--git-branch BRANCH | --git-tag TAG] [--start-at ISO] [--git-commit SHA]
                     [--command C] [--kernel K] [--output-format F] [--email-to E] [--project ID]
harumi schedules update [SCHEDULE_ID] [--cron CRON] [--start-at ISO] [--git-branch B | --git-tag T] [--git-commit SHA]
                        [--command C] [--kernel K] [--output-format F] [--email-to E] [--project ID]
harumi schedules rm [SCHEDULE_ID] [--yes] [--project ID]
```

`--project` on every subcommand overrides the `.harumi` binding.

**Ids.** `list` shows the first 8 characters of each id; `get`, `update` and `rm` take that or the full id. With no id, a terminal gets a picker and anything else gets the list of choices and the argument to pass. `rm --yes` needs the id (it never picks for you).

**`--cron`** is a raw 5-field cron expression (e.g. `"0 9 * * *"`), **interpreted in UTC**. The CLI does not validate it — the server validates with `croniter` and returns **400 Invalid cron expression** on a bad value, and 400 if it would fire more often than every 5 minutes. There is no calendar/builder UX; pass the cron string directly.

**`--start-at`** is an ISO-8601 datetime; defaults to "now" (UTC) if omitted on `add`.

**`--git-branch` / `--git-tag`.** A schedule follows a branch (default `main`) **or** a tag, never both; the CLI refuses `--git-tag` with `--git-branch` or `--git-commit` before the request. With `--git-tag prod` it runs whatever `prod` points at **each time it fires** (a deleted tag fails that run). On `update`, passing one replaces the other. `list` shows `tag NAME` in the `branch` column for a tag schedule.

**`--email-to`** accepts `only-me` | `everyone` | a comma-separated list of email addresses (resolved server-side).

**`add`** calls `POST /projects/{project_id}/schedules` with `{cron, start_at, git_branch | git_tag, git_commit?, command?, kernel_spec?, output_format?, email_to?}` → `Schedule`.

**`update`** calls `PUT /projects/{project_id}/schedules/{schedule_id}` with only the fields you pass (partial update); errors locally with "No fields to update" if no flags are given.

**`rm`** calls `DELETE /projects/{project_id}/schedules/{schedule_id}`, prompting for confirmation unless `--yes`. **This is the only way to stop a schedule** — there is no pause/enable flag.

**No separate "run now."** Immediate execution is `harumi run` (git-ref based); schedules only manage recurring cron runs.

## `secrets`

Project-scoped environment variables, stored as SSM SecureStrings and injected into kernels/apps at run time.

```
harumi secrets list [--project ID] [--api-url URL] [--org ORG]
harumi secrets set NAME [--project ID]
harumi secrets rm NAME [--yes] [--project ID]
```

- **`list`**: `GET /projects/{id}/secrets` → names only. Values are never printed.
- **`set`**: prompts for the value with hidden input, then `POST /projects/{id}/secrets` with `{name, value}`. There is no update endpoint — `set` on an existing name overwrites it.
- **`rm`**: `DELETE /projects/{id}/secrets/{name}`, prompting for confirmation unless `--yes`.

## `memories` and `skills`

Project memories: notes the project's chat remembers. Backed by `/projects/{id}/memories`.

```
harumi memories list [--query TEXT] [--mode always|on_demand] [--project ID] [--api-url URL] [--org ORG]
harumi memories add [CONTENT|-] [--mode always|on_demand] [--project ID]
harumi memories edit [MEMORY_ID] [--content TEXT] [--mode always|on_demand] [--project ID]
harumi memories rm [MEMORY_ID] [--yes] [--project ID]
harumi skills library [--category C] [--query TEXT] [--json] [--api-url URL] [--org ORG]
harumi skills add SLUG [--force] [--api-url URL] [--org ORG]
```

- **`list`**: `GET /projects/{id}/memories?q=&retrieval_mode=` → a table of `id` (the first 8 characters; `edit` and `rm` accept the start of an id), `scope`, `mode`, `content` (truncated) and `updated`. The list also carries memories inherited from the project's organization or the owner's personal workspace (`scope` is `organization` or `user`); those are read-only through the project routes.
- **`add`**: `POST /projects/{id}/memories` with `{content, source: "cli", retrieval_mode}`. `content` is 1-4000 characters; `--mode` defaults to `on_demand`. `CONTENT` of `-` reads standard input. If it is omitted, a terminal is prompted and anything else is told to pass it.
- **`edit`**: `PATCH /projects/{id}/memories/{memory_id}` with only the fields given. At least one of `--content` / `--mode` is required, otherwise the CLI fails before calling the API.
- **`rm`**: `DELETE /projects/{id}/memories/{memory_id}`, prompting for confirmation unless `--yes`. With no id a terminal gets a picker of the project's own memories; `--yes` needs the id. A workspace memory (scope `organization` or `user`) is named as such instead of reported as not found.
- **`skills library`** (`--json` prints `slug`, `name`, `description`, `category`, `tags`, `version` and never a skill's content; a search with no match prints the categories that exist): `GET /skill-library` → the platform's preset skills (`slug`, `name`, `category`, `version`, `description`). `--category` matches exactly; `--query` is a case-insensitive substring of name, description or tags.
- **`skills add SLUG`**: needs a bound clone. `GET /skill-library/{slug}`, then writes `<clone>/skills/<slug>/SKILL.md` with `source: harumi/<slug>` and `version: N` set in the frontmatter (replacing any the preset had), exactly as the platform's Install button does. Refuses when that file exists unless `--force`: if the existing file's `source:` is `harumi/<slug>` it says the preset is already installed, otherwise that the folder belongs to a different skill. It writes the file only and prints the `git add / commit / push harumi` line; the project's assistant loads skills from the live branch. A slug the library does not have says so, offers the nearest one and points at `skills library` (a server error is not called a missing skill). The commit command it prints starts with `cd <project root>`, so it works from a subfolder.
- A project's *own* skills are plain files at `skills/<slug>/SKILL.md`: create, edit and delete them in the clone with your editor and git. There is no command for that on purpose.

## `agent-skills`

```
harumi agent-skills install [--agent KEY]... [--project] [--dry-run] [--force]
```

Copies the bundled `harumi-cli` and `harumi-cli-setup` skills onto this machine (it prints, per agent, which skills went into which folder; with no agent found it says so and uses `~/.agents/skills`; a file or an unwritable folder in the way is explained, not an `[Errno 17]`) for Cursor, Claude Code, Codex or the universal `~/.agents/skills` folder (auto-detected, or `--agent`). `--project` writes to `./.agents/skills` instead. `--dry-run` prints each `source -> destination` and writes nothing (it replaces `skill path`). Not the same as `harumi skills`, which is about a project's skills. Replaces `skill install`.

## `org`

```
harumi org list [--api-url URL]
harumi org create BUSINESS_NAME [--api-url URL]
harumi org rename ORG NEW_NAME [--api-url URL]
harumi org rm [ORG] [--yes] [--api-url URL]
harumi org members [ORG] [--api-url URL]
harumi org invite ORG --email EMAIL [--role ROLE] [--api-url URL]
harumi org role ORG [MEMBER] --role ROLE [--api-url URL]
harumi org rm-member ORG [MEMBER] [--yes] [--api-url URL]
harumi org use [ORG | --personal]
```

**`ORG`** is the organization's name (any case) or the start of its id; **`MEMBER`** is an email or the start of a user id. Where the command deletes for good (`org rm` for `ORG`, `org rm-member` for `MEMBER`) an id prefix must be at least 8 characters, so a stray `9` cannot name one; a name or an email always works. `members` and `rm` also take no `ORG`: `members` then uses the workspace you are in (a terminal picks if there is none), while `rm` never does that and always asks, since it would delete the organization you are standing in; `rm --yes` needs the name or at least 8 characters of the id. With no `MEMBER`, a terminal gets a picker (without the owner) and anything else the list of choices.

`--role` is one of `admin | member | viewer`, in any case. **`owner` is refused before any request**: an organization has one owner (its creator), the server never grants it through `invite` or `role`, and moving it is a separate "transfer ownership" action the CLI does not have (use the web app).

- **`list`**: `GET /users/organizations`; ends with the workspace you are working in.
- **`create`**: `POST /users/organizations` with `{business_name}`.
- **`rename`**: `PATCH /users/organizations/{id}` with `{business_name}`.
- **`rm`**: `DELETE /users/organizations/{id}`; asks you to type the organization's name unless `--yes` (see [Confirmations](#confirmations---yes)). **Permanent**: unlike projects, an organization is not soft-deleted (harumi-api purges its projects at once and cancels its subscription), so this is the one delete here with no way back. The question names the organization and what goes with it. If it was the workspace you were working in, the saved choice is cleared and you are back in your personal workspace. Only the owner can delete it; a 403 `mfa_required` (the organization requires a fresh authenticator code) is reported as "do this in the web app", as the CLI cannot ask for one.
- **`members`**: `GET /users/organizations/{id}/users`; a table of name, email, role (`(invited)` while pending) and the first 8 characters of the user id.
- **`invite`**: `POST /users/organizations/{id}/users` with `{email, role}`.
- **`role`**: `PUT /users/organizations/{id}/users/{user_id}` with `{role}`.
- **`rm-member`**: `DELETE /users/organizations/{id}/users/{user_id}`, prompting for confirmation unless `--yes`.
- **`use`**: `ORG` is checked against your organizations first (name or start of an id, saved as the real id), so a name or an organization you do not belong to is refused instead of being saved. With no argument opens a menu of your organizations plus "Personal workspace" (at a terminal; otherwise it lists the choices and fails). `--personal` clears the saved org. Persists `org_id` in `~/.harumi/config.json` (per environment); every later request sends it as `X-Organization`. The header scopes the *read* endpoints (`projects list`); creation reads the workspace from the request body, so `new` and `import` default `customer_id` to this org — pass `--personal` to create in your personal workspace anyway. It needs the network to check the choice (and a login); if harumi-api cannot be reached at all, a full id is still saved as given, with a note. Replaces `config set-org`.

## Scripts and CI (`--json`)

**A table is for a person and is not a stable format**: its columns, the width of its ids (8 characters in `runs list`) and whether it is a table at all (narrow terminals get two lines per project) can change. A script uses `--json`, which always has whole ids.

A table printed for a person never rewrites what is stored: `[red]` in a branch or project name is shown as typed (Rich would have taken it for a colour), and control characters are removed.

Every command that reads data takes `--json`: `projects list|get`, `runs list|get`, `files ls`, `share list|get`, `datasources list|get`, `schedules list|get`, `secrets list`, `memories list`, `org list|members`, `specs`, `examples`, `usage`, `whoami`, and `compare`. `run --json` is below.

```bash
harumi run --watch --json | jq -e '.succeeded'      # exit 1 if the run failed or the JSON says it did not succeed
harumi runs list --json | jq -r '.[] | select(.status=="failed") | .id'
```

**The contract**

- **stdout is exactly one JSON document**, nothing before or after it: an array for a list, an object for one item. No table, no hint line, no colour, never wrapped.
- **An empty list is `[]`**, not a sentence.
- **Errors are unchanged**: a message on stderr, exit 1, empty stdout. A warning (a project list that could not be fully fetched) is on stderr too, so the document stays valid.
- **Ids are whole, timestamps are ISO 8601, text is exactly as stored.** Output is ASCII-only JSON, so a control character in a value cannot reach your terminal.
- **Fields are listed by name** (`src/harumi/json_out.py`), not dumped from the API, so nothing internal appears: a file's storage key and a run's signed output URL are left out (`runs get` has `has_output`; `harumi runs download` fetches it), `secrets list` is names only, and `share list` has no token. `share get <id> --json` is the one place the token and URL appear, as `share get` shows them.
- **No menus.** A command that would pick from a list at a terminal (`projects get`, `share get`, `schedules get`, `org members` with no workspace) says to pass the id instead of opening one.
- **`runs list --json` is every run**, unlike the table's latest 25; `--limit N` still caps it. `files ls --json` is an object, `{"files": [...], "is_truncated": bool}`, because a listing can be cut and a script has to be able to tell.

**`run --json`** prints `{"run_id", "status", "exit_code", "succeeded", "took_seconds", "error", "watched"}`. Without `--watch` it is printed once the run is queued (`status: "queued"`, `succeeded: null`); with `--watch`, once it finishes. The exit code does not change: `run --watch` exits 1 when the run does not succeed, with or without `--json`.

**Exit codes:** 0 success, 1 any failure (including a watched run that failed), 2 a usage error. Menus are off whenever stdin or stdout is not a terminal, under `CI`, with `TERM=dumb`, or with `HARUMI_NO_INTERACTIVE=1`; a command that needs a choice then fails and lists the choices.

## Confirmations (`--yes`)

Every command that deletes or removes something follows one rule, from `projects rm` to `org rm-member`:

| Where you run it | What happens |
|---|---|
| `--yes` (or `-y`) | Goes ahead, no question. |
| At a terminal, no `--yes` | Asks `[y/N]`. Enter is **No**. Declining prints `Aborted.` and **exits 1**, so `harumi org rm X && deploy` stops. |
| No terminal (a script, CI, an agent, a pipe), no `--yes` | **Refuses at once**: `Cannot ask here: no interactive terminal. Run it again with --yes to go ahead.` and exits 1. It never prints a prompt into a log and waits, and a `y` piped to stdin is **not** consent (it was not given to this question). |

The commands: `projects rm`, `files rm`, `share rm`, `share rotate`, `share update --no-password`, `datasources rm`, `tags rm`, `schedules rm`, `secrets rm`, `memories rm`, `org rm`, `org rm-member`. The deprecated `repo rm` / `repo branch-rm` keep their old y/N until they are removed in 1.0.0.

- **The permanent ones that take other things with them ask you to type the name** instead of `y`: `projects rm` (the project name) and `org rm` (the organization name, exactly: case and spaces count; Enter is a no). An agent passes `--yes` and still must name the project or organization (`--yes` with no name is refused).
- **The question says which one**, not a UUID: a schedule by its cron, branch and command, a memory by its text, a file by its name and size, a share link by its label.
- **`share update --no-password`** asks because it opens the dashboard to anyone with the link; setting a password or any other change never asks. Its `--yes` only covers that.
- **`secrets set` of a name that exists** says `Replaced` and that the old value is gone; it does not ask, since replacing is what the command is for.
- **Hidden prompts** (`secrets set`, `share update --password`, `datasources add|update|test`): at a terminal the value is typed hidden; with no terminal one line is read from stdin without echo warnings, so `printf '%s' "$KEY" | harumi secrets set API_KEY` works. An empty stdin fails with `No value on standard input`. A secret is never a flag.

## Command grammar

- **Asking for help:** `-h` and `--help` work on every command and group (`harumi projects rm -h`); `harumi help [COMMAND]` is the same. `-v` is `--version`.
- **The verbs** repeat across groups: `list`, `get`, `add`, `update`, `rm`. Four groups are irregular and are not renamed, because a rename needs a deprecation window: `files` uses `ls`, `put`, `get`, `rm`; `org` uses `create`; `memories` uses `edit`; `secrets` and `profile` use `set`.
- **A wrong verb is answered, never guessed:** `harumi secrets create` exits 2 with `No such command 'create'. Did you mean 'set'? Commands: list, set, rm.`; `org add` names both `create` (a new organization) and `invite` (a person); `harumi version` points at `--version`. It is a hint, not an alias, so nothing runs. A misspelling (`harumi shar`) gets `Did you mean 'share'?` on every supported Typer version. Hidden deprecated names (`projects delete`, ...) keep working and are not listed.
- **Removed names:** the 28 names deprecated in 0.11 (`push`, `repo ...`, `outputs`, the `remove`/`delete` verbs, ...) were removed in 1.0.0. Running one exits 2 with the hint (`` `harumi push` was removed in 1.0.0. Use: harumi import ``) instead of a bare "No such command", and sends nothing to the API; a name that never existed gets no such history. `run --output-dir` exits 1 with the same kind of hint, before anything runs. Still working, with a `deprecated:` line on stderr and removed in 1.1.0: `harumi blueprints` and `harumi new --blueprint` (use `examples` and `--example`).
- **The help is grouped** into *Get started*, *Run and results*, *Project*, *Account and team* and *Help and updates*. `--api-url`, `--git-url` and `--org` (which point a command at another server or organization) are listed under an **Advanced** heading in every command's help; they are all still there, and still in the contract, only apart. (`usage --org` is the exception: it is what the command is about.)
- **Examples:** the commands people reach for first (about 30 of them, from `login` to `projects rm`) end their `--help` with an `Examples:` block. Each line is parsed against the real command in the test suite, so none can name a flag that does not exist. Words in capitals (`PROJECT_ID`, `SLUG`) are for you to fill in. A secret is never in an example: those are prompted for.
- **Completion** (`harumi --install-completion`, once per shell): commands, subcommands, flags, and the fixed-list values `--type` (`postgresql`, `mysql`, `sqlserver`, `oracle`), `--role` and `--mode`. Ids and names are not completed: that would need a login and a request on every TAB.

## Config & credential files

Environment selection precedence (highest first): **`--env` > `HARUMI_ENV` > the choice made at `harumi login` > `production`**. See [Environments](#environments) for the environment table.

Within the active environment, URL/org overrides (highest first): **CLI flags > env vars > per-env `config.json` > the environment's built-in endpoints**.

| Setting | Env var | Per-env config key | Default (per environment) |
|---|---|---|---|
| API base URL | `HARUMI_API_URL` | `api_url` | environment's `api_url` |
| Gitea URL | `HARUMI_GIT_URL` | `git_url` | environment's `git_url` |
| Org id (`X-Organization`) | `HARUMI_ORG` | `org_id` | none (from login) |
| Web app URL (printed project links) | `HARUMI_PLATFORM_URL` | `platform_url` | environment's `platform_url` |
| Status page (`harumi status`, unreachable-API hint) | `HARUMI_STATUS_URL` | `status_url` | environment's `status_url` |

- `~/.harumi/config.json` — global; stores only the selected `environment`.
- `~/.harumi/environments/<env>/credentials.json` — per-environment `access_token`, `refresh_token`, `git_token`, `git_url`, `user_id`, `email`, `expires_at`; mode `0600`.
- `~/.harumi/environments/<env>/config.json` — per-environment `org_id` (and any local `api_url`/`git_url` overrides).
- `.harumi/config.json` (per-project) — `project_id`, `repo.owner/name/clone_url/default_branch`; written by `harumi new` / `harumi import` / `harumi clone` / `harumi link`, searched upward from cwd.
- Override the home dir with `HARUMI_HOME`.
- **Upgrading from a pre-environments install:** the old flat `~/.harumi/credentials.json` + `config.json` are migrated automatically into the `production` environment on first run.

## Troubleshooting

| Symptom / error | Cause | Fix |
|---|---|---|
| `Error: You're not logged in.` or `Error: Your Harumi session has expired.` (both go on to say to run harumi login) | No session, or it ended | Ask user to run `harumi login` (an agent must never automate the emailed code) |
| `Error: Could not renew your login: harumi-api returned HTTP 403 ...` | A firewall, proxy or outage blocked the token refresh; the session may be fine | Check network/VPN and retry; logging in again will not help |
| `Couldn't connect to Harumi: the connection was refused.` / `Harumi didn't answer in time.` / `The connection to Harumi was cut off.` | Harumi is down or unreachable. Each is followed by a link to the status page | `harumi status`; retry in a minute. On `staging` it also asks whether the VPN is connected |
| `Couldn't find Harumi's address (the DNS lookup failed).` | Your network, not Harumi: no internet, or a DNS problem (on staging, the VPN) | Check the connection. The status page is not offered, as it would not help |
| `Couldn't make a secure connection to Harumi: its certificate wasn't trusted.` | A company proxy that re-signs HTTPS traffic | Point `SSL_CERT_FILE` at the company's CA certificate file |
| `...it may have gone through before the connection failed. Look at the current state before you try again.` | A request that changes something (a run, a create, a delete) was sent and the answer never came | Look first (`harumi runs list`, `... list`); repeating it blindly can do it twice. Not said when the connection never opened |
| `Got a web page (HTTP 502) instead of an answer from Harumi` | A proxy, firewall, captive-portal or outage page replied for harumi-api. Its text is not shown | A 502/503/504 also offers the status page; otherwise ask your network admin |
| `Harumi's reply isn't in the form this version of the CLI expects.` | The server and this CLI disagree on a response's shape, or a proxy mangled it | Upgrade the CLI (the message names the command for how it was installed) |
| `Something unexpected went wrong in harumi (KeyError).` | A bug in the CLI. The exception text is not printed, as it can contain a token | Report it with the command and version; `HARUMI_DEBUG=1` prints the traceback (without local variables) |
| `harumi-api returned HTTP 429: ... Try again in 37 seconds.` | A rate limit; the wait is the server's `Retry-After` | Wait that long. Without a usable `Retry-After` it just says so |
| `Connection trouble. The run keeps going; trying again...` (in `run --watch`) | A poll for the run's status failed (no answer, or a 502/503/504) | Nothing: up to 4 failures in a row are ridden out. After that: `Lost touch with harumi-api ... The run is probably still going` and exit 1; `--json` reports `status: "unknown"`, not a failure |
| `harumi-api returned HTTP 422: ... Signups not allowed for otp` | New email, no account | Re-run `harumi login --signup` |
| `Provide --project or run from a directory with a .harumi binding` | No `--project` and `.harumi/config.json` missing in cwd + parents | `harumi new`/`harumi clone <ID>` (nothing/existing project) or `harumi import` (existing folder) or `harumi link --project <ID>` (already checked out), or pass `--project` |
| `No Gitea token found. Run harumi login` | `git_token` absent in credentials | `harumi login` again |
| `git push failed: ...` | Network (on staging, the VPN not connected) or bad credentials | Check the network (and VPN on staging); re-run `harumi login` to refresh token |
| `git not found` | git missing from PATH | Install git |
| `Run ended with status: failed` | Solver code raised or exited non-zero | `harumi runs get <RUN_ID>` for stdout/stderr/error |
| `No run id returned; cannot watch this run` | Backend didn't return a `project_run_id` | Check `harumi runs list` manually |
| `harumi-api returned HTTP 403: Only SELECT queries are allowed ...` | `datasources query` SQL contains a destructive keyword or doesn't start with SELECT/WITH | Rewrite the query as a read-only SELECT/WITH |
| `harumi-api returned HTTP 404: ...` (on `datasources`/`schedules`/`secrets` get/update/rm) | Resource name/id doesn't exist for this project | List the resource first to check the exact name/id |
| `[yellow]Result was truncated at the server-side row cap` | Query returned more rows than `--limit` (or the 100000 hard max) | Narrow the query (add a `WHERE`/`LIMIT`) or raise `--limit` |
| `No fields to update.` | An `update`/`set` command called with no flags | Pass at least one field flag |
| `harumi-api returned HTTP 400: Invalid cron expression: ...` | `schedules add/update --cron` failed server-side `croniter` validation | Fix the cron string (5 fields: minute hour day month weekday) |
| A widget is missing from the dashboard, no error shown | The platform silently drops a widget with an unknown `type` or a missing/renamed required key (e.g. `valueKey` instead of `value_key`) | `harumi dashboard validate` on the file before pushing it |
| A widget renders but stays empty | Its `*_key` dot-path doesn't match anything in the run's `output.json` | `harumi dashboard validate --latest` (or `--against <output.json>`) to see exactly which key and what's available instead |

## Python library (`Client`) alternative

When scripting is better than shelling out. A `Client` is self-contained: two clients for two environments (`Client(environment="staging")`, `Client(environment="production")`) each use their own login, in any order and on any thread. Everything the library raises is a `harumi.errors.HarumiError` (`ApiError`, `NotAuthenticatedError`, ...), and `import harumi` loads none of the CLI. It signs in from the credentials file `harumi login` writes; there is no token argument or environment variable for CI yet.

```python
from harumi import Client
from harumi.config import ProjectBinding

binding = ProjectBinding.load()        # reads .harumi/config.json
client = Client()                      # loads ~/.harumi/credentials.json

# Queue a git-ref run
response = client.execute_project(
    binding.project_id,
    branch="feature/solver-v2",
    command="python main.py",
)

# Poll until done
from harumi.execution import wait_for_run, download_run_output
result = wait_for_run(client.api, binding.project_id, response.project_run_id)
print(result.status, result.succeeded)

# Download artifacts
download_run_output(client.api, binding.project_id, result, "./out")

# Repo file operations
files = client.list_repo_files(binding.project_id)
client.apply_repo_changes(
    binding.project_id,
    operations=[{"action": "update", "path": "config.yaml", "content": "..."}],  # base64
)

# Datasources
result = client.execute_datasource_query(binding.project_id, "sales_db", "SELECT * FROM orders LIMIT 10")
print(result.columns, result.row_count, result.was_limited)

# Schedules
schedule = client.create_schedule(
    binding.project_id,
    {"cron": "0 9 * * *", "start_at": "2026-01-22T09:00:00Z", "git_branch": "main"},
)

# Secrets
client.create_secret(binding.project_id, "API_KEY", "s3cr3t")

# Organizations
orgs = client.list_organizations()

# Examples (the API, and so the library, still call them blueprints)
examples = client.list_blueprints()

# Dashboard spec discovery, widget contract + validation
from harumi.dashboard import (
    widget_schemas,
    local_dashboard_paths,
    pick_dashboard_paths,
    validate_dashboard_toml,
)
for path in local_dashboard_paths(Path(".")):  # dashboard/*.toml, then dashboard.toml
    widgets, issues = validate_dashboard_toml(Path(path).read_text())
# ...or the repo's copies:
paths = pick_dashboard_paths(f.path for f in client.list_repo_files(project_id, ref="main"))

# Public share link (a project can have several; each has its own token and permissions)
link = client.create_share_link(binding.project_id, {"app_enabled": True})
print(link.token)
links = client.list_share_links(binding.project_id)

# Create a project
project = client.create_project("New Project")  # project.repo is None if unprovisioned
```

`Client(api_url=..., git_url=..., org_id=...)` accepts the same overrides as the CLI flags. Requires the user to have run `harumi login` at least once.

