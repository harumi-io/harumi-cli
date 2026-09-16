"""Run local code on Harumi's infrastructure.

Public API:

    from harumi import Client

    client = Client()                 # loads stored credentials
    result = client.execute_project("proj-id", branch="main")
"""

from __future__ import annotations

from harumi.client import Client
from harumi.models import (
    BlueprintSummary,
    BranchInfo,
    GitCredentials,
    KernelSpec,
    LoggedUser,
    Project,
    ProjectExecuteResponse,
    ProjectRun,
    ProjectShareLink,
    ProjectWithRepo,
    RepoDirListing,
    RepoInfo,
    Schedule,
    Secret,
)

__version__ = "0.7.0"

__all__ = [
    "Client",
    "BlueprintSummary",
    "BranchInfo",
    "GitCredentials",
    "KernelSpec",
    "LoggedUser",
    "Project",
    "ProjectExecuteResponse",
    "ProjectRun",
    "ProjectShareLink",
    "ProjectWithRepo",
    "RepoDirListing",
    "RepoInfo",
    "Schedule",
    "Secret",
    "__version__",
]
