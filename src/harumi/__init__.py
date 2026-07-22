"""Run local code on Harumi's infrastructure.

Public API:

    from harumi import Client

    client = Client()                 # loads stored credentials
    result = client.execute_project("proj-id", branch="main", watch=True)
"""

from __future__ import annotations

from harumi.client import Client
from harumi.models import (
    ExecutionOutput,
    GitUserToken,
    KernelSpec,
    LoggedUser,
    ProjectRepo,
    ProjectRunResponse,
)

__version__ = "0.1.0"

__all__ = [
    "Client",
    "ExecutionOutput",
    "GitUserToken",
    "KernelSpec",
    "LoggedUser",
    "ProjectRepo",
    "ProjectRunResponse",
    "__version__",
]
