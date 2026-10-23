# Changelog

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
