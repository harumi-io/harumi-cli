"""A login that survives being used: refreshes keep the git token, parallel commands
do not log each other out, and the credentials file is never half-written.

The concurrency test uses real processes and a real local HTTP server that enforces
what Supabase enforces (a refresh token works once). Threads would not do: the bug
is between processes.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import httpx
import pytest

from harumi import auth, config
from harumi.client import ApiClient
from harumi.config import Config
from harumi.errors import CredentialsLockError, NotAuthenticatedError
from harumi.models import LoggedUser


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch, isolate_harumi_home):
    real_home = os.path.expanduser("~/.harumi")
    isolate_harumi_home(tmp_path)
    assert not str(config.credentials_path()).startswith(real_home), "setup left HARUMI_HOME pointing at the real one"
    yield tmp_path
    # A test that reaches the real ~/.harumi could read, and print, a real login. Checked
    # again at the end, because a patch can be undone mid-test (that is how it once happened).
    assert not str(config.credentials_path()).startswith(real_home), "a test escaped to the real HARUMI_HOME"


def on_disk() -> dict:
    return json.loads(config.credentials_path().read_text())


def log_in(*, expires_in=-100, refresh="r1", access="old"):
    config.save_credentials(
        access_token=access, refresh_token=refresh, email="ana@example.com", user_id="u1",
        expires_at=int(time.time()) + expires_in, replace=True,
    )
    config.save_git_token("gitea-secret", git_url="https://git.example", username="u-1")


GIT_KEYS = {"git_token", "git_url", "git_username"}


# -- #123: a refresh keeps the git credentials ----------------------------------------------


def test_a_session_refresh_keeps_the_git_token_url_and_username():
    log_in()

    auth._persist(LoggedUser(id="u1", email="ana@example.com", access_token="a2", refresh_token="r2", expires_at=9))

    stored = on_disk()
    assert GIT_KEYS <= set(stored)
    assert stored["access_token"] == "a2" and stored["refresh_token"] == "r2"
    assert config.load_git_token() == "gitea-secret" and config.load_git_username() == "u-1"


def test_save_credentials_merges_by_default_and_replaces_on_request():
    log_in()
    config.save_credentials(access_token="a2", refresh_token="r2")
    assert GIT_KEYS <= set(on_disk())

    config.save_credentials(access_token="a3", refresh_token="r3", replace=True)
    assert not GIT_KEYS & set(on_disk())


def test_a_fresh_login_does_not_inherit_the_previous_accounts_git_token(monkeypatch):
    log_in()  # account A, with a git token
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"id": "u2", "email": "bob@example.com", "access_token": "b1", "refresh_token": "rb1", "expires_at": 9})
    )

    auth.verify_otp(Config.load(), "bob@example.com", "123456", transport=transport)

    assert on_disk()["email"] == "bob@example.com"
    assert config.load_git_token() is None, "account B must not be left holding account A's git token"


def test_saving_the_git_token_keeps_the_session():
    config.save_credentials(access_token="a1", refresh_token="r1", email="ana@example.com", replace=True)
    config.save_git_token("g", git_url="https://git.example", username="u-1")
    assert on_disk()["refresh_token"] == "r1" and on_disk()["git_token"] == "g"


# -- the file is never half-written, and never readable by others ------------------------------


def test_an_interrupted_write_leaves_the_old_file_intact_and_no_litter(tmp_path, monkeypatch):
    log_in(refresh="r-original")
    before = config.credentials_path().read_text()

    def explode(src, dst):
        raise OSError("disk full")

    with monkeypatch.context() as m:  # NOT monkeypatch.undo(): that would also undo the fixture's temp home
        m.setattr(os, "replace", explode)
        with pytest.raises(OSError):
            config.save_credentials(access_token="new", refresh_token="r-new")

    assert config.credentials_path().read_text() == before
    assert json.loads(before)["refresh_token"] == "r-original"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == [], "a temp file was left behind"


def test_the_file_is_owner_only_from_the_moment_it_exists(monkeypatch):
    """Not chmod-after-write: the secrets must never be world-readable even briefly."""
    modes = []
    real_replace = os.replace

    def spy(src, dst):
        modes.append(os.stat(src).st_mode & 0o777)
        return real_replace(src, dst)

    old = os.umask(0o022)  # the common default, under which a naive write is 0644
    try:
        monkeypatch.setattr(os, "replace", spy)
        config.save_credentials(access_token="a", refresh_token="r")
    finally:
        os.umask(old)

    assert modes == [0o600]
    assert config.credentials_path().stat().st_mode & 0o777 == 0o600


# -- the lock ------------------------------------------------------------------------------------


def test_the_lock_can_be_taken_again_by_the_code_that_already_holds_it():
    """save_credentials locks, and renew_session holds the lock while it saves."""
    done = []

    def work():
        with config.credentials_lock(timeout=2):
            config.save_credentials(access_token="a", refresh_token="r")
            config.save_git_token("g")
            done.append(True)

    t = threading.Thread(target=work)
    started = time.monotonic()
    t.start()
    t.join(5)

    assert done == [True] and time.monotonic() - started < 2, "re-entering the lock deadlocked"


def test_a_lock_that_cannot_be_taken_does_not_hang_the_command():
    holder_has_it = threading.Event()
    release = threading.Event()

    def hold():
        with config.credentials_lock(timeout=5):
            holder_has_it.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    assert holder_has_it.wait(5)

    started = time.monotonic()
    try:
        with pytest.raises(CredentialsLockError, match="retry"):
            with config.credentials_lock(timeout=0.3):
                pytest.fail("entered the critical section without the lock")
        waited = time.monotonic() - started
    finally:
        release.set()
        t.join()

    assert 0.25 <= waited < 2, f"gave up after {waited:.2f}s"


@pytest.mark.parametrize("failure", ["open", "unsupported", "busy"])
def test_lock_failure_never_spends_a_refresh_token_or_changes_credentials(monkeypatch, failure):
    log_in()
    before = on_disk()
    if failure == "open":
        monkeypatch.setattr(config, "_lock_file", lambda path: None)
    elif failure == "unsupported":
        monkeypatch.setattr(config, "fcntl", None)
        monkeypatch.setattr(config, "msvcrt", None)
    else:
        monkeypatch.setattr(config, "_try_lock", lambda handle, timeout: False)
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500))

    with pytest.raises(CredentialsLockError):
        ApiClient(Config.load(), transport=transport).request("GET", "/users/profile")

    assert calls == []
    assert on_disk() == before


def test_after_lock_contention_the_next_command_can_renew(monkeypatch):
    log_in()
    real_try_lock = config._try_lock
    monkeypatch.setattr(config, "_try_lock", lambda handle, timeout: False)
    with pytest.raises(CredentialsLockError):
        auth.get_valid_access_token(Config.load())

    monkeypatch.setattr(config, "_try_lock", real_try_lock)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "id": "u1", "access_token": "fresh", "refresh_token": "r2", "expires_at": int(time.time()) + 3600,
    }))
    assert auth.get_valid_access_token(Config.load(), transport=transport) == "fresh"
    assert on_disk()["refresh_token"] == "r2"


@pytest.mark.parametrize("expiry", [3600, None])
@pytest.mark.parametrize("failure", ["open", "unsupported", "filesystem", "busy"])
def test_valid_session_can_make_requests_when_lock_is_unavailable(monkeypatch, expiry, failure):
    log_in(expires_in=3600)
    if expiry is None:
        config.save_credentials(access_token="old", refresh_token="r1", expires_at=None)
    before = on_disk()
    if failure == "open":
        monkeypatch.setattr(config, "_lock_file", lambda path: None)
    elif failure == "unsupported":
        monkeypatch.setattr(config, "fcntl", None)
        monkeypatch.setattr(config, "msvcrt", None)
    elif failure == "filesystem":
        if config.fcntl is None:
            pytest.skip("POSIX locking test")
        def unsupported(*args):
            raise OSError(errno.ENOSYS, "file locking unavailable")
        monkeypatch.setattr(config.fcntl, "flock", unsupported)
    else:
        def busy(handle, timeout):
            assert timeout == 0, "valid tokens must not wait for a busy lock"
            return False
        monkeypatch.setattr(config, "_try_lock", busy)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    api = ApiClient(Config.load(), transport=httpx.MockTransport(handler))
    assert api.request("GET", "/users/profile").status_code == 200
    assert [(r.url.path, r.headers.get("authorization")) for r in calls] == [("/api/users/profile", "Bearer old")]
    assert on_disk() == before


@pytest.mark.parametrize("expires_in", [-100, 0, 59])
def test_a_session_due_for_refresh_cannot_sign_without_a_lock(monkeypatch, expires_in):
    log_in(expires_in=expires_in)
    monkeypatch.setattr(config, "_lock_file", lambda path: None)
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    with pytest.raises(CredentialsLockError):
        ApiClient(Config.load(), transport=transport).request("GET", "/users/profile")
    assert calls == []
    assert on_disk()["refresh_token"] == "r1"


def test_a_valid_near_expiry_token_without_a_refresh_token_needs_no_lock(monkeypatch):
    log_in(expires_in=30)
    config.save_credentials(access_token="old", refresh_token="", expires_at=int(time.time()) + 30)
    monkeypatch.setattr(config, "_lock_file", lambda path: None)
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request.url.path) or httpx.Response(200))

    assert ApiClient(Config.load(), transport=transport).request("GET", "/users/profile").status_code == 200
    assert calls == ["/api/users/profile"]


def test_a_snapshot_that_becomes_near_expiry_while_locking_does_not_bypass_renewal(monkeypatch):
    log_in(expires_in=3600)
    config.save_credentials(access_token="old", refresh_token="r1", expires_at=1061)
    monkeypatch.setattr(config, "_lock_file", lambda path: None)
    now = iter([1000, 1002])
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: next(now)))
    with pytest.raises(CredentialsLockError):
        auth.signing_session(Config.load())


@pytest.mark.parametrize("stream", [False, True])
def test_an_unlocked_valid_token_that_gets_401_cannot_refresh_without_the_lock(monkeypatch, stream):
    log_in(expires_in=3600)
    monkeypatch.setattr(config, "_lock_file", lambda path: None)
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request.url.path) or httpx.Response(401))

    api = ApiClient(Config.load(), transport=transport)
    with pytest.raises(CredentialsLockError):
        if stream:
            with api.stream("GET", "/users/profile"):
                pytest.fail("a refused stream must not be entered")
        else:
            api.request("GET", "/users/profile")

    assert calls == ["/api/users/profile"]
    assert on_disk()["refresh_token"] == "r1"


def test_unlocked_snapshot_keeps_the_matching_refresh_token_if_a_rival_renews(monkeypatch):
    log_in(expires_in=3600)
    real_lock_file = config._lock_file
    sent = []

    def unavailable_after_rival(path):
        current = on_disk()
        current.update(access_token="rival", refresh_token="r2", expires_at=int(time.time()) + 3600)
        config._write_json(config.credentials_path(), current)
        return None

    def handler(request):
        sent.append((request.url.path, request.headers.get("authorization")))
        if len(sent) == 1:
            monkeypatch.setattr(config, "_lock_file", real_lock_file)
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(config, "_lock_file", unavailable_after_rival)
    api = ApiClient(Config.load(), transport=httpx.MockTransport(handler))
    assert api.request("GET", "/users/profile").status_code == 200
    assert sent == [("/api/users/profile", "Bearer old"), ("/api/users/profile", "Bearer rival")]
    assert on_disk()["refresh_token"] == "r2"


def test_a_filesystem_lock_error_is_reported_without_waiting_or_refreshing(monkeypatch):
    if config.fcntl is None:
        pytest.skip("POSIX locking test")
    log_in()
    before = on_disk()

    def unsupported(*args):
        raise OSError(errno.ENOSYS, "file locking unavailable")

    monkeypatch.setattr(config.fcntl, "flock", unsupported)
    started = time.monotonic()
    with pytest.raises(CredentialsLockError, match="filesystem supports file locking"):
        auth.get_valid_access_token(Config.load())
    assert time.monotonic() - started < 2
    assert on_disk() == before


def test_whoami_reports_lock_contention_without_claiming_the_login_expired(monkeypatch):
    from typer.testing import CliRunner
    from harumi.cli import app

    log_in()
    before = on_disk()
    monkeypatch.setenv("HARUMI_NO_UPDATE_CHECK", "1")
    held, release = threading.Event(), threading.Event()

    def hold():
        with config.credentials_lock():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(5)
        real_try_lock = config._try_lock
        monkeypatch.setattr(config, "_try_lock", lambda handle, timeout: real_try_lock(handle, 0.1))
        result = CliRunner().invoke(app, ["whoami"])
    finally:
        release.set()
        thread.join()

    assert result.exit_code != 0
    assert "Could not lock your Harumi login" in result.output
    assert "retry" in result.output
    assert "expired" not in result.output and "harumi login" not in result.output
    assert on_disk() == before


def test_slow_refresh_cannot_be_duplicated_after_the_lock_timeout(monkeypatch):
    log_in()
    started, release = threading.Event(), threading.Event()
    refreshes, result = [], []

    def handler(request):
        if request.url.path.endswith("/users/refresh"):
            refreshes.append(request)
            started.set()
            assert release.wait(5)
            return httpx.Response(200, json={
                "id": "u1", "access_token": "fresh", "refresh_token": "r2", "expires_at": int(time.time()) + 3600,
            })
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    api = ApiClient(Config.load(), transport=transport)

    def first_command():
        try:
            result.append(api.request("GET", "/users/profile").status_code)
        except Exception as exc:
            result.append(exc)

    thread = threading.Thread(target=first_command)
    thread.start()
    try:
        assert started.wait(5)
        real_try_lock = config._try_lock
        monkeypatch.setattr(config, "_try_lock", lambda handle, timeout: real_try_lock(handle, 0.1))
        with pytest.raises(CredentialsLockError):
            api.request("GET", "/users/profile")
        assert len(refreshes) == 1
    finally:
        release.set()
        thread.join(5)

    assert result == [200]
    assert on_disk()["refresh_token"] == "r2"
    assert api.request("GET", "/users/profile").status_code == 200
    assert len(refreshes) == 1


@pytest.mark.parametrize("helper", ["session", "workspace", "git", "names", "example"])
def test_cli_helpers_do_not_swallow_credentials_lock_errors(monkeypatch, helper):
    from harumi import cli

    log_in(expires_in=3600)
    cfg = Config.load()
    error = CredentialsLockError("lock unavailable")

    class BrokenClient:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise error
            return fail

    client = BrokenClient()
    monkeypatch.setattr(cli, "_get_client", lambda **kwargs: client)
    monkeypatch.delenv("HARUMI_ORG", raising=False)
    calls = {
        "session": lambda: cli._session_still_works(cfg, "ana@example.com"),
        "workspace": lambda: cli._resolve_and_store_org(cfg),
        "git": lambda: cli._provision_git_token(cfg),
        "names": lambda: cli._workspace_names(client),
        "example": lambda: cli._checked_example(client, "example"),
    }
    with pytest.raises(CredentialsLockError, match="lock unavailable"):
        calls[helper]()


def test_login_does_not_send_an_otp_when_session_check_hits_a_lock_error(monkeypatch):
    from typer.testing import CliRunner
    from harumi import cli

    log_in()
    monkeypatch.setenv("HARUMI_NO_UPDATE_CHECK", "1")
    client = SimpleNamespace(get_profile=lambda: (_ for _ in ()).throw(CredentialsLockError("lock unavailable")))
    monkeypatch.setattr(cli, "_get_client", lambda **kwargs: client)
    sent = []
    monkeypatch.setattr(auth, "request_otp", lambda *args, **kwargs: sent.append(True))

    result = CliRunner().invoke(cli.app, ["login", "--email", "ana@example.com"])

    assert result.exit_code != 0
    assert "lock unavailable" in result.output
    assert sent == []


def test_provisioning_does_not_misreport_a_lock_error_when_saving_the_git_token(monkeypatch):
    from harumi import cli

    log_in(expires_in=3600)
    cfg = Config.load()
    creds = SimpleNamespace(token="git-token", git_url="https://git.example", username="user")
    monkeypatch.setattr(cli, "_get_client", lambda **kwargs: SimpleNamespace(get_git_token=lambda: creds))
    monkeypatch.setattr(config, "_lock_file", lambda path: None)

    with pytest.raises(CredentialsLockError):
        cli._provision_git_token(cfg)


def test_dashboard_validation_reports_lock_error_instead_of_missing_output(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from harumi import cli

    spec = tmp_path / "dashboard.toml"
    spec.write_text("")
    client = SimpleNamespace(
        get_run=lambda *args: SimpleNamespace(id="run-1"),
        get_run_output=lambda *args: (_ for _ in ()).throw(CredentialsLockError("lock unavailable")),
    )
    monkeypatch.setenv("HARUMI_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(cli, "_resolve_project", lambda *args: "proj-1")
    monkeypatch.setattr(cli, "_get_client", lambda **kwargs: client)

    result = CliRunner().invoke(cli.app, ["dashboard", "validate", str(spec), "--run", "run-1"])

    assert result.exit_code != 0
    assert "lock unavailable" in result.output
    assert "no output.json" not in result.output


# -- #115: adopting someone else's refresh -------------------------------------------------------


def test_if_another_process_already_renewed_its_session_is_used_not_refreshed_again(monkeypatch):
    log_in(refresh="r1")
    stale = "r1"
    # While we waited for the lock, another process renewed:
    config.save_credentials(access_token="theirs", refresh_token="r2", email="ana@example.com", expires_at=int(time.time()) + 3600)
    monkeypatch.setattr(auth, "refresh_session", lambda *a, **k: pytest.fail("spent a refresh token that was already used"))

    assert auth.renew_session(Config.load(), stale) == "theirs"


def test_a_refusal_is_not_a_logout_when_someone_else_got_in_first(monkeypatch):
    log_in(refresh="r1")

    def refused_after_a_rival_won(cfg, token, transport=None):
        config.save_credentials(access_token="rival", refresh_token="r2", email="ana@example.com")
        raise NotAuthenticatedError(expired=True)

    monkeypatch.setattr(auth, "refresh_session", refused_after_a_rival_won)

    assert auth.renew_session(Config.load(), "r1") == "rival"


def test_a_genuinely_dead_session_is_still_reported_as_expired(monkeypatch):
    log_in(refresh="r1")

    def refused(cfg, token, transport=None):
        raise NotAuthenticatedError(expired=True)

    monkeypatch.setattr(auth, "refresh_session", refused)

    with pytest.raises(NotAuthenticatedError) as exc:
        auth.renew_session(Config.load(), "r1")
    assert exc.value.expired


# -- #115, for real: separate processes against a server that enforces single use ----------------


class SingleUseServer:
    """What Supabase does: each refresh token works once; the old access token is rejected."""

    def __init__(self):
        self.lock = threading.Lock()
        self.valid_refresh = "r1"
        self.accepted = 0
        self.attempts = 0
        self.rejected_reuse = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if not self.path.endswith("/users/refresh"):
                    return self._send(404, {})
                time.sleep(0.2)  # a real network round trip: the window the race lives in
                with outer.lock:
                    outer.attempts += 1
                    if body.get("refresh_token") != outer.valid_refresh:
                        outer.rejected_reuse += 1
                        return self._send(422, {"detail": "Invalid Refresh Token: Already Used"})
                    outer.accepted += 1
                    outer.valid_refresh = f"r{outer.accepted + 1}"
                    n = outer.accepted
                self._send(200, {"id": "u1", "email": "ana@example.com", "access_token": f"fresh-{n}",
                                 "refresh_token": f"r{n + 1}", "expires_at": int(time.time()) + 3600})

            def do_GET(self):
                if self.headers.get("Authorization") == "Bearer old":
                    return self._send(401, {"detail": "expired"})
                self._send(200, {"id": "u1", "email": "ana@example.com"})

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/api"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


WORKER = """
import os, sys, time
gate = sys.argv[1]
while not os.path.exists(gate):
    time.sleep(0.001)
