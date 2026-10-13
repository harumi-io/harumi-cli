---
name: harumi-cli
description: Guide for using the `harumi` CLI to run local optimization/solver code (Gurobi, OR-Tools, plain Python) on Harumi's infrastructure via the project's self-hosted Gitea repo, to select the backend environment (production vs internal staging), start a new project, import an existing folder of code as a new project, clone an existing project locally, bind a directory to a project, edit the project's repo with git in a clone, install preset skills from the Harumi skill library, inspect, cancel and download runs, manage the project's dashboard widgets and public share link, manage datasources, schedules, secrets, organizations, and your profile. Use when the user wants to run, import, or debug a local script against a Harumi project, mentions `harumi start`, `harumi new`, `harumi import`, `harumi clone`, `harumi link`, `harumi run`, `harumi runs`, `harumi check`, `harumi dashboard`, `harumi share`, `harumi blueprints`, `harumi env`, `harumi projects`, `harumi datasources`, `harumi schedules`, `harumi secrets`, `harumi memories`, `harumi skills`, `harumi agent-skills`, `harumi org`, `harumi profile`, asks about switching between production and staging, creating a new Harumi project, turning an existing local folder/repo into a Harumi project, getting an existing Harumi project's code onto their machine, Harumi kernel specs, project blueprints, Gitea remotes, scratch branches, database connections, running SQL queries against a project datasource, scheduling/cron runs, managing environment variables/secrets, organization members, dashboard widgets (metric/table/line-chart/bar-chart/gantt-chart), `dashboard.toml`, `dashboard/*.toml`, multiple dashboards per project, the dashboard picker, `output.json`, what components/widgets the platform dashboard can render, sharing/publishing a project's dashboard publicly, or needs to fetch/download results or files from a Harumi run/repo.
---

# Harumi CLI

Drives the `harumi` CLI (source: `harumi-cli`, package `harumi`). Every run is git-ref based: code lives in a per-project Harumi Git (Gitea) repo. The CLI auto-manages a scratch branch so users can iterate without manually committing.

