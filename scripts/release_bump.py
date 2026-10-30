#!/usr/bin/env python3
"""Prepare a release: set `__version__`, date the changelog heading, and regenerate cli-surface.json.

The newest heading in `src/harumi/changelog.md` is the release being prepared. Write its notes under an undated
`## X.Y.Z` while you work; when `dev` is promoted to `main`, the Release bump workflow runs this and commits the result
to `dev`, so the promotion PR carries the bump and merging it is the only step left.

    python scripts/release_bump.py            # do it
    python scripts/release_bump.py --check    # say what it would do, change nothing

What it decides, from the newest heading and `__version__`:

    undated, version ahead          -> bump:  set the version, add today's date, regenerate the surface
    dated,   version ahead          -> bump:  set the version, keep the date it has
    undated, version equal          -> date:  add today's date (the version was bumped by hand)
    dated,   version equal          -> noop:  already prepared; running again changes nothing
    version behind, or no entry     -> error: exit 1, with the reason

A hotfix merged straight to `main` is not prepared by this: bump `__version__` by hand there, as 0.11.1 was.

Prints `action=... version=... date=...` lines (GitHub Actions can append them to `$GITHUB_OUTPUT`). Exit 0 for
bump, date and noop; 1 with the reason on stderr when there is nothing it can safely do.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The same anchored line `release.yml` reads the version with: it must not match the "__version__" entry in `__all__`.
VERSION_LINE = re.compile(r'^__version__ = "([^"]+)"$', re.M)
# The same line, tolerating a Windows line ending (`$` stops before `\n` but not before `\r`). release.yml reads the file
# in text mode, which turns CRLF into LF, so its own pattern above is right for what it sees.
_VERSION_LINE_ANY_EOL = re.compile(r'^__version__ = "([^"]+)"(?=\r?$)', re.M)
# The same heading pattern as `harumi.changelog`, copied so deciding needs no install. A test keeps the two in step.
# `\r?` so a changelog with Windows line endings reads the same: `$` stops before the `\n` but not before a `\r`.
HEADING = re.compile(r"^## (\d+(?:\.\d+)*)(?:[ \t]+-[ \t]+(\S.*?))?[ \t]*\r?$", re.M)
# Any '## ' line that looks like it is meant to be a release but is not one the parser reads (a prerelease, a typo).
_ATTEMPT = re.compile(r"^## (?:v|\d)\S*", re.M)
_NUMERIC = re.compile(r"^\d+(?:\.\d+)*$")


class ReleaseError(Exception):
    """There is nothing safe to do, and the message says why."""


@dataclass(frozen=True)
class Release:
    version: str
    date: Optional[str]


@dataclass(frozen=True)
class Plan:
    action: str  # "bump" | "date" | "noop"
    version: str
    date: Optional[str]


def releases(changelog_text: str) -> list[Release]:
    return [Release(m.group(1), m.group(2)) for m in HEADING.finditer(changelog_text)]


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def plan(changelog_text: str, current_version: str, *, today: str) -> Plan:
    """What to do, without touching anything. Raises `ReleaseError` when there is nothing safe to do."""
    try:
        dt.date.fromisoformat(today)
    except ValueError:
        raise ReleaseError(f"the date {today!r} is not a real YYYY-MM-DD date") from None
    if not _NUMERIC.match(current_version or ""):
        raise ReleaseError(f"__version__ is {current_version!r}, which is not a plain number like 1.2.3")
    first = HEADING.search(changelog_text)
    found = releases(changelog_text)
    # A '## ' line that looks like a release but is not one the parser reads (a prerelease, a typo), ABOVE the first
    # one it does read: the notes the author meant to release would be skipped and the previous release published
    # instead. Compared by position in the file, not by looking the heading's text up, which finds the wrong line
    # whenever one heading starts with another ('## 1.1.0rc1' and '## 1.1.0').
    unread = next(
        (m for m in _ATTEMPT.finditer(changelog_text) if not HEADING.match(changelog_text, m.start())),
        None,
    )
    if unread is not None and (first is None or unread.start() < first.start()):
        raise ReleaseError(
            f"no release notes: the heading {unread.group(0)!r} is not one the changelog reads (a plain 'X.Y.Z', "
            "optionally followed by ' - YYYY-MM-DD'; a prerelease like 1.1.0rc1 is not supported). Fix it, or the bot "
            f"would quietly release {found[0].version if found else 'nothing'} instead."
        )
    if not found:
        raise ReleaseError(
            "there are no release notes: src/harumi/changelog.md has no '## X.Y.Z' heading. "
            "Write the notes for this release under an undated '## X.Y.Z' first."
        )
    top = found[0]
    if _key(top.version) < _key(current_version):
        raise ReleaseError(
            f"the newest entry in the changelog is {top.version}, which is behind __version__ {current_version}. "
            "Put the notes for the release being prepared above it, under an undated '## X.Y.Z'."
        )
    if top.version == current_version:
        if top.date:
            return Plan("noop", top.version, top.date)
        return Plan("date", top.version, today)
    return Plan("bump", top.version, top.date or today)


def _generate_surface(root: pathlib.Path) -> str:
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    result = subprocess.run(
        [sys.executable, "scripts/emit_cli_surface.py"], cwd=root, env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ReleaseError(
            "could not regenerate cli-surface.json (scripts/emit_cli_surface.py failed): "
            + (result.stderr.strip().splitlines() or ["no output"])[-1]
        )
    return result.stdout


def _read(path: pathlib.Path) -> str:
    """The file's text with its line endings untouched (`read_text` would turn CRLF into LF)."""
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: pathlib.Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def run(
    root: pathlib.Path = ROOT, *, today: Optional[str] = None, surface: bool = True, check: bool = False
) -> Plan:
    """Decide, and unless `check`, write. Either everything is written or nothing is."""
    today = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    init_path = root / "src" / "harumi" / "__init__.py"
    notes_path = root / "src" / "harumi" / "changelog.md"
    surface_path = root / "cli-surface.json"
    init_text, notes_text = _read(init_path), _read(notes_path)
    match = _VERSION_LINE_ANY_EOL.search(init_text)
    if match is None:
        raise ReleaseError('could not find `__version__ = "..."` in src/harumi/__init__.py')

    decided = plan(notes_text, match.group(1), today=today)
    if check or decided.action == "noop":
        return decided

    new_init = _VERSION_LINE_ANY_EOL.sub(f'__version__ = "{decided.version}"', init_text, count=1)
    top = HEADING.search(notes_text)
    ending = "\r" if notes_text[top.end() - 1 : top.end()] == "\r" else ""  # keep a CRLF heading a CRLF heading
    new_notes = notes_text[: top.start()] + f"## {decided.version} - {decided.date}{ending}" + notes_text[top.end() :]
    old_surface = _read(surface_path) if surface_path.exists() else None

    # The surface generator reads `harumi.__version__`, so the source is written first. If anything after that fails,
    # put every file back: half a release is worse than none.
    written = []
    try:
        _write(init_path, new_init)
        written.append((init_path, init_text))
        new_surface = None
        if surface:
            new_surface = _generate_surface(root)
            json.loads(new_surface)  # refuse to write something that is not JSON
        _write(notes_path, new_notes)
        written.append((notes_path, notes_text))
        if new_surface is not None:
            _write(surface_path, new_surface)
            written.append((surface_path, old_surface if old_surface is not None else ""))
    except BaseException:
        for path, original in reversed(written):
            try:
                _write(path, original)
            except OSError:
                pass  # the original error is the one worth reporting
        raise
    return decided


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=pathlib.Path, default=ROOT, help="the repository to work on (default: this one)")
    parser.add_argument("--date", help="YYYY-MM-DD to put on the heading (default: today, UTC)")
    parser.add_argument("--check", action="store_true", help="say what it would do and change nothing")
    parser.add_argument("--no-surface", action="store_true", help="leave cli-surface.json alone")
    args = parser.parse_args(argv)
    try:
        decided = run(args.root, today=args.date, surface=not args.no_surface, check=args.check)
    except (ReleaseError, ValueError) as exc:
        print(f"release_bump: {exc}", file=sys.stderr)
        return 1
    print(f"action={decided.action}")
    print(f"version={decided.version}")
    print(f"date={decided.date or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
