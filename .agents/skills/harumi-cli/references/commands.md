# harumi CLI — Command Reference

Detailed flag reference, config/credential storage, troubleshooting, and the Python `Client` SDK. Loaded on demand from [SKILL.md](../SKILL.md).

## Contents

- [Auth commands](#auth-commands)
- [env](#env)
- [profile](#profile)
- [config set-org](#config-set-org)
- [specs](#specs)
- [templates](#templates)
- [notebooks](#notebooks)
- [projects](#projects)
- [init](#init)
- [import](#import)
- [run](#run)
- [runs](#runs)
- [outputs](#outputs)
- [repo](#repo)
- [dashboard](#dashboard)
- [share](#share)
- [datasources](#datasources)
- [schedules](#schedules)
- [secrets](#secrets)
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
- On success, stores `access_token`/`refresh_token` at `~/.harumi/credentials.json` (mode `0600`), then calls `POST /git/credentials` to provision a per-user Gitea personal access token (`git_token` + `git_url`), used by `harumi init` and `harumi run` for git-over-HTTPS.
- Best-effort org resolution: if exactly one org, stored automatically; if multiple, prints a table and instructs `harumi config set-org`.

### `harumi logout`

Clears `~/.harumi/credentials.json`. No flags.

### `harumi whoami`

```
harumi whoami [--api-url URL] [--org ORG]
```

Prints the email and id of the currently logged-in user (`GET /users/profile`), plus the active environment.

## `env`

```
harumi env list [--all]
harumi env current
harumi env use NAME
```

Selects the backend environment. Two are built in:

| Env | API | Gitea | Access |
|---|---|---|---|
| `production` (default) | `https://api.harumi.io/api` | `https://git.harumi.io` | public |
| `staging` | `https://api.dev.harumi.io/api` | `https://git.dev.harumi.io` | internal, VPN-only |

- **`list`**: shows selectable environments with the active one flagged. Internal (VPN-only) environments are hidden unless `--all` is passed or `HARUMI_INTERNAL=1` is set.
- **`current`**: shows the active environment and its endpoints.
- **`use NAME`**: persists the default environment in `~/.harumi/config.json`. Warns if the target is internal (VPN required) and if you're not yet logged in on it.

Selection precedence: `--env` (top-level flag) > `HARUMI_ENV` > saved default from `env use` > `production`.

**Each environment has its own Supabase, so each has its own stored session** (`~/.harumi/environments/<env>/credentials.json`). Switching environments never logs you out of the other — but you must `harumi login` at least once per environment. `--api-url`/`--git-url` (and `HARUMI_API_URL`/`HARUMI_GIT_URL`) still override the active environment's endpoints for local development without changing which environment you're on.

Staging is hidden from regular users as a UX convenience only — the real access gate is needing an account in staging's Supabase plus the VPN. Any internal dev can `harumi env use staging` or pass `--env staging`.

## `profile`

```
harumi profile show [--api-url URL] [--org ORG]
harumi profile set [--first-name N] [--last-name N] [--bio TEXT] [--api-url URL] [--org ORG]
```

`show` prints `id`/`email`/`first_name`/`last_name`/`bio` (`GET /users/profile`). `set` sends only the flags you pass as a partial update (`PUT /users/profile`); errors locally with "No fields to update" if none are given.

## `config set-org`

```
harumi config set-org <ORG_ID>
```

Persists `org_id` in `~/.harumi/config.json`; every subsequent request sends it as `X-Organization`.

The header scopes the *read* endpoints (`projects list`, `projects trash`, `notebooks`). Creation reads the workspace from the request body instead, so `projects create` and `import` default `customer_id` to this org — pass `--personal` to create in your personal workspace anyway.

## `specs`

```
harumi specs [--api-url URL] [--org ORG]
```

Lists kernel specs (`name`, `display_name`, `cpu`, `memory`, `subscription_required`) from `GET /sandbox/specs`. `name` is what you pass to `run --kernel`.

## `templates`

```
harumi templates [--api-url URL] [--org ORG]
```

Lists project templates (`id`, `slug`, `name`, `description`) from `GET /templates`. Pass a template's `id` as `projects create --template-id`.

## `notebooks`

```
harumi notebooks [--project PROJECT_ID] [--api-url URL] [--org ORG]
```

Lists every project and its notebooks (legacy notebook-centric view; most projects have exactly one). Useful for finding a `PROJECT_ID`, though `harumi projects list` is the more direct way to do that today.

## `projects`

```
harumi projects create NAME [--customer-id ID] [--personal] [--template-id ID] [--bind/--no-bind]
                       [--api-url URL] [--git-url URL] [--org ORG]
harumi projects list [--api-url URL] [--org ORG]
harumi projects get PROJECT_ID [--api-url URL] [--org ORG]
harumi projects rename PROJECT_ID NAME [--api-url URL] [--org ORG]
harumi projects delete PROJECT_ID [--yes] [--api-url URL] [--org ORG]
```

- **`create`**: `POST /projects`, then `GET /projects/{id}/repo` to fetch the Gitea repo and (unless `--no-bind`) bind the current directory the same way `harumi init` does. If the repo fetch 404s (Harumi Git not configured for this backend), the project is still created — the CLI prints a warning and skips binding instead of failing. The project is created in the configured org (`config set-org` / `--org` / `HARUMI_ORG`) unless you pass `--customer-id` to pick a different one or `--personal` to create it in your personal workspace; the command prints which workspace it landed in.
- **`list`**: `GET /projects` → table of `id`, `name`, `kernel_spec`, `role`.
- **`get`**: `GET /projects/{id}` → detail view.
- **`rename`**: `PUT /projects/{id}` with `{name}`.
- **`delete`**: `DELETE /projects/{id}`. Prompts you to type the exact project name to confirm unless `--yes`.

## `init`

```
harumi init --project PROJECT_ID [--api-url URL] [--git-url URL] [--org ORG]
```

**Run once per project directory.** Binds the current working directory to a Harumi project:

1. Calls `GET /projects/{id}/repo`.
2. Writes `.harumi/config.json` in the current directory with `project_id` + repo metadata.
3. Configures the `harumi` git remote with an authenticated HTTPS URL (requires a `git_token` in credentials from `harumi login` and the repo to be a git working tree).

After `harumi init`, `harumi run`, `harumi runs`, `harumi repo`, `harumi outputs`, `harumi datasources`, `harumi schedules`, and `harumi secrets` all work without any `--project` flag.

**Note:** `.harumi/config.json` is searched upward from cwd, so these commands work from subdirectories.

## `import`

```
harumi import [PATH] [--project-name NAME] [--from-git URL] [--bind/--no-bind] [--personal]
              [--api-url URL] [--git-url URL] [--org ORG]
```

Turns a downloaded/unzipped project export (e.g. from the web app's "Download
project" button) into a brand-new git-based Harumi project. `PATH` defaults to
the current directory and **must be a directory** — unzip the export first
(`import` fails with `Not a directory: <path>` on a `.zip`).

| Flag | Meaning |
|---|---|
| `PATH` | Folder to import (positional). Default: current directory. |
| `--project-name` | Name for the new project. Default: the folder's name. |
| `--from-git` | Also clone this git URL (e.g. the project's old GitHub repo) flat into the folder — `.git` stripped, files copied alongside the exported ones — before importing. On a filename collision the exported file wins; the CLI warns and lists the first few colliding paths. |
| `--bind / --no-bind` | Bind the folder to the new project afterward, like `harumi init`. Default: `--bind`. |
| `--personal` | Create the project in your personal workspace, ignoring the configured org. |

Sequence:

1. `POST /projects` to create the project (`name` = `--project-name` or the folder name), in the configured org unless `--personal` is passed.
2. If `--from-git` is set, shallow-clones that URL into a temp dir and copies its tree (minus `.git`) flat into the folder first.
3. If the backend didn't provision a Gitea repo for the project (`project.repo is None`), prints a warning and stops — nothing is pushed, no binding happens.
4. Otherwise, requires a Gitea token from `harumi login` (prints a warning and stops if missing — never fails hard), then commits and pushes the **entire folder** as one commit ("Import project") to the new repo's default branch.
5. If the folder contains a `HARUMI_IMPORT.md` (part of the export, with follow-ups like re-adding datasource credentials or the old GitHub URL), prints a pointer to it.
6. Unless `--no-bind`, binds the folder the same way `harumi init` does.

## `run`

```
harumi run [--branch B] [--commit SHA] [--command C] [--kernel K]
           [--watch] [--output-dir DIR]
           [--api-url URL] [--git-url URL] [--org ORG]
```

Requires the directory (or a parent) to be bound via `harumi init`.

| Flag | Meaning |
|---|---|
| `--branch, -b` | Run a specific branch. Default: current branch (or scratch branch if dirty/unpushed). |
| `--commit` | Run a specific commit SHA. |
| `--command, -c` | Override the command in `harumi.toml`. |
| `--kernel, -k` | Override the kernel spec (e.g. `or_python_small`, `gurobi_python_medium`). |
| `--watch, -w` | Block until the run reaches a terminal status. |
| `--output-dir, -o` | With `--watch`: download output zip here on success. |

**Scratch-branch flow (default when tree is dirty or has unpushed commits):**

The CLI detects local changes, creates a temporary branch `harumi-scratch/<user>/<yyyymmdd-HHMMSS>` from HEAD, commits the full working tree to it using a throwaway git index (the user's real index/HEAD are untouched), pushes it to the `harumi` remote, queues the run, then deletes the remote scratch branch when finished (best-effort cleanup). The user never has to commit manually for a quick iteration.

**Calls:** `POST /projects/{id}/execute` with `{ branch, commit?, command?, kernel_spec? }`, which returns `{execution_log_id, status, workflow_run_id?, project_run_id?}`. With `--watch`, the CLI then polls `GET /projects/{id}/runs/{run_id}` until it reaches a terminal status.

## `runs`

```
harumi runs list [--project ID] [--api-url URL] [--org ORG]
harumi runs get RUN_ID [--project ID] [--api-url URL] [--org ORG]
harumi runs cancel RUN_ID [--project ID] [--api-url URL] [--org ORG]
```

- **`list`**: `GET /projects/{id}/runs` → table of `id`, `status`, `source`, `git_branch`, `started`, `ended`, newest first.
- **`get`**: `GET /projects/{id}/runs/{run_id}` → detail view plus `stdout`/`stderr`/`error` if present.
- **`cancel`**: `POST /projects/{id}/runs/{run_id}/cancel` on an in-flight run.

`--project` on every subcommand overrides the `.harumi` binding.

## `outputs`

```
harumi outputs [--project ID] [--latest] [--download RUN_ID] [--output-dir DIR]
               [--api-url URL] [--org ORG]
```

Deprecated alias kept for backwards compatibility — prefer `harumi runs` for new usage.

- `--project` optional if run from a bound directory.
- No extra flags: table of all runs (`id`, `status`, `started`, `ended`, `git_branch`).
- `--latest`: only the most recently started run.
- `--download <run_id> [--output-dir DIR]`: downloads the run's output artifacts as a zip via `GET /projects/{id}/runs/{run_id}/output/archive` (proxied by the API from S3 for current runs, or Gitea for pre-migration runs — transparently to the caller).

## `repo`

```
harumi repo ls [--ref REF] [--project ID] [--api-url URL] [--org ORG]
harumi repo dir [PATH] [--ref REF] [--project ID] [--api-url URL] [--org ORG]
harumi repo cat PATH [--ref REF] [--output FILE] [--project ID]
harumi repo put LOCAL_PATH REPO_PATH [-m MSG] [--branch B] [--project ID]
harumi repo rm PATH [-m MSG] [--branch B] [--yes] [--project ID]
harumi repo mv FROM TO [-m MSG] [--branch B] [--project ID]
harumi repo download -o OUT.zip [--path DIR] [--ref REF] [--project ID]
harumi repo branches [--project ID]
harumi repo branch-create NAME [--from BRANCH] [--project ID]
harumi repo branch-rm NAME [--yes] [--project ID]
harumi repo promote NAME [--title T] [--delete-after] [--project ID]
```

Real endpoints on harumi-api's git router. All writes go through the batch `POST /projects/{id}/repo/changes` endpoint, so every `put`/`rm`/`mv` is exactly one commit.

- **`ls`**: `GET /projects/{id}/repo/files[?ref=]` → flat, recursive file list.
- **`dir`**: `GET /projects/{id}/repo/dir?path=&ref=` → one folder level (GitHub-style repo browser: immediate children only, each with its last commit, plus the branch's latest commit/total commit count). Use `ls` instead for a flat, whole-repo listing.
- **`cat`**: `GET /projects/{id}/repo/file-content?path=...[&ref=]`, base64-decodes `content`. Prints to stdout, or writes bytes to `--output` (required for binary files — the CLI refuses to print non-UTF-8 content without `--output`).
- **`put`**: probes `get_repo_file` first to decide `create` vs `update`, then sends one `repo/changes` operation with base64-encoded file content.
- **`rm`**: sends a `delete` operation for the path (file or folder — deletes everything under a folder prefix). Prompts for confirmation unless `--yes`.
- **`mv`**: sends a `move` operation (`from_path` → `path`).
- **`download`**: `GET /projects/{id}/repo/archive?path=&ref=`, streamed to the `--output` zip path.
- **`branches`**: `GET /projects/{id}/repo/branches` → table with the live branch flagged.
- **`branch-create`**: `POST /projects/{id}/repo/branches` with `{name, from_branch?}`.
- **`branch-rm`**: `DELETE /projects/{id}/repo/branches/{name}`. Refuses (server-side) to delete the live branch.
- **`promote`**: `POST /projects/{id}/repo/branches/{name}/promote` with `{title?, delete_after}`; merges the version into the live branch. On a merge conflict the response's `conflict=true` and the CLI surfaces `message` as an error instead of a fake success.

`--project` on every subcommand overrides the `.harumi` binding.

## `dashboard`

```
harumi dashboard widgets [--type TYPE]
harumi dashboard validate [PATH] [--ref REF] [--against FILE | --run RUN_ID | --latest] [--project ID]
```

No backend endpoint — a dashboard spec is a plain file in the project's Gitea repo (read/write it with `repo cat`/`repo put` like any other file). A project can have several: every `dashboard/<name>.toml` (alphabetical) plus the legacy root `dashboard.toml` (last), each an entry in the platform's dashboard picker. This command group only helps you get their contents right. Full per-type reference and folder layout: [dashboard.md](dashboard.md).

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
harumi share add [--label TEXT] [--chat/--no-chat] [--run-history/--no-run-history]
                  [--run-control/--no-run-control] [--io-control/--no-io-control] [--project ID]
harumi share update LINK_ID [--label TEXT] [--enable/--disable] [--chat/--no-chat]
                     [--run-history/--no-run-history] [--run-control/--no-run-control]
                     [--io-control/--no-io-control] [--project ID]
harumi share remove LINK_ID [--yes] [--project ID]
harumi share rotate LINK_ID [--yes] [--project ID]
harumi share set-password LINK_ID [--project ID]
harumi share rm-password LINK_ID [--project ID]
```

Manages `/projects/{id}/share-links*` — a project's public, unauthenticated dashboard links (read-only view of the project's dashboard specs, with the same picker when there are several, + a chosen run's `output.json`; no login required to view). A project can have several links, each independently revocable, each with its own permissions and optional password.

- **`list`**: `GET /projects/{id}/share-links` → `ProjectShareLinkList {links: [ProjectShareLink]}`. Prints a table with each link's id, label, `enabled`, its enabled permissions, and whether it's password protected.
- **`get`**: same list call, filtered to one link — prints its full viewer URL (built client-side as `{platform_url}/share/{token}`, since the API doesn't know its own public origin) and every permission flag.
- **`add`**: `POST /projects/{id}/share-links` → `ProjectShareLink`. Every permission flag (`--chat`, `--run-history`, `--run-control`, `--io-control`) defaults to off, so creating a link never silently grants more than a bare read-only, latest-run-only dashboard view.
  - `--chat`: read-only assistant for signed-in visitors.
  - `--run-history`: browse past runs instead of only ever the latest.
  - `--run-control`: signed-in visitors can run now, override the kernel, and manage schedules.
  - `--io-control`: control/edit inputs and outputs.
- **`update`**: `PATCH /projects/{id}/share-links/{link_id}`. Only the flags you pass are changed; `--enable`/`--disable` toggles the link without touching its permissions.
- **`remove`**: `DELETE /projects/{id}/share-links/{link_id}`. The old URL stops working immediately. Prompts for confirmation unless `--yes`.
- **`rotate`**: `POST /projects/{id}/share-links/{link_id}/rotate` — invalidates the current token and mints a new one; permission flags are unchanged. Prompts for confirmation unless `--yes`.
- **`set-password`**: prompts for a password (hidden input, server-enforced 8–200 chars), then `PUT /projects/{id}/share-links/{link_id}/password`. Changing the password invalidates every previously issued unlock session for that link — viewers must re-enter it.
- **`rm-password`**: `DELETE /projects/{id}/share-links/{link_id}/password`. That link becomes freely viewable (no password prompt).

`--project` on every subcommand overrides the `.harumi` binding.


## `datasources`

Real endpoints, live today (`harumi-api/src/api/datasources/router.py`). Scoped per-project by `(project_id, name)`.

```
harumi datasources list [--project ID] [--api-url URL] [--org ORG]
harumi datasources get NAME [--project ID]
harumi datasources add NAME --type TYPE [--host H] [--port P] [--database D] [--username U]
                            [--use-proxy] [--proxy-host H] [--proxy-port P] [--proxy-server-name N]
                            [--project ID]
harumi datasources update NAME [--name NEW_NAME] [--type T] [--host H] [--port P] [--database D]
                               [--username U] [--set-credentials] [--use-proxy/--no-use-proxy]
                               [--proxy-host H] [--proxy-port P] [--proxy-server-name N] [--project ID]
harumi datasources remove NAME [--yes] [--project ID]
harumi datasources test --type TYPE --host H --port P --database D --username U
                        [--use-proxy] [--proxy-host H] [--proxy-port P] [--proxy-server-name N]
harumi datasources query NAME --sql "SELECT ..." [--limit N] [--csv PATH] [--project ID]
```

`--project` on every subcommand overrides the `.harumi` binding.

**Credentials are always prompted, never a flag.** `add`, `update --set-credentials`, and `test` each prompt with `typer.prompt(hide_input=True)`. This is deliberate — secrets must never land in shell history, process listings, or `--help` output. The server stores credentials in AWS SSM (SecureString) and never returns them; `get`/`list` responses have no credential field.

**`type`** must be one of `postgresql | mysql | sqlserver | oracle`.

**`add`** calls `POST /datasources/{project_id}` — the backend tests the connection before persisting, so a bad host/credentials fails the `add` with the same error `test` would surface.

**`update`** calls `PUT /datasources/{project_id}/{name}` with only the fields you pass (partial update). Passing `--name` renames the datasource (and its SSM parameter, server-side). Omit `--set-credentials` to leave the stored credentials untouched.

**`remove`** calls `DELETE /datasources/{project_id}/{name}`, prompting for confirmation unless `--yes`. Deletes the DB row and the SSM parameter.

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
harumi schedules remove SCHEDULE_ID [--yes] [--project ID]
```

`--project` on every subcommand overrides the `.harumi` binding.

**`--cron`** is a raw 5-field cron expression (e.g. `"0 9 * * *"`), **interpreted in UTC**. The CLI does not validate it — the server validates with `croniter` and returns **400 Invalid cron expression** on a bad value. There is no calendar/builder UX; pass the cron string directly.

**`--start-at`** is an ISO-8601 datetime; defaults to "now" (UTC) if omitted on `add`.

**`--email-to`** accepts `only-me` | `everyone` | a comma-separated list of email addresses (resolved server-side).

**`add`** calls `POST /projects/{project_id}/schedules` with `{cron, start_at, git_branch, git_commit?, command?, kernel_spec?, output_format?, email_to?}` → `Schedule`.

**`update`** calls `PUT /projects/{project_id}/schedules/{schedule_id}` with only the fields you pass (partial update); errors locally with "No fields to update" if no flags are given.

**`remove`** calls `DELETE /projects/{project_id}/schedules/{schedule_id}`, prompting for confirmation unless `--yes`. **This is the only way to stop a schedule** — there is no pause/enable flag.

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

## `org`

```
harumi org list [--api-url URL]
harumi org create BUSINESS_NAME [--api-url URL]
harumi org rename ORG_ID BUSINESS_NAME [--api-url URL]
harumi org delete ORG_ID [--yes] [--api-url URL]
harumi org members ORG_ID [--api-url URL]
harumi org invite ORG_ID --email EMAIL [--role ROLE] [--api-url URL]
harumi org role ORG_ID USER_ID --role ROLE [--api-url URL]
harumi org remove ORG_ID USER_ID [--yes] [--api-url URL]
```

`--role` is one of `owner | admin | member | viewer`.

- **`list`**: `GET /users/organizations`.
- **`create`**: `POST /users/organizations` with `{business_name}`.
- **`rename`**: `PUT /users/organizations/{id}` with `{business_name}`.
- **`delete`**: `DELETE /users/organizations/{id}`, prompting for confirmation unless `--yes`.
- **`members`**: `GET /users/organizations/{id}/users`.
- **`invite`**: `POST /users/organizations/{id}/users` with `{email, role}`.
- **`role`**: `PUT /users/organizations/{id}/users/{user_id}` with `{role}`.
- **`remove`**: `DELETE /users/organizations/{id}/users/{user_id}`, prompting for confirmation unless `--yes`.

## Config & credential files

Environment selection precedence (highest first): **`--env` > `HARUMI_ENV` > saved default (`harumi env use`) > `production`**. See [env](#env) for the environment table.

Within the active environment, URL/org overrides (highest first): **CLI flags > env vars > per-env `config.json` > the environment's built-in endpoints**.

| Setting | Env var | Per-env config key | Default (per environment) |
|---|---|---|---|
| API base URL | `HARUMI_API_URL` | `api_url` | environment's `api_url` |
| Gitea URL | `HARUMI_GIT_URL` | `git_url` | environment's `git_url` |
| Org id (`X-Organization`) | `HARUMI_ORG` | `org_id` | none (from login) |

- `~/.harumi/config.json` — global; stores only the selected `environment`.
- `~/.harumi/environments/<env>/credentials.json` — per-environment `access_token`, `refresh_token`, `git_token`, `git_url`, `user_id`, `email`, `expires_at`; mode `0600`.
- `~/.harumi/environments/<env>/config.json` — per-environment `org_id` (and any local `api_url`/`git_url` overrides).
- `.harumi/config.json` (per-project) — `project_id`, `repo.owner/name/clone_url/default_branch`; written by `harumi init` / `harumi projects create`, searched upward from cwd.
- Override the home dir with `HARUMI_HOME`.
- **Upgrading from a pre-environments install:** the old flat `~/.harumi/credentials.json` + `config.json` are migrated automatically into the `production` environment on first run.

## Troubleshooting

| Symptom / error | Cause | Fix |
|---|---|---|
| `Error: Not logged in. Run harumi login first.` | No/expired session | Ask user to run `harumi login` |
| `harumi-api returned HTTP 422: ... Signups not allowed for otp` | New email, no account | Re-run `harumi login --signup` |
| `Provide --project or run from a directory with a .harumi binding` | No `--project` and `.harumi/config.json` missing in cwd + parents | `harumi init --project <ID>` or pass `--project` |
| `No Gitea token found. Run harumi login` | `git_token` absent in credentials | `harumi login` again |
| `git push failed: ...` | Network (VPN not connected) or bad credentials | Check VPN; re-run `harumi login` to refresh token |
| `git not found` | git missing from PATH | Install git |
| `Run ended with status: failed` | Solver code raised or exited non-zero | `harumi runs get <RUN_ID>` for stdout/stderr/error |
| `No run id returned; cannot watch this run` | Backend didn't return a `project_run_id` | Check `harumi runs list` manually |
| `harumi-api returned HTTP 403: Only SELECT queries are allowed ...` | `datasources query` SQL contains a destructive keyword or doesn't start with SELECT/WITH | Rewrite the query as a read-only SELECT/WITH |
| `harumi-api returned HTTP 404: ...` (on `datasources`/`repo`/`schedules`/`secrets` get/update/remove) | Resource name/id doesn't exist for this project | List the resource first to check the exact name/id |
| `[yellow]Result was truncated at the server-side row cap` | Query returned more rows than `--limit` (or the 100000 hard max) | Narrow the query (add a `WHERE`/`LIMIT`) or raise `--limit` |
| `No fields to update.` | An `update`/`set` command called with no flags | Pass at least one field flag |
| `harumi-api returned HTTP 400: Invalid cron expression: ...` | `schedules add/update --cron` failed server-side `croniter` validation | Fix the cron string (5 fields: minute hour day month weekday) |
| `{path!r} is not valid UTF-8 text.` | `repo cat` on a binary file without `--output` | Re-run with `--output <local_path>` |
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

# Templates
templates = client.list_templates()

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

