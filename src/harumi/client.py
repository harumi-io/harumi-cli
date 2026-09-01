"""HTTP client for harumi-api: auth injection, 401-refresh-and-retry, and the
public `Client` SDK surface used by both the CLI and library consumers.

All paths below are confirmed live in harumi-api (see src/api/*/router.py in
that repo). Every caller in cli.py goes through this layer so a future path
or schema change only needs to be made here.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote

import httpx

from harumi import auth
from harumi.config import Config
from harumi.errors import ApiError, NotAuthenticatedError
from harumi.models import (
    BranchInfo,
    ConnectionTestResponse,
    Datasource,
    DatasourceList,
    DeleteFilesResult,
    FileDownloadUrl,
    FileUploadUrl,
    GitCredentials,
    KernelSpec,
    Notebook,
    Organization,
    OrganizationMember,
    Project,
    ProjectExecuteResponse,
    ProjectFileList,
    ProjectRun,
    ProjectShareLink,
    ProjectWithRepo,
    PromoteResult,
    QueryResult,
    RepoChangesResult,
    RepoDirListing,
    RepoFileContent,
    RepoFileEntry,
    RepoInfo,
    Schedule,
    Secret,
    TemplateSummary,
    UserProfile,
)


def _q(value: str) -> str:
    """URL-quote a path segment (names/ids that may contain spaces, slashes)."""
    return quote(value, safe="")


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
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Iterator[httpx.Response]:
        """Open a streaming (e.g. file download) request. Retries once on 401
        like `request()`, but since the retry needs a fresh connection, the
        auth check happens eagerly before the stream is opened.
        """
        url = f"{self.config.api_url}{path}"
        headers = self._headers()

        with httpx.Client(timeout=timeout or self.timeout, transport=self.transport) as client:
            with client.stream(method, url, json=json, params=params, headers=headers) as response:
                if response.status_code == 401:
                    creds = auth.current_credentials()
                    if not creds or not creds.get("refresh_token"):
                        raise NotAuthenticatedError()
                    auth.refresh_session(self.config, creds["refresh_token"], transport=self.transport)
                    headers = self._headers()
                    with client.stream(
                        method, url, json=json, params=params, headers=headers
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
        environment: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.config = Config.load(
            api_url=api_url, git_url=git_url, org_id=org_id, environment=environment
        )
        self.api = ApiClient(self.config, transport=transport)

    # -- Auth ---------------------------------------------------------------

    def request_otp(self, email: str) -> None:
        auth.request_otp(self.config, email)

    def verify_otp(self, email: str, token: str):
        return auth.verify_otp(self.config, email, token)

    def logout(self) -> None:
        auth.logout()

    def get_profile(self) -> UserProfile:
        response = self.api.request("GET", "/users/profile")
        return UserProfile.model_validate(response.json())

    def update_profile(self, body: dict[str, Any]) -> UserProfile:
        response = self.api.request("POST", "/users/profile", json=body)
        return UserProfile.model_validate(response.json())

    # -- Git credentials ------------------------------------------------
    # POST /git/credentials — provisions (idempotently) the current user's
    # Gitea identity and returns their CLI token. This is `harumi login`.

    def get_git_token(self) -> GitCredentials:
        """Fetch (or create) the per-user Gitea personal access token."""
        response = self.api.request("POST", "/git/credentials")
        return GitCredentials.model_validate(response.json())

    # -- Discovery ------------------------------------------------------

    def list_projects(self) -> list[Project]:
        response = self.api.request("GET", "/projects")
        data = response.json()
        projects = data.get("projects", data) if isinstance(data, dict) else data
        return [Project.model_validate(p) for p in projects]

    def get_project(self, project_id: str) -> Project:
        response = self.api.request("GET", f"/projects/{project_id}")
        return Project.model_validate(response.json())

    def update_project(self, project_id: str, body: dict[str, Any]) -> Project:
        response = self.api.request("PUT", f"/projects/{project_id}", json=body)
        return Project.model_validate(response.json())

    def delete_project(self, project_id: str) -> Project:
        response = self.api.request("DELETE", f"/projects/{project_id}")
        return Project.model_validate(response.json())

    def list_notebooks(self, project_id: str) -> list[Notebook]:
        response = self.api.request("GET", f"/projects/{project_id}/notebooks")
        return [Notebook.model_validate(n) for n in response.json()]

    def get_specs(self) -> list[KernelSpec]:
        response = self.api.request("GET", "/sandbox/specs")
        return [KernelSpec.model_validate(s) for s in response.json()]

    def list_templates(self) -> list[TemplateSummary]:
        """List project templates (pass a `.id` as `--template-id` to `create_project`)."""
        response = self.api.request("GET", "/templates")
        return [TemplateSummary.model_validate(t) for t in response.json().get("templates", [])]

    # -- Project creation -------------------------------------------------
    # POST /projects creates the project row AND (best-effort, synchronously)
    # provisions its Gitea repo server-side — but the response body is a bare
    # `Project` with no `repo` field. Fetch the repo separately.

    def create_project(
        self,
        name: str,
        customer_id: Optional[str] = None,
        template_id: Optional[str] = None,
        personal: bool = False,
    ) -> ProjectWithRepo:
        """Create a new Harumi project and fetch its (auto-provisioned) repo.

        The owning workspace comes from `customer_id`, defaulting to the
        configured org (`harumi config set-org` / `--org` / `HARUMI_ORG`).
        POST /projects reads the workspace from the body only — it ignores the
        `X-Organization` header that scopes the read endpoints — so without
        this default a configured org would still create the project in the
        caller's personal workspace, where `projects list` (which *does* filter
        by that header) would then hide it. Pass `personal=True` to create in
        the personal workspace despite a configured org; combining it with an
        explicit `customer_id` is contradictory and raises `ValueError` rather
        than silently dropping one of them.

        `repo` is `None` only when Harumi Git isn't configured on this
        backend (e.g. local dev without Gitea) — callers should handle that
        case rather than assume a repo always exists.
        """
        if personal and customer_id:
            raise ValueError(
                "personal=True and customer_id are mutually exclusive. Pass only one."
            )

        owner = None if personal else (customer_id or self.config.org_id)

        body: dict[str, Any] = {"name": name}
        if owner:
            body["customer_id"] = owner
        if template_id:
            body["template_id"] = template_id

        response = self.api.request("POST", "/projects", json=body)
        project = ProjectWithRepo.model_validate(response.json())

        try:
            project.repo = self.get_project_repo(project.id)
        except ApiError as exc:
            if exc.status_code != 404:
                raise
            project.repo = None

        return project

    # -- Repo metadata + branches (versions) -----------------------------

    def get_project_repo(self, project_id: str) -> RepoInfo:
        """Return the Gitea repo bound to a project."""
        response = self.api.request("GET", f"/projects/{project_id}/repo")
        return RepoInfo.model_validate(response.json())

    def list_repo_branches(self, project_id: str) -> list[BranchInfo]:
        """List versions (git branches) for a project's repo."""
        response = self.api.request("GET", f"/projects/{project_id}/repo/branches")
        return [BranchInfo.model_validate(b) for b in response.json()]

    def create_repo_branch(
        self, project_id: str, name: str, from_branch: Optional[str] = None
    ) -> BranchInfo:
        body: dict[str, Any] = {"name": name}
        if from_branch:
            body["from_branch"] = from_branch
        response = self.api.request(
            "POST", f"/projects/{project_id}/repo/branches", json=body
        )
        return BranchInfo.model_validate(response.json())

    def delete_repo_branch(self, project_id: str, name: str) -> None:
        self.api.request("DELETE", f"/projects/{project_id}/repo/branches/{_q(name)}")

    def promote_repo_branch(
        self,
        project_id: str,
        name: str,
        title: Optional[str] = None,
        delete_after: bool = False,
    ) -> PromoteResult:
        body: dict[str, Any] = {"name": name, "delete_after": delete_after}
        if title:
            body["title"] = title
        response = self.api.request(
            "POST", f"/projects/{project_id}/repo/promote", json=body
        )
        return PromoteResult.model_validate(response.json())

    # -- Repo files (the only write path is `apply_repo_changes`) -------

    def list_repo_files(self, project_id: str, ref: Optional[str] = None) -> list[RepoFileEntry]:
        params = {"ref": ref} if ref else None
        response = self.api.request(
            "GET", f"/projects/{project_id}/repo/files", params=params
        )
        return [RepoFileEntry.model_validate(f) for f in response.json()]

    def list_repo_dir(
        self, project_id: str, path: str = "", ref: Optional[str] = None
    ) -> RepoDirListing:
        """One folder level of the repo (GitHub-style repo browser) — use
        `list_repo_files` instead for a flat, whole-repo listing."""
        params: dict[str, Any] = {"path": path}
        if ref:
            params["ref"] = ref
        response = self.api.request(
            "GET", f"/projects/{project_id}/repo/dir", params=params
        )
        return RepoDirListing.model_validate(response.json())

    def get_repo_file(
        self, project_id: str, path: str, ref: Optional[str] = None
    ) -> RepoFileContent:
        params: dict[str, Any] = {"path": path}
        if ref:
            params["ref"] = ref
        response = self.api.request(
            "GET", f"/projects/{project_id}/repo/file-content", params=params
        )
        return RepoFileContent.model_validate(response.json())

    def apply_repo_changes(
        self,
        project_id: str,
        operations: list[dict[str, Any]],
        message: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> RepoChangesResult:
        """Apply create/update/delete/move operations as one commit.

        Each operation is `{action, path, from_path?, content?}` — `content`
        must be base64-encoded (required for create, replaces on update).
        """
        body: dict[str, Any] = {"operations": operations}
        if message:
            body["message"] = message
        if branch:
            body["branch"] = branch
        response = self.api.request(
            "POST", f"/projects/{project_id}/repo/changes", json=body
        )
        return RepoChangesResult.model_validate(response.json())

    def download_repo_archive(
        self,
        project_id: str,
        dest_path: Path | str,
        path: str = "",
        ref: Optional[str] = None,
    ) -> Path:
        """Download the repo (or a folder within it) as a zip to `dest_path`."""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        params: dict[str, Any] = {"path": path}
        if ref:
            params["ref"] = ref
        with self.api.stream(
            "GET",
            f"/projects/{project_id}/repo/archive",
            params=params,
            timeout=300.0,
        ) as response:
            with open(dest_path, "wb") as f:
                for chunk in response.iter_bytes():
                    f.write(chunk)
        return dest_path

    # -- Execution / runs --------------------------------------------------

    def execute_project(
        self,
        project_id: str,
        branch: Optional[str] = None,
        commit: Optional[str] = None,
        command: Optional[str] = None,
        kernel_spec: Optional[str] = None,
        source: str = "cli",
    ) -> ProjectExecuteResponse:
        """Queue a git-ref-based run for a project."""
        body: dict[str, Any] = {"source": source}
        if branch:
            body["branch"] = branch
        if commit:
            body["commit"] = commit
        if command:
            body["command"] = command
        if kernel_spec:
            body["kernel_spec"] = kernel_spec

        response = self.api.request("POST", f"/projects/{project_id}/execute", json=body)
        return ProjectExecuteResponse.model_validate(response.json())

    def list_runs(self, project_id: str) -> list[ProjectRun]:
        from harumi.execution import list_runs

        return list_runs(self.api, project_id)

    def get_run(self, project_id: str, run_id: str) -> ProjectRun:
        from harumi.execution import get_run

        return get_run(self.api, project_id, run_id)

    def get_run_output(self, project_id: str, run_id: str) -> dict[str, Any]:
        from harumi.execution import get_run_output

        return get_run_output(self.api, project_id, run_id)

    def cancel_run(self, project_id: str, run_id: str) -> ProjectRun:
        from harumi.execution import cancel_run

        return cancel_run(self.api, project_id, run_id)

    def get_latest_run(self, project_id: str):
        from harumi.execution import get_latest_run

        return get_latest_run(self.api, project_id)

    def wait_for_run(self, project_id: str, run_id: str, **kwargs: Any) -> ProjectRun:
        from harumi.execution import wait_for_run

        return wait_for_run(self.api, project_id, run_id, **kwargs)

    def download_run_output(self, project_id: str, run: ProjectRun, dest_dir: Path | str) -> Path:
        from harumi.execution import download_run_output

        return download_run_output(self.api, project_id, run, Path(dest_dir))

    # -- Schedules ----------------------------------------------------------
    # /projects/{id}/schedules — project-scoped git-ref cron schedules.

    def list_schedules(self, project_id: str) -> list[Schedule]:
        response = self.api.request("GET", f"/projects/{project_id}/schedules")
        return [Schedule.model_validate(s) for s in response.json().get("schedules", [])]

    def get_schedule(self, project_id: str, schedule_id: str) -> Schedule:
        response = self.api.request(
            "GET", f"/projects/{project_id}/schedules/{schedule_id}"
        )
        return Schedule.model_validate(response.json())

    def create_schedule(self, project_id: str, body: dict[str, Any]) -> Schedule:
        response = self.api.request(
            "POST", f"/projects/{project_id}/schedules", json=body
        )
        return Schedule.model_validate(response.json())

    def update_schedule(
        self, project_id: str, schedule_id: str, body: dict[str, Any]
    ) -> Schedule:
        response = self.api.request(
            "PUT", f"/projects/{project_id}/schedules/{schedule_id}", json=body
        )
        return Schedule.model_validate(response.json())

    def delete_schedule(self, project_id: str, schedule_id: str) -> Schedule:
        response = self.api.request(
            "DELETE", f"/projects/{project_id}/schedules/{schedule_id}"
        )
        return Schedule.model_validate(response.json())

    # -- Datasources --------------------------------------------------------
    # Scoped per-project; credentials are write-only (never returned).

    def list_datasources(self, project_id: str, limit: int = 100, offset: int = 0) -> DatasourceList:
        response = self.api.request(
            "GET",
            f"/datasources/{project_id}",
            params={"limit": limit, "offset": offset},
        )
        return DatasourceList.model_validate(response.json())

    def get_datasource(self, project_id: str, name: str) -> Datasource:
        response = self.api.request("GET", f"/datasources/{project_id}/{_q(name)}")
        return Datasource.model_validate(response.json())

    def create_datasource(self, project_id: str, body: dict[str, Any]) -> Datasource:
        response = self.api.request("POST", f"/datasources/{project_id}", json=body)
        return Datasource.model_validate(response.json())

    def update_datasource(self, project_id: str, name: str, body: dict[str, Any]) -> Datasource:
        response = self.api.request(
            "PUT", f"/datasources/{project_id}/{_q(name)}", json=body
        )
        return Datasource.model_validate(response.json())

    def delete_datasource(self, project_id: str, name: str) -> Datasource:
        response = self.api.request("DELETE", f"/datasources/{project_id}/{_q(name)}")
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
            f"/datasources/{project_id}/{_q(name)}/execute",
            json={"query": query, "dataframe_name": dataframe_name, "limit": limit},
        )
        return QueryResult.model_validate(response.json())

    # -- Secrets --------------------------------------------------------
    # Stored in AWS SSM under /harumi/projects/{id}/secrets/{name}; the
    # delete endpoint's `secret_id` path segment is just the secret's name.

    def list_secrets(self, project_id: str) -> list[Secret]:
        response = self.api.request("GET", f"/projects/{project_id}/secrets")
        return [Secret.model_validate(s) for s in response.json()]

    def create_secret(self, project_id: str, name: str, value: str) -> Secret:
        response = self.api.request(
            "POST", f"/projects/{project_id}/secrets", json={"name": name, "value": value}
        )
        return Secret.model_validate(response.json())

    def delete_secret(self, project_id: str, name: str) -> None:
        self.api.request("DELETE", f"/projects/{project_id}/secrets/{_q(name)}")

    # -- Organizations / members ------------------------------------------

    def list_organizations(self) -> list[Organization]:
        response = self.api.request("GET", "/users/organizations")
        return [Organization.model_validate(o) for o in response.json()]

    def get_organization(self, organization_id: str) -> Organization:
        response = self.api.request("GET", f"/users/organizations/{organization_id}")
        return Organization.model_validate(response.json())

    def create_organization(self, business_name: str) -> Organization:
        response = self.api.request(
            "POST", "/users/organizations", json={"business_name": business_name}
        )
        return Organization.model_validate(response.json())

    def update_organization(self, organization_id: str, business_name: str) -> Organization:
        response = self.api.request(
            "PATCH",
            f"/users/organizations/{organization_id}",
            json={"business_name": business_name},
        )
        return Organization.model_validate(response.json())

    def delete_organization(self, organization_id: str) -> None:
        self.api.request("DELETE", f"/users/organizations/{organization_id}")

    def list_organization_members(self, organization_id: str) -> list[OrganizationMember]:
        response = self.api.request("GET", f"/users/organizations/{organization_id}/users")
        return [OrganizationMember.model_validate(m) for m in response.json()]

    def invite_organization_member(
        self, organization_id: str, email: str, role: str
    ) -> OrganizationMember:
        response = self.api.request(
            "POST",
            f"/users/organizations/{organization_id}/users",
            json={"email": email, "role": role},
        )
        return OrganizationMember.model_validate(response.json())

    def update_organization_member_role(
        self, organization_id: str, user_id: str, role: str
    ) -> OrganizationMember:
        response = self.api.request(
            "PUT",
            f"/users/organizations/{organization_id}/users/{user_id}",
            json={"role": role},
        )
        return OrganizationMember.model_validate(response.json())

    def remove_organization_member(self, organization_id: str, user_id: str) -> None:
        self.api.request(
            "DELETE", f"/users/organizations/{organization_id}/users/{user_id}"
        )

    # -- Project share links -------------------------------------------------
    # /projects/{id}/share-links — a project's public dashboard links (many
    # per project). `password` in the request bodies is never echoed back
    # (see ProjectShareLink.password_set).

    def list_share_links(self, project_id: str) -> list[ProjectShareLink]:
        response = self.api.request("GET", f"/projects/{project_id}/share-links")
        return [
            ProjectShareLink.model_validate(link)
            for link in response.json().get("links", [])
        ]

    def create_share_link(
        self, project_id: str, body: dict[str, Any]
    ) -> ProjectShareLink:
        response = self.api.request(
            "POST", f"/projects/{project_id}/share-links", json=body
        )
        return ProjectShareLink.model_validate(response.json())

    def update_share_link(
        self, project_id: str, link_id: str, body: dict[str, Any]
    ) -> ProjectShareLink:
        response = self.api.request(
            "PATCH", f"/projects/{project_id}/share-links/{link_id}", json=body
        )
        return ProjectShareLink.model_validate(response.json())

    def delete_share_link(self, project_id: str, link_id: str) -> None:
        self.api.request(
            "DELETE", f"/projects/{project_id}/share-links/{link_id}"
        )

    def rotate_share_link(self, project_id: str, link_id: str) -> ProjectShareLink:
        response = self.api.request(
            "POST", f"/projects/{project_id}/share-links/{link_id}/rotate"
        )
        return ProjectShareLink.model_validate(response.json())

    def set_share_link_password(
        self, project_id: str, link_id: str, password: str
    ) -> ProjectShareLink:
        response = self.api.request(
            "PUT",
            f"/projects/{project_id}/share-links/{link_id}/password",
            json={"password": password},
        )
        return ProjectShareLink.model_validate(response.json())

    def remove_share_link_password(
        self, project_id: str, link_id: str
    ) -> ProjectShareLink:
        response = self.api.request(
            "DELETE", f"/projects/{project_id}/share-links/{link_id}/password"
        )
        return ProjectShareLink.model_validate(response.json())

    # -- Project files --------------------------------------------------------
    # /projects/{id}/files* — a project's non-git file storage, distinct from
    # the repo (`repo_*` above): uploads land in a shared bucket under this
    # project's own prefix and appear at `inputs/` inside every run's sandbox.
    # Mirrors `harumi-platform`'s `use-project-files.ts` hooks. The presigned
    # PUT/GET URLs carry their own auth in the query string and must NOT go
    # through `self.api` — adding this client's Authorization/X-Organization
    # headers to them would invalidate the signature.

    def list_project_files(self, project_id: str) -> ProjectFileList:
        response = self.api.request("GET", f"/projects/{project_id}/files")
        return ProjectFileList.model_validate(response.json())

    def create_file_upload_url(
        self, project_id: str, path: str, content_type: str = "application/octet-stream"
    ) -> FileUploadUrl:
        response = self.api.request(
            "POST",
            f"/projects/{project_id}/files/upload-url",
            json={"path": path, "content_type": content_type},
        )
        return FileUploadUrl.model_validate(response.json())

    def create_file_download_url(self, project_id: str, path: str) -> FileDownloadUrl:
        response = self.api.request(
            "GET",
            f"/projects/{project_id}/files/download-url",
            params={"path": path},
        )
        return FileDownloadUrl.model_validate(response.json())

    def delete_project_file(self, project_id: str, path: str) -> DeleteFilesResult:
        response = self.api.request(
            "DELETE", f"/projects/{project_id}/files", params={"path": path}
        )
        return DeleteFilesResult.model_validate(response.json())

    def upload_file_to_presigned_url(
        self, upload_url: FileUploadUrl, local_path: Path, content_type: str
    ) -> None:
        """PUT a local file's bytes straight to S3. No Authorization header —
        the presigned URL's query string is the only auth it accepts."""
        with httpx.Client(timeout=300.0, transport=self.api.transport) as http_client:
            response = http_client.put(
                upload_url.url,
                content=local_path.read_bytes(),
                headers={"Content-Type": content_type},
            )
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.text or response.reason_phrase)

    def download_file_from_presigned_url(
        self, download_url: FileDownloadUrl, dest_path: Path
    ) -> None:
        """GET a file straight from S3 to `dest_path`. No Authorization header,
        same reasoning as `upload_file_to_presigned_url`."""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=300.0, transport=self.api.transport) as http_client:
            with http_client.stream("GET", download_url.url) as response:
                if response.status_code >= 400:
                    response.read()
                    raise ApiError(response.status_code, response.text or response.reason_phrase)
                with open(dest_path, "wb") as f:
                    for chunk in response.iter_bytes():
                        f.write(chunk)

