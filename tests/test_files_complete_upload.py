"""`harumi files put` must finish what the API started.

Since harumi-api commit aae13d75 ("quarantine project file uploads"), `POST .../files/upload-url`
hands out a presigned PUT to an opaque `{project}/.quarantine/<uuid>` key. The object is not a
project file until `POST .../files/complete-upload` has virus-scanned it and promoted a clean copy.
The CLI used to PUT and stop, printing "Uploaded" for a file that never appeared.

An older server (production, until `dev` is promoted) returns the final key and has no
complete-upload route; for it the extra call must not be made.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config
from harumi.client import Client

runner = CliRunner()
P = "proj-1"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A throwaway home: these tests must not read, or depend on, the real ~/.harumi."""
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


class Api:
    """The slice of harumi-api that `files put` talks to, in either generation."""

    def __init__(self, staged: bool = True, complete_status: int = 204, complete_detail: str = ""):
        self.staged = staged
        self.complete_status = complete_status
        self.complete_detail = complete_detail
        self.calls: list[str] = []
        self.completed: list[dict] = []
        self.timeouts: list[object] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if request.url.host == "s3.test":
            return httpx.Response(200)
        if path.endswith("/files") and request.method == "GET":
            return httpx.Response(200, json={"files": [], "is_truncated": False})
        if path.endswith("/files/upload-url"):
            dest = json.loads(request.content)["path"]
            key = f"{P}/.quarantine/3f2a-uuid" if self.staged else f"{P}/{dest}"
            return httpx.Response(200, json={"url": f"https://s3.test/{key}", "key": key, "expires_in": 900})
        if path.endswith("/files/complete-upload"):
            self.completed.append(json.loads(request.content))
            self.timeouts.append(request.extensions.get("timeout"))
            if self.complete_status == 204:
                return httpx.Response(204)
            return httpx.Response(self.complete_status, json={"detail": self.complete_detail})
        return httpx.Response(404, json={"detail": "not routed"})


@pytest.fixture
def api(monkeypatch):
    def make(**kw) -> Api:
        a = Api(**kw)
        monkeypatch.setattr(
            cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler))
        )
        return a

    return make


@pytest.fixture
def csv(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("a,b\n1,2\n")
    return f


def put(csv, *extra):
    return runner.invoke(cli.app, ["files", "put", str(csv), *extra, "--project", P])


def plain(text: str) -> str:
    return " ".join(text.split())


# -- a server that stages uploads ---------------------------------------------------------------------------


def test_a_staged_upload_is_completed_with_the_path_and_the_key_it_was_given(api, csv):
    a = api()

    result = put(csv, "inputs/data.csv")

    assert result.exit_code == 0, result.output
    assert a.completed == [{"path": "inputs/data.csv", "upload_key": f"{P}/.quarantine/3f2a-uuid"}]


def test_the_order_is_ask_for_a_url_upload_then_complete(api, csv):
    a = api()

    put(csv)

    wanted = [c for c in a.calls if c.endswith("/files/upload-url") or c.endswith("/files/complete-upload") or ".quarantine" in c]
    assert [c.rsplit("/", 1)[-1] for c in wanted] == ["upload-url", "3f2a-uuid", "complete-upload"], a.calls


def test_uploaded_is_only_printed_once_the_server_has_accepted_the_file(api, csv):
    api()

    out = plain(put(csv, "inputs/data.csv").output)

    assert "Uploaded" in out and "inputs/data.csv" in out


@pytest.mark.parametrize(
    "status, detail, said, next_step",
    [
        (422, "Uploaded file could not be verified", "could not be verified as safe", None),
        (413, "Uploaded file is too large to verify", "too large to be checked", None),
        (503, "Uploaded file could not be verified", "could not check this file right now", "Try again in a few minutes"),
        (404, "Upload not found", "did not arrive", "Try again"),
    ],
)
def test_a_refused_upload_says_so_and_never_claims_success(api, csv, status, detail, said, next_step):
    api(complete_status=status, complete_detail=detail)

    result = put(csv)

    out = plain(result.output)
    assert result.exit_code == 1
    assert said in out and "was not added" in out
    if next_step:  # a temporary failure says what to do; a refusal does not invite a retry of the same file
        assert next_step in out
    else:
        assert "Try again" not in out
    assert "Uploaded" not in out.replace("was not added", "")  # no success line


def test_an_unexpected_failure_is_not_dressed_up(api, csv):
    api(complete_status=500, complete_detail="boom")

    result = put(csv)

    assert result.exit_code == 1 and "HTTP 500" in result.output and "Uploaded" not in result.output


def test_the_check_is_given_time_to_finish(api, csv):
    """The server scans with ClamAV for up to two minutes before answering."""
    a = api()

    put(csv)

    assert a.timeouts and a.timeouts[0]["read"] >= 150


# -- a server that does not stage (production until dev is promoted) -------------------------------------


def test_an_older_server_gets_no_extra_call(api, csv):
    a = api(staged=False)

    result = put(csv)

    assert result.exit_code == 0 and "Uploaded" in result.output
    assert not any("complete-upload" in c for c in a.calls)


def test_only_a_key_in_this_projects_quarantine_counts_as_staged():
    from harumi.client import upload_is_staged

    assert upload_is_staged("proj-1", "proj-1/.quarantine/abc")
    assert not upload_is_staged("proj-1", "proj-1/inputs/data.csv")
    assert not upload_is_staged("proj-1", "proj-2/.quarantine/abc")  # someone else's staging area
    assert not upload_is_staged("proj-1", "proj-1/.quarantine/")  # nothing after the prefix
    assert not upload_is_staged("proj-1", "proj-1/data/.quarantine/abc")  # a user folder with that name
    assert not upload_is_staged("proj-1", "proj-1/.quarantine/a/b")  # staging keys are flat


# -- what does not change -----------------------------------------------------------------------------------


def test_the_size_cap_is_still_checked_before_anything_is_sent(api, csv, monkeypatch):
    a = api()
    monkeypatch.setattr("harumi.cli.check_project_sync_cap", lambda *x, **k: type("V", (), {"reason": "file-count", "would_be": 501, "limit": 500})())

    result = put(csv)

    assert result.exit_code == 1 and "Remove some files first" in plain(result.output)
    assert not any("upload-url" in c for c in a.calls)


# -- the skill reference tells an agent about the check ------------------------------------------------------


def test_the_skill_reference_documents_files_and_the_completion_step():
    import pathlib

    ref = (pathlib.Path(cli.__file__).resolve().parents[2] / ".agents/skills/harumi-cli/references/commands.md").read_text(encoding="utf-8")
    section = ref[ref.index("## `files`") : ref.index("## `dashboard`")]

    for needle in ("complete-upload", "500 files", "Checking the file", "was not added", "503", "413", "422", ".part"):
        assert needle in section, f"the files section does not mention {needle!r}"
    for sub in ("ls", "put", "get", "rm"):
        assert f"harumi files {sub}" in section
