# Changelog

## 1.1.0

### New

- A command that needs a project and has none (no `--project`, and the folder is not bound to one) now offers the same arrow-key list of your projects that `clone` and `link` have, instead of stopping with "Provide --project or run from a directory bound to a project". This covers `runs`, `tags`, `compare`, `check`, `files`, `share`, `datasources`, `schedules`, `secrets`, `memories` and `dashboard validate`. Without a terminal nothing changes: same error, no request.
- `harumi start` asks "What do you have?" with the arrow menu instead of a typed 1, 2 or 3.
- `harumi org leave` leaves an organization you belong to (it asks first; an owner cannot leave and is told how to transfer ownership or delete the organization). `harumi org invitations` lists who invited you and as what (`--json` too), and `org invitations accept` / `decline` answer one. Before, the server's own message told you to "leave the organization" and the CLI had no way to.

### Removed

- `harumi blueprints` and `harumi new --blueprint`, as announced in 1.0.0. Examples is the only name now. Running the command exits 2, and the option exits 1 before anything is created, each saying `harumi examples` / `--example` (`harumi blueprints` was removed in 1.1.0. Use: harumi examples).

## 1.0.0 - 2026-10-27

A friendlier first session, readable output everywhere, and the same rules for every command: how it confirms, how it fails, what it prints for a script. 0.11 said the old names would go in 0.12.0; they go here, in 1.0.0, instead.

### New

- Bare `harumi` is a quick start, and `harumi start` asks which of `new`, `import` and `clone` you want. At a terminal, commands that need an id offer a menu instead (`HARUMI_NO_INTERACTIVE=1` turns menus off; without a terminal they never appear).
- `harumi login` asks which workspace to use (an organization or your personal one) and offers next steps. Harumi staff choose production or staging inside `login`; `harumi env` is gone. `org use` has a picker and `--personal`.
- `--json` on the commands that list or show things (`projects`, `runs`, `compare`, `files ls`, `share`, `datasources`, `schedules`, `secrets`, `memories`, `org`, `skills library`, `specs`, `examples`, `usage`, `whoami`) and on `run` (`run --watch --json` prints data on stdout and progress on stderr). Only the fields meant for scripts are included: no tokens, signed URLs or secret values. `check`, `status`, `dashboard validate` and `datasources query` do not have it yet.
- `harumi run` prints one line per phase; `--watch` shows one live line with elapsed time. A failed run leads with its cause and the last lines of stderr. Ctrl-C stops watching, never the run (exit 130, with the commands to follow or cancel it).
- `runs get` and `runs download` use the latest run when you give none; `runs cancel` offers the runs still going; `harumi compare` lets you pick the runs.
- `harumi import` shows what it will upload and leaves out virtual environments, `node_modules` and files that look like secrets. A secret that git tracks is warned about; without a terminal, `import` refuses unless you pass `--include-tracked-secrets`.
- `harumi specs` shows whole kernel names and says how to use one; `harumi usage` shows percentages and dollars; a plan limit tells you what to do about it (subscribe, ask an admin, enable overage), including the trial's limits.
- **Blueprints are now called Examples**, as in the web app: `harumi examples` lists them and `harumi new --example SLUG` starts a copy of one. `harumi blueprints` and `new --blueprint` still work for one more release, hidden, with a `deprecated:` line on stderr, and are removed in 1.1.0. `--json` output is unchanged. (`projects create --blueprint` is not renamed: that whole command is removed in this release, see below.)
- `harumi agent-skills install` says which skills went into which agent's folder (and when it found no agent), and a wrong skill or example name suggests the nearest real one. The git command `skills add` prints now works from a subfolder of the project.
- `-h` works like `--help`, and `-v` like `--version`. The command list is grouped, and about thirty commands end their `--help` with an `Examples:` block.
- Press TAB after `harumi --install-completion` to complete commands, flags and the values with a fixed list (`--type`, `--role`, `--mode`).
- `share add|update --comparisons` lets viewers compare runs (it needs `--run-history`).
- **Tags.** `harumi tags list`, `tags set` and `tags rm` label a version with a movable tag such as `prod`. `harumi run --tag prod` runs whatever the tag points at, and `schedules add --git-tag prod` follows it on every fire (`schedules update` takes it too). Moving a tag changes later runs only: a run keeps the commit it started from (`runs get` shows `prod @ 3f2a9c1e`). A tag cannot share a name with a branch.

### Changes a script can notice

- **A declined confirmation exits 1** (it was 0).
- **A delete with no `--yes` and no terminal refuses, exit 1,** and tells you to pass `--yes`. It used to read `y` from standard input, so `printf y | harumi projects rm ...` no longer works.
- `org rm` asks you to type the organization's name; `share update --no-password` asks first. A permanent delete by id needs the exact name or 8 or more characters of the id.
- **`files rm` of a file that is not there exits 1** (harumi-api says "deleted" for any path). Pass `--missing-ok` for the old idempotent behaviour.
- `harumi check` exits 1 when the project cannot run.
- The `harumi` remote has a plain URL with no user or token in it. A script that read the token from `git remote get-url harumi` or `.git/config` no longer finds one; git authenticates through `harumi git-credential`, which needs `harumi` on your PATH (a pip or pipx install puts it there).
- **The 28 names removed below now exit 2 and say what to use instead,** without calling the API. In 0.11 they ran and warned, so a script that still uses `harumi projects delete` or `harumi repo rm` stops deleting and starts failing. `harumi run --output-dir` exits 1 before anything runs.
- `compare --json` prints named fields (`columns`, `kpis`, `winner_run_id`, `no_winner_reason`, `warnings`, `fairness`) instead of the API's response as it came, so a field the server adds later does not reach your script. Today's output has the same fields; a column's `source` is `null` when the API sent none, instead of missing.
- `runs list`, `schedules list`, `memories list`, `share list` and `org members` print the first 8 characters of an id, and the commands that take one accept the start of it (`projects list` still prints whole ids). Their columns changed: `runs list` shows `started` and `took`, `schedules list` no longer shows the kernel, and `share list` no longer prints tokens.
- Errors say what failed, whose problem it is and what to do: your network, a company TLS proxy (`SSL_CERT_FILE`), a refused connection and a cut-off reply each read differently, and when a change may have gone through before the connection dropped, it says so, so you can check before you repeat it. `HARUMI_DEBUG=1` shows the full trace.
- Names are matched, not guessed: `org use Acme` saves the real organization id (it used to save the word you typed), `--role owner` is refused before the request, and a wrong verb says which command exists (`secrets create` -> `set`).
- `projects rm` says it moves a project to the Trash, where it stays for 7 days.
- `datasources`, `schedules`, `memories` and `secrets` show text as stored (a `[/x]` in a query result used to crash), and `files put` completes the upload so the file really appears.

