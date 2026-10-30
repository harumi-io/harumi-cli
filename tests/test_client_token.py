"""`Client(token=...)`: use the library from CI without `harumi login`.

A token is a bearer credential handed in by the caller (an environment variable in a pipeline, say). It is used as given:
it is never written to disk, never shown by `repr` or in an error, and since there is no session behind it, a 401 means
the token was refused and nothing is renewed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from harumi import Client
from harumi.errors import HarumiError, NotAuthenticatedError

TOKEN = "ci-token-SECRET-0123456789"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A throwaway home with no login in it at all: a token must be enough on its own."""
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "home"


class Server:
    def __init__(self, status=200):
        self.requests: list[httpx.Request] = []
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})
        return httpx.Response(200, json={"id": "u1", "email": "ci@example.com"})

    def client(self, **kw):
        return Client(token=TOKEN, transport=httpx.MockTransport(self.handler), **kw)


def files_under(path):
    return sorted(str(p) for p in path.rglob("*")) if path.exists() else []


def test_the_token_is_sent_as_the_bearer_credential_with_no_login_on_file():
    server = Server()

    profile = server.client().get_profile()

    assert profile.email == "ci@example.com"
    assert server.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


def test_the_organization_is_still_sent():
    server = Server()

    server.client(org_id="org-9").get_profile()

    assert server.requests[0].headers["X-Organization"] == "org-9"


def test_nothing_is_written_to_disk(home):
    server = Server()
    client = server.client()

    client.get_profile()
    client.get_profile()

    assert files_under(home) == [], "a token must not create credentials, a config or a lock file"


def test_it_does_not_read_or_need_the_stored_login(home, monkeypatch):
    """Even a broken credentials file must not matter: the token is the credential."""
    path = home / "environments" / "production" / "credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json")
    server = Server()

    server.client().get_profile()

    assert server.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert path.read_text() == "{ not json", "left exactly as it was"


@pytest.mark.parametrize("what", ["client", "api", "config"])
def test_the_token_is_not_in_repr_or_str(what):
    client = Server().client()
    obj = {"client": client, "api": client.api, "config": client.config}[what]

    for text in (repr(obj), str(obj), repr(vars(obj))):
        assert TOKEN not in text and TOKEN[:8] not in text


def test_the_token_is_not_in_a_dump_of_the_client_objects_attributes():
    client = Server().client()

    dumped = repr(vars(client)) + repr(vars(client.api)) + repr(getattr(client.api, "__dict__", {}))

    assert TOKEN not in dumped


def test_a_refused_token_says_so_and_does_not_tell_you_to_log_in():
    server = Server(status=401)

    with pytest.raises(NotAuthenticatedError) as exc:
        server.client().get_profile()

    message = str(exc.value)
    assert "token" in message.lower() and "refused" in message.lower()
    assert "harumi login" not in message, "there is no login to renew: the advice would be wrong"
    assert TOKEN not in message


def test_a_refused_token_is_not_retried_and_nothing_is_renewed():
    server = Server(status=401)

    with pytest.raises(NotAuthenticatedError):
        server.client().get_profile()

    assert len(server.requests) == 1, "a retry would send the same refused token again"
    assert not any("refresh" in r.url.path or "otp" in r.url.path for r in server.requests)


@pytest.mark.parametrize("status", [403, 404, 500])
def test_other_errors_are_the_usual_api_errors_without_the_token(status):
    server = Server(status=status)

    with pytest.raises(HarumiError) as exc:
        server.client().get_profile()

    assert TOKEN not in str(exc.value) and TOKEN not in repr(exc.value)


def test_a_network_failure_does_not_carry_the_token():
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = Client(token=TOKEN, transport=httpx.MockTransport(boom))

    with pytest.raises(HarumiError) as exc:
        client.get_profile()

    assert TOKEN not in str(exc.value) and TOKEN not in repr(exc.value)
    assert TOKEN not in repr(exc.value.__cause__ or "")


def test_an_empty_token_is_refused_up_front():
    """`Client(token=os.environ.get("X"))` with X unset would otherwise fall back to the stored login without a word."""
    for bad in ("", "   "):
        with pytest.raises(HarumiError) as exc:
            Client(token=bad)
        assert "token" in str(exc.value).lower()


