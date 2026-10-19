"""Test-wide settings.

The CLI prints through rich, and whether rich (and typer's help, which is built on
it) emits colour depends on the environment: GITHUB_ACTIONS, FORCE_COLOR, PY_COLORS,
NO_COLOR, TERM. A test that compares output then passes on one machine and fails
on another. Pin it once, here, before anything imports rich or typer, so the suite
does not depend on who runs it.
"""

import os

# Typer reads these at import time.
for _var in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS", "TTY_COMPATIBLE", "TTY_INTERACTIVE"):
    os.environ.pop(_var, None)
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"  # typer's own switch: never force colour
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"
os.environ.setdefault("COLUMNS", "200")
