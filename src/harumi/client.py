"""HTTP client for harumi-api: auth injection, 401-refresh-and-retry, and the
public `Client` SDK surface used by both the CLI and library consumers.

All paths below are confirmed live in harumi-api (see src/api/*/router.py in
that repo). Every caller in cli.py goes through this layer so a future path
or schema change only needs to be made here.
"""

from __future__ import annotations

import posixpath
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote

import httpx

from harumi import auth
from harumi.config import Config, environment_scope
from harumi.errors import (
    ApiError,
    HarumiError,
    NotAuthenticatedError,
    guard_json,
    raise_for_status as _raise_for_status,
    transport_errors_as_api_error,
)
from harumi.models import (
    BranchInfo,
    TagInfo,
    BlueprintSummary,
    ConnectionTestResponse,
    CreditUsage,
    Datasource,
    DatasourceList,
    DeleteFilesResult,
    FileDownloadUrl,
    FileUploadUrl,
    GitCredentials,
    KernelSpec,
    Memory,
    MemoryList,
    MyEntitlement,
    Organization,
    OrganizationMember,
    Project,
    ProjectExecuteResponse,
    ProjectFileList,
    ProjectReadiness,
    ProjectRun,
    ProjectShareLink,
    ProjectWithRepo,
    PromoteResult,
    QueryResult,
    RepoChangesResult,
    RepoCommitList,
    RepoDirListing,
    RepoFileContent,
    RepoFileEntry,
    RepoInfo,
    Schedule,
    Secret,
    SkillLibraryDetail,
    SkillLibraryList,
    SkillLibrarySummary,
    UserProfile,
)


def _q(value: str) -> str:
    """URL-quote a path segment (names/ids that may contain spaces, slashes)."""
    return quote(value, safe="")


# Projects come back a page at a time. The server allows up to 1000 per page; a
# smaller page keeps each request quick. The cap only guards against a server that
# never says "no more" (10,000 projects at this size).
PROJECTS_PAGE_SIZE = 200
MAX_PROJECT_PAGES = 50


