#!/usr/bin/env python3
"""Emits a machine-readable description of the `harumi` command tree.

harumi-docs uses this to detect when content/docs/cli/commands.mdx has fallen
behind the actual CLI (a command was added, removed, or renamed) without
hand-maintaining a second copy of the command list.

Off-the-shelf click doc tools (sphinx-click, mkdocs-click, click-man) do not
work here: Typer >=0.27 vendors click as `typer._click`, and that vendored
copy has no `Group`/`MultiCommand` class at all, so any tool that branches on
`isinstance(cmd, click.Group)` breaks at import. Detecting a group by
`getattr(command, "commands", None)` instead — as
tests/test_cli.py::test_every_command_builds already does — is the only
approach that works against the vendored click.

Usage:
    python scripts/emit_cli_surface.py > cli-surface.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

from typer.main import get_command

import harumi.cli as cli
from harumi import __version__

# Typer >=0.27 vendors its own click (`typer._click`) which renamed the builtin
# param types to Python spellings: STRING is "str" and INT is "int". Every
# release of real click — 8.1.8 through 8.4.2 — still calls them "text" and
# "integer". Python 3.9 pins typer to 0.23.x (the last line supporting it),
# which uses real click, so the same CLI emits different type labels depending
# on the interpreter. Collapse both spellings onto the vendored one so the
# committed contract compares equal on every supported Python.
#
# Only the labels that actually differ are listed: "boolean", "path", "float"
# and "uuid" are already identical across both click flavours.
_TYPE_ALIASES = {"text": "str", "integer": "int"}


def _leaves(command: Any, path: list[str]):
    """Yields (path, command) for every leaf (non-group) command.

    Mirrors tests/test_cli.py::test_every_command_builds's `leaves()` —
    duck-typed on `.commands` rather than `isinstance(..., click.Group)`,
    which is unavailable on Typer's vendored click.
    """
    subcommands = getattr(command, "commands", None)
    if subcommands:
        for name, sub in subcommands.items():
            yield from _leaves(sub, path + [name])
    else:
        yield path, command


def _param_info(param: Any) -> dict:
    return {
        "opts": list(param.opts),
        "type": _TYPE_ALIASES.get(param.type.name, param.type.name),
        "required": bool(param.required),
        "default": param.default if isinstance(param.default, (str, int, float, bool, type(None))) else None,
        "help": param.help or None,
    }


def build_surface() -> dict:
    root = get_command(cli.app)
    commands = []
    for path, command in _leaves(root, []):
        commands.append(
            {
                "path": " ".join(path),
                "help": command.get_short_help_str(limit=10_000) or None,
                "params": [_param_info(p) for p in command.params],
            }
        )
    commands.sort(key=lambda c: c["path"])
    return {"cli_version": __version__, "commands": commands}


def main() -> None:
    json.dump(build_surface(), sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
