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
    ProjectShareStatus,
    ProjectWithRepo,
    RepoDirListing,
    RepoInfo,
    Schedule,
    Secret,
    TemplateSummary,
)

__version__ = "0.4.2"

__all__ = [
    "Client",
    "BranchInfo",
    "GitCredentials",
    "KernelSpec",
    "LoggedUser",
    "Project",
    "ProjectExecuteResponse",
    "ProjectRun",
    "ProjectShareStatus",
    "ProjectWithRepo",
    "RepoDirListing",
    "RepoInfo",
    "Schedule",
    "Secret",
    "TemplateSummary",
    "__version__",
]