from harumi.cli import app
app(["whoami"], standalone_mode=True)
"""


@pytest.mark.parametrize("processes", [2, 6])
def test_commands_started_at_the_same_moment_all_succeed_on_a_single_refresh(tmp_path, processes):
    server = SingleUseServer()
    try:
        log_in(expires_in=-100, refresh="r1", access="old")  # an expired access token, a valid refresh token
        gate = tmp_path / "go"
        env = {**os.environ, "HARUMI_HOME": str(tmp_path), "HARUMI_API_URL": server.url, "HARUMI_NO_UPDATE_CHECK": "1", "NO_COLOR": "1"}
        env.pop("HARUMI_ENV", None)
        procs = [
            subprocess.Popen([sys.executable, "-c", WORKER, str(gate)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for _ in range(processes)
        ]
        time.sleep(1.5)  # every worker has imported harumi and is waiting at the gate
        gate.write_text("go")
        outputs = [(p.wait(timeout=60), p.stdout.read()) for p in procs]
    finally:
        server.close()

    failures = [(code, out.strip()[:200]) for code, out in outputs if code != 0]
    assert failures == [], f"{len(failures)} of {processes} commands failed: {failures}"
    assert server.accepted == 1, f"expected one refresh, the server accepted {server.accepted}"
    assert server.rejected_reuse == 0, f"{server.rejected_reuse} commands spent an already-used refresh token"
    assert on_disk()["refresh_token"] == server.valid_refresh, "the session on disk is not the live one"
    assert GIT_KEYS <= set(on_disk()), "the refresh lost the git token"


# -- the reactive path (a 401 on a request) must take the same care --------------------------------


def test_a_401_mid_request_is_renewed_under_the_lock_not_by_a_blind_refresh(monkeypatch):
    """The proactive path (token about to expire) and the reactive one (the server says
    401) both reach the same single-use refresh token. Only going through
    `renew_session` makes the second one adopt a rival's refresh instead of spending it."""
    log_in(expires_in=3600, refresh="r1", access="looks-valid")  # not expired as far as the file knows
    # A rival process renews before our retry: the file moves on to r2.
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if len(seen) == 1:
            config.save_credentials(access_token="rival", refresh_token="r2", email="ana@example.com", expires_at=int(time.time()) + 3600)
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(auth, "refresh_session", lambda *a, **k: pytest.fail("spent a refresh token a rival had already used"))
    api = ApiClient(Config.load(), transport=httpx.MockTransport(handler))

    assert api.request("GET", "/users/profile").status_code == 200
    assert seen == ["Bearer looks-valid", "Bearer rival"]


