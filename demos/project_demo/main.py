"""Entry point for the multi-file directory demo.

Shows the job-mode upload path: `harumi run` on a directory uploads every
file in it (preserving subpaths) to the notebook's project workspace, so a
notebook whose live version imports local modules has them available.

    harumi run demos/project_demo --notebook <NOTEBOOK_ID> --mode job --watch

Job mode re-runs the notebook's saved live version rather than this
`main.py` directly — set the notebook's live version to this file's logic
in the Harumi web app first. To execute this exact file's code immediately
instead, use `--mode interactive` on a single file (see hello_world.py).
"""

from costs import Order, total_spend

orders = [
    Order(name="steel_beams", quantity=40, unit_cost=125.0),
    Order(name="bolts", quantity=2000, unit_cost=0.15),
    Order(name="paint", quantity=15, unit_cost=32.0),
]

for order in orders:
    print(f"{order.name}: {order.quantity} units @ ${order.unit_cost:.2f} = ${order.total_cost:,.2f}")

print(f"\nTotal spend: ${total_spend(orders):,.2f}")
