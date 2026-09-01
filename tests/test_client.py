"""Offline tests for ApiClient/Client using httpx.MockTransport — no live
backend or network required.
"""

from __future__ import annotations

import json

import httpx
import pytest

from harumi.client import ApiClient, Client
from harumi.config import Config
from harumi.errors import ApiError, HarumiError, NotAuthenticatedError


@pytest.fixture(autouse=True)
def isolated_harumi_home(tmp_path, monkeypatch):
    """Redirect ~/.harumi to a temp dir so tests never touch the real
    machine's stored credentials, and never hit the real network.
    """
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.delenv("HARUMI_ENV", raising=False)
    monkeypatch.delenv("HARUMI_API_URL", raising=False)
    monkeypatch.delenv("HARUMI_GIT_URL", raising=False)
    monkeypatch.delenv("HARUMI_ORG", raising=False)
    yield


def _write_credentials(access_token: str = "token-1", refresh_token: str | None = None, expires_at=None):
    from harumi.config import save_credentials

    extra = {}
    if expires_at is not None:
        extra["expires_at"] = expires_at
    save_credentials(access_token=access_token, refresh_token=refresh_token or "", **extra)


def _config() -> Config:
    return Config.load(api_url="https://harumi-api.test/api")


def test_request_injects_auth_and_org_headers():
    _write_credentials(access_token="secret-token")
    config = _config()
    config.org_id = "org-123"

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["x_organization"] = request.headers.get("x-organization")
        return httpx.Response(200, json={"ok": True})

    api = ApiClient(config, transport=httpx.MockTransport(handler))
    response = api.request("GET", "/health")

    assert response.json() == {"ok": True}
    assert captured["authorization"] == "Bearer secret-token"
    assert captured["x_organization"] == "org-123"


def test_request_raises_api_error_on_4xx():
    _write_credentials()
    config = _config()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Notebook not found"})

    api = ApiClient(config, transport=httpx.MockTransport(handler))

    with pytest.raises(ApiError) as exc_info:
        api.request("GET", "/notebooks/missing")

    assert exc_info.value.status_code == 404
    assert "Notebook not found" in str(exc_info.value)


def test_request_retries_once_after_401_via_refresh():
    _write_credentials(access_token="stale-token", refresh_token="refresh-abc")
    config = _config()

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.headers.get("authorization")))

        if request.url.path == "/api/users/refresh":
            body = json.loads(request.content)
            assert body["refresh_token"] == "refresh-abc"
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh-token",
                    "refresh_token": "refresh-def",
                    "id": "user-1",
                    "email": "dev@example.com",
                },
            )

        if request.headers.get("authorization") == "Bearer stale-token":
            return httpx.Response(401, json={"detail": "expired"})

        assert request.headers.get("authorization") == "Bearer fresh-token"
        return httpx.Response(200, json={"ok": True})

    api = ApiClient(config, transport=httpx.MockTransport(handler))
    response = api.request("GET", "/notebooks")

    assert response.json() == {"ok": True}
    # First attempt (401), then a refresh call, then the retried request.
    assert len(calls) == 3


def test_request_without_refresh_token_raises_not_authenticated_on_401():
    _write_credentials(access_token="stale-token", refresh_token=None)
    config = _config()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "expired"})

    api = ApiClient(config, transport=httpx.MockTransport(handler))

    with pytest.raises(NotAuthenticatedError):
        api.request("GET", "/notebooks")


