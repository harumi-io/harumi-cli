"""Pydantic models mirroring harumi-api response shapes.

These intentionally mirror (a subset of) the backend schemas so the CLI/SDK
can parse responses without depending on harumi-api's package:
  - LoggedUser  <-> harumi-api/src/api/users/schemas.py:LoggedUser
  - KernelSpec  <-> harumi-api/src/api/sandbox/specs.py:get_all_specs()
  - ExecutionOutput <-> harumi-api/src/api/notebooks/schemas.py:NotebookOutput
  - ProjectRepo / ProjectRunResponse <-> assumed git-pivot endpoints (Workstream B/C)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class LoggedUser(BaseModel):
    """Response from /users/otp/verify and /users/refresh."""

    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    email: Optional[str] = None
    first_name: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_at: Optional[int] = None


class KernelSpecSize(BaseModel):
    name: str
    cpu: float
    memory: str
    gpu: bool = False


class KernelSpec(BaseModel):
    """One entry from GET /sandbox/specs."""

    name: str
    display_name: str
    language: str
    description: str
    size: KernelSpecSize
    subscription_required: bool = False
    icon: Optional[str] = None


class Project(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    kernel_spec: Optional[str] = None
    notebook_ids: list[str] = Field(default_factory=list)


class Notebook(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: Optional[str] = None


class ExecutionOutput(BaseModel):
    """One entry from GET /notebooks/{id}/outputs (NotebookOutput schema)."""

    model_config = ConfigDict(extra="allow")

    id: str
    notebook_id: str
    scenario_id: Optional[str] = None
    scenario_name: Optional[str] = None
    status: Optional[str] = None
    started: Optional[datetime] = None
    ended: Optional[datetime] = None
    output_url: Optional[str] = None
    log_url: Optional[str] = None
    execution_log_id: Optional[str] = None

    @property
    def finished(self) -> bool:
        return self.status in {"finished", "completed", "failed", "timeout", "cancelled"}

    @property
    def succeeded(self) -> bool:
        return self.status in {"finished", "completed"}


class InteractiveResult(BaseModel):
    """Aggregated result of a raw SSE execution run (see sse.py).

    Retained so sse.py and its tests continue to work. Not used by the
    git-ref execution path in the CLI.
    """

    status: str = "success"
    stdout: str = ""
    stderr: str = ""
    results: list[dict[str, Any]] = Field(default_factory=list)
    error: Optional[dict[str, Any]] = None
    execution_time_ms: Optional[int] = None
    fs_changes: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "success"


# ---------------------------------------------------------------------------
# Git-pivot models (assumed contract — Workstream B/C of the git-first pivot)
# These mirror the planned endpoints in harumi-api. Update when the coworker's
# branch lands and the real schemas are confirmed.
# ---------------------------------------------------------------------------

class ProjectRepo(BaseModel):
    """Response from GET /projects/{id}/repo.

    Assumed contract — endpoint does not exist yet in harumi-api.
    """

    model_config = ConfigDict(extra="allow")

    owner: str
    name: str
    clone_url: str
    default_branch: str = "main"


class ProjectRepoBranch(BaseModel):
    """One entry from GET /projects/{id}/repo/branches."""

    model_config = ConfigDict(extra="allow")

    name: str
    commit_sha: Optional[str] = None


class ProjectRunResponse(BaseModel):
    """Response from POST /projects/{id}/execute.

    Assumed contract — endpoint does not exist yet in harumi-api.
    Mirrors the shape of NotebookExecuteResponse so wait_for_output
    can be reused unchanged.
    """

    model_config = ConfigDict(extra="allow")

    task_id: str
    status: str
    message: str
    output_id: Optional[str] = None
    execution_log_id: Optional[str] = None


class GitUserToken(BaseModel):
    """Response from POST /users/git-token (assumed — per-user Gitea token).

    Assumed contract — endpoint does not exist yet in harumi-api.
    """

    model_config = ConfigDict(extra="allow")

    token: str
    username: str


# ---------------------------------------------------------------------------
# Datasource models <-> harumi-api/src/api/datasources/schemas.py
# These endpoints exist today (not assumed) — see client.py.
# ---------------------------------------------------------------------------

class Datasource(BaseModel):
    """Response shape for datasource endpoints (never includes credentials)."""

    model_config = ConfigDict(extra="allow")

    id: str
    project_id: str
    name: str
    type: str
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    use_proxy: bool = False
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_server_name: Optional[str] = None
    ssm_parameter_name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DatasourceList(BaseModel):
    """Response from GET /datasources/{project_id}."""

    model_config = ConfigDict(extra="allow")

    datasources: list[Datasource] = Field(default_factory=list)
    total_count: int = 0


class ConnectionTestResponse(BaseModel):
    """Response from POST /datasources/test-connection."""

    model_config = ConfigDict(extra="allow")

    success: bool
    message: str


class QueryResult(BaseModel):
    """Response from POST /datasources/{project_id}/{name}/execute."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    columns: list[str] = Field(default_factory=list)
    data: list[list[Any]] = Field(default_factory=list)
    row_count: int = Field(0, alias="rowCount")
    was_limited: bool = Field(False, alias="wasLimited")
    max_rows: Optional[int] = Field(None, alias="maxRows")
    dataframe_name: Optional[str] = None
