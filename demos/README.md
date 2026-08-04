# demos

Small, self-contained Python snippets to try out `harumi run` without
writing your own solver code first.

| File | What it shows | Suggested kernel |
|---|---|---|
| `hello_world.py` | Zero-dependency smoke test — confirms the CLI, auth, and project binding are wired up correctly. | any |
| `or_tools_lp.py` | Linear program (furniture-making) solved with OR-Tools. | `or_python_small` |
| `gurobi_knapsack.py` | 0/1 knapsack MIP solved with Gurobi. | `gurobi_python_medium` |
| `project_demo/` | Multi-file directory with a local import (`main.py` + `costs.py`), for exercising multi-file runs. | `or_python_small` |

## Try it

`harumi run` executes whatever is committed on the bound project's git ref (or a scratch branch of your dirty working tree) — it doesn't take a file path directly. Point a project's `harumi.toml` `command` at one of these files (or push it as `main.py`), then run it:

```bash
# bind this directory to a project (once)
harumi init --project <PROJECT_ID>

# run the demo committed here, streaming stdout/stderr, and watch for completion
harumi run --command "python demos/hello_world.py" --watch

# run the OR-Tools LP with a specific kernel
harumi run --command "python demos/or_tools_lp.py" --kernel or_python_small --watch

# run the Gurobi knapsack MIP
harumi run --command "python demos/gurobi_knapsack.py" --kernel gurobi_python_medium --watch
```

`--watch` blocks until the run reaches a terminal status and prints `stdout`/`stderr`/`error` on failure; add `--output-dir ./out` to download any committed output artifacts on success. Without `--watch`, check on the run later with `harumi runs get <RUN_ID>`.

`project_demo/` is shaped to exercise a multi-file run: commit the whole directory, then set `command` to `python demos/project_demo/main.py` so it can `import costs`.

