"""Not logged in, or logged in and expired: always say so, and say how to fix it.

Before this, a lapsed session surfaced as `harumi-api returned HTTP 422: Refresh
token is not valid`, and a missing one was only noticed after a command had
already printed "Creating project...".
"""

from __future__ import annotations

import time

import httpx
import pytest
from typer.testing import CliRunner

from harumi import auth, cli, config
from harumi.client import ApiClient, Client
from harumi.config import Config
from harumi.errors import ApiError, NotAuthenticatedError

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _store(*, expires_in: float = -3600, refresh: str = "dead-refresh", email: str = "ana@example.com"):
    config.save_credentials(
        access_token="old-access",
        refresh_token=refresh,
        email=email,
        expires_at=time.time() + expires_in,
    )


def _transport(*, refresh_status: int, api_status: int = 401) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/refresh"):
            return httpx.Response(refresh_status, json={"detail": "Refresh token is not valid"})
        return httpx.Response(api_status, json={"detail": "Invalid authentication credentials"})

    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> Client:
    return Client(api_url="https://api.harumi.io/api", transport=transport)


# -- the messages themselves --------------------------------------------------


def test_not_logged_in_says_how_to_log_in_and_how_to_sign_up():
    message = str(NotAuthenticatedError())
    assert "not logged in" in message
    assert "`harumi login`" in message
    assert "--signup" in message


def test_expired_says_so_and_how_to_sign_in_again():
    message = str(NotAuthenticatedError(expired=True))
    assert "expired" in message
    assert "`harumi login`" in message


def test_the_environment_is_named_only_when_it_is_not_production():
    assert "staging" in str(NotAuthenticatedError(environment="staging"))
    assert "staging" in str(NotAuthenticatedError(expired=True, environment="staging"))
    assert "production" not in str(NotAuthenticatedError(environment="production"))
    assert " on " not in str(NotAuthenticatedError(expired=True, environment="production"))


def test_a_custom_message_is_kept():
    assert str(NotAuthenticatedError("custom")) == "custom"


# -- a refused refresh token is the end of the session ------------------------


@pytest.mark.parametrize("status", [400, 401, 422])
def test_a_refused_refresh_token_is_an_expired_session_not_an_http_error(status):
    cfg = Config.load()
    transport = _transport(refresh_status=status)

    with pytest.raises(NotAuthenticatedError) as exc:
        auth.refresh_session(cfg, "dead-refresh", transport=transport)

    assert exc.value.expired is True
    assert "HTTP" not in str(exc.value)


@pytest.mark.parametrize("status", [403, 404, 408, 429, 500, 502, 503])
def test_a_refresh_that_fails_for_other_reasons_is_not_called_an_expired_session(status):
    """A server hiccup, a rate limit, or a proxy or firewall in front of the API
    (403, 404) says nothing about the login. Telling someone to sign in again then
    sends them into a login loop that cannot fix it."""
    cfg = Config.load()

    with pytest.raises(ApiError) as exc:
        auth.refresh_session(cfg, "refresh", transport=_transport(refresh_status=status))

    assert not isinstance(exc.value, NotAuthenticatedError)


# -- through the client -------------------------------------------------------


def test_request_with_a_dead_session_raises_the_friendly_error():
    _store()
    api = ApiClient(Config.load(), transport=_transport(refresh_status=422))

    with pytest.raises(NotAuthenticatedError) as exc:
        api.request("GET", "/users/profile")

    assert exc.value.expired is True
    assert "`harumi login`" in str(exc.value)


def test_a_401_that_survives_a_successful_refresh_is_still_an_expired_login():
    """Refresh worked, retry still refused: the account has no access."""
    _store(expires_in=3600)
    calls = {"api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/refresh"):
            return httpx.Response(
                200,
                json={"id": "u1", "email": "ana@example.com", "access_token": "new", "refresh_token": "r2"},
            )
        calls["api"] += 1
        return httpx.Response(401, json={"detail": "Invalid authentication credentials"})

    api = ApiClient(Config.load(), transport=httpx.MockTransport(handler))

    with pytest.raises(NotAuthenticatedError):
        api.request("GET", "/users/profile")
    assert calls["api"] == 2  # tried once, refreshed, tried once more, then stopped


