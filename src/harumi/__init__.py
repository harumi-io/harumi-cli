"""Run local code on Harumi's infrastructure.

Public API:

    from harumi import Client

    client = Client()                 # loads stored credentials
    client = Client(token="...")      # or a bearer token, for CI: nothing stored is used or written
    result = client.execute_project("proj-id", branch="main")

This is the CLI's own client, published for scripts. It is not a stability promise: use the CLI's `--json` output for
anything that must not change.
"""

from __future__ import annotations

from harumi.client import Client
from harumi.models import (
    BlueprintSummary,
    BranchInfo,
    GitCredentials,
    KernelSpec,
    LoggedUser,
    Memory,
    Project,
    ProjectExecuteResponse,
    ProjectRun,
    ProjectShareLink,
    ProjectWithRepo,
    RepoDirListing,
    RepoInfo,
    Schedule,
    Secret,
    SkillLibraryDetail,
    SkillLibrarySummary,
)

__version__ = "1.0.0"

__all__ = [
    "Client",
    "BlueprintSummary",
    "BranchInfo",
    "GitCredentials",
    "KernelSpec",
    "LoggedUser",
    "Memory",
    "Project",
    "ProjectExecuteResponse",
    "ProjectRun",
    "ProjectShareLink",
    "ProjectWithRepo",
    "RepoDirListing",
    "RepoInfo",
    "Schedule",
    "Secret",
    "SkillLibraryDetail",
    "SkillLibrarySummary",
    "__version__",
]
