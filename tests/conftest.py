"""Test-wide settings.

The CLI prints through rich, and whether rich (and typer's help, which is built on
it) emits colour depends on the environment: GITHUB_ACTIONS, FORCE_COLOR, PY_COLORS,
NO_COLOR, TERM. A test that compares output then passes on one machine and fails
on another. Pin it once, here, before anything imports rich or typer, so the suite
does not depend on who runs it.
"""

import os

import pytest

# Typer reads these at import time.
for _var in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
    os.environ.pop(_var, None)
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"  # typer's own switch: never force colour
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"
os.environ.setdefault("COLUMNS", "200")


# The environment a command reads its endpoints from. A test that forgets to clear one
# talks to whatever the developer's shell points at.
HARUMI_ENV_VARS = ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG")


@pytest.fixture
def isolate_harumi_home(monkeypatch):
    """Return ``isolate(home, extra_env=())``: point the CLI's config at ``home``.

    Every test module needs a throwaway HARUMI_HOME so it never reads, or writes, the real
    ``~/.harumi``. That patch is the same in all of them, so it lives here; what each module
    adds on top of it (a login, a terminal width, a working directory) stays in its own
    ``home`` fixture, which calls this and then does only that.
    """

    def isolate(home, extra_env=()):
        monkeypatch.setattr("harumi.config.HARUMI_HOME", home)
        monkeypatch.setattr("harumi.config.CONFIG_PATH", home / "config.json")
        monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", home / "credentials.json")
        monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
        monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
        for var in HARUMI_ENV_VARS + tuple(extra_env):
            monkeypatch.delenv(var, raising=False)
        return home

    return isolate