def test_a_401_on_a_download_stream_is_renewed_the_same_way(monkeypatch):
    log_in(expires_in=3600, refresh="r1", access="looks-valid")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if len(seen) == 1:
            config.save_credentials(access_token="rival", refresh_token="r2", email="ana@example.com", expires_at=int(time.time()) + 3600)
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, content=b"data")

    monkeypatch.setattr(auth, "refresh_session", lambda *a, **k: pytest.fail("spent a refresh token a rival had already used"))
    api = ApiClient(Config.load(), transport=httpx.MockTransport(handler))

    with api.stream("GET", "/files/x") as response:
        assert response.read() == b"data"
    assert seen == ["Bearer looks-valid", "Bearer rival"]


# -- the lock and the error labelling working together ----------------------------------------------------


def test_a_blocked_refresh_inside_the_lock_is_reported_as_a_refresh_problem_not_a_logout(monkeypatch):
    """A firewall answering 403 while several commands queue for the lock: each one must
    say its login could not be renewed, and none may be told the session ended."""
    from harumi.errors import SessionRefreshError

    log_in(refresh="r1")
    transport = httpx.MockTransport(lambda r: httpx.Response(403, json={"detail": "blocked by firewall"}))

    with pytest.raises(SessionRefreshError) as exc:
        auth.renew_session(Config.load(), "r1", transport=transport)

    assert exc.value.status_code == 403
    assert on_disk()["refresh_token"] == "r1", "a failed refresh must leave the session as it was"
    assert GIT_KEYS <= set(on_disk())


