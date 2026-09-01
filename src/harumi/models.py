"""Pydantic models mirroring harumi-api response shapes.

These intentionally mirror (a subset of) the backend schemas so the CLI/SDK
can parse responses without depending on harumi-api's package. Each section
below cites the harumi-api schema file it mirrors.
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


class UserProfile(BaseModel):
    """Response from GET/POST /users/profile <-> users/schemas.py:UserProfile."""

    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    bio: Optional[str] = None
    avatar: Optional[str] = None


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
    """A Harumi project <-> projects/schemas.py:Project."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    customer_id: Optional[str] = None
    kernel_spec: Optional[str] = None
    notebook_ids: list[str] = Field(default_factory=list)
    template_id: Optional[str] = None
    role_name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


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
# Git repo (Gitea) <-> harumi-api/src/api/git/schemas.py — all live endpoints.
# ---------------------------------------------------------------------------

class RepoInfo(BaseModel):
    """Response from GET /projects/{id}/repo."""

    model_config = ConfigDict(extra="allow")

    project_id: Optional[str] = None
    owner: str
    name: str
    clone_url: str
    default_branch: str = "main"


class BranchInfo(BaseModel):
    """One entry from GET /projects/{id}/repo/branches. `is_live` marks the
    default branch — the version apps/schedules always run."""

    model_config = ConfigDict(extra="allow")

    name: str
    commit_sha: Optional[str] = None
    is_live: bool = False


class PromoteResult(BaseModel):
    """Response from POST /projects/{id}/repo/promote."""

    model_config = ConfigDict(extra="allow")

    merged: bool
    conflict: bool = False
    pr_number: Optional[int] = None
    message: Optional[str] = None
    deleted: Optional[bool] = None


class RepoFileEntry(BaseModel):
    """One entry from GET /projects/{id}/repo/files (flat, recursive)."""

    model_config = ConfigDict(extra="allow")

    name: str
    path: str
    type: str
    sha: Optional[str] = None
    size: Optional[int] = None


class RepoCommitInfo(BaseModel):
    """A single Gitea commit, trimmed to what the repo browser renders."""

    model_config = ConfigDict(extra="allow")

    sha: str
    message: str = ""
    author_name: Optional[str] = None
    author_login: Optional[str] = None
    committed_at: Optional[datetime] = None


class RepoDirEntry(BaseModel):
    """One row from GET /projects/{id}/repo/dir: a file or folder, with its
    most recent commit."""

    model_config = ConfigDict(extra="allow")

    name: str
    path: str
    type: str
    sha: Optional[str] = None
    size: Optional[int] = None
    last_commit: Optional[RepoCommitInfo] = None


class RepoDirListing(BaseModel):
    """Response from GET /projects/{id}/repo/dir — one folder level (GitHub-
    style repo browser); use `list_repo_files` instead for a flat listing."""

    model_config = ConfigDict(extra="allow")

    path: str
    ref: str
    latest_commit: Optional[RepoCommitInfo] = None
    total_commits: Optional[int] = None
    entries: list[RepoDirEntry] = Field(default_factory=list)


class RepoFileContent(BaseModel):
    """Response from GET /projects/{id}/repo/file-content. `content` is base64."""

    model_config = ConfigDict(extra="allow")

    path: str
    sha: Optional[str] = None
    encoding: str = "base64"
    content: str = ""


class RepoChangesResult(BaseModel):
    """Response from POST /projects/{id}/repo/changes (the one write endpoint)."""

    model_config = ConfigDict(extra="allow")

    commit_sha: Optional[str] = None
    changed: int = 0


