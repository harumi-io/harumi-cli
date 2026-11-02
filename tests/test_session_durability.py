"""A login that survives being used: refreshes keep the git token, parallel commands
do not log each other out, and the credentials file is never half-written.

The concurrency test uses real processes and a real local HTTP server that enforces
what Supabase enforces (a refresh token works once). Threads would not do: the bug
is between processes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from harumi import auth, config
from harumi.client import ApiClient
from harumi.config import Config
from harumi.errors import NotAuthenticatedError
from harumi.models import LoggedUser


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    real_home = os.path.expanduser("~/.harumi")
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
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
    with config.credentials_lock(timeout=0.3):  # a different thread: it has to wait, then give up
        waited = time.monotonic() - started
    release.set()
    t.join()

    assert 0.25 <= waited < 2, f"gave up after {waited:.2f}s"


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