def test_the_proactive_refresh_goes_through_the_lock_and_the_clients_transport(monkeypatch):
    """Both of the previous fixes in one path: get_valid_access_token -> renew_session (lock,
    adopt a rival) -> the transport the client was given."""
    log_in(expires_in=-100, refresh="r1", access="old")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200, json={"id": "u1", "email": "ana@example.com", "access_token": "fresh", "refresh_token": "r2", "expires_at": int(time.time()) + 3600}
        )

    token = auth.get_valid_access_token(Config.load(), transport=httpx.MockTransport(handler))

    assert token == "fresh" and seen == ["/api/users/refresh"]
    assert on_disk()["refresh_token"] == "r2" and GIT_KEYS <= set(on_disk())


# -- the token a request was signed with: after the proactive refresh, not before -------------------------


def _revoking_server(revoked: str):
    """Refresh works (and rotates); the access token `revoked` is rejected forever."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/users/refresh"):
            n = sum(1 for p, _ in calls if p.endswith("/users/refresh"))
            return httpx.Response(
                200,
                json={"id": "u1", "email": "ana@example.com", "access_token": f"fresh{n}", "refresh_token": f"r{n + 1}", "expires_at": int(time.time()) + 3600},
            )
        return httpx.Response(401 if request.headers.get("authorization") == f"Bearer {revoked}" else 200, json={"ok": True})

    return httpx.MockTransport(handler), calls


def test_a_revoked_token_after_a_proactive_refresh_is_renewed_again_not_reported_as_a_logout():
    """The access token is expired, so building the request renews the session (the
    refresh token rotates r1 -> r2). The server then revokes the token it was given. The
    renewal that follows has to be judged against r2, the session the request was signed
    with. Judged against r1 it looks as though a rival already renewed, so nothing is
    refreshed and the retry repeats the 401: a login problem one more refresh would fix."""
    log_in(expires_in=-100, refresh="r1", access="old")
    transport, calls = _revoking_server(revoked="fresh1")

    response = ApiClient(Config.load(), transport=transport).request("GET", "/users/profile")

    assert response.status_code == 200
    assert [a for p, a in calls if not p.endswith("/users/refresh")] == ["Bearer fresh1", "Bearer fresh2"]
    assert sum(1 for p, _ in calls if p.endswith("/users/refresh")) == 2
    assert on_disk()["refresh_token"] == "r3"


def test_the_same_holds_for_a_download_stream():
    log_in(expires_in=-100, refresh="r1", access="old")
    transport, calls = _revoking_server(revoked="fresh1")

    with ApiClient(Config.load(), transport=transport).stream("GET", "/files/x") as response:
        assert response.status_code == 200

    assert [a for p, a in calls if not p.endswith("/users/refresh")] == ["Bearer fresh1", "Bearer fresh2"]


def test_a_rival_renewing_between_the_proactive_refresh_and_the_send_is_still_adopted(monkeypatch):
    """The other side of the same boundary: the token read after building the headers must
    still defer to a rival who renews in the gap before the 401 comes back."""
    log_in(expires_in=-100, refresh="r1", access="old")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/refresh"):
            return httpx.Response(200, json={"id": "u1", "email": "ana@example.com", "access_token": "mine", "refresh_token": "r2", "expires_at": int(time.time()) + 3600})
        seen.append(request.headers.get("authorization"))
        if len(seen) == 1:  # a rival renews while our request is in flight, then ours is refused
            config.save_credentials(access_token="rival", refresh_token="r3", email="ana@example.com", expires_at=int(time.time()) + 3600)
            return httpx.Response(401, json={"detail": "x"})
        return httpx.Response(200, json={"ok": True})

    real = auth.refresh_session
    refreshes = []
    monkeypatch.setattr(auth, "refresh_session", lambda *a, **k: refreshes.append(a[1]) or real(*a, **k))

    assert ApiClient(Config.load(), transport=httpx.MockTransport(handler)).request("GET", "/users/profile").status_code == 200
    assert seen == ["Bearer mine", "Bearer rival"]
    assert refreshes == ["r1"], f"spent a refresh token the rival had already replaced: {refreshes}"


# -- a symlinked credentials file stays a symlink --------------------------------------------------------


def test_a_refresh_writes_through_a_symlinked_credentials_file(tmp_path):
    """People keep credentials in a dotfiles repo and link them in. Replacing the link
    with a copy on every refresh leaves the repo's file stale and the link gone."""
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "credentials.json"
    real.write_text(json.dumps({"access_token": "a1", "refresh_token": "r1", "git_token": "keep-me"}))
    link = config.credentials_path()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(real)

    config.save_credentials(access_token="a2", refresh_token="r2")

    assert link.is_symlink(), "the link was replaced by a regular file"
    assert json.loads(real.read_text()) == {"access_token": "a2", "refresh_token": "r2", "git_token": "keep-me"}
    assert [p.name for p in dotfiles.iterdir() if p.name.endswith(".tmp")] == []
    assert real.stat().st_mode & 0o777 == 0o600