def test_stream_with_a_dead_session_raises_the_friendly_error():
    _store()
    api = ApiClient(Config.load(), transport=_transport(refresh_status=422))

    with pytest.raises(NotAuthenticatedError) as exc:
        with api.stream("GET", "/files/x"):
            pass

    assert exc.value.expired is True


def _refused(config_, refresh_token, transport=None):
    raise NotAuthenticatedError(expired=True, environment=config_.environment)


def test_an_access_token_already_past_expiry_with_a_refused_refresh_fails_up_front(monkeypatch):
    """Nothing can work, so say so now rather than sending a request that is
    certain to be rejected."""
    _store(expires_in=-60)
    monkeypatch.setattr(auth, "refresh_session", _refused)

    with pytest.raises(NotAuthenticatedError) as exc:
        auth.get_valid_access_token(Config.load())

    assert exc.value.expired is True


def test_a_token_with_seconds_left_is_still_tried_when_the_refresh_is_refused(monkeypatch):
    """Inside the refresh window but not yet expired: the request may still work,
    and if it does not, the 401 is reported properly."""
    _store(expires_in=30)
    monkeypatch.setattr(auth, "refresh_session", _refused)

    assert auth.get_valid_access_token(Config.load()) == "old-access"


def test_a_server_error_while_refreshing_falls_back_to_the_stored_token(monkeypatch):
    """Nothing wrong with the login: the API had a bad moment. Don't ask for a
    new login; let the real request decide."""
    _store(expires_in=-60)

    def boom(config_, refresh_token, transport=None):
        raise ApiError(503, "unavailable")

    monkeypatch.setattr(auth, "refresh_session", boom)

    assert auth.get_valid_access_token(Config.load()) == "old-access"


# -- at the command line ---------------------------------------------------------


@pytest.fixture
def api(monkeypatch):
    """Point every command at `_transport` instead of the network.

    Swaps only the `Client` class, so the real `cli._get_client` (including its
    "is there a login at all" check) is what the commands run.
    """
    holder = {"transport": _transport(refresh_status=422)}

    def make_client(api_url=None, git_url=None, org_id=None):
        return Client(
            api_url="https://api.harumi.io/api",
            git_url=git_url,
            org_id=org_id,
            transport=holder["transport"],
        )

    monkeypatch.setattr(cli, "Client", make_client)
    return holder


def test_a_command_without_a_login_fails_before_printing_anything_else(monkeypatch):
    """`harumi new` used to print "Creating project..." and then fail."""
    monkeypatch.setattr(
        Client, "create_project", lambda *a, **k: pytest.fail("must not reach the network")
    )

    result = runner.invoke(cli.app, ["new", "Demo", "--no-clone"])

    assert result.exit_code == 1
    assert "Creating project" not in result.output
    assert "not logged in" in result.output
    assert "harumi login" in result.output


@pytest.mark.parametrize("argv", [["whoami"], ["specs"], ["examples"], ["projects", "list"], ["usage"]])
def test_every_kind_of_command_says_how_to_log_in_when_there_is_no_login(argv):
    result = runner.invoke(cli.app, argv)

    assert result.exit_code == 1
    assert "harumi login" in result.output
    assert "HTTP" not in result.output


def test_a_dead_session_at_the_command_line_is_not_an_http_422(api):
    _store()

    result = runner.invoke(cli.app, ["whoami"])

    assert result.exit_code == 1
    assert "expired" in result.output
    assert "harumi login" in result.output
    assert "422" not in result.output
    assert "HTTP" not in result.output


def test_the_message_names_staging_when_staging_is_the_environment(monkeypatch):
    monkeypatch.setenv("HARUMI_ENV", "staging")

    result = runner.invoke(cli.app, ["whoami"])

    assert "not logged in on staging" in result.output
    assert "harumi login" in result.output


