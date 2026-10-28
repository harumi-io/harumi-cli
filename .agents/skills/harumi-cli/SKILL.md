---
name: harumi-cli
description: Guide for using the `harumi` CLI to run local optimization/solver code (Gurobi, OR-Tools, plain Python) on Harumi's infrastructure via the project's self-hosted Gitea repo, to select the backend environment (production vs internal staging), start a new project, import an existing folder of code as a new project, clone an existing project locally, bind a directory to a project, edit the project's repo with git in a clone, install preset skills from the Harumi skill library, inspect, cancel and download runs, manage the project's dashboard widgets and public share link, manage datasources, schedules, secrets, organizations, and your profile. Use when the user wants to run, import, or debug a local script against a Harumi project, mentions `harumi start`, `harumi new`, `harumi import`, `harumi clone`, `harumi link`, `harumi run`, `harumi runs`, `harumi tags`, `harumi check`, `harumi dashboard`, `harumi share`, `harumi examples`, `harumi env`, `harumi projects`, `harumi datasources`, `harumi schedules`, `harumi secrets`, `harumi memories`, `harumi skills`, `harumi agent-skills`, `harumi org`, `harumi profile`, asks about switching between production and staging, creating a new Harumi project, turning an existing local folder/repo into a Harumi project, getting an existing Harumi project's code onto their machine, Harumi kernel specs, example projects (formerly called blueprints), Gitea remotes, scratch branches, database connections, running SQL queries against a project datasource, scheduling/cron runs, managing environment variables/secrets, organization members, dashboard widgets (metric/table/line-chart/bar-chart/gantt-chart), `dashboard.toml`, `dashboard/*.toml`, multiple dashboards per project, the dashboard picker, `output.json`, what components/widgets the platform dashboard can render, sharing/publishing a project's dashboard publicly, or needs to fetch/download results or files from a Harumi run/repo.
---

# Harumi CLI

Drives the `harumi` CLI (source: `harumi-cli`, package `harumi`). Every run is git-ref based: code lives in a per-project Harumi Git (Gitea) repo. The CLI auto-manages a scratch branch so users can iterate without manually committing.

