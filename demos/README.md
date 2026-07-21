# demos

Small, self-contained Python snippets to try out `harumi run` without
writing your own solver code first.

| File | What it shows | Suggested kernel |
|---|---|---|
| `hello_world.py` | Zero-dependency smoke test — confirms the CLI, auth, and `--notebook` id are wired up correctly. | any |
| `or_tools_lp.py` | Linear program (furniture-making) solved with OR-Tools. | `or_python_small` |
| `gurobi_knapsack.py` | 0/1 knapsack MIP solved with Gurobi. | `gurobi_python_medium` |
| `project_demo/` | Multi-file directory with a local import (`main.py` + `costs.py`), for exercising directory uploads. | `or_python_small` |

## Try it

```bash
# find a notebook id to run against
harumi notebooks

# run the smoke test interactively (streams output live)
harumi run demos/hello_world.py --notebook <NOTEBOOK_ID> --mode interactive

# run the OR-Tools LP
harumi run demos/or_tools_lp.py --notebook <NOTEBOOK_ID> \
  --mode interactive --kernel or_python_small

# run the Gurobi knapsack MIP
harumi run demos/gurobi_knapsack.py --notebook <NOTEBOOK_ID> \
  --mode interactive --kernel gurobi_python_medium
```

`--mode interactive` sends the local file's code as-is to the notebook's
live sandbox kernel and streams stdout/results back — the fastest way to
try one of these files exactly as written.

`--mode job` (default) instead uploads the given path to the notebook's
project and queues the notebook's *own saved live version* on the async
job queue — useful for long/heavy runs you can walk away from, but it
won't execute a local file directly unless that file's code is also saved
as the notebook's live version in the Harumi web app. `project_demo/`
is shaped for that workflow: upload the directory in job mode, then have
the notebook's live version import from `costs.py` the same way
`main.py` does.
