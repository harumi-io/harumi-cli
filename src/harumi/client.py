"""HTTP client for harumi-api: auth injection, 401-refresh-and-retry, and the
public `Client` SDK surface used by both the CLI and library consumers.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx

from harumi import auth
from harumi.config import Config
from harumi.errors import ApiError, NotAuthenticatedError
from harumi.models import KernelSpec, Notebook, Project


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
        client.run_job("solver.py", notebook_id="...", watch=True)
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        org_id: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.config = Config.load(api_url=api_url, org_id=org_id)
        self.api = ApiClient(self.config, transport=transport)

    # -- Auth -------------------------------------------------------------

    def request_otp(self, email: str) -> None:
        auth.request_otp(self.config, email)

    def verify_otp(self, email: str, token: str):
        return auth.verify_otp(self.config, email, token)

    def logout(self) -> None:
        auth.logout()

    # -- Discovery ----------------------------------------------------------

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

    # -- Files ----------------------------------------------------------

    def upload_path(self, project_id: str, local_path: Path) -> list[dict]:
        from harumi.files import upload_path

        return upload_path(self.api, project_id, local_path)

    # -- Execution --------------------------------------------------------

    def run_interactive(self, code: str, notebook_id: str, kernel_spec: Optional[str] = None, **kwargs):
        from harumi.execution import run_interactive

        return run_interactive(self.api, code, notebook_id, kernel_spec=kernel_spec, **kwargs)

    def run_job(
        self,
        path: Path | str,
        notebook_id: str,
        project_id: Optional[str] = None,
        kernel_spec: Optional[str] = None,
        watch: bool = False,
        **kwargs: Any,
    ):
        from harumi.execution import run_job

        return run_job(
            self.api,
            Path(path),
            notebook_id,
            project_id=project_id,
            kernel_spec=kernel_spec,
            watch=watch,
            **kwargs,
        )

    def list_outputs(self, notebook_id: str):
        from harumi.execution import list_outputs

        return list_outputs(self.api, notebook_id)

    def wait_for_output(self, notebook_id: str, output_id: str, **kwargs):
        from harumi.execution import wait_for_output

        return wait_for_output(self.api, notebook_id, output_id, **kwargs)

    def download_output(self, notebook_id: str, output_id: str, dest_dir: Path | str):
        from harumi.execution import download_output

        return download_output(self.api, notebook_id, output_id, Path(dest_dir))
