"""0/1 knapsack solved as a MIP with Gurobi.

Pick the subset of items that maximizes total value without exceeding the
knapsack's weight capacity. Good for exercising the `gurobi_python_*`
kernels, which have a Gurobi license already configured on Harumi's infra.

    harumi run demos/gurobi_knapsack.py --notebook <NOTEBOOK_ID> \\
        --mode interactive --kernel gurobi_python_medium

(Interactive mode sends this file's code as-is to the sandbox and streams
the result back. For a long/heavy run instead, save this code as the
notebook's live version in the Harumi web app, then use
`--mode job --watch` to queue it and check back later — job mode uploads
supporting files but does not push local code into the notebook.)

Requires the `gurobipy` package and a valid license, both provided by the
`gurobi_python_*` kernels — no local Gurobi install needed to run it there.
"""

import gurobipy as gp
from gurobipy import GRB

ITEMS = {
    "laptop": (2500, 3),
    "camera": (1500, 2),
    "tent": (400, 6),
    "sleeping_bag": (200, 3),
    "first_aid_kit": (150, 1),
    "water_filter": (300, 1),
}
CAPACITY = 10  # max total weight the knapsack can carry


def main() -> None:
    model = gp.Model("knapsack")
    model.Params.OutputFlag = 0  # keep stdout clean; drop this to see solver logs

    take = model.addVars(ITEMS.keys(), vtype=GRB.BINARY, name="take")

    model.setObjective(
        gp.quicksum(take[item] * value for item, (value, _weight) in ITEMS.items()),
        GRB.MAXIMIZE,
    )
    model.addConstr(
        gp.quicksum(take[item] * weight for item, (_value, weight) in ITEMS.items()) <= CAPACITY,
        name="capacity",
    )

    model.optimize()

    if model.Status == GRB.OPTIMAL:
        chosen = [item for item in ITEMS if take[item].X > 0.5]
        total_weight = sum(ITEMS[item][1] for item in chosen)
        print("Optimal packing:")
        for item in chosen:
            value, weight = ITEMS[item]
            print(f"  {item}: value={value}, weight={weight}")
        print(f"Total value: {model.ObjVal:.0f}, total weight: {total_weight}/{CAPACITY}")
    else:
        print(f"No optimal solution (status={model.Status}).")


if __name__ == "__main__":
    main()
