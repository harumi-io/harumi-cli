"""Small linear program solved with Google OR-Tools.

A furniture maker has limited wood and labor hours, and wants to decide how
many tables and chairs to build to maximize profit. Classic LP example —
runs in milliseconds, good for exercising the `or_python_*` kernels.

    harumi run demos/or_tools_lp.py --notebook <NOTEBOOK_ID> \\
        --mode interactive --kernel or_python_small

(Interactive mode sends this file's code as-is to the sandbox and streams
the result back — job mode instead re-runs whatever is already saved as
the notebook's live version, so it wouldn't execute this file directly.)

Requires the `ortools` package, which is preinstalled on Harumi's
`or_python_*` kernels (no local install needed to run it there).
"""

from ortools.linear_solver import pywraplp


def main() -> None:
    solver = pywraplp.Solver.CreateSolver("GLOP")
    if solver is None:
        raise RuntimeError("Could not create GLOP solver")

    # Decision variables: units of tables and chairs to build.
    tables = solver.NumVar(0, solver.infinity(), "tables")
    chairs = solver.NumVar(0, solver.infinity(), "chairs")

    # Resource constraints.
    solver.Add(4 * tables + 3 * chairs <= 240)  # wood (board-feet)
    solver.Add(2 * tables + 1 * chairs <= 100)  # labor (hours)

    # Objective: maximize profit ($70/table, $50/chair).
    solver.Maximize(70 * tables + 50 * chairs)

    status = solver.Solve()

    if status == pywraplp.Solver.OPTIMAL:
        print("Optimal solution found:")
        print(f"  tables = {tables.solution_value():.2f}")
        print(f"  chairs = {chairs.solution_value():.2f}")
        print(f"  profit = ${solver.Objective().Value():.2f}")
    else:
        print("No optimal solution found.")


if __name__ == "__main__":
    main()
