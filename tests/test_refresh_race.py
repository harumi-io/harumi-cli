"""A request is renewed with the refresh token that belongs to the access token it was signed with.

Found by a flaky thread test (2 failures in 30 runs on Python 3.9): `ApiClient` read the access token and then, in a
separate read, "the refresh token in use". If another thread renewed the session between the two reads, the first
thread signed with the OLD access token but recorded the NEW refresh token. Its request failed with 401 (as it must:
the old token is dead), it renewed with a refresh token that was not stale, and so spent a good, just-issued pair that
the other thread was about to use. Four threads sharing a login could make three refreshes where one was needed, and
one of them could end with the user told to log in again.

The server below behaves like the real one: each refresh token works exactly once, and a refresh invalidates the
access token it replaces.
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from harumi import auth, config
from harumi.client import Client


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr(config, "CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr(config, "_ACTIVE_ENV", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    config.set_active_environment("production")
    # httpx would otherwise be free to reach a real server if a request ignored the transport
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda self, request: (_ for _ in ()).throw(AssertionError("a test reached the network")))


class Server:
    """Single-use refresh tokens. Only the newest access token works; the stored one starts out expired."""

    def __init__(self):
        self.valid_refresh = {"R0"}
        self.valid_access: set[str] = set()
        self.count = 0
        self.log: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, request):
        if request.url.path.endswith("/users/refresh"):
            token = json.loads(request.content)["refresh_token"]
            with self.lock:
                if token not in self.valid_refresh:
                    self.log.append(f"REFUSED {token}: already used")
                    return httpx.Response(422, json={"detail": "Refresh token is not valid"})
                self.valid_refresh.discard(token)
                self.count += 1
                n = self.count
                self.valid_refresh.add(f"R{n}")
                self.valid_access = {f"A{n}"}
                self.log.append(f"refreshed {token} -> R{n}")
            return httpx.Response(200, json={"access_token": f"A{n}", "refresh_token": f"R{n}", "id": "u", "email": "a@b.c"})
        bearer = (request.headers.get("authorization") or "").removeprefix("Bearer ")
        if bearer in self.valid_access:
            return httpx.Response(200, json=[])
        return httpx.Response(401, json={"detail": "expired"})

    @property
    def refreshes(self) -> int:
        return sum(1 for line in self.log if line.startswith("refreshed"))


@pytest.fixture
def server():
    config.save_credentials(access_token="A0", refresh_token="R0")
    return Server()


def client_for(server):
    return Client(environment="production", transport=httpx.MockTransport(server))


class Rival:
    """Another thread that renews the session the moment it can, started from inside the window where a request is
    being signed. `join(timeout)` is how long we are willing to wait to see it get through: the old code let it
    through at once (nothing held it back), the fixed code makes it wait until the request has its pair."""

    def __init__(self, cfg, transport, stale="R0", wait=0.4):
        self.thread = threading.Thread(target=lambda: auth.renew_session(cfg, stale, transport=transport))
        self.thread.start()
        self.thread.join(wait)

    @property
    def got_through(self) -> bool:
        return not self.thread.is_alive()

    def finish(self):
        self.thread.join(10)


def signing_with_a_rival(monkeypatch, rivals):
    """Make a rival thread try to renew right after the first access token is read: the window."""
    real = auth.get_valid_access_token
    state = {"first": True}

    def patched(cfg, **kwargs):
        token = real(cfg, **kwargs)
        if state["first"]:
            state["first"] = False
            rivals.append(Rival(cfg, kwargs.get("transport")))
        return token

    monkeypatch.setattr(auth, "get_valid_access_token", patched)


def test_a_rival_renewing_while_a_request_is_being_signed_waits_for_it(server, monkeypatch):
    """The window, made deterministic. The old code read the access token and, separately, the refresh token in use:
    a rival that renewed between the two got through, and the request was signed with the old access token but
    recorded the rival's NEW refresh token."""
    rivals: list[Rival] = []
    signing_with_a_rival(monkeypatch, rivals)

    token, refresh = auth.signing_session(config.Config.load(), transport=httpx.MockTransport(server))
    rivals[0].finish()

    assert (token, refresh) == ("A0", "R0"), "the pair must be one session's"


def test_a_rival_could_not_get_through_while_the_pair_was_being_read(server, monkeypatch):
    rivals: list[Rival] = []
    signing_with_a_rival(monkeypatch, rivals)

    auth.signing_session(config.Config.load(), transport=httpx.MockTransport(server))

    assert rivals[0].got_through is False, "a rival renewed in the middle of signing: the pair is not one session's"
    rivals[0].finish()


def test_the_whole_request_costs_one_refresh_when_a_rival_overlaps(server, monkeypatch):
    client = client_for(server)
    rivals: list[Rival] = []
    signing_with_a_rival(monkeypatch, rivals)

    client.list_projects()
    rivals[0].finish()

    assert server.refreshes == 1, server.log  # the rival's, and nobody else's
    assert not any("REFUSED" in line for line in server.log)
    stored = config.load_credentials()
    assert (stored["access_token"], stored["refresh_token"]) == ("A1", "R1")


def test_streaming_downloads_sign_and_renew_the_same_way(server, monkeypatch):
    client = client_for(server)
    rivals: list[Rival] = []
    signing_with_a_rival(monkeypatch, rivals)

    with client.api.stream("GET", "/anything") as response:
        response.read()
    rivals[0].finish()

    assert server.refreshes == 1, server.log


def test_a_streaming_download_that_gets_a_401_renews_once_and_retries_with_the_new_pair(server):
    """The retry inside `stream()` re-signs after the renewal; it must use the renewed session, not the stale one."""
    client = client_for(server)

    with client.api.stream("GET", "/anything") as response:
        body = response.read()

    assert response.status_code == 200 and body is not None
    assert server.refreshes == 1, server.log
    assert (config.load_credentials()["access_token"], config.load_credentials()["refresh_token"]) == ("A1", "R1")


def test_signing_returns_the_refresh_token_that_goes_with_the_access_token(server):
    token, refresh = auth.signing_session(config.Config.load(), transport=httpx.MockTransport(server))

    assert (token, refresh) == ("A0", "R0")


def test_signing_without_a_login_raises_like_before(tmp_path):
    from harumi.errors import NotAuthenticatedError

    config.clear_credentials()

    with pytest.raises(NotAuthenticatedError):
        auth.signing_session(config.Config.load())


def test_holding_the_lock_for_signing_does_not_block_the_same_thread_from_renewing(server):
    """`get_valid_access_token` may itself renew (a token near expiry); the lock is re-entrant, so signing must not
    deadlock against itself."""
    config.save_credentials(access_token="A0", refresh_token="R0", expires_at=1.0)  # long past: renew while signing

    token, refresh = auth.signing_session(config.Config.load(), transport=httpx.MockTransport(server))

    assert (token, refresh) == ("A1", "R1") and server.refreshes == 1


def test_several_threads_sharing_a_login_never_make_more_refreshes_than_the_server_issued_tokens_for(server):
    """The shape that was flaky, run many times with the faithful server. Each refresh the threads cause must be a
    renewal somebody needed: never more than one, since only one token can ever expire here."""
    client = client_for(server)
    errors: list[str] = []

    def work():
        try:
            client.list_projects()
        except Exception as exc:  # pragma: no cover - reported by the assertion
            errors.append(repr(exc))

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == [], server.log
    assert server.refreshes == 1, server.log