The CLI targets one of two environments (see [Config & environments](#config--environments)): `production` (default) and `staging` (internal, VPN-only, `git.dev.harumi.io`). Each has its own Supabase, so each has its own login. The git-first `run` flow currently only works on `staging` — production Gitea (`git.harumi.io`) is not live yet, so on production only the non-git commands (`projects`/`datasources`/`secrets`/`org`/`profile`) work.

For the full flag-by-flag reference and troubleshooting table, see [references/commands.md](references/commands.md).

## VPN requirement

The staging endpoints (`api.dev.harumi.io`, `git.dev.harumi.io`) are internal ALBs — anything on the `staging` environment (login, `harumi new`/`import`/`clone`/`link`, git push, runs) only works over VPN. On `staging` a failed connection already asks "Is the VPN connected?" (production users are never asked); surface that to the user if they aren't on VPN.

## Preflight

1. Check CLI is installed: `harumi --version`. Not installed, not on PATH, or `You're not logged in.` / `Your Harumi session has expired.` (both tell you to run `harumi login`) on any command? Switch to the **`harumi-cli-setup`** skill — it covers install method selection, environment selection, the interactive OTP login (never automate this), and diagnosing a broken install. Come back here once `harumi whoami` succeeds.
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
harumi new "My Project" [--dir PATH] [--example SLUG] [--customer-id ID]
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

Find project IDs with `harumi projects list`. Run `harumi examples` to list the `--example` values for `harumi new` (finished projects you can start a copy of).

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
harumi run --tag prod                    # whatever the "prod" tag points at (see Tag versions)
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
harumi runs list                          # the latest 25 runs for the bound project, newest first (--limit N, --all)
harumi runs get <RUN_ID>                  # status, git ref, exit code, stdout/stderr/error
harumi runs cancel <RUN_ID>               # cancel an in-flight run
harumi runs download [RUN_ID] [-o DIR]    # download a run's committed output as a zip (latest run by default)
harumi check [--ref REF]                  # server-side readiness: repo exists, harumi.toml valid, kernel allowed
```

## Editing the project

A project's repo is an ordinary git repo, and `harumi clone` leaves a remote named **`harumi`** that git signs in to by itself (git runs `harumi git-credential`; there is no token in the URL). There is no `harumi repo` command: use the shell and git.

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
- The `harumi` remote's URL has no token in it; git gets the password from `harumi git-credential` (a hidden command git runs, from the credentials file `harumi login` wrote). Never paste `~/.harumi/` files into chat, logs, issues or commits. A clone made before 1.0 had the token in its URL; any `harumi` command run inside it removes it.

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

## Tag versions

A tag is a **movable label** on a commit (for example `prod`), so you can run or schedule "whatever is in production" without remembering a branch or SHA.

```bash
harumi tags list                          # tag, commit, message (--json for scripts)
harumi tags set prod                      # label the branch you are on (it must be pushed)
harumi tags set prod --to release-2       # label another branch, or a commit SHA, creating or MOVING the tag
harumi tags rm prod                       # delete it (asks; --yes skips; refused while a schedule follows it)
harumi run --tag prod --watch             # run what prod points at now
harumi schedules add --cron "0 9 * * *" --git-tag prod   # follow it on every fire
```

- Moving a tag changes what *future* runs and schedule fires use. A run already started keeps the commit it resolved, and `harumi runs get` shows `tag @ commit`.
- `--tag` cannot be combined with `--branch` or `--commit` (on `run`) or `--git-branch`/`--git-commit` (on `schedules`).
- A tag cannot have the same name as a branch. Tags are ordinary git tags: `git fetch harumi --tags --force` brings them into a clone.

## Schedule runs

`harumi schedules` manages project-scoped cron schedules for git-ref runs (`/projects/{project_id}/schedules`).

```bash
harumi schedules list                                       # table of schedules for the bound project
harumi schedules get [SCHEDULE_ID]                           # detail view; the id, or the start of it (what `list` shows); omit it to pick
harumi schedules add --cron "0 9 * * *" [--git-branch main | --git-tag prod] [--start-at ISO] [--kernel ...] [--email-to ...]
harumi schedules update [SCHEDULE_ID] --cron "0 */6 * * *"
harumi schedules rm [SCHEDULE_ID]                            # `--yes` needs the id: it never picks for you
```

Key semantics:

- **Cron is a raw 5-field expression, interpreted in UTC, and may not fire more often than every 5 minutes.** The CLI does not validate it client-side — the server validates with `croniter` and returns a clear 400 on a bad expression or a too-short interval. Don't try to build a calendar/human-friendly UI; just pass the cron string.
- **A schedule follows a branch (default `main`) or a tag, not both.** With `--git-tag` it runs whatever the tag points at each time it fires.
- **No pause/enable flag exists.** The only way to stop a schedule from firing is `harumi schedules rm`.
- **No separate "run now" for a schedule.** Immediate execution is already covered by `harumi run` (git-ref based); schedules only control recurring runs.

All `schedules` subcommands accept `--project` to override the `.harumi` binding.

## Manage secrets

`harumi secrets` manages project-scoped environment variables, injected into kernels/apps. Values are stored as SSM SecureStrings; there is no update endpoint — `set` on an existing name overwrites it.

```bash
harumi secrets list                 # names only, never values
harumi secrets set <NAME>           # prompts for the value (hidden input); no spaces or slashes in NAME; a name that is not a valid env var (`my-key`) saves with a note
harumi secrets rm <NAME>
```

## Manage project memories and skills

`harumi memories` manages the project's memories: short notes the project's chat remembers. `always` memories are loaded into every chat; `on_demand` ones (the default) are only retrieved when relevant.

```bash
harumi memories list [--query TEXT] [--mode always|on_demand]   # includes read-only memories inherited from the org / personal workspace
harumi memories add "Use metric units" [--mode always]          # `-` reads the text from stdin; with neither, you are prompted at a terminal and told to pass it otherwise
harumi memories edit [MEMORY_ID] [--content TEXT] [--mode always|on_demand]   # id, or the start of it; omit it to pick
harumi memories rm [MEMORY_ID]                                 # `--yes` needs the id
```

- Inherited rows (scope `organization` or `user`) are listed but can't be edited or removed here; trying says so and points at the workspace's settings.
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
harumi org rename <ORG> <NEW_NAME>      # <ORG> is a name or the start of an id, not a UUID you have to copy
harumi org rm [ORG]                     # asks, naming what goes with it; --yes needs the name
harumi org members [ORG]                # defaults to the workspace you are in
harumi org invite <ORG> --email a@b.com --role member   # admin | member | viewer; owner is refused
harumi org role <ORG> [MEMBER] --role admin             # MEMBER is an email or the start of an id
harumi org rm-member <ORG> [MEMBER]
harumi org use <ORG>                   # a name or the start of an id; persists the org sent as X-Organization (checked first)
harumi org use --personal              # back to the personal workspace (no argument at a terminal: a menu)

harumi whoami                          # profile, environment and active org
harumi profile set --first-name Ana --bio "..."    # --last-name "" / --bio "" clear them
```

## Scripts and CI

Every delete follows one rule: `--yes` goes ahead; at a terminal it asks (declining exits 1); with no terminal and no `--yes` it **refuses at once** and names the flag. A `y` piped to stdin is not consent. `projects rm` and `org rm` ask for the name, so an agent passes `--yes` and the name. See [Confirmations](references/commands.md#confirmations---yes).

Read commands take `--json`: stdout is one JSON document and nothing else (an array for a list, `[]` when empty), errors stay on stderr with exit 1. `harumi run --watch --json` prints `{"run_id","status","exit_code","succeeded",...}` and still exits 1 if the run fails. Prefer it to parsing a table; the contract and the list of commands are in [references/commands.md](references/commands.md#scripts-and-ci---json).

## Config & environments

There are two built-in environments, selectable in the CLI:

| Env | API | Gitea | Access |
|---|---|---|---|
| `production` (default) | `https://api.harumi.io/api` | `https://git.harumi.io` | public |
| `staging` | `https://api.dev.harumi.io/api` | `https://git.dev.harumi.io` | internal, VPN-only |

Auth flows through harumi-api (`/users/otp`, `/users/refresh`), and each environment has its own Supabase — so **each environment has its own stored session**. You must `harumi login` once per environment; switching does not log you out of the other.

Harumi staff (`@harumi.io`) choose the environment inside `harumi login`, right after typing their email; the choice is remembered. To switch, run `harumi login` again and pick the other one (no new code is sent if that environment already has a session for the same email). For a single command, or whenever there is no terminal (agents, scripts), use `harumi --env staging run` or `HARUMI_ENV=staging`. There is no `harumi env` command.

Staging is **internal-only**: the real gate is needing a staging Supabase account and the VPN.

Environment selection precedence: `--env` > `HARUMI_ENV` > the choice made at `harumi login` > `production`.

- Per-command URL overrides: `--api-url` / `HARUMI_API_URL`, `--git-url` / `HARUMI_GIT_URL` (e.g. for a local harumi-api). These override the active environment's endpoints without changing which env you're on.
- Org: `harumi org use <ORG_ID>` / `--org` / `HARUMI_ORG` (scoped per environment). Sent as `X-Organization` to scope reads, and used as the owning workspace when creating a project — `--personal` on `new`/`import` opts out.
- Stored under `~/.harumi/`: global `config.json` (just the selected environment) + per-env `environments/<env>/{credentials,config}.json`. Override the home dir with `HARUMI_HOME`.

## Updates

`harumi whats-new` shows what changed in the installed version (`--all` for history). A one-line "new version available" notice may appear on stderr (at most once a day, interactive terminals only); it is not an error. Set `HARUMI_NO_UPDATE_CHECK=1` to silence it, and upgrade with the command the notice prints (`pipx upgrade harumi`, `uv tool upgrade harumi`, or `python3 -m pip install --upgrade harumi`).

## Renamed in 0.11, removed in 1.0.0

These old names were removed in 1.0.0: running one exits 2 and prints what to use instead (`` `harumi push` was removed in 1.0.0. Use: harumi import ``). Use the new ones: `push` -> `import`, `projects create` -> `new --no-clone`, `outputs` / `run --output-dir` -> `runs download`, `config set-org` -> `org use`, `profile show` -> `whoami`, `skill install` / `skill path` -> `agent-skills install [--dry-run]`, `repo readiness` -> `check`, `share set-password` / `rm-password` -> `share update --password` / `--no-password`, every `remove` / `delete` -> `rm` (`org remove` -> `org rm-member`), and every `harumi repo ...` -> git (above).