### Fixed

- **The git token is no longer written into the project's `.git/config`.** `harumi clone`, `new`, `link` and `import` used to set the `harumi` remote to `https://user:token@host/...`, so `git remote -v`, an IDE or a screen share showed a token with access to your repositories. The remote is now the plain URL, and git asks `harumi git-credential` for the password when it needs it, for that repository and Harumi's git host only. A clone made before this release is cleaned the first time you run any `harumi` command inside it, and a plain `git push harumi <branch>` keeps working. The token itself is unchanged by this release: if you ever shared `git remote -v` output or a project's `.git/config` from an older clone, treat that token as exposed and ask Harumi to rotate it (signing in again does not).
- `Client` (the Python library) uses its own environment's login. With clients for two environments, the first one used to send the second one's token.
- `dashboard validate` knows the current widgets: `filter`, `detail` and `kpi-rail` are gone (`metric` replaces `kpi-rail`).

### Removed

- `harumi env`. Set `HARUMI_ENV=staging` for scripts, or pass `--env`.
- The 28 names that 0.11 deprecated. Running one exits 2 and prints what replaced it ("harumi push was removed in 1.0.0. Use: harumi import"):
  - `harumi push` -> `harumi import`
  - `harumi outputs` -> `harumi runs list` and `harumi runs download`
  - `harumi projects create` -> `harumi new <NAME> --no-clone`
  - `harumi profile show` -> `harumi whoami`
  - `harumi config ...` (`harumi config set-org`) -> `harumi org use`
  - `harumi skill ...` (`harumi skill path`, `harumi skill install`) -> `harumi agent-skills install`
  - `harumi repo ...` (all twelve commands, from `harumi repo ls` to `harumi repo readiness`) -> git in your clone; `harumi repo readiness` -> `harumi check`
  - `harumi share set-password` and `harumi share rm-password` -> `harumi share update --password` and `--no-password`
  - `harumi projects delete`, `harumi share remove`, `harumi datasources remove`, `harumi schedules remove` and `harumi org delete` -> the same command with `rm`; `harumi org remove` -> `harumi org rm-member`
- `harumi run --output-dir`. Use `harumi runs download`.

`harumi blueprints` and `harumi new --blueprint` are deprecated in this release and still work until 1.1.0.

## 0.11.1 - 2026-10-23

A fix for installs on Python 3.9 and 3.10.

### Fixed

- `harumi` did not start on Python older than 3.11 after a plain `pip install harumi` (`ModuleNotFoundError: No module named 'tomli'`). The missing dependency is now declared. If you are on 3.9 or 3.10 and have 0.11.0, run `pip install --upgrade harumi` (or `pipx upgrade harumi`).

## 0.11.0 - 2026-10-13

A smaller, more consistent command set, real `skills` commands, and update notices.

### New

- `harumi skills library` lists the preset skills from the Harumi skill library; `harumi skills add <slug>` installs one into your clone as `skills/<slug>/SKILL.md`. Commit and push it so the project's assistant can use it.
- `harumi check` reports everything blocking a project from running (the old `repo readiness`).
- `harumi runs download [RUN_ID]` downloads a run's output (the latest run by default).
- `harumi whats-new` shows these release notes.
- `harumi org use <ORG_ID>` sets the organization sent with every request.
- `harumi new <NAME> --no-clone` creates a project without cloning it.
- `harumi share update <LINK_ID> --password / --no-password` sets or removes a share link's password.
- `harumi whoami` now also shows your name, bio and active organization.
- harumi tells you when a newer version is available (at most once a day, only in an interactive terminal). Turn it off with `HARUMI_NO_UPDATE_CHECK=1`.

### Changed

- `push` is now `import`. It always creates a new project; to send edits to an existing one, use `git push harumi <branch>` from your clone.
- Every delete command is now `rm`: `projects rm`, `share rm`, `datasources rm`, `schedules rm`, `org rm`. Removing an organization member is `org rm-member`.
- `skill install` is now `agent-skills install` (use `--dry-run` instead of `skill path`).

### Removed (still works in 0.11.x with a warning, removed in 0.12.0)

- `harumi repo ...`: a project's repo is a plain git repo, so use git in your clone. `harumi clone` already sets up a `harumi` remote with your token.
- `projects create`, `outputs`, `run --output-dir`, `config set-org`, `profile show`, `env current`, `skill install`/`skill path`, `share set-password`/`rm-password`, and the old `remove`/`delete` verbs. Each prints what to use instead.

## 0.10.0 - 2026-10-13

- `harumi compare <RUN_ID>...` compares 2-4 runs: KPIs, the winner, and whether they used the same inputs.
- `harumi memories list|add|edit|rm` manages the notes a project's assistant remembers.
