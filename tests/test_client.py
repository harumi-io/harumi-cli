"""Offline tests for ApiClient/Client using httpx.MockTransport — no live
backend or network required.
"""

from __future__ import annotations

import json
from pathlib import Path

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
                "task_id": "task-1",
                "status": "queued",
                "message": "Run queued successfully",
                "output_id": "out-1",
            },
        )

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    response = client.execute_project("proj-1", branch="main")

    assert response.task_id == "task-1"
    assert response.output_id == "out-1"


def test_client_get_project_repo_returns_repo():
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/projects/proj-1/repo"
        return httpx.Response(
            200,
            json={
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


def test_client_get_project_repo_wraps_missing_endpoint():
    """When the backend doesn't have the endpoint yet, surface a clear message."""
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))

    with pytest.raises(HarumiError, match="not yet available"):
        client.get_project_repo("proj-1")


def test_client_download_output_writes_zip_to_dest(tmp_path):
    _write_credentials()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/notebooks/proj-1/outputs/out-1/download"
        return httpx.Response(200, stream=httpx.ByteStream(b"PK\x03\x04fakezip"))

    client = Client(api_url="https://harumi-api.test/api", transport=httpx.MockTransport(handler))
    zip_path = client.download_output("proj-1", "out-1", tmp_path)

    assert zip_path == tmp_path / "output_out-1.zip"
    assert zip_path.read_bytes() == b"PK\x03\x04fakezip"
