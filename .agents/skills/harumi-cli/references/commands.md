# harumi CLI — Command Reference

Detailed flag reference, config/credential storage, troubleshooting, and the Python `Client` SDK. Loaded on demand from [SKILL.md](../SKILL.md).

## Contents

- [Auth commands](#auth-commands)
- [env](#env)
- [profile](#profile)
- [specs](#specs)
- [blueprints](#blueprints)
- [projects](#projects)
- [new](#new)
- [import](#import)
- [clone](#clone)
- [link](#link)
- [start](#start)
- [run](#run)
- [runs](#runs)
- [Editing the repo (git)](#editing-the-repo-git)
- [check](#check)
- [dashboard](#dashboard)
- [share](#share)
- [datasources](#datasources)
- [schedules](#schedules)
- [secrets](#secrets)
- [memories and skills](#memories-and-skills)
- [agent-skills](#agent-skills)
- [org](#org)
- [Config & credential files](#config--credential-files)
- [Troubleshooting](#troubleshooting)
- [Python library (`Client`) alternative](#python-library-client-alternative)

## Auth commands

### `harumi login`

```
harumi login [--email EMAIL] [--signup] [--api-url URL] [--git-url URL]
```

Logs into the **active environment** (see [env](#env)) — pass `harumi --env staging login` to log into staging. Each environment has its own Supabase and its own stored session.

- Prompts for email if `--email` omitted, then prompts for the OTP code emailed by Supabase.
- `--signup`: creates the Supabase account first. Required the *first* time a new email logs in.
- On success, stores `access_token`/`refresh_token` at `~/.harumi/credentials.json` (mode `0600`), then calls `POST /git/credentials` to provision a per-user Gitea personal access token (`git_token` + `git_url`), used by `harumi new`/`import`/`clone`/`link` and `harumi run` for git-over-HTTPS.
- Best-effort org resolution: if exactly one org, stored automatically; if multiple, prints a table and instructs `harumi org use`.

### `harumi logout`

Clears `~/.harumi/credentials.json`. No flags.

### `harumi whoami`

```
harumi whoami [--api-url URL] [--org ORG]
```

Prints the email and id of the logged-in user (`GET /users/profile`), the active environment, and — when set — the profile's name and bio and the active organization (`--org` or the one from `org use`). Replaces `profile show`.

## `whats-new` and update notices

```
harumi whats-new [--all]
```

Prints the bundled release notes for the installed version (`--all`: every release, newest first). It works offline; the notes ship inside the package.

Separately, real invocations may print a one-line notice on **stderr** after the command: a newer version exists on PyPI (with the upgrade command for pipx / uv / pip), or "harumi updated X -> Y, see `harumi whats-new`" once after an upgrade. PyPI is asked at most once a day, in the background with a 2 s timeout, and the same notice repeats at most once a day (a newer release is announced immediately). Nothing is printed unless stderr is a terminal, under `CI`, with `HARUMI_NO_UPDATE_CHECK=1`, or for `--version`. State: `~/.harumi/update-check.json`.

## `env`

```
harumi env list [--all]
harumi env use NAME
```

Selects the backend environment. Two are built in:

| Env | API | Gitea | Access |
|---|---|---|---|
| `production` (default) | `https://api.harumi.io/api` | `https://git.harumi.io` | public |
| `staging` | `https://api.dev.harumi.io/api` | `https://git.dev.harumi.io` | internal, VPN-only |

- **`list`**: shows selectable environments with the active one flagged `*`, and each one's API and git endpoints. Internal (VPN-only) environments are hidden unless `--all` is passed or `HARUMI_INTERNAL=1` is set. (`env current` is deprecated; `list` shows the same information.)
- **`use NAME`**: persists the default environment in `~/.harumi/config.json`. Warns if the target is internal (VPN required) and if you're not yet logged in on it.

Selection precedence: `--env` (top-level flag) > `HARUMI_ENV` > saved default from `env use` > `production`.

**Each environment has its own Supabase, so each has its own stored session** (`~/.harumi/environments/<env>/credentials.json`). Switching environments never logs you out of the other — but you must `harumi login` at least once per environment. `--api-url`/`--git-url` (and `HARUMI_API_URL`/`HARUMI_GIT_URL`) still override the active environment's endpoints for local development without changing which environment you're on.

Staging is hidden from regular users as a UX convenience only — the real access gate is needing an account in staging's Supabase plus the VPN. Any internal dev can `harumi env use staging` or pass `--env staging`.

## `profile`

```
harumi profile set [--first-name N] [--last-name N] [--bio TEXT] [--api-url URL] [--org ORG]
```

`set` sends only the flags you pass as a partial update (`PUT /users/profile`); errors locally with "No fields to update" if none are given.

## `specs`

```
harumi specs [--api-url URL] [--org ORG]
```

Lists kernel specs (`name`, `display_name`, `cpu`, `memory`, `subscription_required`) from `GET /sandbox/specs`. `name` is what you pass to `run --kernel`.

## `blueprints`

```
harumi blueprints [--api-url URL] [--org ORG]
```

Lists project blueprints (`slug`, `name`, `description`) from `GET /blueprints`. Pass a blueprint's `slug` as `new --blueprint`.

## `projects`

```
harumi projects list [--api-url URL] [--org ORG]
harumi projects get PROJECT_ID [--api-url URL] [--org ORG]
harumi projects rename PROJECT_ID NAME [--api-url URL] [--org ORG]
harumi projects rm PROJECT_ID [--yes] [--api-url URL] [--org ORG]
```

Creating a project is `harumi new` (`--no-clone` to only create it; see [new](#new)).

- **`list`**: `GET /projects` → table of `id`, `name`, `kernel_spec`, `role`.
- **`get`**: `GET /projects/{id}` → detail view.
- **`rename`**: `PUT /projects/{id}` with `{name}`.
- **`rm`**: `DELETE /projects/{id}`. Prompts you to type the exact project name to confirm unless `--yes`.

## Onboarding: `new` / `import` / `clone` / `link` / `start`

Three journeys, three commands — plus the narrower `link` primitive and the interactive `start` entry point. All of `new`/`import`/`clone` leave the target directory bound (`.harumi/config.json` written, `harumi` git remote configured) and ready for `harumi run`.

### `new`

```
harumi new NAME [--dir PATH] [--customer-id ID] [--personal] [--blueprint SLUG] [--no-clone]
          [--api-url URL] [--git-url URL] [--org ORG]
```

Use when there's nothing yet.

1. `POST /projects` to create the project (seeds a runnable `harumi.toml` + `main.py` + `dashboard.toml` server-side, or a blueprint's files if `--blueprint` is given — see `harumi blueprints`).
2. `GET /projects/{id}/repo` to fetch the Gitea repo.
3. Clones that repo into `--dir` (default: `./<slugified-name>`, refuses to clone into an existing non-empty directory).
4. Binds the clone the same way `link` does (writes `.harumi/config.json`, configures the `harumi` git remote).

`--no-clone` stops after step 1: it only creates the project (no clone, no binding, so `--dir` is refused with it) and prints a `harumi clone <id>` hint. Use it for scripting. The project is created in the configured org (`org use` / `--org` / `HARUMI_ORG`) unless you pass `--customer-id` to pick a different one or `--personal` for your personal workspace; the command prints which workspace it landed in. Replaces `projects create`.

### `import`

```
harumi import [PATH] [--project-name NAME] [--from-git URL] [--bind/--no-bind] [--personal]
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

1. Calls `GET /projects/{id}/repo` (prompts you to pick from `harumi projects list` if `--project` is omitted).
2. Writes `.harumi/config.json` in the current directory with `project_id` + repo metadata.
3. Configures the `harumi` git remote with an authenticated HTTPS URL (requires a `git_token` in credentials from `harumi login` and the current directory to already be a git working tree — `link` warns and skips remote setup otherwise).

After binding (via `new`/`import`/`clone`/`link`), `harumi run`, `harumi runs`, `harumi check`, `harumi skills add`, `harumi datasources`, `harumi schedules`, and `harumi secrets` all work without any `--project` flag. `.harumi/config.json` is searched upward from cwd, so these commands work from subdirectories too.

### `start`

```
harumi start
```

Interactive entry point for a confused or new user: asks "What do you have?" (nothing yet / a local folder / an existing Harumi project) and dispatches to `new`/`import`/`clone` accordingly. Equivalent to picking the right one yourself.

## `run`

```
harumi run [--branch B] [--commit SHA] [--command C] [--kernel K]
           [--watch]
           [--api-url URL] [--git-url URL] [--org ORG]
```

Requires the directory (or a parent) to be bound — via `harumi new`, `harumi import`, `harumi clone`, or `harumi link`.

| Flag | Meaning |
|---|---|
| `--branch, -b` | Run a specific branch. Default: current branch (or scratch branch if dirty/unpushed). |
| `--commit` | Run a specific commit SHA. |
| `--command, -c` | Override the command in `harumi.toml`. |
| `--kernel, -k` | Override the kernel spec (e.g. `or_python_small`, `gurobi_python_medium`). |
| `--watch, -w` | Block until the run reaches a terminal status. |

With `--watch`, a successful run ends by printing `harumi runs download <run-id>`. (`--output-dir` is deprecated: it still downloads, with a warning.)

**Scratch-branch flow (default when tree is dirty or has unpushed commits):**

The CLI detects local changes, creates a temporary branch `harumi-scratch/<user>/<yyyymmdd-HHMMSS>` from HEAD, commits the full working tree to it using a throwaway git index (the user's real index/HEAD are untouched), pushes it to the `harumi` remote, queues the run, then deletes the remote scratch branch when finished (best-effort cleanup). The user never has to commit manually for a quick iteration.

**Calls:** `POST /projects/{id}/execute` with `{ branch, commit?, command?, kernel_spec? }`, which returns `{execution_log_id, status, workflow_run_id?, project_run_id?}`. With `--watch`, the CLI then polls `GET /projects/{id}/runs/{run_id}` until it reaches a terminal status.

## `runs`

```
harumi runs list [--project ID] [--api-url URL] [--org ORG]
harumi runs get RUN_ID [--project ID] [--api-url URL] [--org ORG]
harumi runs cancel RUN_ID [--project ID] [--api-url URL] [--org ORG]
harumi runs download [RUN_ID] [--output-dir DIR] [--project ID] [--api-url URL] [--org ORG]
```

- **`list`**: `GET /projects/{id}/runs` → table of `id`, `status`, `source`, `git_branch`, `started`, `ended`, newest first.
- **`get`**: `GET /projects/{id}/runs/{run_id}` → detail view plus `stdout`/`stderr`/`error` if present.
- **`cancel`**: `POST /projects/{id}/runs/{run_id}/cancel` on an in-flight run.
- **`download`**: downloads the run's output artifacts as a zip via `GET /projects/{id}/runs/{run_id}/output/archive` (proxied by the API from S3 for current runs, or Gitea for pre-migration runs). With no `RUN_ID` it takes the latest run (`GET /projects/{id}/runs`, newest first); errors if the project has no runs. `--output-dir` defaults to the current directory. Replaces `outputs --download` and `run --output-dir`.

`--project` on every subcommand overrides the `.harumi` binding.

## `compare`

```
harumi compare RUN_ID... [--json] [--project ID] [--api-url URL] [--org ORG]
```

- Compares 2–4 distinct runs (the first is the baseline): `GET /projects/{id}/compare?runs=a,b,c`. Checked before any request is made.
- Prints the project's `[[output.kpi]]` values with the change against the baseline, runtimes, the winner (best value of the primary KPI; or why there is none), whether the runs used the same inputs (`same` / `differs` with the files / `unknown`), and the server's warnings.
- **`--json`**: prints the API's response unchanged, for scripts.

## Editing the repo (git)

There is no `harumi repo`. A project's repo is a plain git repo; `harumi clone` leaves a remote named `harumi` with the token embedded in its URL, so `git fetch harumi` / `git push harumi <branch>` work with no further setup. Before 0.11 each of these was a `harumi repo` subcommand; those still run (hidden, with a warning) until 0.12.0.

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
| Promote into live | `git fetch harumi && git switch main && git merge --ff-only harumi/main && git merge --no-ff harumi/NAME && git push harumi main` |

The remote's URL embeds your Gitea token (kept in `.git/config`, printed by `git remote -v`): never paste either into chat, logs or issues.

Commit once per change and push once; the old file-by-file commands made one server commit per file and never updated the local clone.

## `check`

```
harumi check [--ref REF] [--project ID] [--api-url URL] [--org ORG]
```

`GET /projects/{id}/readiness?ref=` → everything blocking this project from running (repo exists, `harumi.toml` present and valid, kernel allowed), answered up front instead of surfacing as a 4xx from `run`/`schedules add`. Replaces `repo readiness`.

## `dashboard`

```
harumi dashboard widgets [--type TYPE]
harumi dashboard validate [PATH] [--ref REF] [--against FILE | --run RUN_ID | --latest] [--project ID]
```

No backend endpoint — a dashboard spec is a plain file in the project's Gitea repo (edit it in the clone and `git push harumi` like any other file). A project can have several: every `dashboard/<name>.toml` (alphabetical) plus the legacy root `dashboard.toml` (last), each an entry in the platform's dashboard picker. This command group only helps you get their contents right. Full per-type reference and folder layout: [dashboard.md](dashboard.md).

- **`widgets`**: prints the current widget-type contract (required/optional keys, enum values) for all 5 types, or one with `--type`. Sourced from `harumi.dashboard.WIDGET_SCHEMAS`, a hand-maintained mirror of harumi-platform's `schema.ts` (see the `ponytail:` comment in that module) — always current with this CLI version, but can drift from the platform between CLI releases if a new widget type ships there first.
- **`validate`**: parses each dashboard spec the same way the platform's `parseDashboardConfig` does, and reports every widget that would be **dropped** (unknown `type`, missing/invalid required key — e.g. a `valueKey` typo for `value_key`). Exits 1 if any spec drops a widget or isn't valid TOML.
  - With no `PATH`: every `./dashboard/*.toml`, else `./dashboard.toml`. Each filename is printed as a heading when there's more than one.
  - `PATH`: just that one file, wherever it lives.
  - `--ref BRANCH`: the repo's copies instead of local files — lists the tree via `GET /repo/files`, then reads each spec via `GET /repo/file-content`. Never guesses a filename.
  - `--against FILE`: additionally resolves every widget's dot-path keys (`value_key`, `rows_key`, `data_key`, `tasks_key`, etc.) against a local `output.json` and reports any that don't resolve (widget renders empty on the platform, not an error there).
  - `--run RUN_ID` / `--latest`: same dot-path check, but fetches the run's structured output from `GET /projects/{id}/runs/{run_id}/output` (S3-backed for current runs, Gitea for pre-migration runs — resolved transparently by the API) instead of a local file.
  - At most one of `--against`/`--run`/`--latest` may be passed.

## `share`

```
harumi share list [--project ID]
harumi share get LINK_ID [--project ID]
harumi share add [--label TEXT] [--app/--no-app] [--chat/--no-chat] [--run-history/--no-run-history]
                  [--run-control/--no-run-control] [--project ID]
harumi share update LINK_ID [--label TEXT] [--enable/--disable] [--app/--no-app] [--chat/--no-chat]
                     [--run-history/--no-run-history] [--run-control/--no-run-control]
                     [--password/--no-password] [--project ID]
harumi share rm LINK_ID [--yes] [--project ID]
harumi share rotate LINK_ID [--yes] [--project ID]
```

Manages `/projects/{id}/share-links*` — a project's public, unauthenticated dashboard links (read-only view of the project's dashboard specs, with the same picker when there are several, + a chosen run's `output.json`; no login required to view). A project can have several links, each independently revocable, each with its own permissions and optional password.

- **`list`**: `GET /projects/{id}/share-links` → `ProjectShareLinkList {links: [ProjectShareLink]}`. Prints a table with each link's id, label, `enabled`, its enabled permissions, and whether it's password protected.
- **`get`**: same list call, filtered to one link — prints its full viewer URL (built client-side as `{platform_url}/share/{token}`, since the API doesn't know its own public origin) and every permission flag.
- **`add`**: `POST /projects/{id}/share-links` → `ProjectShareLink`. Every permission flag (`--app`, `--chat`, `--run-history`, `--run-control`) defaults to off, so creating a link never silently grants more than a bare read-only, latest-run-only dashboard view.
  - `--app`: let visitors open the project's deployed Streamlit app.
  - `--chat`: read-only assistant for signed-in visitors.
  - `--run-history`: browse past runs instead of only ever the latest.
  - `--run-control`: signed-in visitors can run now, override the kernel, and manage schedules.
- **`update`**: `PATCH /projects/{id}/share-links/{link_id}`. Only the flags you pass are changed; `--enable`/`--disable` toggles the link without touching its permissions.
- **`rm`**: `DELETE /projects/{id}/share-links/{link_id}`. The old URL stops working immediately. Prompts for confirmation unless `--yes`.
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

**`type`** must be one of `postgresql | mysql | sqlserver | oracle`.

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
harumi schedules get SCHEDULE_ID [--project ID]
harumi schedules add --cron CRON --git-branch BRANCH [--start-at ISO] [--git-commit SHA]
                     [--command C] [--kernel K] [--output-format F] [--email-to E] [--project ID]
harumi schedules update SCHEDULE_ID [--cron CRON] [--start-at ISO] [--git-branch B] [--git-commit SHA]
                        [--command C] [--kernel K] [--output-format F] [--email-to E] [--project ID]
harumi schedules rm SCHEDULE_ID [--yes] [--project ID]
```

`--project` on every subcommand overrides the `.harumi` binding.

**`--cron`** is a raw 5-field cron expression (e.g. `"0 9 * * *"`), **interpreted in UTC**. The CLI does not validate it — the server validates with `croniter` and returns **400 Invalid cron expression** on a bad value. There is no calendar/builder UX; pass the cron string directly.

**`--start-at`** is an ISO-8601 datetime; defaults to "now" (UTC) if omitted on `add`.

**`--email-to`** accepts `only-me` | `everyone` | a comma-separated list of email addresses (resolved server-side).

**`add`** calls `POST /projects/{project_id}/schedules` with `{cron, start_at, git_branch, git_commit?, command?, kernel_spec?, output_format?, email_to?}` → `Schedule`.

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
harumi memories add [CONTENT] [--mode always|on_demand] [--project ID]
harumi memories edit MEMORY_ID [--content TEXT] [--mode always|on_demand] [--project ID]
harumi memories rm MEMORY_ID [--yes] [--project ID]
harumi skills library [--category C] [--query TEXT] [--api-url URL] [--org ORG]
harumi skills add SLUG [--force] [--api-url URL] [--org ORG]
```

- **`list`**: `GET /projects/{id}/memories?q=&retrieval_mode=` → a table of `id`, `scope`, `mode`, `content` (truncated) and `updated`. The list also carries memories inherited from the project's organization or the owner's personal workspace (`scope` is `organization` or `user`); those are read-only through the project routes.
- **`add`**: `POST /projects/{id}/memories` with `{content, source: "cli", retrieval_mode}`. `content` is 1-4000 characters; `--mode` defaults to `on_demand`. If `CONTENT` is omitted it is prompted for.
- **`edit`**: `PATCH /projects/{id}/memories/{memory_id}` with only the fields given. At least one of `--content` / `--mode` is required, otherwise the CLI fails before calling the API.
- **`rm`**: `DELETE /projects/{id}/memories/{memory_id}`, prompting for confirmation unless `--yes`.
- **`skills library`**: `GET /skill-library` → the platform's preset skills (`slug`, `name`, `category`, `version`, `description`). `--category` matches exactly; `--query` is a case-insensitive substring of name, description or tags.
- **`skills add SLUG`**: needs a bound clone. `GET /skill-library/{slug}`, then writes `<clone>/skills/<slug>/SKILL.md` with `source: harumi/<slug>` and `version: N` set in the frontmatter (replacing any the preset had), exactly as the platform's Install button does. Refuses when that file exists unless `--force`: if the existing file's `source:` is `harumi/<slug>` it says the preset is already installed, otherwise that the folder belongs to a different skill. It writes the file only and prints the `git add / commit / push harumi` line; the project's assistant loads skills from the live branch.
- A project's *own* skills are plain files at `skills/<slug>/SKILL.md`: create, edit and delete them in the clone with your editor and git. There is no command for that on purpose.

## `agent-skills`

```
harumi agent-skills install [--agent KEY]... [--project] [--dry-run] [--force]
```

Copies the bundled `harumi-cli` and `harumi-cli-setup` skills onto this machine for Cursor, Claude Code, Codex or the universal `~/.agents/skills` folder (auto-detected, or `--agent`). `--project` writes to `./.agents/skills` instead. `--dry-run` prints each `source -> destination` and writes nothing (it replaces `skill path`). Not the same as `harumi skills`, which is about a project's skills. Replaces `skill install`.

## `org`

```
harumi org list [--api-url URL]
harumi org create BUSINESS_NAME [--api-url URL]
harumi org rename ORG_ID BUSINESS_NAME [--api-url URL]
harumi org rm ORG_ID [--yes] [--api-url URL]
harumi org members ORG_ID [--api-url URL]
harumi org invite ORG_ID --email EMAIL [--role ROLE] [--api-url URL]
harumi org role ORG_ID USER_ID --role ROLE [--api-url URL]
harumi org rm-member ORG_ID USER_ID [--yes] [--api-url URL]
harumi org use ORG_ID
```

`--role` is one of `owner | admin | member | viewer`.

- **`list`**: `GET /users/organizations`.
- **`create`**: `POST /users/organizations` with `{business_name}`.
- **`rename`**: `PUT /users/organizations/{id}` with `{business_name}`.
- **`rm`**: `DELETE /users/organizations/{id}`, prompting for confirmation unless `--yes`.
- **`members`**: `GET /users/organizations/{id}/users`.
- **`invite`**: `POST /users/organizations/{id}/users` with `{email, role}`.
- **`role`**: `PUT /users/organizations/{id}/users/{user_id}` with `{role}`.
- **`rm-member`**: `DELETE /users/organizations/{id}/users/{user_id}`, prompting for confirmation unless `--yes`.
- **`use`**: persists `org_id` in `~/.harumi/config.json` (per environment); every later request sends it as `X-Organization`. The header scopes the *read* endpoints (`projects list`); creation reads the workspace from the request body, so `new` and `import` default `customer_id` to this org — pass `--personal` to create in your personal workspace anyway. No network call. Replaces `config set-org`.

## Config & credential files

Environment selection precedence (highest first): **`--env` > `HARUMI_ENV` > saved default (`harumi env use`) > `production`**. See [env](#env) for the environment table.

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
| `Error: Not logged in. Run harumi login first.` | No/expired session | Ask user to run `harumi login` |
| `harumi-api returned HTTP 422: ... Signups not allowed for otp` | New email, no account | Re-run `harumi login --signup` |
| `Provide --project or run from a directory with a .harumi binding` | No `--project` and `.harumi/config.json` missing in cwd + parents | `harumi new`/`harumi clone <ID>` (nothing/existing project) or `harumi import` (existing folder) or `harumi link --project <ID>` (already checked out), or pass `--project` |
| `No Gitea token found. Run harumi login` | `git_token` absent in credentials | `harumi login` again |
| `git push failed: ...` | Network (VPN not connected) or bad credentials | Check VPN; re-run `harumi login` to refresh token |
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

When scripting is better than shelling out:

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

# Blueprints
blueprints = client.list_blueprints()

# Dashboard spec discovery, widget contract + validation
from harumi.dashboard import (
    WIDGET_SCHEMAS,
    local_dashboard_paths,
    pick_dashboard_paths,
    validate_dashboard_toml,
)
for path in local_dashboard_paths(Path(".")):  # dashboard/*.toml, then dashboard.toml
    widgets, issues = validate_dashboard_toml(Path(path).read_text())
# ...or the repo's copies:
paths = pick_dashboard_paths(f.path for f in client.list_repo_files(project_id, ref="main"))

# Public share link
status = client.enable_share(binding.project_id)
print(status.share_token)

# Create a project
project = client.create_project("New Project")  # project.repo is None if unprovisioned
```

`Client(api_url=..., git_url=..., org_id=...)` accepts the same overrides as the CLI flags. Requires the user to have run `harumi login` at least once.