def test_without_a_token_the_stored_login_and_settings_are_still_what_is_used(home):
    """The other half of the promise: a plain `Client()` is unchanged, so the saved login, host and organization apply."""
    import base64
    import time

    from harumi import config

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    access = f"{b64({'alg': 'none'})}.{b64({'exp': int(time.time()) + 3600, 'sub': 'u'})}.sig"
    config.save_credentials(access_token=access, refresh_token="stored-refresh", email="a@b.co")
    stored(home)
    server = Server()

    Client(transport=httpx.MockTransport(server.handler)).get_profile()

    assert server.requests, "a request must actually have been made"
    assert server.requests[0].headers["Authorization"] == f"Bearer {access}"
    assert server.requests[0].url.host == "stored.example" and server.requests[0].headers["X-Organization"] == "org-stored"


def test_two_clients_keep_their_own_tokens():
    first, second = Server(), Server()

    Client(token="token-one", transport=httpx.MockTransport(first.handler)).get_profile()
    Client(token="token-two", transport=httpx.MockTransport(second.handler)).get_profile()

    assert first.requests[0].headers["Authorization"] == "Bearer token-one"
    assert second.requests[0].headers["Authorization"] == "Bearer token-two"


def test_a_streamed_download_uses_the_token_too_and_does_not_renew_on_401():
    server = Server(status=401)
    client = server.client()

    with pytest.raises(NotAuthenticatedError) as exc:
        with client.api.stream("GET", "/anything"):
            pass

    assert server.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert len(server.requests) == 1 and "harumi login" not in str(exc.value)


def test_login_methods_do_not_pretend_to_work_with_a_token():
    """`logout()` on a token client would delete someone else's stored login. There is nothing to log out of."""
    client = Server().client()

    with pytest.raises(HarumiError) as exc:
        client.logout()

    assert "token" in str(exc.value).lower()


def test_the_documented_signature_names_the_parameter():
    import inspect

    assert "token" in inspect.signature(Client.__init__).parameters
    assert "token" in (Client.__doc__ or "") or "token" in (Client.__init__.__doc__ or "")


# -- what a token does NOT read: the stored configuration ------------------------------------------------------------


def stored(home, *, api_url="https://stored.example/api", org_id="org-stored"):
    path = home / "environments" / "production" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"api_url": api_url, "org_id": org_id}))
    return path


def test_a_stored_api_url_cannot_redirect_a_token_to_another_host(home):
    """Where the token goes must be what the caller said, never what an old ~/.harumi happens to hold."""
    stored(home)
    server = Server()

    server.client().get_profile()

    assert server.requests[0].url.host != "stored.example"
    assert server.requests[0].url.host == "api.harumi.io"


def test_a_stored_organization_is_not_attached_to_a_token(home):
    stored(home)
    server = Server()

    server.client().get_profile()

    assert "X-Organization" not in server.requests[0].headers


def test_an_explicit_url_or_org_still_wins_over_the_defaults(home):
    stored(home)
    server = Server()

    server.client(org_id="org-given", api_url="https://given.example/api").get_profile()

    assert server.requests[0].url.host == "given.example"
    assert server.requests[0].headers["X-Organization"] == "org-given"


def test_the_legacy_credentials_file_is_not_moved(home, tmp_path):
    """Config.load used to migrate ~/.harumi/credentials.json into place: a write the docs said never happens."""
    home.mkdir(parents=True)
    legacy = home / "credentials.json"
    legacy.write_text(json.dumps({"access_token": "old", "refresh_token": "old"}))

    Server().client().get_profile()

    assert legacy.exists() and not (home / "environments").exists(), "nothing may be moved or created"


def test_the_environment_variables_still_apply(home, monkeypatch):
    """A CI job sets HARUMI_API_URL / HARUMI_ORG: those are the caller's explicit choice, not stored state."""
    monkeypatch.setenv("HARUMI_API_URL", "https://from-env.example/api")
    monkeypatch.setenv("HARUMI_ORG", "org-env")
    server = Server()

    server.client().get_profile()

    assert server.requests[0].url.host == "from-env.example"
    assert server.requests[0].headers["X-Organization"] == "org-env"
