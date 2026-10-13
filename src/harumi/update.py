"""Update awareness: a throttled "a newer version is available" notice, a one-time
"you just upgraded" hint, and the bundled changelog behind `harumi whats-new`.

Design constraints, because this runs on every real invocation:

* It must never break or slow a command. Every entry point swallows its own
  errors, the network lookup runs in a daemon thread with a short timeout, and
  the notice is printed *after* the command, from the cache.
* It must not nag. PyPI is asked at most once per `CHECK_INTERVAL`, and the same
  notice is shown at most once per `REMIND_INTERVAL`. A newer release than the
  one already announced is shown straight away.
* It must not pollute scripts. Nothing is printed unless stderr is a terminal,
  and never under CI, `HARUMI_NO_UPDATE_CHECK`, or for `--version` (whose output
  is a machine-parsed contract — see the harumi-cli-setup skill).

State lives in `~/.harumi/update-check.json`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, TextIO

import httpx

from harumi import __version__
from harumi import config

PYPI_URL = "https://pypi.org/pypi/harumi/json"
CHECK_INTERVAL = 24 * 60 * 60  # ask PyPI at most once a day
REMIND_INTERVAL = 24 * 60 * 60  # show the same notice at most once a day
FETCH_TIMEOUT = 2.0  # seconds, for the PyPI request
JOIN_TIMEOUT = 1.0  # seconds the process will wait for that request at exit
DISABLE_ENV = "HARUMI_NO_UPDATE_CHECK"

Version = tuple[int, ...]


def parse_version(text: str) -> Optional[Version]:
    """`1.2.3` -> (1, 2, 3). Anything else (a pre-release like `1.2.3rc1`, a
    local build) is None, so it is never announced or compared."""
    if not re.fullmatch(r"\d+(\.\d+)*", text.strip()):
        return None
    return tuple(int(part) for part in text.strip().split("."))


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def state_path() -> Path:
    return config.HARUMI_HOME / "update-check.json"


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def fetch_latest(url: str = PYPI_URL, timeout: float = FETCH_TIMEOUT) -> Optional[str]:
    """The newest stable version on PyPI, or None on any failure."""
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        version = response.json()["info"]["version"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return version if isinstance(version, str) and parse_version(version) else None


def upgrade_command() -> str:
    """The command that upgrades *this* install, guessed from where it lives."""
    prefix = Path(sys.prefix).as_posix().lower()
    if "/pipx/" in prefix:
        return "pipx upgrade harumi"
    if "/uv/tools/" in prefix:
        return "uv tool upgrade harumi"
    return "python3 -m pip install --upgrade harumi"


def _command_name(argv: list[str]) -> tuple[Optional[str], list[str]]:
    """The subcommand and the global flags that precede it.

    Only `--env` takes a value among the global options, so its value must not be
    mistaken for the command (`harumi --env staging whats-new`). Anything after
    the command belongs to the command, so a `--version` there is not ours.
    """
    flags: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
        elif token == "--env":
            flags.append(token)
            skip_next = True
        elif token.startswith("-"):
            flags.append(token)
        else:
            return token, flags
    return None, flags


def enabled(argv: list[str], environ: Mapping[str, str], stream: TextIO) -> bool:
    """Whether this invocation may print an update message at all."""
    if environ.get(DISABLE_ENV, "").lower() in {"1", "true", "yes"}:
        return False
    if environ.get("CI"):
        return False
    command, global_flags = _command_name(argv)
    if "--version" in global_flags:
        return False
    # `whats-new` is the thing the notice points at; don't nag inside it.
    if command == "whats-new":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


@dataclass
class Notice:
    kind: str  # "update" | "upgraded"
    current: str
    latest: Optional[str] = None
    previous: Optional[str] = None


def decide(state: dict, current: str, now: float) -> tuple[Optional[Notice], dict]:
    """Pure core: given saved state, what (if anything) to say, and the new state.

    Pre-existing keys: `checked_at`, `latest`, `notified_at`, `notified_version`,
    `last_seen_version`.
    """
    new_state = dict(state)
    notice: Optional[Notice] = None

    last_seen = state.get("last_seen_version")
    if isinstance(last_seen, str) and last_seen != current:
        # Just upgraded (or, rarely, downgraded): say so once.
        if is_newer(current, last_seen):
            notice = Notice("upgraded", current=current, previous=last_seen)
    new_state["last_seen_version"] = current

    if notice is None:
        latest = state.get("latest")
        if isinstance(latest, str) and is_newer(latest, current):
            announced = state.get("notified_version")
            notified_at = state.get("notified_at")
            recently = isinstance(notified_at, (int, float)) and now - notified_at < REMIND_INTERVAL
            if announced != latest or not recently:
                notice = Notice("update", current=current, latest=latest)
                new_state["notified_version"] = latest
                new_state["notified_at"] = now
    return notice, new_state


def needs_refresh(state: dict, now: float) -> bool:
    checked_at = state.get("checked_at")
    return not isinstance(checked_at, (int, float)) or now - checked_at >= CHECK_INTERVAL


def render(notice: Notice) -> str:
    if notice.kind == "upgraded":
        return (
            f"[green]harumi updated[/green] {notice.previous} -> [bold]{notice.current}[/bold]. "
            "See what changed: [bold]harumi whats-new[/bold]"
        )
    return (
        f"[yellow]A new version of harumi is available:[/yellow] {notice.current} -> "
        f"[bold]{notice.latest}[/bold]\n"
        f"  Upgrade: [bold]{upgrade_command()}[/bold]    What's new: [bold]harumi whats-new[/bold]\n"
        f"  [dim]Silence this with {DISABLE_ENV}=1[/dim]"
    )


class UpdateCheck:
    """Wrap one CLI invocation: `start()` before it, `finish()` after it."""

    def __init__(
        self,
        argv: list[str],
        *,
        environ: Mapping[str, str] = os.environ,
        stream: Optional[TextIO] = None,
        fetch: Callable[[], Optional[str]] = fetch_latest,
        clock: Callable[[], float] = time.time,
        current: str = __version__,
        printer: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.active = enabled(argv, environ, self.stream)
        self._fetch = fetch
        self._clock = clock
        self._current = current
        self._printer = printer
        self._thread: Optional[threading.Thread] = None
        self._fetched: Optional[str] = None

    def start(self) -> None:
        try:
            if not self.active:
                return
            if needs_refresh(load_state(), self._clock()):
                self._thread = threading.Thread(target=self._refresh, daemon=True)
                self._thread.start()
        except Exception:
            self.active = False

    def _refresh(self) -> None:
        try:
            self._fetched = self._fetch()
        except Exception:
            self._fetched = None

    def finish(self) -> None:
        try:
            if not self.active:
                return
            if self._thread is not None:
                self._thread.join(JOIN_TIMEOUT)
            state = load_state()
            now = self._clock()
            if self._thread is not None:
                # Record the attempt even when it failed, so an offline machine
                # retries tomorrow instead of on every command.
                state["checked_at"] = now
                if self._fetched:
                    state["latest"] = self._fetched
            notice, state = decide(state, self._current, now)
            save_state(state)
            if notice is not None:
                self._emit(render(notice))
        except Exception:
            return

    def _emit(self, message: str) -> None:
        if self._printer is not None:
            self._printer(message)
            return
        from rich.console import Console

        Console(file=self.stream).print("\n" + message)