class _Secret:
    """A bearer token that cannot be printed by accident.

    Anything that formats an object (`repr`, `str`, a logger, a debugger, a traceback with locals) shows `<token ***>`;
    the value comes out only through `reveal()`, which only the request signing calls. It is not a security boundary
    against code in the same process, only against the token ending up in a log."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<token ***>"

    __str__ = __repr__

    def __reduce__(self):  # a pickle or a deepcopy would carry the value somewhere it should not go
        raise TypeError("a token is not copied or pickled")


class ApiClient:
    """Low-level HTTP wrapper: injects auth headers and retries once on 401
    after refreshing the session. Not usually used directly — see `Client`.
    """

    def __init__(
        self,
        config: Config,
        timeout: float = 60.0,
        transport: Optional[httpx.BaseTransport] = None,
        token: Optional[str] = None,
    ) -> None:
        self.config = config
        self.timeout = timeout
        # Injectable for tests (httpx.MockTransport); None uses the real network.
        self.transport = transport
        # A token handed in by the caller (CI) stands in for the stored login: it is sent as given, never saved, and
        # there is nothing to renew behind it.
        self._token: Optional[_Secret] = _Secret(token) if token else None

    def _unauthorized(self) -> NotAuthenticatedError:
        """The error for a 401 that renewing cannot fix. A token has no session to renew, so it must not say to log in."""
        if self._token is not None:
            return NotAuthenticatedError(
                "harumi-api refused the token this Client was given (HTTP 401). Check that it is current and is for "
                "this environment.",
                expired=True,
                environment=self.config.environment,
            )
        return NotAuthenticatedError(expired=True, environment=self.config.environment)

    def _signed(self) -> tuple[dict[str, str], Optional[str]]:
        """(the request headers, the refresh token of the session they were signed with).

        If the server then says 401, renewal is judged against that refresh token, not against whatever the file
        holds by then: another process or thread may already have renewed, and its fresh token must be adopted,
        not spent as if it were stale. The two come from one read of the credentials (`auth.signing_session`):
        read apart, a renewal in between paired the old access token with the new refresh token.
        """
        if self._token is not None:
            headers = {"Authorization": f"Bearer {self._token.reveal()}"}
            if self.config.org_id:
                headers["X-Organization"] = self.config.org_id
            return headers, None  # no refresh token: a 401 is final
        token, refresh = auth.signing_session(self.config, transport=self.transport)
        headers = {"Authorization": f"Bearer {token}"}
        if self.config.org_id:
            headers["X-Organization"] = self.config.org_id
        return headers, refresh

    def _headers(self) -> dict[str, str]:
        return self._signed()[0]

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
        # Everything below reads and writes credentials for the environment this client is for, not for
        # whichever Config was loaded last (see `config.environment_scope`).
        with environment_scope(self.config.environment):
            return self._request(method, path, json=json, params=params, timeout=timeout, _retried=_retried, **kwargs)

    def _request(
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
        headers, sent_with = self._signed()
        headers.update(kwargs.pop("headers", {}) or {})

        with transport_errors_as_api_error(), httpx.Client(
            timeout=timeout or self.timeout, transport=self.transport
        ) as client:
            response = client.request(
                method, url, json=json, params=params, headers=headers, **kwargs
            )

        if response.status_code == 401 and not _retried:
            if sent_with:
                auth.renew_session(self.config, sent_with, transport=self.transport)
                return self.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    timeout=timeout,
                    _retried=True,
                    **kwargs,
                )
            raise self._unauthorized()

        if response.status_code == 401:
            # Refreshed and retried, and still refused: the login is no good.
            raise self._unauthorized()
        _raise_for_status(response)
        return guard_json(response)

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

        Keep the `with` body to reading `response`: an `httpx.HTTPError` raised
        there is reported as an interrupted transfer, which would mislabel one
        from an unrelated request made inside the block.
        """
        url = f"{self.config.api_url}{path}"
        # Scoped for the credential work only. The caller's `with` body is theirs and must not run inside it.
        with environment_scope(self.config.environment):
            headers, sent_with = self._signed()

        with httpx.Client(timeout=timeout or self.timeout, transport=self.transport) as client, ExitStack() as stack:
            # Only *opening* the stream means "could not reach harumi-api". A failure while the
            # caller reads the body is a dropped transfer, reported below without the
            # "is Harumi down?" framing.
            with transport_errors_as_api_error():
                response = stack.enter_context(
                    client.stream(method, url, json=json, params=params, headers=headers)
                )
            if response.status_code == 401:
                # The 401 was entered on the same stack as the retry, so without
                # this its connection stayed open for the whole download.
                response.close()
                if not sent_with:
                    raise self._unauthorized()
                with environment_scope(self.config.environment):
                    auth.renew_session(self.config, sent_with, transport=self.transport)
                    headers, _ = self._signed()
                with transport_errors_as_api_error():
                    response = stack.enter_context(
                        client.stream(method, url, json=json, params=params, headers=headers)
                    )
            if response.status_code == 401:
                raise NotAuthenticatedError(expired=True, environment=self.config.environment)
            _raise_for_status(response, streamed=True)
            try:
                yield response
            except httpx.HTTPError as exc:
                raise ApiError(0, f"Transfer interrupted: {exc}") from exc


# harumi-api scans an uploaded file for up to `clamav_scan_timeout_seconds` (120) before it answers.
_COMPLETE_UPLOAD_TIMEOUT = 180.0
_QUARANTINE_PREFIX = ".quarantine/"


