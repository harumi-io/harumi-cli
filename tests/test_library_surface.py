"""The Python library (`from harumi import Client`): kept as it is, but checked (decision D5).

Two kinds of check. The documented surface must be real: every call the README and the reference show is bound
to the signature it names, which would have caught `client.enable_share(...)` (replaced by link-scoped share
methods in August, the doc never updated). And a `Client` must be self-contained: the CLI is a single-shot
process with one active environment, a library user may hold two.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import re
import subprocess
import sys
import threading

import httpx
import pytest

from harumi import Client, config
from harumi.errors import HarumiError, NotAuthenticatedError

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ["README.md", ".agents/skills/harumi-cli/references/commands.md", ".agents/skills/harumi-cli/SKILL.md"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing in this file may reach a real server: a call that ignores the injected transport fails loudly."""

    def refuse(self, request):
        raise AssertionError(f"a test reached the network: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr(config, "CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr(config, "_ACTIVE_ENV", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)


# -- the documented surface is real -------------------------------------------------------------------------------------


def python_blocks(path):
    return re.findall(r"```python\n(.*?)```", (ROOT / path).read_text(), re.S)


def documented_calls():
    """(doc, call text, target, ast.Call) for every method call on `client` and every harumi function called."""
    out = []
    for doc in DOCS:
        for block in python_blocks(doc):
            tree = ast.parse(block)
            imported = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("harumi"):
                    for alias in node.names:
                        imported[alias.asname or alias.name] = (node.module, alias.name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "client":
                    out.append((doc, ast.unparse(node)[:70], ("Client", func.attr), node))
                elif isinstance(func, ast.Name) and func.id in imported and func.id != "Client":
                    out.append((doc, ast.unparse(node)[:70], imported[func.id], node))
    return out


def target_function(target):
    first, name = target
    if first == "Client":
        return getattr(Client, name, None)
    return getattr(importlib.import_module(first), name, None)


CALLS = documented_calls()


def test_the_scan_found_the_documented_calls():
    assert len(CALLS) >= 12, "found too few calls, so every check below would prove little"


@pytest.mark.parametrize("doc, text, target, node", CALLS, ids=[f"{c[0].split('/')[-1]}:{c[1]}" for c in CALLS])
def test_every_call_the_docs_show_exists_and_takes_those_arguments(doc, text, target, node):
    function = target_function(target)
    assert function is not None, f"{doc} calls {target[1]}, which does not exist"

    if any(isinstance(arg, ast.Starred) for arg in node.args) or any(kw.arg is None for kw in node.keywords):
        pytest.skip("a *args / **kwargs call cannot be bound without running it")
    keywords = {kw.arg: object() for kw in node.keywords}
    arguments = ([object()] if target[0] == "Client" else []) + [object() for _ in node.args]  # `self` first
    inspect.signature(function).bind(*arguments, **keywords)  # raises TypeError on a wrong name or count


@pytest.mark.parametrize("doc", DOCS)
def test_every_import_the_docs_show_works(doc):
    broken = []
    for block in python_blocks(doc):
        for node in ast.walk(ast.parse(block)):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("harumi"):
                module = importlib.import_module(node.module)
                broken += [f"from {node.module} import {a.name}" for a in node.names if not hasattr(module, a.name)]

    assert broken == []


def test_the_check_would_have_caught_enable_share():
    ghost = ast.parse("client.enable_share(binding.project_id)").body[0].value

    assert target_function(("Client", ghost.func.attr)) is None  # what the old snippet called
    assert target_function(("Client", "create_share_link")) is not None  # what exists


def test_a_wrong_argument_name_in_a_snippet_is_caught():
    function = target_function(("Client", "execute_project"))

    with pytest.raises(TypeError):
        inspect.signature(function).bind(object(), "p", branchh="main")


def test_every_name_in_all_exists():
    import harumi

    assert [n for n in harumi.__all__ if not hasattr(harumi, n)] == []


# -- importing it costs nothing ---------------------------------------------------------------------------------------------


def test_importing_the_library_does_not_load_the_cli_or_print_anything():
    """A script that only wants `Client` should not pay for typer or a menu library, nor load our CLI modules, and an
    import must never write to the terminal a pipeline is reading.

    `rich` and `click` are not checked: on Python 3.9, `import httpx` alone loads both (httpx's own CLI extras),
    so that is outside this package and would make the test pass or fail by interpreter."""
    code = (
        "import sys, harumi; "
        "mine = sorted(m for m in ('harumi.cli', 'harumi.interactive', 'harumi.update') if m in sys.modules); "
        "theirs = sorted(m for m in ('typer', 'prompt_toolkit') if m in sys.modules); "
        "print('OURS:' + ','.join(mine) + ' THEIRS:' + ','.join(theirs))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)

    assert result.stderr == "" and result.stdout.strip() == "OURS: THEIRS:", result.stdout + result.stderr


def test_every_library_error_is_one_catchable_family():
    """A script writes `except HarumiError`; nothing the library raises may escape that."""
    import harumi.errors as errors

    outside = [n for n, c in inspect.getmembers(errors, inspect.isclass) if n.endswith("Error") and not issubclass(c, HarumiError)]

    assert outside == []


def test_a_logged_out_client_raises_the_family_error_not_a_bare_exception():
    with pytest.raises(NotAuthenticatedError):
        Client().list_projects()


# -- a Client is self-contained -----------------------------------------------------------------------------------------------


def log_in(environment, token):
    config.set_active_environment(environment)
    config.save_credentials(access_token=token, refresh_token="refresh-" + token)


class Recorder:
    def __init__(self):
        self.seen = []
        self.lock = threading.Lock()

    def __call__(self, request):
        with self.lock:
            self.seen.append((request.url.host, request.headers.get("authorization"), request.url.path))
        return httpx.Response(200, json=[])


def test_two_clients_for_two_environments_each_use_their_own_login():
    """Found by this check: the second Client built overwrote a process-wide "active environment", so the first
    sent ITS login: a staging client sent the production token to the staging host."""
    log_in("production", "PROD-TOKEN")
    log_in("staging", "STAGING-TOKEN")
    seen = Recorder()
    transport = httpx.MockTransport(seen)
    staging = Client(environment="staging", transport=transport)
    production = Client(environment="production", transport=transport)  # built second

    staging.list_projects()
    production.list_projects()

    assert seen.seen[0][1] == "Bearer STAGING-TOKEN" and seen.seen[0][0] == "api.dev.harumi.io"
    assert seen.seen[1][1] == "Bearer PROD-TOKEN" and seen.seen[1][0] == "api.harumi.io"


def test_the_order_the_clients_are_used_in_does_not_matter():
    log_in("production", "PROD-TOKEN")
    log_in("staging", "STAGING-TOKEN")
    seen = Recorder()
    transport = httpx.MockTransport(seen)
    staging = Client(environment="staging", transport=transport)
    production = Client(environment="production", transport=transport)

    for client in (production, staging, staging, production):
        client.list_projects()

    assert [(h, t) for h, t, _ in seen.seen] == [
        ("api.harumi.io", "Bearer PROD-TOKEN"),
        ("api.dev.harumi.io", "Bearer STAGING-TOKEN"),
        ("api.dev.harumi.io", "Bearer STAGING-TOKEN"),
        ("api.harumi.io", "Bearer PROD-TOKEN"),
    ]


def test_clients_in_different_threads_do_not_share_a_login():
    log_in("production", "PROD-TOKEN")
    log_in("staging", "STAGING-TOKEN")
    seen = Recorder()
    transport = httpx.MockTransport(seen)
    clients = {"staging": Client(environment="staging", transport=transport), "production": Client(environment="production", transport=transport)}
    errors = []

    def work(name):
        try:
            for _ in range(25):
                clients[name].list_projects()
        except Exception as exc:  # pragma: no cover - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in clients]
    [t.start() for t in threads]
    [t.join() for t in threads]

    expected = {"api.dev.harumi.io": "Bearer STAGING-TOKEN", "api.harumi.io": "Bearer PROD-TOKEN"}
    assert errors == [] and len(seen.seen) == 50
    assert all(expected[host] == token for host, token, _ in seen.seen)


def test_a_refreshed_session_is_saved_for_the_environment_that_refreshed_it():
    """The renewal writes the new tokens: into the client's own environment, not the last one touched."""
    log_in("production", "PROD-TOKEN")
    log_in("staging", "STAGING-TOKEN")
    calls = []

    def handler(request):
        calls.append((request.url.host, request.url.path))
        if request.url.path.endswith("/users/refresh"):
            return httpx.Response(200, json={"access_token": "STAGING-NEW", "refresh_token": "refresh-NEW", "id": "u", "email": "a@b.c"})
        if request.headers.get("authorization") == "Bearer STAGING-TOKEN":
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    staging = Client(environment="staging", transport=transport)
    Client(environment="production", transport=transport)  # built last, so it would be "the active one"

    staging.list_projects()

    config.set_active_environment("staging")
    assert config.load_credentials()["access_token"] == "STAGING-NEW"
    config.set_active_environment("production")
    assert config.load_credentials()["access_token"] == "PROD-TOKEN", "production's login was overwritten by staging's refresh"


def test_logging_out_one_client_leaves_the_other_logged_in():
    log_in("production", "PROD-TOKEN")
    log_in("staging", "STAGING-TOKEN")
    staging = Client(environment="staging")
    Client(environment="production")

    staging.logout()

    config.set_active_environment("production")
    assert config.load_credentials() is not None
    config.set_active_environment("staging")
    assert config.load_credentials() is None


def test_logging_in_through_a_client_saves_the_login_for_that_clients_environment():
    """The worst version of the bug: a staging client's login saved as production's."""
    reply = {"access_token": "STAGING-NEW", "refresh_token": "refresh-NEW", "id": "u1", "email": "a@harumi.io"}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=reply))
    log_in("production", "PROD-TOKEN")
    staging = Client(environment="staging", transport=transport)
    Client(environment="production", transport=transport)  # built last, so it is the "active" one

    staging.verify_otp("a@harumi.io", "123456")

    config.set_active_environment("staging")
    assert config.load_credentials()["access_token"] == "STAGING-NEW"
    config.set_active_environment("production")
    assert config.load_credentials()["access_token"] == "PROD-TOKEN", "the staging login replaced production's"


def test_login_calls_use_the_clients_transport_like_every_other_call():
    """`request_otp` and `verify_otp` ignored the transport a Client was given, so a proxy or a test double was
    skipped on login only."""
    seen = Recorder()
    client = Client(environment="production", transport=httpx.MockTransport(seen))

    client.request_otp("a@b.c")

    assert [path for _, _, path in seen.seen] == ["/api/users/otp"]


def test_the_scope_is_live_during_the_call_and_gone_after_it():
    """Inside a call the client's environment wins; outside it the CLI's own global is back, untouched. Checked from
    the handler (during the call) because looking only before and after cannot tell a scope from no scope."""
    log_in("production", "PROD-TOKEN")
    log_in("staging", "STAGING-TOKEN")
    during = []

    def handler(request):
        during.append(config.active_environment())
        return httpx.Response(200, json=[])

    client = Client(environment="staging", transport=httpx.MockTransport(handler))
    config.set_active_environment("production")  # what the CLI would have set

    client.list_projects()

    assert during == ["staging"]
    assert config.active_environment() == "production"


def test_many_threads_sharing_one_client_refresh_an_expired_login_once():
    """The docs promise a Client works on any thread. Six threads that all find the access token expired must spend
    the single-use refresh token once between them (the credentials lock), not race and log each other out.

    This is a smoke test of the shape, not proof of the property: it failed about 1 run in 15 on Python 3.9 because
    of a race it could not reliably trigger (see `tests/test_refresh_race.py`, which makes the interleaving
    deterministic and is the real check)."""
    log_in("production", "OLD")
    used, guard, errors = [], threading.Lock(), []

    def handler(request):
        if request.url.path.endswith("/users/refresh"):
            with guard:
                if used:
                    return httpx.Response(422, json={"detail": "Refresh token is not valid"})
                used.append(1)
            return httpx.Response(200, json={"access_token": "NEW", "refresh_token": "R2", "id": "u", "email": "a@b.c"})
        if request.headers.get("authorization") == "Bearer OLD":
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json=[])

    client = Client(environment="production", transport=httpx.MockTransport(handler))

    def work():
        try:
            client.list_projects()
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == [] and len(used) == 1
    assert config.load_credentials()["access_token"] == "NEW"


def test_a_scope_nests_and_restores_on_an_error():
    with config.environment_scope("staging"):
        with config.environment_scope("production"):
            assert config.active_environment() == "production"
        assert config.active_environment() == "staging"
        with pytest.raises(RuntimeError):
            with config.environment_scope("production"):
                raise RuntimeError("boom")
        assert config.active_environment() == "staging"
    assert config.active_environment() == config.resolve_environment()


def test_streaming_downloads_use_the_clients_own_login_too():
    log_in("production", "PROD-TOKEN")
    log_in("staging", "STAGING-TOKEN")
    seen = Recorder()
    transport = httpx.MockTransport(seen)
    staging = Client(environment="staging", transport=transport)
    Client(environment="production", transport=transport)

    with staging.api.stream("GET", "/anything") as response:
        response.read()

    assert seen.seen[0][1] == "Bearer STAGING-TOKEN"