def test_an_expired_staging_session_names_staging_too(api, monkeypatch):
    monkeypatch.setenv("HARUMI_ENV", "staging")
    _store()

    result = runner.invoke(cli.app, ["whoami"])

    assert "session on staging has expired" in result.output


def test_a_server_outage_is_still_reported_as_an_outage_not_as_a_login_problem(api):
    _store(expires_in=3600)
    api["transport"] = _transport(refresh_status=500, api_status=503)

    result = runner.invoke(cli.app, ["whoami"])

    assert result.exit_code == 1
    assert "expired" not in result.output
    assert "not logged in" not in result.output


def test_run_without_a_login_does_not_push_your_code_first(tmp_path, monkeypatch):
    """`harumi run` pushes a scratch branch *before* its first API call. Without
    the up-front login check it would upload the working tree and only then
    discover there is nobody logged in."""
    import json

    folder = tmp_path / "work"
    (folder / ".harumi").mkdir(parents=True)
    (folder / ".harumi" / "config.json").write_text(
        json.dumps(
            {
                "project_id": "proj-1",
                "repo": {
                    "owner": "acme",
                    "name": "solver",
                    "clone_url": "https://git.harumi.test/acme/solver.git",
                    "default_branch": "main",
                },
            }
        )
    )
    monkeypatch.chdir(folder)
    monkeypatch.setattr(cli, "is_dirty", lambda: True)
    monkeypatch.setattr(cli, "has_unpushed_commits", lambda: False)
    monkeypatch.setattr(
        cli, "push_scratch", lambda **k: pytest.fail("pushed code although nobody is logged in")
    )

    result = runner.invoke(cli.app, ["run"])

    assert result.exit_code == 1
    assert "not logged in" in result.output
    assert "harumi login" in result.output


# -- a failed refresh is labelled as one ----------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 404, 500, 502, 503])
def test_a_refresh_that_fails_without_ending_the_session_is_its_own_kind_of_error(status):
    from harumi.errors import SessionRefreshError

    with pytest.raises(SessionRefreshError) as exc:
        auth.refresh_session(Config.load(), "r", transport=_transport(refresh_status=status))

    assert exc.value.status_code == status
    assert "renew your login" in str(exc.value)
    assert isinstance(exc.value, ApiError)  # existing handlers still catch it


def test_an_unreachable_server_is_not_dressed_up_as_a_refresh_failure():
    from harumi.errors import ApiUnreachableError, SessionRefreshError

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ApiUnreachableError) as exc:
        auth.refresh_session(Config.load(), "r", transport=httpx.MockTransport(down))
    assert not isinstance(exc.value, SessionRefreshError)


def test_at_the_command_line_a_blocked_refresh_says_so_and_does_not_say_expired(api):
    _store()
    api["transport"] = _transport(refresh_status=403)  # e.g. a firewall in front of the API

    result = runner.invoke(cli.app, ["whoami"])

    out = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "Could not renew your login" in out and "403" in out
    assert "has expired" not in out and "Run `harumi login`" not in out, "sent them into a login loop"


# -- the proactive refresh goes where the client goes ----------------------------------------------------


def test_the_proactive_refresh_uses_the_clients_own_transport():
    """The refresh that runs *before* a request (the access token is about to expire)
    used a default transport, so the five tests above it had been sending a fake refresh
    token to the real production API, and passed only because it answered 422 like the
    fake did. It has to go through the transport the client was given."""
    _store()  # an expired access token, so the refresh happens before the request
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/users/refresh"):
            return httpx.Response(
                200, json={"id": "u1", "email": "ana@example.com", "access_token": "new", "refresh_token": "r2", "expires_at": int(time.time()) + 3600}
            )
        return httpx.Response(200, json={"ok": True})

    ApiClient(Config.load(), transport=httpx.MockTransport(handler)).request("GET", "/users/profile")

    assert seen == ["/api/users/refresh", "/api/users/profile"], seen
