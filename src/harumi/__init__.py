"""Run local code on Harumi's infrastructure.

Public API:

    from harumi import Client, login, run_job, run_interactive

    client = Client()                 # loads stored credentials
    result = client.run_job("solver.py", notebook_id="...", watch=True)
"""

from __future__ import annotations

from harumi.client import Client
from harumi.models import (
    ExecutionOutput,
    InteractiveResult,
    KernelSpec,
    LoggedUser,
)

__version__ = "0.1.0"

__all__ = [
    "Client",
    "ExecutionOutput",
    "InteractiveResult",
    "KernelSpec",
    "LoggedUser",
    "__version__",
]