def test_a_dangling_symlink_is_written_through_too(tmp_path):
    target = tmp_path / "elsewhere" / "credentials.json"
    target.parent.mkdir()
    link = config.credentials_path()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)  # the file it points at does not exist yet

    config.save_credentials(access_token="a", refresh_token="r")

    assert link.is_symlink() and json.loads(target.read_text())["refresh_token"] == "r"


# -- a script can tell why a refresh failed ----------------------------------------------------------------------------


def test_a_rate_limited_refresh_keeps_the_code_and_the_wait_on_the_error():
    """`SessionRefreshError` is an `ApiError`, so a script that catches it reads `.code` and `.retry_after`. It used to
    rebuild the error from the status and the text alone, so a 429 with `Retry-After` and a reason looked like any 429."""
    from harumi.errors import SessionRefreshError

    log_in(refresh="r1")
    transport = httpx.MockTransport(
        lambda r: httpx.Response(429, json={"detail": "slow down", "code": "rate_limited"}, headers={"Retry-After": "37"})
    )

    with pytest.raises(SessionRefreshError) as exc:
        auth.renew_session(Config.load(), "r1", transport=transport)

    assert exc.value.status_code == 429 and exc.value.code == "rate_limited" and exc.value.retry_after == 37


def test_a_blocked_refresh_keeps_its_reason_code():
    from harumi.errors import SessionRefreshError

    log_in(refresh="r1")
    transport = httpx.MockTransport(lambda r: httpx.Response(403, json={"detail": "blocked", "code": "waf_block"}))

    with pytest.raises(SessionRefreshError) as exc:
        auth.renew_session(Config.load(), "r1", transport=transport)

    assert exc.value.code == "waf_block" and exc.value.retry_after is None


def test_the_message_does_not_change_when_the_code_is_kept():
    from harumi.errors import SessionRefreshError

    error = SessionRefreshError(429, "slow down", code="rate_limited", retry_after=37)

    assert str(error) == "Could not renew your login: harumi-api returned HTTP 429: slow down"
    assert SessionRefreshError(403, "x").code is None and SessionRefreshError(403, "x").retry_after is None
