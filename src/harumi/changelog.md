# Changelog

## 0.12.0

A friendlier first session, readable output everywhere, and the same rules for every command: how it confirms, how it fails, what it prints for a script.

### New

- Bare `harumi` is a quick start, and `harumi start` asks which of `new`, `import` and `clone` you want. At a terminal, commands that need an id offer a menu instead (`HARUMI_NO_INTERACTIVE=1` turns menus off; without a terminal they never appear).
- `harumi login` asks which workspace to use (an organization or your personal one) and offers next steps. Harumi staff choose production or staging inside `login`; `harumi env` is gone. `org use` has a picker and `--personal`.
- `--json` on the commands that list or show things (`projects`, `runs`, `compare`, `files ls`, `share`, `datasources`, `schedules`, `secrets`, `memories`, `org`, `skills library`, `specs`, `blueprints`, `usage`, `whoami`) and on `run` (`run --watch --json` prints data on stdout and progress on stderr). Only the fields meant for scripts are included: no tokens, signed URLs or secret values. `check`, `status`, `dashboard validate` and `datasources query` do not have it yet.
- `harumi run` prints one line per phase; `--watch` shows one live line with elapsed time. A failed run leads with its cause and the last lines of stderr. Ctrl-C stops watching, never the run (exit 130, with the commands to follow or cancel it).
- `runs get` and `runs download` use the latest run when you give none; `runs cancel` offers the runs still going; `harumi compare` lets you pick the runs.
- `harumi import` shows what it will upload and leaves out virtual environments, `node_modules` and files that look like secrets. A secret that git tracks is warned about; without a terminal, `import` refuses unless you pass `--include-tracked-secrets`.
- `harumi specs` shows whole kernel names and says how to use one; `harumi usage` shows percentages and dollars; a plan limit tells you what to do about it (subscribe, ask an admin, enable overage), including the trial's limits.
- `harumi agent-skills install` says which skills went into which agent's folder (and when it found no agent), and a wrong skill or blueprint name suggests the nearest real one. The git command `skills add` prints now works from a subfolder of the project.
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
- `runs list`, `schedules list`, `memories list`, `share list` and `org members` print the first 8 characters of an id, and the commands that take one accept the start of it (`projects list` still prints whole ids). Their columns changed: `runs list` shows `started` and `took`, `schedules list` no longer shows the kernel, and `share list` no longer prints tokens.
- Errors say what failed, whose problem it is and what to do: your network, a company TLS proxy (`SSL_CERT_FILE`), a refused connection and a cut-off reply each read differently, and when a change may have gone through before the connection dropped, it says so, so you can check before you repeat it. `HARUMI_DEBUG=1` shows the full trace.
- Names are matched, not guessed: `org use Acme` saves the real organization id (it used to save the word you typed), `--role owner` is refused before the request, and a wrong verb says which command exists (`secrets create` -> `set`).
- `projects rm` says it moves a project to the Trash, where it stays for 7 days.
- `datasources`, `schedules`, `memories` and `secrets` show text as stored (a `[/x]` in a query result used to crash), and `files put` completes the upload so the file really appears.

### Fixed

- `Client` (the Python library) uses its own environment's login. With clients for two environments, the first one used to send the second one's token.
- `dashboard validate` knows the current widgets: `filter`, `detail` and `kpi-rail` are gone (`metric` replaces `kpi-rail`).

### Removed

- `harumi env`. Set `HARUMI_ENV=staging` for scripts, or pass `--env`.

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