def test_client_list_projects_parses_projects_envelope():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects"
        return httpx.Response(
            200,
            json={
                "projects": [
                    {"id": "p1", "name": "Routing", "notebook_ids": ["nb1"]},
                ],
                "total_count": 1,
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    projects = client.list_projects()

    assert len(projects) == 1
    assert projects[0].id == "p1"
    assert projects[0].name == "Routing"


def test_client_get_specs_parses_kernel_specs():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/sandbox/specs"
        return httpx.Response(
            200,
            json=[
                {
                    "name": "or_python_small",
                    "display_name": "Python (Small)",
                    "language": "python",
                    "description": "1 CPU, 2 GB RAM",
                    "size": {"name": "small", "cpu": 1, "memory": "2Gi", "gpu": False},
                    "subscription_required": False,
                    "icon": "python",
                }
            ],
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    specs = client.get_specs()

    assert len(specs) == 1
    assert specs[0].name == "or_python_small"
    assert specs[0].size.cpu == 1


def test_client_execute_project_queues_run():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/execute"
        body = json.loads(request.content)
        assert body["branch"] == "main"
        return httpx.Response(
            202,
            json={
                "execution_log_id": "log-1",
                "status": "queued",
                "workflow_run_id": "wf-1",
                "project_run_id": "run-1",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    response = client.execute_project("proj-1", branch="main")

    assert response.execution_log_id == "log-1"
    assert response.project_run_id == "run-1"


def test_client_get_project_repo_returns_repo():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/repo"
        return httpx.Response(
            200,
            json={
                "project_id": "proj-1",
                "owner": "dev-alice",
                "name": "supply-chain",
                "clone_url": "https://git.dev.harumi.io/dev-alice/supply-chain.git",
                "default_branch": "main",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    repo = client.get_project_repo("proj-1")

    assert repo.owner == "dev-alice"
    assert repo.name == "supply-chain"
    assert repo.default_branch == "main"


def test_client_get_project_repo_raises_api_error_on_404():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "No repository provisioned for this project"})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))

    with pytest.raises(ApiError) as exc_info:
        client.get_project_repo("proj-1")
    assert exc_info.value.status_code == 404


def test_client_create_project_fetches_repo_separately():
    """POST /projects returns a bare project; the repo is a separate GET."""
    _write_credentials()

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/projects":
            body = json.loads(request.content)
            assert body["name"] == "New Project"
            return httpx.Response(
                201, json={"id": "proj-2", "name": "New Project", "notebook_ids": []}
            )
        assert request.url.path == "/api/projects/proj-2/repo"
        return httpx.Response(
            200,
            json={
                "project_id": "proj-2",
                "owner": "dev-alice",
                "name": "new-project",
                "clone_url": "https://git.dev.harumi.io/dev-alice/new-project.git",
                "default_branch": "main",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    project = client.create_project("New Project")

    assert project.id == "proj-2"
    assert project.repo is not None
    assert project.repo.clone_url == "https://git.dev.harumi.io/dev-alice/new-project.git"
    assert calls == ["/api/projects", "/api/projects/proj-2/repo"]


def test_client_create_project_repo_none_when_not_provisioned():
    """A 404 fetching the repo (Harumi Git unconfigured) leaves repo=None, not an error."""
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/projects":
            return httpx.Response(
                201, json={"id": "proj-3", "name": "Bare Project", "notebook_ids": []}
            )
        return httpx.Response(404, json={"detail": "No repository provisioned for this project"})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    project = client.create_project("Bare Project")

    assert project.id == "proj-3"
    assert project.repo is None


def _create_project_transport(captured: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/projects":
            captured["body"] = json.loads(request.content)
            captured["header"] = request.headers.get("x-organization")
            return httpx.Response(
                201,
                json={
                    "id": "proj-4",
                    "name": "Scoped Project",
                    "customer_id": captured["body"].get("customer_id"),
                    "notebook_ids": [],
                },
            )
        return httpx.Response(404, json={"detail": "No repository provisioned for this project"})

    return httpx.MockTransport(handler)


def test_client_create_project_defaults_customer_id_to_configured_org():
    """A configured org must reach the POST body, not just the header.

    POST /projects reads the owning workspace from `customer_id` in the body and
    ignores X-Organization, so sending only the header created the project in the
    caller's personal workspace despite `harumi config set-org`.
    """
    _write_credentials()
    captured: dict = {}

    client = Client(
        api_url="https://harumi-api.test/api",
        org_id="org-acme",
        transport=_create_project_transport(captured),
    )
    project = client.create_project("Scoped Project")

    assert captured["body"]["customer_id"] == "org-acme"
    assert captured["header"] == "org-acme"
    assert project.customer_id == "org-acme"


def test_client_create_project_explicit_customer_id_wins_over_configured_org():
    _write_credentials()
    captured: dict = {}

    client = Client(
        api_url="https://harumi-api.test/api",
        org_id="org-acme",
        transport=_create_project_transport(captured),
    )
    client.create_project("Scoped Project", customer_id="org-other")
    assert captured["body"]["customer_id"] == "org-other"


def test_client_create_project_personal_omits_customer_id_despite_configured_org():
    """`--personal` opts out of the configured org so private projects stay private."""
    _write_credentials()
    captured: dict = {}

    client = Client(
        api_url="https://harumi-api.test/api",
        org_id="org-acme",
        transport=_create_project_transport(captured),
    )
    project = client.create_project("Scoped Project", personal=True)

    assert "customer_id" not in captured["body"]
    assert project.customer_id is None


def test_client_create_project_rejects_personal_with_explicit_customer_id():
    """`Client` is public API, so the contradiction must fail loudly here too.

    The CLI guards this before calling, but a library caller passing both would
    otherwise have `customer_id` silently dropped and land the project in the
    personal workspace it asked to override.
    """
    _write_credentials()
    captured: dict = {}

    client = Client(
        api_url="https://harumi-api.test/api",
        org_id="org-acme",
        transport=_create_project_transport(captured),
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        client.create_project("Scoped Project", customer_id="org-other", personal=True)

    # Rejected before any request went out.
    assert "body" not in captured


def test_client_create_project_without_configured_org_stays_personal():
    _write_credentials()
    captured: dict = {}

    client = Client(
        api_url="https://harumi-api.test/api",
        transport=_create_project_transport(captured),
    )
    client.create_project("Scoped Project")

    assert "customer_id" not in captured["body"]


def test_client_get_project_returns_project():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1"
        return httpx.Response(200, json={"id": "proj-1", "name": "Routing", "notebook_ids": []})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    project = client.get_project("proj-1")

    assert project.id == "proj-1"
    assert project.name == "Routing"


def test_client_update_project_sends_patch_body():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1"
        assert request.method == "PUT"
        body = json.loads(request.content)
        assert body == {"name": "Renamed"}
        return httpx.Response(200, json={"id": "proj-1", "name": "Renamed", "notebook_ids": []})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    project = client.update_project("proj-1", {"name": "Renamed"})

    assert project.name == "Renamed"


def test_client_delete_project_returns_deleted_project():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1"
        assert request.method == "DELETE"
        return httpx.Response(200, json={"id": "proj-1", "name": "Routing", "notebook_ids": []})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    deleted = client.delete_project("proj-1")

    assert deleted.id == "proj-1"


def test_client_get_git_token_calls_credentials_endpoint():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/git/credentials"
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "username": "harumi-alice",
                "token": "gitea-token-abc",
                "git_url": "https://git.dev.harumi.io",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    creds = client.get_git_token()

    assert creds.username == "harumi-alice"
    assert creds.token == "gitea-token-abc"


def test_client_list_repo_files_and_get_file_content():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/projects/proj-1/repo/files":
            return httpx.Response(
                200,
                json=[{"name": "main.py", "path": "main.py", "type": "file", "size": 10}],
            )
        assert request.url.path == "/api/projects/proj-1/repo/file-content"
        assert request.url.params["path"] == "main.py"
        return httpx.Response(
            200,
            json={"path": "main.py", "sha": "abc123", "encoding": "base64", "content": "cHJpbnQoJ2hpJyk="},
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    files = client.list_repo_files("proj-1")
    assert files[0].path == "main.py"

    content = client.get_repo_file("proj-1", "main.py")
    assert content.content == "cHJpbnQoJ2hpJyk="


def test_client_apply_repo_changes_sends_operations():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/repo/changes"
        body = json.loads(request.content)
        assert body["operations"] == [
            {"action": "update", "path": "main.py", "content": "aGVsbG8="}
        ]
        return httpx.Response(200, json={"commit_sha": "deadbeef", "changed": 1})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    result = client.apply_repo_changes(
        "proj-1", [{"action": "update", "path": "main.py", "content": "aGVsbG8="}]
    )

    assert result.commit_sha == "deadbeef"
    assert result.changed == 1


def test_client_list_repo_branches_flags_live():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/repo/branches"
        return httpx.Response(
            200,
            json=[
                {"name": "main", "commit_sha": "abc", "is_live": True},
                {"name": "feature-x", "commit_sha": "def", "is_live": False},
            ],
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    branches = client.list_repo_branches("proj-1")

    assert branches[0].is_live is True
    assert branches[1].name == "feature-x"


def test_client_list_runs_parses_envelope():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/runs"
        return httpx.Response(
            200,
            json={
                "runs": [
                    {
                        "id": "run-1",
                        "project_id": "proj-1",
                        "status": "completed",
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:05:00Z",
                    }
                ]
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    runs = client.list_runs("proj-1")

    assert len(runs) == 1
    assert runs[0].id == "run-1"
    assert runs[0].succeeded is True
    assert runs[0].finished is True


def test_client_get_run_surfaces_stdout_and_error():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/runs/run-1"
        return httpx.Response(
            200,
            json={
                "id": "run-1",
                "project_id": "proj-1",
                "status": "failed",
                "stdout": "hello\n",
                "stderr": "",
                "error": "boom",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:05:00Z",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    run = client.get_run("proj-1", "run-1")

    assert run.stdout == "hello\n"
    assert run.error == "boom"
    assert run.succeeded is False
    assert run.finished is True


def test_client_get_run_output_reads_parsed_json():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/runs/run-1/output"
        return httpx.Response(200, json={"makespan": 42})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    output = client.get_run_output("proj-1", "run-1")

    assert output == {"makespan": 42}


def test_client_get_run_output_raises_harumi_error_on_404():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "No output.json for this run"})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))

    with pytest.raises(HarumiError):
        client.get_run_output("proj-1", "run-1")


def test_client_cancel_run_posts_to_cancel_endpoint():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/runs/run-1/cancel"
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "id": "run-1",
                "project_id": "proj-1",
                "status": "cancelled",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:05:00Z",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    run = client.cancel_run("proj-1", "run-1")

    assert run.status == "cancelled"


def test_client_list_datasources_parses_envelope():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/datasources/proj-1"
        assert request.url.params["limit"] == "100"
        assert request.url.params["offset"] == "0"
        return httpx.Response(
            200,
            json={
                "datasources": [
                    {
                        "id": "ds-1",
                        "project_id": "proj-1",
                        "name": "sales_db",
                        "type": "postgresql",
                        "host": "db.internal",
                        "port": 5432,
                        "database": "sales",
                        "username": "reader",
                        "use_proxy": False,
                        "ssm_parameter_name": "/harumi/projects/proj-1/datasources/sales_db",
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "total_count": 1,
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    result = client.list_datasources("proj-1")

    assert result.total_count == 1
    assert result.datasources[0].name == "sales_db"
    assert result.datasources[0].type == "postgresql"
    # Credentials must never be present on the response model.
    assert not hasattr(result.datasources[0], "credentials")


def test_client_get_datasource_url_encodes_name():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/api/datasources/proj-1/sales%20db"
        return httpx.Response(
            200,
            json={
                "id": "ds-1",
                "project_id": "proj-1",
                "name": "sales db",
                "type": "postgresql",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    ds = client.get_datasource("proj-1", "sales db")

    assert ds.name == "sales db"


def test_client_create_datasource_sends_credentials_and_returns_datasource():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/datasources/proj-1"
        body = json.loads(request.content)
        assert body["credentials"] == "s3cr3t"
        assert body["name"] == "sales_db"
        return httpx.Response(
            201,
            json={
                "id": "ds-1",
                "project_id": "proj-1",
                "name": "sales_db",
                "type": "postgresql",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    ds = client.create_datasource(
        "proj-1",
        {"name": "sales_db", "type": "postgresql", "credentials": "s3cr3t"},
    )

    assert ds.name == "sales_db"


def test_client_test_datasource_connection_reports_failure():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/datasources/test-connection"
        return httpx.Response(400, json={"detail": "Connection refused"})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))

    with pytest.raises(ApiError, match="Connection refused"):
        client.test_datasource_connection(
            {
                "type": "postgresql",
                "host": "db.internal",
                "port": 5432,
                "database": "sales",
                "username": "reader",
                "credentials": "s3cr3t",
            }
        )


def test_client_execute_datasource_query_parses_result_shape():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/datasources/proj-1/sales_db/execute"
        body = json.loads(request.content)
        assert body["query"] == "SELECT * FROM orders"
        assert body["limit"] == 10000
        return httpx.Response(
            200,
            json={
                "columns": ["id", "total"],
                "data": [[1, 100], [2, 250]],
                "rowCount": 2,
                "wasLimited": False,
                "maxRows": 10000,
                "dataframe_name": "df",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    result = client.execute_datasource_query("proj-1", "sales_db", "SELECT * FROM orders")

    assert result.columns == ["id", "total"]
    assert result.row_count == 2
    assert result.was_limited is False
    assert result.data == [[1, 100], [2, 250]]


def test_client_execute_datasource_query_rejects_non_select():
    """The server enforces read-only queries with a 403; the CLI should surface it clearly."""
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"detail": "Only SELECT queries are allowed. Destructive operations (DELETE) are not permitted."},
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))

    with pytest.raises(ApiError, match="Only SELECT queries are allowed"):
        client.execute_datasource_query("proj-1", "sales_db", "DELETE FROM orders")


def test_client_list_schedules_parses_schedule_array():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/schedules"
        return httpx.Response(
            200,
            json={
                "schedules": [
                    {
                        "id": "sched-1",
                        "project_id": "proj-1",
                        "cron": "0 9 * * *",
                        "start_at": "2026-01-22T09:00:00Z",
                        "git_branch": "main",
                        "kernel_spec": "or_python_small",
                        "last_executed_at": None,
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                ]
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    schedules = client.list_schedules("proj-1")

    assert len(schedules) == 1
    assert schedules[0].id == "sched-1"
    assert schedules[0].cron == "0 9 * * *"
    assert schedules[0].git_branch == "main"
    assert schedules[0].kernel_spec == "or_python_small"


def test_client_create_schedule_posts_body_and_returns_schedule():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/schedules"
        body = json.loads(request.content)
        assert body["cron"] == "0 9 * * *"
        assert body["start_at"] == "2026-01-22T09:00:00Z"
        return httpx.Response(
            201,
            json={
                "id": "sched-1",
                "project_id": "proj-1",
                "cron": "0 9 * * *",
                "start_at": "2026-01-22T09:00:00Z",
                "git_branch": "main",
                "kernel_spec": "or_python_small",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    schedule = client.create_schedule(
        "proj-1", {"cron": "0 9 * * *", "start_at": "2026-01-22T09:00:00Z"}
    )

    assert schedule.id == "sched-1"
    assert schedule.project_id == "proj-1"


def test_client_list_schedules_raises_api_error_on_404():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))

    with pytest.raises(ApiError) as exc_info:
        client.list_schedules("proj-1")
    assert exc_info.value.status_code == 404


def test_client_delete_schedule_returns_deleted_row():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/schedules/sched-1"
        assert request.method == "DELETE"
        return httpx.Response(
            200,
            json={
                "id": "sched-1",
                "project_id": "proj-1",
                "cron": "0 9 * * *",
                "git_branch": "main",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    schedule = client.delete_schedule("proj-1", "sched-1")

    assert schedule.id == "sched-1"


def test_client_list_secrets_returns_names_and_values():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/secrets"
        return httpx.Response(200, json=[{"name": "API_KEY", "value": "***"}])

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    secrets = client.list_secrets("proj-1")

    assert secrets[0].name == "API_KEY"


def test_client_create_secret_sends_name_and_value():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/secrets"
        body = json.loads(request.content)
        assert body == {"name": "API_KEY", "value": "s3cr3t"}
        return httpx.Response(200, json={"name": "API_KEY", "value": "s3cr3t"})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    secret = client.create_secret("proj-1", "API_KEY", "s3cr3t")

    assert secret.name == "API_KEY"


def test_client_delete_secret_uses_name_as_secret_id():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/secrets/API_KEY"
        assert request.method == "DELETE"
        return httpx.Response(204)

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    client.delete_secret("proj-1", "API_KEY")


def test_client_list_organizations_and_members():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/users/organizations":
            return httpx.Response(
                200, json=[{"id": "org-1", "business_name": "Acme", "role": "owner"}]
            )
        assert request.url.path == "/api/users/organizations/org-1/users"
        return httpx.Response(
            200,
            json=[
                {"user_id": "u-1", "role": "owner", "email": "a@example.com", "pending": False}
            ],
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    orgs = client.list_organizations()
    assert orgs[0].business_name == "Acme"

    members = client.list_organization_members("org-1")
    assert members[0].email == "a@example.com"


def test_client_get_profile_and_git_credentials_use_new_paths():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/users/profile":
            return httpx.Response(
                200, json={"id": "u-1", "email": "a@example.com", "first_name": "Alice"}
            )
        assert request.url.path == "/api/git/credentials"
        return httpx.Response(
            200,
            json={"username": "harumi-alice", "token": "tok", "git_url": "https://git.dev.harumi.io"},
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    profile = client.get_profile()
    assert profile.email == "a@example.com"

    creds = client.get_git_token()
    assert creds.username == "harumi-alice"


# ---------------------------------------------------------------------------
# Project files — presigned upload/download must never carry this client's
# Authorization/X-Organization headers (they'd invalidate the signature).
# ---------------------------------------------------------------------------

def test_client_list_project_files():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/files"
        return httpx.Response(
            200,
            json={
                "files": [
                    {"name": "data.csv", "key": "proj-1/data.csv", "last_modified": "2026-01-01T00:00:00Z", "etag": "abc", "size": 42}
                ],
                "is_truncated": False,
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    result = client.list_project_files("proj-1")
    assert result.files[0].name == "data.csv"
    assert result.files[0].size == 42
    assert result.is_truncated is False


def test_client_upload_file_to_presigned_url_carries_no_auth_header(tmp_path):
    """The presigned URL's query string is the only auth it accepts — adding
    this client's Authorization/X-Organization headers would invalidate the
    signature, so the PUT must go out with neither."""
    _write_credentials()
    local = tmp_path / "data.csv"
    local.write_text("a,b\n1,2\n")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/proj-1/data.csv"
        assert "authorization" not in {k.lower() for k in request.headers}
        assert "x-organization" not in {k.lower() for k in request.headers}
        assert request.content == b"a,b\n1,2\n"
        return httpx.Response(200)

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    from harumi.models import FileUploadUrl

    upload_url = FileUploadUrl(
        url="https://uploads.test.s3.amazonaws.com/proj-1/data.csv?X-Amz-Signature=abc",
        key="proj-1/data.csv",
        expires_in=900,
    )
    client.upload_file_to_presigned_url(upload_url, local, "text/csv")


def test_client_upload_file_to_presigned_url_streams_with_a_content_length(tmp_path):
    """The body is streamed from the open file, not read into memory — but it
    must still carry a Content-Length: S3 rejects a chunked presigned PUT."""
    _write_credentials()
    local = tmp_path / "data.csv"
    local.write_bytes(b"0" * 4096)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-length"] == "4096"
        assert "transfer-encoding" not in {k.lower() for k in request.headers}
        assert request.content == b"0" * 4096
        return httpx.Response(200)

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    from harumi.models import FileUploadUrl

    upload_url = FileUploadUrl(url="https://uploads.test/proj-1/data.csv", key="proj-1/data.csv", expires_in=900)
    client.upload_file_to_presigned_url(upload_url, local, "text/csv")


def test_client_upload_file_to_presigned_url_raises_on_error_status(tmp_path):
    _write_credentials()
    local = tmp_path / "data.csv"
    local.write_text("x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    from harumi.models import FileUploadUrl

    upload_url = FileUploadUrl(url="https://uploads.test/proj-1/data.csv", key="proj-1/data.csv", expires_in=900)
    with pytest.raises(ApiError):
        client.upload_file_to_presigned_url(upload_url, local, "text/csv")


def test_client_download_file_from_presigned_url_carries_no_auth_header(tmp_path):
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/proj-1/data.csv"
        assert "authorization" not in {k.lower() for k in request.headers}
        return httpx.Response(200, content=b"a,b\n1,2\n")

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    from harumi.models import FileDownloadUrl

    dest = tmp_path / "nested" / "data.csv"
    download_url = FileDownloadUrl(url="https://uploads.test.s3.amazonaws.com/proj-1/data.csv", expires_in=900)
    client.download_file_from_presigned_url(download_url, dest)

    assert dest.read_bytes() == b"a,b\n1,2\n"


def test_client_delete_project_file():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/files"
        assert request.url.params["path"] == "data.csv"
        return httpx.Response(200, json={"deleted": 1})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    result = client.delete_project_file("proj-1", "data.csv")
    assert result.deleted == 1



