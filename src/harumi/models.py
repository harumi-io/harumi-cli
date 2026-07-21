"""Pydantic models mirroring harumi-api response shapes.

These intentionally mirror (a subset of) the backend schemas so the CLI/SDK
can parse responses without depending on harumi-api's package:
  - LoggedUser  <-> harumi-api/src/api/users/schemas.py:LoggedUser
  - KernelSpec  <-> harumi-api/src/api/sandbox/specs.py:get_all_specs()
  - ExecutionOutput <-> harumi-api/src/api/notebooks/schemas.py:NotebookOutput
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


class ProjectFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    key: str
    last_modified: Optional[datetime] = None
    etag: Optional[str] = None
    size: Optional[int] = None


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


class NotebookExecuteResponse(BaseModel):
    """Response from POST /notebooks/{id}/execute."""

    model_config = ConfigDict(extra="allow")

    task_id: str
    status: str
    message: str
    output_id: Optional[str] = None
    execution_log_id: Optional[str] = None


class InteractiveResult(BaseModel):
    """Aggregated result of an interactive SSE run (see sse.py)."""

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
