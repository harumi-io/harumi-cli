# harumi CLI — Command Reference

Detailed flag reference, config/credential storage, troubleshooting, and the Python `Client` SDK. Loaded on demand from [SKILL.md](../SKILL.md).

## Contents

- [Auth commands](#auth-commands)
- [config set-org](#config-set-org)
- [specs](#specs)
- [notebooks](#notebooks)
- [init](#init)
- [run](#run)
- [outputs](#outputs)
- [Config & credential files](#config--credential-files)
- [Troubleshooting](#troubleshooting)
- [Python library (`Client`) alternative](#python-library-client-alternative)

## Auth commands

### `harumi login`

```
harumi login [--email EMAIL] [--signup] [--api-url URL] [--git-url URL]
```

- Prompts for email if `--email` omitted, then prompts for the OTP code emailed by Supabase.
- `--signup`: creates the Supabase account first. Required the *first* time a new email logs in.
- On success, stores `access_token`/`refresh_token`/`git_token` at `~/.harumi/credentials.json` (mode `0600`). The `git_token` is the per-user Gitea personal access token used by `harumi init` and `harumi run` for git-over-HTTPS. If the backend endpoint isn't live yet, login skips token provisioning and prints a notice.
- Best-effort org resolution: if exactly one org, stored automatically; if multiple, prints a table and instructs `harumi config set-org`.

### `harumi logout`

Clears `~/.harumi/credentials.json`. No flags.

### `harumi whoami`

```
harumi whoami [--api-url URL] [--org ORG]
```

Prints the email and id of the currently logged-in user (`GET /users/me`).

## `config set-org`

```
harumi config set-org <ORG_ID>
```

Persists `org_id` in `~/.harumi/config.json`; every subsequent request sends it as `X-Organization`.

## `specs`

```
harumi specs [--api-url URL] [--org ORG]
```

Lists kernel specs (`name`, `display_name`, `cpu`, `memory`, `subscription_required`) from `GET /sandbox/specs`. `name` is what you pass to `run --kernel`.

## `notebooks`

```
harumi notebooks [--project PROJECT_ID] [--api-url URL] [--org ORG]
```

Lists every project and its notebooks. Useful for finding `PROJECT_ID` to pass to `harumi init`.

## `init`

```
harumi init --project PROJECT_ID [--api-url URL] [--git-url URL] [--org ORG]
```

**Run once per project directory.** Binds the current working directory to a Harumi project:

1. Calls `GET /projects/{id}/repo` (assumed endpoint — Workstream B of the git-first pivot).
2. Writes `.harumi/config.json` in the current directory with `project_id` + repo metadata.
3. Configures the `harumi` git remote with an authenticated HTTPS URL (requires a `git_token` in credentials from `harumi login` and the repo to be a git working tree).

After `harumi init`, `harumi run` and `harumi outputs` work without any `--project` flag.

**Note:** `.harumi/config.json` is searched upward from cwd, so `harumi run` works from subdirectories.

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

**Calls:** `POST /projects/{id}/execute` with `{ branch, commit?, command?, kernel_spec? }` (assumed endpoint — Workstream C of the git-first pivot).

## `outputs`

```
harumi outputs [--project ID] [--latest] [--download OUTPUT_ID] [--output-dir DIR]
               [--api-url URL] [--org ORG]
```

- `--project` optional if run from a bound directory.
- No extra flags: table of all outputs (`id`, `status`, `started`, `ended`, `scenario`).
- `--latest`: only the most recently started output.
- `--download <id> [--output-dir DIR]`: streams the output zip to `<output-dir>/output_<id>.zip`.

## Config & credential files

Precedence (highest first): **CLI flags > env vars > `~/.harumi/config.json` > defaults**.

| Setting | Env var | Config key | Default |
|---|---|---|---|
| API base URL | `HARUMI_API_URL` | `api_url` | `https://api.harumi.io/api` |
| Gitea URL | `HARUMI_GIT_URL` | `git_url` | `https://git.dev.harumi.io` |
| Org id (`X-Organization`) | `HARUMI_ORG` | `org_id` | none (from login) |

- `~/.harumi/config.json` — non-secret settings.
- `~/.harumi/credentials.json` — `access_token`, `refresh_token`, `git_token`, `user_id`, `email`, `expires_at`; mode `0600`.
- `.harumi/config.json` (per-project) — `project_id`, `repo.owner/name/clone_url/default_branch`; written by `harumi init`, searched upward from cwd.
- Override the home dir with `HARUMI_HOME`.

## Troubleshooting

| Symptom / error | Cause | Fix |
|---|---|---|
| `Error: Not logged in. Run harumi login first.` | No/expired session | Ask user to run `harumi login` |
| `harumi-api returned HTTP 422: ... Signups not allowed for otp` | New email, no account | Re-run `harumi login --signup` |
| `No Harumi project found ... Run harumi init` | `.harumi/config.json` missing in cwd + parents | `harumi init --project <ID>` |
| `No Gitea token found. Run harumi login` | `git_token` absent in credentials | `harumi login` again once backend is live |
| `The Gitea user provisioning endpoint (/users/git-token) is not yet available` | Workstream B not deployed | Ask team; token provisioning is a no-op until the backend lands |
| `Could not fetch repo for project ... not yet available` | Workstream B not deployed | Wait for backend; meanwhile the assumed endpoint contract is in `client.py` |
| `Could not queue a run ... not yet available` | Workstream C not deployed | Wait for backend |
| `git push failed: ...` | Network (VPN not connected) or bad credentials | Check VPN; re-run `harumi login` to refresh token |
| `git not found` | git missing from PATH | Install git |
| `Timed out after <n>s waiting for output` | Job still running past timeout | Check `harumi outputs --latest` manually |
| `No output_id returned; cannot watch` | Backend didn't return an output_id | Check `harumi outputs` manually |

## Python library (`Client`) alternative

When scripting is better than shelling out:

```python
from harumi import Client
from harumi.config import ProjectBinding

binding = ProjectBinding.load()        # reads .harumi/config.json
client = Client()                      # loads ~/.harumi/credentials.json

# Queue a git-ref run (assumed endpoint — Workstream C)
response = client.execute_project(
    binding.project_id,
    branch="feature/solver-v2",
    command="python main.py",
)

# Poll until done
from harumi.execution import wait_for_output
output = wait_for_output(client.api, binding.project_id, response.output_id)
print(output.status, output.output_url)

# Download artifacts
client.download_output(binding.project_id, output.id, "./out")
```

`Client(api_url=..., git_url=..., org_id=...)` accepts the same overrides as the CLI flags. Requires the user to have run `harumi login` at least once.