def upload_is_staged(project_id: str, key: str, requested: Optional[str] = None) -> bool:
    """Whether `key` is a staging key that still needs `complete_file_upload`.

    harumi-api issues `{project}/.quarantine/<one flat name>`. A server that predates the
    quarantine change returns the file's own final key, so for it the extra call is skipped
    rather than sent to a route it does not have.

    `requested` is the path the user asked for. If the key is exactly `{project}/{that path}`
    (after the normalising the server does), it is the file's own key and final, whatever it
    contains: `put x .quarantine/x.csv` on an older server returns `{project}/.quarantine/x.csv`,
    which is a plain user path and must not be mistaken for staging.

    Only when the key differs from the requested path does its shape matter, and then the match is
    looser than today's exact shape: if the server moves the staging area (a nested name, a prefix),
    a strict match would say "not staged", skip the completion and report success for a file that
    never appears. So `.quarantine` as the first path segment, or as the segment right after the
    project, counts. A user's own folder of that name deeper inside the project does not.
    """
    if requested is not None:
        own = f"{project_id}/{posixpath.normpath(requested.strip('/'))}"
        if key == own:
            return False
    parts = [p for p in key.split("/") if p]
    staging = _QUARANTINE_PREFIX.rstrip("/")
    for i, part in enumerate(parts[:-1]):  # the last segment is a name, never the staging folder
        if part != staging:
            continue
        before = parts[:i]
        # directly at the front, or only a prefix and the project id in front of it
        if not before or before[-1] == project_id:
            return True
    return False


