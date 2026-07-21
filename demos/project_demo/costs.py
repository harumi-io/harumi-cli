"""Reusable helpers for the multi-file `project_demo` — separate module to
show that `harumi run` (job mode) uploads whole directories, not just a
single script.
"""

from dataclasses import dataclass


@dataclass
class Order:
    name: str
    quantity: int
    unit_cost: float

    @property
    def total_cost(self) -> float:
        return self.quantity * self.unit_cost


def total_spend(orders: list[Order]) -> float:
    return sum(order.total_cost for order in orders)
