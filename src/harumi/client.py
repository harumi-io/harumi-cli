"""HTTP client for harumi-api: auth injection, 401-refresh-and-retry, and the
public `Client` SDK surface used by both the CLI and library consumers.

Assumed git-pivot endpoints (Workstream B/C — not yet in harumi-api):
  GET  /projects/{id}/repo           -> ProjectRepo
  GET  /projects/{id}/repo/branches  -> list[ProjectRepoBranch]
  POST /projects/{id}/execute        -> ProjectRunResponse
  POST /users/git-token              -> GitUserToken
  POST /projects/{project_id}/schedules (+ /{schedule_id})  -> Schedule

Methods wrapping assumed endpoints are clearly marked.  When the coworker's
branch lands, update the path strings and schema field names here — all
callers in cli.py go through this layer so changes are contained.

Datasource endpoints (below) are real and exist today in harumi-api
(src/api/datasources/router.py) — no wrapping needed.

POST /projects is also real today, but only creates a bare project (no
repo) — create_project() calls it and clearly flags the still-assumed
repo-provisioning contract if `repo` comes back empty. See create_project().
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote

import httpx

from harumi import auth
from harumi.config import Config
from harumi.errors import ApiError, HarumiError, NotAuthenticatedError
from harumi.models import (
    ConnectionTestResponse,
    Datasource,
    DatasourceList,
    ExecutionOutput,
    GitUserToken,
    KernelSpec,
    Notebook,
    Project,
    ProjectRepo,
    ProjectRepoBranch,
    ProjectRunResponse,
    ProjectWithRepo,
    QueryResult,
    Schedule,
)


class ApiClient:
    """Low-level HTTP wrapper: injects auth headers and retries once on 401
    after refreshing the session. Not usually used directly — see `Client`.
    """

    def __init__(
        self,
        config: Config,
        timeout: float = 60.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.config = config
        self.timeout = timeout
        # Injectable for tests (httpx.MockTransport); None uses the real network.
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        token = auth.get_valid_access_token(self.config, allow_refresh=True)
        headers = {"Authorization": f"Bearer {token}"}
        if self.config.org_id:
            headers["X-Organization"] = self.config.org_id
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        _retried: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        url = f"{self.config.api_url}{path}"
        headers = self._headers()
        headers.update(kwargs.pop("headers", {}) or {})

        with httpx.Client(timeout=timeout or self.timeout, transport=self.transport) as client:
            response = client.request(
                method, url, json=json, params=params, headers=headers, **kwargs
            )

        if response.status_code == 401 and not _retried:
            creds = auth.current_credentials()
            if creds and creds.get("refresh_token"):
                auth.refresh_session(self.config, creds["refresh_token"], transport=self.transport)
                return self.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    timeout=timeout,
                    _retried=True,
                    **kwargs,
                )
            raise NotAuthenticatedError()

        _raise_for_status(response)
        return response

    @contextmanager
    def stream(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        timeout: Optional[float] = None,
    ) -> Iterator[httpx.Response]:
        """Open a streaming (e.g. SSE) request. Retries once on 401 like
        `request()`, but since the retry needs a fresh connection, the
        auth check happens eagerly before the stream is opened.
        """
        url = f"{self.config.api_url}{path}"
        headers = self._headers()

        with httpx.Client(timeout=timeout or self.timeout, transport=self.transport) as client:
            with client.stream(method, url, json=json, headers=headers) as response:
                if response.status_code == 401:
                    creds = auth.current_credentials()
                    if not creds or not creds.get("refresh_token"):
                        raise NotAuthenticatedError()
                    auth.refresh_session(self.config, creds["refresh_token"], transport=self.transport)
                    headers = self._headers()
                    with client.stream(
                        method, url, json=json, headers=headers
                    ) as retried_response:
                        _raise_for_status(retried_response, streamed=True)
                        yield retried_response
                        return
                _raise_for_status(response, streamed=True)
                yield response


def _raise_for_status(response: httpx.Response, streamed: bool = False) -> None:
    if response.status_code >= 400:
        detail = "" if streamed else response.text
        if not streamed:
            try:
                detail = response.json().get("detail", detail)
            except Exception:
                pass
        raise ApiError(response.status_code, str(detail) or response.reason_phrase)


class Client:
    """Main SDK entry point.

    Usage:
        client = Client()  # loads ~/.harumi/credentials.json
        client.execute_project("proj-id", branch="main")
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        git_url: Optional[str] = None,
        org_id: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.config = Config.load(api_url=api_url, git_url=git_url, org_id=org_id)
        self.api = ApiClient(self.config, transport=transport)

    # -- Auth -------------------------------------------------------------

    def request_otp(self, email: str) -> None:
        auth.request_otp(self.config, email)

    def verify_otp(self, email: str, token: str):
        return auth.verify_otp(self.config, email, token)

    def logout(self) -> None:
        auth.logout()

    # -- Git credential ---------------------------------------------------
    # ASSUMED ENDPOINT: POST /users/git-token
    # Returns the per-user Gitea token (provisioned at first login).
    # Update path/schema when the coworker's harumi-api branch lands.

    def get_git_token(self) -> GitUserToken:
        """Fetch (or create) the per-user Gitea personal access token.

        ASSUMED ENDPOINT — not yet in harumi-api.
        """
        try:
            response = self.api.request("POST", "/users/git-token")
        except ApiError as exc:
            raise HarumiError(
                "The Gitea user provisioning endpoint (/users/git-token) is not yet "
                "available on this harumi-api version. "
                "Ask your team when Workstream B of the git-first pivot lands."
            ) from exc
        return GitUserToken.model_validate(response.json())

    # -- Discovery --------------------------------------------------------

    def list_projects(self) -> list[Project]:
        response = self.api.request("GET", "/projects")
        data = response.json()
        projects = data.get("projects", data) if isinstance(data, dict) else data
        return [Project.model_validate(p) for p in projects]

    def list_notebooks(self, project_id: str) -> list[Notebook]:
        response = self.api.request("GET", f"/projects/{project_id}/notebooks")
        return [Notebook.model_validate(n) for n in response.json()]

    def get_specs(self) -> list[KernelSpec]:
        response = self.api.request("GET", "/sandbox/specs")
        return [KernelSpec.model_validate(s) for s in response.json()]

    # -- Project creation ---------------------------------------------------
    # POST /projects is a REAL, live endpoint today — but it only creates a
    # bare project row (name/customer_id/notebook_ids/template_id). It does
    # NOT yet provision a Gitea repo. Per the git-first pivot, project
    # creation is expected to become atomic (create project + provision its
    # repo in one call). This method calls the real endpoint and clearly
    # flags the missing `repo` field rather than silently returning a
    # half-usable project (one you can't `harumi init` into yet).

    def create_project(
        self,
        name: str,
        customer_id: Optional[str] = None,
        template_id: Optional[str] = None,
    ) -> ProjectWithRepo:
        """Create a new Harumi project.

        Calls the real POST /projects endpoint. Once the git-first pivot
        lands, the response is expected to include a `repo` field (the
        project's auto-provisioned Gitea repo) so the project is immediately
        usable with `harumi init`. Until then, raises HarumiError if `repo`
        is absent so callers don't proceed assuming a repo exists.
        """
        body: dict[str, Any] = {"name": name}
        if customer_id:
            body["customer_id"] = customer_id
        if template_id:
            body["template_id"] = template_id

        response = self.api.request("POST", "/projects", json=body)
        project = ProjectWithRepo.model_validate(response.json())
        if project.repo is None:
            raise HarumiError(
                f"Project {project.id!r} was created, but harumi-api did not return "
                "repo metadata (no Gitea repo was provisioned). Atomic project+repo "
                "creation is not yet available on this harumi-api version — ask your "
                "team when the git-first pivot's project creation flow lands, then "
                "retry `harumi init --project " + project.id + "` once it does."
            )
        return project

    # -- Git-pivot: repo metadata -----------------------------------------
    # ASSUMED ENDPOINTS: GET /projects/{id}/repo and /repo/branches
    # Update paths/schemas when the coworker's harumi-api branch lands.

    def get_project_repo(self, project_id: str) -> ProjectRepo:
        """Return the Gitea repo bound to a project.

        ASSUMED ENDPOINT — not yet in harumi-api.
        """
        try:
            response = self.api.request("GET", f"/projects/{project_id}/repo")
        except ApiError as exc:
            raise HarumiError(
                f"Could not fetch repo for project {project_id!r}. "
                "The /projects/{id}/repo endpoint is not yet available on this "
                "harumi-api version. Ask your team when Workstream B lands."
            ) from exc
        return ProjectRepo.model_validate(response.json())

    def list_repo_branches(self, project_id: str) -> list[ProjectRepoBranch]:
        """List branches for the Gitea repo bound to a project.

        ASSUMED ENDPOINT — not yet in harumi-api.
        """
        try:
            response = self.api.request("GET", f"/projects/{project_id}/repo/branches")
        except ApiError as exc:
            raise HarumiError(
                f"Could not list branches for project {project_id!r}. "
                "The /projects/{id}/repo/branches endpoint is not yet available."
            ) from exc
        return [ProjectRepoBranch.model_validate(b) for b in response.json()]

    # -- Git-pivot: execution ---------------------------------------------
    # ASSUMED ENDPOINT: POST /projects/{id}/execute
    # Update path/schema when the coworker's harumi-api branch lands.

    def execute_project(
        self,
        project_id: str,
        branch: Optional[str] = None,
        commit: Optional[str] = None,
        command: Optional[str] = None,
        kernel_spec: Optional[str] = None,
    ) -> ProjectRunResponse:
        """Queue a git-ref-based run for a project.

        ASSUMED ENDPOINT — not yet in harumi-api.
        """
        body: dict[str, Any] = {}
        if branch:
            body["branch"] = branch
        if commit:
            body["commit"] = commit
        if command:
            body["command"] = command
        if kernel_spec:
            body["kernel_spec"] = kernel_spec

        try:
            response = self.api.request("POST", f"/projects/{project_id}/execute", json=body)
        except ApiError as exc:
            raise HarumiError(
                f"Could not queue a run for project {project_id!r}. "
                "The /projects/{id}/execute endpoint is not yet available on this "
                "harumi-api version. Ask your team when Workstream C of the git-first pivot lands."
            ) from exc
        return ProjectRunResponse.model_validate(response.json())

    # -- Git-pivot: schedules ----------------------------------------------
    # ASSUMED ENDPOINTS: /projects/{id}/schedules (project-scoped re-key of
    # the current notebook_id-scoped /notebooks/{id}/schedules).
    # Update paths/schemas when the coworker's harumi-api branch lands.

    def list_schedules(self, project_id: str) -> list[Schedule]:
        """List cron schedules for a project.

        ASSUMED ENDPOINT — not yet in harumi-api.
        """
        try:
            response = self.api.request("GET", f"/projects/{project_id}/schedules")
        except ApiError as exc:
            raise HarumiError(
                f"Could not list schedules for project {project_id!r}. "
                "The /projects/{id}/schedules endpoint is not yet available on this "
                "harumi-api version. Ask your team when Workstream C of the git-first pivot lands."
            ) from exc
        return [Schedule.model_validate(s) for s in response.json()]

    def get_schedule(self, project_id: str, schedule_id: str) -> Schedule:
        """Fetch one schedule for a project.

        ASSUMED ENDPOINT — not yet in harumi-api.
        """
        try:
            response = self.api.request(
                "GET", f"/projects/{project_id}/schedules/{schedule_id}"
            )
        except ApiError as exc:
            raise HarumiError(
                f"Could not fetch schedule {schedule_id!r} for project {project_id!r}. "
                "The /projects/{id}/schedules endpoint is not yet available on this "
                "harumi-api version. Ask your team when Workstream C of the git-first pivot lands."
            ) from exc
        return Schedule.model_validate(response.json())

    def create_schedule(self, project_id: str, body: dict[str, Any]) -> Schedule:
        """Create a cron schedule for a project.

        ASSUMED ENDPOINT — not yet in harumi-api.
        """
        try:
            response = self.api.request(
                "POST", f"/projects/{project_id}/schedules", json=body
            )
        except ApiError as exc:
            raise HarumiError(
                f"Could not create a schedule for project {project_id!r}. "
                "The /projects/{id}/schedules endpoint is not yet available on this "
                "harumi-api version. Ask your team when Workstream C of the git-first pivot lands."
            ) from exc
        return Schedule.model_validate(response.json())

    def update_schedule(
        self, project_id: str, schedule_id: str, body: dict[str, Any]
    ) -> Schedule:
        """Partially update a cron schedule for a project.

        ASSUMED ENDPOINT — not yet in harumi-api.
        """
        try:
            response = self.api.request(
                "PUT", f"/projects/{project_id}/schedules/{schedule_id}", json=body
            )
        except ApiError as exc:
            raise HarumiError(
                f"Could not update schedule {schedule_id!r} for project {project_id!r}. "
                "The /projects/{id}/schedules endpoint is not yet available on this "
                "harumi-api version. Ask your team when Workstream C of the git-first pivot lands."
            ) from exc
        return Schedule.model_validate(response.json())

    def delete_schedule(self, project_id: str, schedule_id: str) -> Schedule:
        """Delete a cron schedule for a project (the only way to stop it firing).

        ASSUMED ENDPOINT — not yet in harumi-api.
        """
        try:
            response = self.api.request(
                "DELETE", f"/projects/{project_id}/schedules/{schedule_id}"
            )
        except ApiError as exc:
            raise HarumiError(
                f"Could not delete schedule {schedule_id!r} for project {project_id!r}. "
                "The /projects/{id}/schedules endpoint is not yet available on this "
                "harumi-api version. Ask your team when Workstream C of the git-first pivot lands."
            ) from exc
        return Schedule.model_validate(response.json())

    # -- Datasources --------------------------------------------------------
    # Real endpoints — exist today in harumi-api (src/api/datasources/router.py).
    # Scoped per-project; credentials are write-only (never returned).

    def list_datasources(self, project_id: str, limit: int = 100, offset: int = 0) -> DatasourceList:
        response = self.api.request(
            "GET",
            f"/datasources/{project_id}",
            params={"limit": limit, "offset": offset},
        )
        return DatasourceList.model_validate(response.json())

    def get_datasource(self, project_id: str, name: str) -> Datasource:
        response = self.api.request("GET", f"/datasources/{project_id}/{quote(name, safe='')}")
        return Datasource.model_validate(response.json())

    def create_datasource(self, project_id: str, body: dict[str, Any]) -> Datasource:
        response = self.api.request("POST", f"/datasources/{project_id}", json=body)
        return Datasource.model_validate(response.json())

    def update_datasource(self, project_id: str, name: str, body: dict[str, Any]) -> Datasource:
        response = self.api.request(
            "PUT", f"/datasources/{project_id}/{quote(name, safe='')}", json=body
        )
        return Datasource.model_validate(response.json())

    def delete_datasource(self, project_id: str, name: str) -> Datasource:
        response = self.api.request("DELETE", f"/datasources/{project_id}/{quote(name, safe='')}")
        return Datasource.model_validate(response.json())

    def test_datasource_connection(self, body: dict[str, Any]) -> ConnectionTestResponse:
        response = self.api.request("POST", "/datasources/test-connection", json=body)
        return ConnectionTestResponse.model_validate(response.json())

    def execute_datasource_query(
        self,
        project_id: str,
        name: str,
        query: str,
        dataframe_name: str = "df",
        limit: int = 10000,
    ) -> QueryResult:
        response = self.api.request(
            "POST",
            f"/datasources/{project_id}/{quote(name, safe='')}/execute",
            json={"query": query, "dataframe_name": dataframe_name, "limit": limit},
        )
        return QueryResult.model_validate(response.json())

    # -- Outputs ----------------------------------------------------------

    def list_outputs(self, notebook_id: str) -> list[ExecutionOutput]:
        from harumi.execution import list_outputs
        return list_outputs(self.api, notebook_id)

    def wait_for_output(self, notebook_id: str, output_id: str, **kwargs: Any):
        from harumi.execution import wait_for_output
        return wait_for_output(self.api, notebook_id, output_id, **kwargs)

    def download_output(self, notebook_id: str, output_id: str, dest_dir: Path | str):
        from harumi.execution import download_output
        return download_output(self.api, notebook_id, output_id, Path(dest_dir))