The CLI targets one of two environments (see [Config & environments](#config--environments)): `production` (default) and `staging` (internal, VPN-only, `git.dev.harumi.io`). Each has its own Supabase, so each has its own login. The git-first `run` flow currently only works on `staging` — production Gitea (`git.harumi.io`) is not live yet, so on production only the non-git commands (`projects`/`datasources`/`secrets`/`org`/`profile`) work.

For the full flag-by-flag reference and troubleshooting table, see [references/commands.md](references/commands.md).

## VPN requirement

The staging endpoints (`api.dev.harumi.io`, `git.dev.harumi.io`) are internal ALBs — anything on the `staging` environment (login, `harumi new`/`import`/`clone`/`link`, git push, runs) only works over VPN. Surface a clear network error if the user isn't on VPN.

## Preflight

1. Check CLI is installed: `harumi --version`. Not installed, not on PATH, or `Not logged in. Run harumi login first.` on any command? Switch to the **`harumi-cli-setup`** skill — it covers install method selection, environment selection, the interactive OTP login (never automate this), and diagnosing a broken install. Come back here once `harumi whoami` succeeds.
2. `harumi login` also provisions a per-user Gitea token via `POST /api/git/credentials`, stored in `~/.harumi/environments/<env>/credentials.json`.

## The always-bound-repo invariant

Every `harumi run` (and every command that accepts `--project`) requires either an explicit `--project <ID>`, or the working directory (or a parent) to be bound to a Harumi project — which `harumi new`/`harumi import`/`harumi clone`/`harumi link` all do. Without either, the command exits with a clear error pointing at those verbs.

## Onboarding — three journeys, three commands

Every project starts one of three ways. Pick the command that matches what the user has, or run `harumi start` to ask them interactively and dispatch to it.

| User has | Command | What it does |
|---|---|---|
| Nothing yet | `harumi new <NAME>` | Creates the project, then clones its seeded scaffold repo locally and binds it. |
| A local folder of existing code | `harumi import [PATH]` | Creates a project, then force-pushes the folder as its first commit and binds it. |
| A project that already exists in Harumi (web app, teammate) | `harumi clone <PROJECT_ID>` | Fetches its repo into a local directory and binds it. |

```bash
harumi start                       # interactive: asks which of the three, then dispatches
harumi new "My Project" [--dir PATH] [--blueprint SLUG] [--customer-id ID]
harumi import [PATH] [--project-name NAME] [--from-git URL]
harumi clone <PROJECT_ID> [--dir PATH]
```

All three leave the working directory `cd`-able and immediately `harumi run`-able — no separate bind step. `harumi import` overwrites whatever scaffold the backend seeded, so if the folder has no `harumi.toml` the CLI writes a minimal one before pushing.

**`harumi link`** is the narrower primitive underneath `clone`: it binds the *current* directory to a project (writes `.harumi/config.json`, configures the `harumi` git remote) without cloning anything. Use it only for the rare case where the project's code is already checked out by hand.

```bash
harumi link --project <PROJECT_ID>
```

**`harumi new <NAME> --no-clone`** creates a project via the API but does *not* touch the local directory or git — it's for scripting/automation. Plain `harumi new` does the same creation step, then clones the result.

> **`harumi import` creates a NEW project.** It is not how you send edits to a project that already exists — that is `git push harumi <branch>` from the clone. Never run `import` on a folder that is already a clone.

Find project IDs with `harumi projects list`. Run `harumi blueprints` to list available `--blueprint` values for `harumi new`.

## Run and inspect code

### 1. Run code

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
harumi run --watch
harumi runs download -o ./out            # the finished run's output (the latest run when no id is given)
```

### 2. Inspect and manage runs

```bash
harumi runs list                          # table of runs for the bound project, newest first
harumi runs get <RUN_ID>                  # status, git ref, exit code, stdout/stderr/error
harumi runs cancel <RUN_ID>               # cancel an in-flight run
harumi runs download [RUN_ID] [-o DIR]    # download a run's committed output as a zip (latest run by default)
harumi check [--ref REF]                  # server-side readiness: repo exists, harumi.toml valid, kernel allowed
```

## Editing the project

A project's repo is an ordinary git repo, and `harumi clone` leaves a remote named **`harumi`** with the token already in its URL. There is no `harumi repo` command: use the shell and git.

```bash
ls skills/ ; cat harumi.toml                          # read
git fetch harumi && git show harumi/<branch>:<path>   # read another branch
git ls-files                                          # list files
$EDITOR main.py                                       # edit in place
git add -A && git commit -m "Update model" && git push harumi main   # one commit, however many files
git switch -c feature-x && git push -u harumi feature-x               # a branch ("version")
git switch main && git merge --no-ff feature-x && git push harumi main  # promote it into live
git push harumi --delete feature-x                    # delete it (check `harumi schedules list` first: a schedule can pin a branch)
git log --oneline -- <path>                           # history
git archive --format=zip -o repo.zip harumi/main      # zip the repo
```

- Commit once and push with `git push harumi <branch>`; do not recreate commits file by file.
- To iterate you often don't need to commit at all: `harumi run` pushes uncommitted work to a scratch branch.
- Run `harumi check` to see what blocks the project from running.
- Never run `harumi import` on an existing project (it creates a new one).
- The `harumi` remote's URL embeds the user's Gitea token (stored in `.git/config`, printed by `git remote -v`). Never paste `git remote -v` output or `.git/config` into chat, logs, issues or commits.

## Dashboards & widgets

A project renders **one dashboard per repo-committed spec**: every `dashboard/<name>.toml` (alphabetical) plus the legacy root `dashboard.toml` (shown last). Specs are ordinary files in the clone (edit, commit, `git push harumi`), and bound by dot-path keys to `output/output.json` — the file a run writes. With more than one spec the platform shows a **dashboard picker** above the run picker, on both the project page and the public share link; with exactly one there's no picker. Each spec's picker label is its optional top-level `title`, falling back to a prettified filename. Five widget types exist: `metric`, `table`, `line-chart`, `bar-chart`, `gantt-chart`. Full per-type key reference, TOML examples, matching `output.json` shapes, and the folder layout: [references/dashboard.md](references/dashboard.md).

```bash
harumi dashboard widgets [--type metric]              # print the widget-type reference (always current)
harumi dashboard validate [PATH] [--ref BRANCH]        # validate every dashboard spec against the contract
                          [--against FILE | --run ID | --latest]  # + check dot-paths against real output.json
```

**Critical:** the platform's parser never errors on a bad widget — an unknown `type`, a missing required key, or a renamed key (e.g. `valueKey` instead of `value_key`) makes it **silently drop that widget** from the dashboard, and a widget whose dot-path doesn't resolve just renders empty. Always run `harumi dashboard validate` (with `--latest` if a run already exists) before committing, and after any edit made on the user's behalf — it's the only point in the toolchain that fails loudly instead of shipping a dashboard that's quietly missing a widget. With no `PATH` it checks every spec at once, so adding a second dashboard doesn't mean remembering to validate it separately.

A project with no spec at all still renders a generic default dashboard rather than erroring. `harumi new` seeds a starter root `dashboard.toml` server-side; `harumi import` does not (it overwrites the scaffold, and only writes a minimal `harumi.toml` — not a dashboard spec), so a pushed project has none until one is committed. To add a second dashboard, commit `dashboard/<name>.toml` — the existing root file keeps working alongside it.

## Share the dashboard publicly

`harumi share` manages a project's public, unauthenticated dashboard link — a read-only view of the project's dashboard specs (with the same picker when there are several) + a chosen run's `output.json`, with no login required.

```bash
harumi share list                                  # a project can have several links
harumi share get <LINK_ID>                         # full URL and permissions
harumi share add [--label L] [--app] [--chat] [--run-history] [--run-control]
harumi share update <LINK_ID> [--label L] [--enable/--disable] [--app/--no-app] [--chat/--no-chat] \
                    [--run-history/--no-run-history] [--run-control/--no-run-control]
harumi share update <LINK_ID> --password           # prompts for a password (hidden, min 8 chars)
harumi share update <LINK_ID> --no-password        # remove it: the link becomes freely viewable
harumi share rotate <LINK_ID>                      # invalidate the current URL, mint a new one
harumi share rm <LINK_ID>                          # delete it for good
```

Rotating the link or changing its password invalidates every previously unlocked viewer session — they must re-enter the (new) password. All `share` subcommands accept `--project` to override the `.harumi` binding.

## Manage datasources

`harumi datasources` manages project-scoped database connections. Credentials are **always prompted interactively with hidden input** — the CLI never accepts them as a flag, and the server never returns them back (stored in AWS SSM).

```bash
harumi datasources list                                   # table of datasources for the bound project
harumi datasources get <name>                              # detail view (no credentials)
harumi datasources add <name> --type postgresql --host ... --port 5432 --database ... --username ...
harumi datasources update <name> --host newhost --set-credentials
harumi datasources rm <name>
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
harumi schedules rm <SCHEDULE_ID>
```

Key semantics:

- **Cron is a raw 5-field expression, interpreted in UTC.** The CLI does not validate it client-side — the server validates with `croniter` and returns a clear 400 on a bad expression. Don't try to build a calendar/human-friendly UI; just pass the cron string.
- **No pause/enable flag exists.** The only way to stop a schedule from firing is `harumi schedules rm`.
- **No separate "run now" for a schedule.** Immediate execution is already covered by `harumi run` (git-ref based); schedules only control recurring runs.

All `schedules` subcommands accept `--project` to override the `.harumi` binding.

## Manage secrets

`harumi secrets` manages project-scoped environment variables, injected into kernels/apps. Values are stored as SSM SecureStrings; there is no update endpoint — `set` on an existing name overwrites it.

```bash
harumi secrets list                 # names only, never values
harumi secrets set <NAME>           # prompts for the value (hidden input)
harumi secrets rm <NAME>
```

## Manage project memories and skills

`harumi memories` manages the project's memories: short notes the project's chat remembers. `always` memories are loaded into every chat; `on_demand` ones (the default) are only retrieved when relevant.

```bash
harumi memories list [--query TEXT] [--mode always|on_demand]   # includes read-only memories inherited from the org / personal workspace
harumi memories add "Use metric units" [--mode always]          # omit the text to be prompted
harumi memories edit <MEMORY_ID> [--content TEXT] [--mode always|on_demand]
harumi memories rm <MEMORY_ID>
```

- Inherited rows (scope `organization` or `user`) are listed but can't be edited or removed here.
- `edit` needs at least one of `--content` / `--mode`.

### Project skills

A project's skills are plain files in its repo at `skills/<slug>/SKILL.md`, so create, edit and delete them in the clone with your editor and git (see [Editing the project](#editing-the-project)). The only skills commands that do something a file edit can't are about the platform's **skill library** of presets:

```bash
harumi skills library [--category C] [--query TEXT]   # list the presets (slug, name, category, version)
harumi skills add <SLUG> [--force]                    # write skills/<SLUG>/SKILL.md into the clone, with source/version provenance
git add skills/<SLUG> && git commit -m "Install <SLUG> skill" && git push harumi main   # publish it
```

`skills add` only writes the file; the project's assistant loads skills from the live branch, so commit and push. It refuses to overwrite an existing `skills/<slug>/SKILL.md` without `--force`.

Not to be confused with `harumi agent-skills install`, which installs the bundled `harumi-cli` agent skills onto the user's machine.

## Manage organizations and your profile

```bash
harumi org list
harumi org create <BUSINESS_NAME>
harumi org rename <ORG_ID> <NEW_NAME>
harumi org rm <ORG_ID>
harumi org members <ORG_ID>
harumi org invite <ORG_ID> --email a@b.com --role member
harumi org role <ORG_ID> <USER_ID> --role admin
harumi org rm-member <ORG_ID> <USER_ID>
harumi org use <ORG_ID>                # persist the org sent as X-Organization

harumi whoami                          # profile, environment and active org
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
harumi env use staging     # persist the default (internal devs, VPN required)
harumi --env staging run   # override for a single command
```

Staging is **internal-only**: it's hidden from `env list`/help for regular users, but the real gate is needing a staging Supabase account and the VPN — anyone internal can `harumi env use staging` (or pass `--env staging`).

Environment selection precedence: `--env` > `HARUMI_ENV` > `harumi env use` (saved default) > `production`.

- Per-command URL overrides: `--api-url` / `HARUMI_API_URL`, `--git-url` / `HARUMI_GIT_URL` (e.g. for a local harumi-api). These override the active environment's endpoints without changing which env you're on.
- Org: `harumi org use <ORG_ID>` / `--org` / `HARUMI_ORG` (scoped per environment). Sent as `X-Organization` to scope reads, and used as the owning workspace when creating a project — `--personal` on `new`/`import` opts out.
- Stored under `~/.harumi/`: global `config.json` (just the selected environment) + per-env `environments/<env>/{credentials,config}.json`. Override the home dir with `HARUMI_HOME`.

## Updates

`harumi whats-new` shows what changed in the installed version (`--all` for history). A one-line "new version available" notice may appear on stderr (at most once a day, interactive terminals only); it is not an error. Set `HARUMI_NO_UPDATE_CHECK=1` to silence it, and upgrade with the command the notice prints (`pipx upgrade harumi`, `uv tool upgrade harumi`, or `python3 -m pip install --upgrade harumi`).

## Renamed in 0.11

The old names still run in 0.11.x (hidden, with a `deprecated:` line naming the replacement) and are removed in 0.12.0. Prefer the new ones: `push` -> `import`, `projects create` -> `new --no-clone`, `outputs` / `run --output-dir` -> `runs download`, `config set-org` -> `org use`, `profile show` / `env current` -> `whoami` / `env list`, `skill install` / `skill path` -> `agent-skills install [--dry-run]`, `repo readiness` -> `check`, `share set-password` / `rm-password` -> `share update --password` / `--no-password`, every `remove` / `delete` -> `rm` (`org remove` -> `org rm-member`), and every `harumi repo ...` -> git (above).
