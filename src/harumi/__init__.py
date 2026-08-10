"""Run local code on Harumi's infrastructure.

Public API:

    from harumi import Client

    client = Client()                 # loads stored credentials
    result = client.execute_project("proj-id", branch="main")
"""

from __future__ import annotations

from harumi.client import Client
from harumi.models import (
    BranchInfo,
    GitCredentials,
    KernelSpec,
    LoggedUser,
    Project,
    ProjectExecuteResponse,
    ProjectRun,
    ProjectWithRepo,
    RepoInfo,
    Schedule,
    Secret,
)

__version__ = "0.3.0"

__all__ = [
    "Client",
    "BranchInfo",
    "GitCredentials",
    "KernelSpec",
    "LoggedUser",
    "Project",
    "ProjectExecuteResponse",
    "ProjectRun",
    "ProjectWithRepo",
    "RepoInfo",
    "Schedule",
    "Secret",
    "__version__",
]
