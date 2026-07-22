# harumi CLI — Command Reference

Detailed flag reference, config/credential storage, troubleshooting, and the Python `Client` SDK. Loaded on demand from [SKILL.md](../SKILL.md).

## Contents

- [Auth commands](#auth-commands)
- [config set-org](#config-set-org)
- [specs](#specs)
- [notebooks](#notebooks)
- [run](#run)
- [outputs](#outputs)
- [Config & credential files](#config--credential-files)
- [Troubleshooting](#troubleshooting)
- [Python library (`Client`) alternative](#python-library-client-alternative)

## Auth commands

### `harumi login`

```
harumi login [--email EMAIL] [--signup] [--api-url URL]
```

- Prompts for email if `--email` omitted, then prompts for the OTP code emailed by Supabase.
- `--signup`: creates the Supabase account first via `POST /users/sign_up`. Required the *first* time a given email logs in — without it, `POST /users/otp` 422s with `"Signups not allowed for otp"`.
- On success, stores `access_token`/`refresh_token` at `~/.harumi/credentials.json` (mode `0600`).
- Best-effort org resolution: if the user belongs to exactly one org, it's saved automatically; if multiple, it prints a table and instructs the user to run `harumi config set-org <ORG_ID>`.

### `harumi logout`

Clears `~/.harumi/credentials.json`. No flags.

## `config set-org`

```
harumi config set-org <ORG_ID>
```

Persists `org_id` in `~/.harumi/config.json`; every subsequent request sends it as the `X-Organization` header (unless overridden by `--org` or `HARUMI_ORG`).

## `specs`

```
harumi specs [--api-url URL] [--org ORG]
```

Lists kernel specs (`name`, `display_name`, `cpu`, `memory`, `subscription_required`) from `GET /sandbox/specs`. `name` is what you pass to `run --kernel`.

## `notebooks`

```
harumi notebooks [--project PROJECT_ID] [--api-url URL] [--org ORG]
```

Lists every project and its notebooks (`GET /projects`, then `GET /projects/{id}/notebooks` per project). `--project` filters to one project id. Output gives the `notebook_id` needed for `run --notebook` / `outputs --notebook`.

## `run`

```
harumi run <path> --notebook ID [--mode interactive|job] [--kernel SPEC]
           [--project PROJECT_ID] [--watch] [--output-dir DIR]
           [--scenario-id ID] [--scenario-name NAME] [--email-to EMAIL]
           [--api-url URL] [--org ORG]
```

| Flag | Modes | Meaning |
|---|---|---|
| `path` (positional) | both | Local file (interactive) or file/directory (job) |
| `--notebook, -n` | both | Target notebook id (required) |
| `--mode, -m` | both | `interactive` or `job` (default `job`) |
| `--kernel, -k` | both | Kernel spec name, default `or_python_small` (see `specs`) |
| `--project` | job | Project id to upload into; auto-resolved via `GET /projects/by-notebook/{id}` if omitted |
| `--watch, -w` | job | Block, polling `GET /notebooks/{id}/outputs/{output_id}` every 5s until terminal status |
| `--output-dir, -o` | both | interactive: saves non-text results (`image/png`, `text/html`, `image/svg+xml`) as files. job: with `--watch`, downloads the output zip here on success |
| `--scenario-id` / `--scenario-name` | job | Tag the run with a scenario |
| `--email-to` | job | Email results when the job finishes |
| `--api-url` / `--org` | both | Per-invocation overrides |

Interactive mode internals: `POST /sandbox/{notebook_id}/execute` with `{"code": <file contents>, "kernel_spec": ...}`, streamed as SSE (`stream`, `error`, `result`, `execution_complete` events). Errors print as `ename: evalue` + traceback and exit code 1.

Job mode internals: uploads `path` to the project (`upload_path`), then `POST /notebooks/{notebook_id}/execute` (no code payload — re-runs whatever is currently saved as the notebook's live version). Returns `task_id`, `output_id`, `message`. Without `--watch`, prints a hint to check back with `harumi outputs --notebook <id> --latest`.

## `outputs`

```
harumi outputs --notebook ID [--latest] [--download OUTPUT_ID] [--output-dir DIR]
               [--api-url URL] [--org ORG]
```

- No flags beyond `--notebook`: table of all outputs (`id`, `status`, `started`, `ended`, `scenario`) from `GET /notebooks/{id}/outputs`.
- `--latest`: only the most recently started output.
- `--download <id> [--output-dir DIR]`: streams `GET /notebooks/{id}/outputs/{id}/download` to `<output-dir>/output_<id>.zip` (default output dir `.`).

Terminal statuses: `finished`, `completed`, `failed`, `timeout`, `cancelled`. `succeeded` is `finished`/`completed` only.

## Config & credential files

Precedence for every setting (highest first): **CLI flags > environment variables > `~/.harumi/config.json` > hardcoded defaults**.

| Setting | Env var | Config file key | Default |
|---|---|---|---|
| API base URL | `HARUMI_API_URL` | `api_url` | `https://api.harumi.io/api` |
| Org id (`X-Organization`) | `HARUMI_ORG` | `org_id` | none (from login) |

- `~/.harumi/config.json` — non-secret settings (`api_url`, `org_id`), written by `config set-org` / org auto-resolution.
- `~/.harumi/credentials.json` — `access_token`, `refresh_token`, `user_id`, `email`, `expires_at`; mode `0600`; written by `login`, cleared by `logout`.
- Override the home dir for both files with `HARUMI_HOME` (defaults to `~/.harumi`).
- Access tokens are proactively refreshed if within 60s of expiry, and reactively refreshed-and-retried once on any HTTP 401.

## Troubleshooting

| Symptom / error text | Cause | Fix |
|---|---|---|
| `Error: Not logged in. Run harumi login first.` | No/expired session, refresh token also invalid | Ask the user to run `harumi login` (or `--signup` for a new email) |
| `harumi-api returned HTTP 422: ... Signups not allowed for otp` | Email has no Supabase account yet | Ask the user to re-run `harumi login --signup` |
| `interactive mode requires a single Python file, not a directory` | `--mode interactive` given a directory | Point at one `.py` file, or switch to `--mode job` |
| `Notebook <id> is not linked to any project; pass --project explicitly.` | Job mode couldn't auto-resolve the project | Ask user for the project id, or run `harumi notebooks` to find it, then pass `--project` |
| `Timed out after <n>s waiting for output <id> (last status: '...')` | `--watch` exceeded its timeout | Re-check later with `harumi outputs --notebook <id> --latest`; the run may still be in progress |
| `No output_id returned; cannot watch this run.` | Job queued but backend didn't return an `output_id` | Check `harumi outputs --notebook <id>` manually |
| Run ended with status `failed`/`timeout`/`cancelled` | Job-mode run didn't succeed | Follow the printed `Logs: <log_url>` if present |
| `FileNotFoundError` | Local `path` doesn't exist | Verify the path relative to the current working directory |

## Python library (`Client`) alternative

When scripting/automation is a better fit than shelling out to the CLI (e.g. inside a Python build step):

```python
from harumi import Client

client = Client()  # loads ~/.harumi/credentials.json + ~/.harumi/config.json
client.run_job("./solver.py", notebook_id="...", watch=True, output_dir="./out")

result = client.run_interactive(open("./solver.py").read(), notebook_id="...")
print(result.ok, result.stdout)

outputs = client.list_outputs(notebook_id="...")
```

`Client(api_url=..., org_id=...)` accepts the same overrides as the CLI's `--api-url`/`--org` flags. This still requires the user to have already run `harumi login` once (same credentials file).