class Client:
    """Main SDK entry point.

    Usage:
        client = Client()  # loads ~/.harumi/credentials.json
        client.execute_project("proj-id", branch="main")

        client = Client(token=os.environ["HARUMI_TOKEN"])  # CI: no `harumi login` needed

    `token` is a bearer token you already have. It is sent as given and nothing else is read or written: no stored
    login is used, nothing is saved, and it is hidden from `repr`. With a token there is no session to renew, so a 401
    raises `NotAuthenticatedError` at once, and `logout()` and the login methods are not available.
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        git_url: Optional[str] = None,
        org_id: Optional[str] = None,
        environment: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
        token: Optional[str] = None,
    ) -> None:
        if token is not None and not token.strip():
            # `Client(token=os.environ.get("X"))` with X unset must not quietly fall back to the stored login.
            raise HarumiError("`token` is empty. Pass the token itself, or leave it out to use the stored login.")
        self.config = Config.load(
            api_url=api_url, git_url=git_url, org_id=org_id, environment=environment
        )
        self.api = ApiClient(self.config, transport=transport, token=token.strip() if token else None)
        self._has_token = token is not None

    # -- Auth ---------------------------------------------------------------

    def _no_login_with_a_token(self, what: str) -> None:
        if self._has_token:
            raise HarumiError(
                f"{what} is not available on a Client created with a token: there is no stored login behind it. "
                "Create a Client without `token` to sign in."
            )

    def request_otp(self, email: str) -> None:
        self._no_login_with_a_token("Signing in")
        auth.request_otp(self.config, email, transport=self.api.transport)

    def verify_otp(self, email: str, token: str):
        self._no_login_with_a_token("Signing in")
        with environment_scope(self.config.environment):  # saves the new login
            return auth.verify_otp(self.config, email, token, transport=self.api.transport)

    def logout(self) -> None:
        self._no_login_with_a_token("Logging out")  # it would delete a stored login this Client never used
        with environment_scope(self.config.environment):  # forgets this client's login, not another's
            auth.logout()

    def get_profile(self) -> UserProfile:
        response = self.api.request("GET", "/users/profile")
        return UserProfile.model_validate(response.json())

    def update_profile(self, body: dict[str, Any]) -> UserProfile:
        response = self.api.request("POST", "/users/profile", json=body)
        return UserProfile.model_validate(response.json())

    # -- Billing --------------------------------------------------------
    # /billing/usage — the caller's personal credit allowance, or (with the
    # client's configured org — see `--org`/`X-Organization`) that org's
    # pooled one, same scoping every other org-aware method here already gets
    # for free via `ApiClient._headers()`.

    def get_credit_usage(self) -> CreditUsage:
        response = self.api.request("GET", "/billing/usage")
        return CreditUsage.model_validate(response.json())

    def get_entitlement(self) -> MyEntitlement:
        """GET /billing/entitlement/me — which workspaces (personal, each org)
        the caller can work in. Not scoped by `X-Organization`: it lists all."""
        response = self.api.request("GET", "/billing/entitlement/me")
        return MyEntitlement.model_validate(response.json())

    # -- Git credentials ------------------------------------------------
    # POST /git/credentials — provisions (idempotently) the current user's
    # Gitea identity and returns their CLI token. This is `harumi login`.

    def get_git_token(self) -> GitCredentials:
        """Fetch (or create) the per-user Gitea personal access token."""
        response = self.api.request("POST", "/git/credentials")
        return GitCredentials.model_validate(response.json())

    # -- Discovery ------------------------------------------------------

    def list_projects(self) -> list[Project]:
        """Every project the user can see, however many there are."""
        return self.list_projects_with_total()[0]

    def list_projects_with_total(self) -> tuple[list[Project], int]:
        """Every project the user can see, plus how many the server says exist.

        `GET /projects` returns one page (100 by default) in creation order, with
        the real count in `total_count`. Asking once and stopping silently dropped
        every project past the first page, so this follows the pages. The two
        numbers differ only if the safety cap was hit (or projects changed
        mid-listing), which callers should say out loud rather than hide.
        """
        found: dict[str, Project] = {}
        total: Optional[int] = None
        offset = 0
        for _ in range(MAX_PROJECT_PAGES):
            response = self.api.request(
                "GET", "/projects", params={"limit": PROJECTS_PAGE_SIZE, "offset": offset}
            )
            data = response.json()
            rows = data.get("projects", data) if isinstance(data, dict) else data
            if isinstance(data, dict) and data.get("total_count") is not None:
                total = int(data["total_count"])
            before = len(found)
            for row in rows:
                project = Project.model_validate(row)
                found.setdefault(project.id, project)
            offset += len(rows)
            if (
                len(rows) < PROJECTS_PAGE_SIZE  # a short page is the last page
                or len(found) == before  # nothing new: a server that ignores paging
                or (total is not None and offset >= total)
            ):
                break
        return list(found.values()), max(total or 0, len(found))

    def get_project(self, project_id: str) -> Project:
        response = self.api.request("GET", f"/projects/{project_id}")
        return Project.model_validate(response.json())

    def update_project(self, project_id: str, body: dict[str, Any]) -> Project:
        response = self.api.request("PUT", f"/projects/{project_id}", json=body)
        return Project.model_validate(response.json())

    def delete_project(self, project_id: str) -> Project:
        response = self.api.request("DELETE", f"/projects/{project_id}")
        return Project.model_validate(response.json())

    def get_specs(self) -> list[KernelSpec]:
        response = self.api.request("GET", "/sandbox/specs")
        return [KernelSpec.model_validate(s) for s in response.json()]

    def list_blueprints(self) -> list[BlueprintSummary]:
        """List project blueprints (pass a `.slug` as `--blueprint` to `create_project`)."""
        response = self.api.request("GET", "/blueprints")
        return [BlueprintSummary.model_validate(b) for b in response.json().get("blueprints", [])]

    # -- Skill library -----------------------------------------------------
    # Read-only presets served by harumi-api; installing one is a file write
    # into the project's repo (see `harumi skills add`).

    def list_skill_library(self) -> list[SkillLibrarySummary]:
        response = self.api.request("GET", "/skill-library")
        return SkillLibraryList.model_validate(response.json()).skills

    def get_skill_library(self, slug: str) -> SkillLibraryDetail:
        response = self.api.request("GET", f"/skill-library/{_q(slug)}")
        return SkillLibraryDetail.model_validate(response.json())

    # -- Project creation -------------------------------------------------
    # POST /projects creates the project row AND (best-effort, synchronously)
    # provisions its Gitea repo server-side — but the response body is a bare
    # `Project` with no `repo` field. Fetch the repo separately.

    def create_project(
        self,
        name: str,
        customer_id: Optional[str] = None,
        blueprint: Optional[str] = None,
        personal: bool = False,
    ) -> ProjectWithRepo:
        """Create a new Harumi project and fetch its (auto-provisioned) repo.

        The owning workspace comes from `customer_id`, defaulting to the
        configured org (`harumi org use` / `--org` / `HARUMI_ORG`).
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
        if blueprint:
            body["blueprint"] = blueprint

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

    # -- Tags: movable labels on a commit (`prod`) that runs and schedules can follow ---

    def list_repo_tags(self, project_id: str) -> list[TagInfo]:
        response = self.api.request("GET", f"/projects/{project_id}/repo/tags")
        return [TagInfo.model_validate(t) for t in response.json()]

    def set_repo_tag(
        self, project_id: str, name: str, target: str, message: Optional[str] = None
    ) -> TagInfo:
        """Point `name` at `target` (a branch or commit SHA): creates the tag, or moves it."""
        body: dict[str, Any] = {"target": target}
        if message:
            body["message"] = message
        response = self.api.request(
            "PUT", f"/projects/{project_id}/repo/tags/{_q(name)}", json=body
        )
        return TagInfo.model_validate(response.json())

    def delete_repo_tag(self, project_id: str, name: str) -> None:
        self.api.request("DELETE", f"/projects/{project_id}/repo/tags/{_q(name)}")

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

    def list_repo_commits(
        self,
        project_id: str,
        ref: Optional[str] = None,
        path: Optional[str] = None,
        page: int = 1,
        per_page: int = 30,
    ) -> RepoCommitList:
        """A page of commit history on `ref` — the whole branch when `path`
        is omitted, or one file/folder's history when it isn't."""
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if ref:
            params["ref"] = ref
        if path:
            params["path"] = path
        response = self.api.request(
            "GET", f"/projects/{project_id}/repo/commits", params=params
        )
        return RepoCommitList.model_validate(response.json())

    def get_project_readiness(
        self, project_id: str, ref: Optional[str] = None
    ) -> ProjectReadiness:
        """Everything blocking this project from running, answered up front
        instead of surfacing as a 4xx from `run`/`schedules add`."""
        params = {"ref": ref} if ref else None
        response = self.api.request(
            "GET", f"/projects/{project_id}/readiness", params=params
        )
        return ProjectReadiness.model_validate(response.json())

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
        tag: Optional[str] = None,
    ) -> ProjectExecuteResponse:
        """Queue a git-ref-based run for a project. `tag` runs the commit a tag points at."""
        body: dict[str, Any] = {"source": source}
        if branch:
            body["branch"] = branch
        if commit:
            body["commit"] = commit
        if tag:
            body["tag"] = tag
        if command:
            body["command"] = command
        if kernel_spec:
            body["kernel_spec"] = kernel_spec

        response = self.api.request("POST", f"/projects/{project_id}/execute", json=body)
        return ProjectExecuteResponse.model_validate(response.json())

    def compare_runs(self, project_id: str, run_ids: list[str]) -> dict[str, Any]:
        """KPI table, deltas, winner and inputs verdict for 2-4 runs (first = baseline).

        Returned as the API sent it, not as a model, so a library caller sees whatever the
        API adds. ``harumi compare --json`` does not: it prints only the fields named in
        ``json_out.comparison``.
        """
        response = self.api.request(
            "GET", f"/projects/{project_id}/compare", params={"runs": ",".join(run_ids)}
        )
        return response.json()

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

    # -- Memories -------------------------------------------------------
    # /projects/{id}/memories — the project's own memories. The list also
    # includes ones inherited from the org/personal workspace (read-only here).

    def list_memories(
        self,
        project_id: str,
        query: str | None = None,
        retrieval_mode: str | None = None,
    ) -> list[Memory]:
        params = {
            k: v for k, v in {"q": query, "retrieval_mode": retrieval_mode}.items() if v
        }
        response = self.api.request(
            "GET", f"/projects/{project_id}/memories", params=params or None
        )
        return MemoryList.model_validate(response.json()).memories

    def create_memory(
        self,
        project_id: str,
        content: str,
        retrieval_mode: str = "on_demand",
        source: str = "cli",
    ) -> Memory:
        response = self.api.request(
            "POST",
            f"/projects/{project_id}/memories",
            json={"content": content, "source": source, "retrieval_mode": retrieval_mode},
        )
        return Memory.model_validate(response.json())

    def update_memory(
        self,
        project_id: str,
        memory_id: str,
        content: str | None = None,
        retrieval_mode: str | None = None,
    ) -> Memory:
        body = {
            k: v for k, v in {"content": content, "retrieval_mode": retrieval_mode}.items() if v is not None
        }
        response = self.api.request(
            "PATCH", f"/projects/{project_id}/memories/{_q(memory_id)}", json=body
        )
        return Memory.model_validate(response.json())

    def delete_memory(self, project_id: str, memory_id: str) -> Memory:
        response = self.api.request(
            "DELETE", f"/projects/{project_id}/memories/{_q(memory_id)}"
        )
        return Memory.model_validate(response.json())

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
        self,
        project_id: str,
        path: str,
        content_type: str = "application/octet-stream",
        size_bytes: Optional[int] = None,
    ) -> FileUploadUrl:
        """`size_bytes` is the file's exact size. It is signed into the URL, so storage
        refuses any other length, and a plan that caps storage requires it."""
        body: dict[str, Any] = {"path": path, "content_type": content_type}
        if size_bytes is not None:
            body["size_bytes"] = size_bytes
        response = self.api.request(
            "POST",
            f"/projects/{project_id}/files/upload-url",
            json=body,
        )
        return FileUploadUrl.model_validate(response.json())

    def complete_file_upload(self, project_id: str, path: str, upload_key: str) -> None:
        """Ask harumi-api to scan a staged upload and promote it to a project file.

        The scan (ClamAV) can take up to two minutes server-side, so this waits longer than
        an ordinary request. Raises `ApiError` when the file is refused or cannot be checked:
        422 (rejected as unsafe), 413 (too large to scan), 503 (no scanner), 404 (nothing
        staged under that key).
        """
        self.api.request(
            "POST",
            f"/projects/{project_id}/files/complete-upload",
            json={"path": path, "upload_key": upload_key},
            timeout=_COMPLETE_UPLOAD_TIMEOUT,
        )

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
        the presigned URL's query string is the only auth it accepts.

        Streams from the open handle rather than reading the whole file in:
        the sync cap allows a single file up to 500 MiB, which is not an
        amount of memory a CLI on a user's laptop should need. httpx sizes
        the body with `fstat`, so this still goes out with a `Content-Length`
        and not `Transfer-Encoding: chunked` — S3 rejects chunked on a
        presigned PUT.
        """
        try:
            with httpx.Client(timeout=300.0, transport=self.api.transport) as http_client:
                with open(local_path, "rb") as f:
                    response = http_client.put(
                        upload_url.url,
                        content=f,
                        headers={"Content-Type": content_type},
                    )
        except httpx.HTTPError as exc:
            # A dropped connection here would otherwise be a bare httpx
            # exception _handle_errors doesn't have a clause for, surfacing
            # as a raw traceback instead of the CLI's normal error message.
            raise ApiError(0, f"Upload failed: {exc}") from exc
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.text or response.reason_phrase)

    def download_file_from_presigned_url(
        self, download_url: FileDownloadUrl, dest_path: Path
    ) -> None:
        """GET a file straight from S3 to `dest_path`. No Authorization header,
        same reasoning as `upload_file_to_presigned_url`.

        Streams to a `.part` sibling and renames into place only on success,
        so a dropped connection never leaves a truncated, silently-corrupt
        file sitting at `dest_path`.
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_name(dest_path.name + ".part")
        try:
            with httpx.Client(timeout=300.0, transport=self.api.transport) as http_client:
                with http_client.stream("GET", download_url.url) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise ApiError(response.status_code, response.text or response.reason_phrase)
                    with open(tmp_path, "wb") as f:
                        for chunk in response.iter_bytes():
                            f.write(chunk)
        except httpx.HTTPError as exc:
            tmp_path.unlink(missing_ok=True)
            # Same reasoning as upload: don't let a bare httpx exception
            # escape _handle_errors as a raw traceback.
            raise ApiError(0, f"Download failed: {exc}") from exc
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        tmp_path.replace(dest_path)

