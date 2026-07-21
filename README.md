# harumi

Run your local optimization code on Harumi's infrastructure — straight from your terminal or IDE — instead of pasting it into the platform's notebook editor.

Optimization/solver code (Gurobi, OR-Tools, etc.) is often too heavy to run on a laptop. `harumi` uploads your local files to an existing Harumi project/notebook, runs the code on Harumi's sandboxes, and streams or fetches the results — reusing the exact same backend endpoints the web app and AI agent already use.

## Install

```bash
pip install -e .
# or, once published:
pip install harumi
```

This installs the `harumi` CLI and the `harumi` Python package (`import harumi`).

## Quick start

```bash
# 1. Log in (Supabase OTP — check your email for the code)
harumi login

# 2. Find a notebook to run against
harumi notebooks

# 3. See available kernel sizes (CPU/RAM, Gurobi vs plain Python)
harumi specs

# 4. Run a local file or directory on the infra
harumi run ./solver.py --notebook <NOTEBOOK_ID> --mode job --watch

# 5. Check/download outputs later
harumi outputs --notebook <NOTEBOOK_ID> --latest
```

## Execution modes

- `--mode interactive` — streams stdout/stderr/results live, like a REPL. Good for fast iteration on small/medium runs. Uses the same sandbox kernel the notebook editor uses.
- `--mode job` (default) — uploads code, queues an async run on the infra's job queue, and (with `--watch`) polls until it finishes, then downloads the output artifacts. Good for long, heavy optimization runs — you can close your laptop and check back later.

## Configuration

Precedence: CLI flags > environment variables > `~/.harumi/config.json` > defaults.

| Env var | Purpose | Default |
|---|---|---|
| `HARUMI_API_URL` | Base URL of `harumi-api` | `http://localhost:8000/api` |
| `HARUMI_ORG` | Organization ID sent as `X-Organization` | (from login) |

Credentials (JWT + refresh token) are stored in `~/.harumi/credentials.json` (mode `0600`) after `harumi login`.

## Library usage

```python
from harumi import Client

client = Client()  # loads stored credentials
result = client.run_job("./solver.py", notebook_id="...", watch=True)
print(result.status, result.output_url)
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

All tests are offline (SSE parser + mocked HTTP transport) — no live backend required.
