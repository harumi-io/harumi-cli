"""Self-check for scripts/emit_cli_surface.py.

Not a framework test — deliberately dependency-free so it can run in a
release job without pytest. Asserts the emitter still sees the real command
tree and that its shape matches what commands.mdx generation/checking in
harumi-docs expects.

Run: python scripts/check_emit_cli_surface.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from emit_cli_surface import build_surface  # noqa: E402


def main() -> None:
    surface = build_surface()
    failures = []

    if not surface.get("cli_version"):
        failures.append("cli_version is missing or empty")

    commands = surface.get("commands", [])
    # Mirrors the >50 sanity floor in tests/test_cli.py::test_every_command_builds.
    if len(commands) <= 50:
        failures.append(f"only {len(commands)} commands found, expected > 50")

    paths = [c["path"] for c in commands]
    if paths != sorted(paths):
        failures.append("commands are not sorted by path")
    if len(paths) != len(set(paths)):
        failures.append("duplicate command paths found")

    known = {"repo put", "run", "projects list"}
    missing_known = known - set(paths)
    if missing_known:
        failures.append(f"expected commands missing from surface: {missing_known}")

    for command in commands:
        if not isinstance(command.get("help"), (str, type(None))):
            failures.append(f"{command['path']}: help is not a string/null")
        for param in command["params"]:
            if not param["opts"]:
                failures.append(f"{command['path']}: a param has empty opts")

    if failures:
        print("FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)

    print(f"OK: {len(commands)} commands, cli_version={surface['cli_version']}")


if __name__ == "__main__":
    main()