class ProjectWithRepo(BaseModel):
    """A just-created project plus its (best-effort, auto-provisioned) Gitea repo.

    `POST /projects` provisions the repo server-side but returns a bare
    `Project` (no `repo` field); the CLI fetches it separately via
    `GET /projects/{id}/repo`. `repo` is `None` only when Harumi Git isn't
    configured on the backend (e.g. local dev without Gitea).
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    customer_id: Optional[str] = None
    kernel_spec: Optional[str] = None
    notebook_ids: list[str] = Field(default_factory=list)
    repo: Optional[RepoInfo] = None


class GitCredentials(BaseModel):
    """Response from POST /git/credentials (provisions the CLI's Gitea identity)."""

    model_config = ConfigDict(extra="allow")

    username: str
    token: str
    git_url: str


# ---------------------------------------------------------------------------
# Runs <-> harumi-api/src/api/git/schemas.py:ProjectRun/ProjectExecuteResponse
# ---------------------------------------------------------------------------

_TERMINAL_RUN_STATUSES = {"completed", "finished", "failed", "timeout", "cancelled"}
_SUCCESS_RUN_STATUSES = {"completed", "finished"}


class ProjectExecuteResponse(BaseModel):
    """Response from POST /projects/{id}/execute (202)."""

    model_config = ConfigDict(extra="allow")

    execution_log_id: str
    status: str
    workflow_run_id: Optional[str] = None
    project_run_id: Optional[str] = None


class ProjectRun(BaseModel):
    """One entry from GET /projects/{id}/runs, or the single-run detail (which
    additionally populates stdout/stderr/error)."""

    model_config = ConfigDict(extra="allow")

    id: str
    project_id: str
    execution_log_id: Optional[str] = None
    run_type: Optional[str] = None
    source: Optional[str] = None
    git_branch: Optional[str] = None
    git_commit: Optional[str] = None
    command: Optional[str] = None
    kernel_spec: Optional[str] = None
    status: str
    exit_code: Optional[int] = None
    output_url: Optional[str] = None
    triggered_by: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    error: Optional[str] = None
    started: Optional[datetime] = None
    ended: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def finished(self) -> bool:
        return self.status in _TERMINAL_RUN_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status in _SUCCESS_RUN_STATUSES


# ---------------------------------------------------------------------------
# Datasource models <-> harumi-api/src/api/datasources/schemas.py — live.
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


# ---------------------------------------------------------------------------
# Schedule <-> harumi-api/src/api/git/schemas.py:ProjectSchedule — live,
# project-scoped git-ref cron schedules.
# ---------------------------------------------------------------------------

class Schedule(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    project_id: str
    cron: str
    start_at: Optional[datetime] = None
    git_branch: str = "main"
    git_commit: Optional[str] = None
    command: Optional[str] = None
    kernel_spec: str = "or_python_small"
    output_format: Optional[str] = None
    email_to: Optional[str] = None
    status: Optional[str] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    last_executed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Secrets <-> harumi-api/src/api/projects/schemas.py:Secret — live.
# Stored in AWS SSM under /harumi/projects/{id}/secrets/{name}; `secret_id`
# used by the delete endpoint is just the secret's name.
# ---------------------------------------------------------------------------

class Secret(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    value: str


# ---------------------------------------------------------------------------
# Organizations / members <-> harumi-api/src/api/users/schemas.py — live.
# ---------------------------------------------------------------------------

class Organization(BaseModel):
    """Response entry from GET /users/organizations, or from create/update."""

    model_config = ConfigDict(extra="allow")

    id: str
    business_name: str
    role: Optional[str] = None
    role_name: Optional[str] = None


class OrganizationMember(BaseModel):
    """One entry from GET /users/organizations/{id}/users."""

    model_config = ConfigDict(extra="allow")

    user_id: str
    role: str
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    pending: bool = False


# ---------------------------------------------------------------------------
# Templates <-> harumi-api/src/api/templates/schemas.py — live, read-only.
# ---------------------------------------------------------------------------

class TemplateSummary(BaseModel):
    """One entry from GET /templates. Pass its `id` as `--template-id` to
    `harumi projects create`."""

    model_config = ConfigDict(extra="allow")

    id: str
    slug: str
    name: str
    description: str
    is_public: bool = True


class TemplateList(BaseModel):
    """Response from GET /templates."""

    model_config = ConfigDict(extra="allow")

    templates: list[TemplateSummary] = Field(default_factory=list)
    total_count: int = 0


# ---------------------------------------------------------------------------
# Project share links <-> harumi-api/src/api/projects/schemas.py — live.
# ---------------------------------------------------------------------------

class ProjectShareLink(BaseModel):
    """One of a project's public dashboard share links (`GET/POST
    /projects/{id}/share-links`, `PATCH/DELETE .../share-links/{link_id}`,
    `.../rotate`, `.../password`). `password_set` never carries the password
    or its hash — just whether a viewer will be asked for one. The four
    permission flags all default to false: creating a link never silently
    grants more than a bare read-only, latest-run-only dashboard view."""

    model_config = ConfigDict(extra="allow")

    id: str
    project_id: str
    token: str
    label: Optional[str] = None
    enabled: bool = True
    chat_enabled: bool = False
    run_history_enabled: bool = False
    run_control_enabled: bool = False
    io_control_enabled: bool = False
    password_set: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ProjectShareLinkList(BaseModel):
    model_config = ConfigDict(extra="allow")

    links: list[ProjectShareLink] = Field(default_factory=list)

