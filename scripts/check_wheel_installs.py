#!/usr/bin/env python3
"""Install a built wheel into a venv with nothing else in it, and check that `harumi` starts.

Why this exists: 0.11.0 did not start on Python 3.9 (`ModuleNotFoundError: No module named 'tomli'`), and
nothing noticed. Every test job installs the dev extras, and pytest depends on `tomli` on 3.9, so the missing
dependency was always present; the release job built the wheel but installed it only on 3.12. A user installs
exactly the wheel, so this does the same: a fresh venv, the wheel, and whatever pip resolves for it.

Usage:
    python scripts/check_wheel_installs.py dist/harumi-X.Y.Z-py3-none-any.whl [--expect-version X.Y.Z]

Exits 0 when `harumi --version` and `harumi --help` run in that environment, 1 with the output otherwise.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

# Packages that would hide a missing dependency if they were present: they are what the test jobs add.
MASKS = ("pytest", "pytest-asyncio")


def run(command: list[str], env: dict[str, str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--expect-version", help="the version `harumi --version` must report")
    args = parser.parse_args(argv)

    if not args.wheel.is_file():
        print(f"no such wheel: {args.wheel}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        env_dir = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
        bindir = env_dir / ("Scripts" if os.name == "nt" else "bin")
        python = str(bindir / ("python.exe" if os.name == "nt" else "python"))
        harumi = str(bindir / ("harumi.exe" if os.name == "nt" else "harumi"))

        env = {
            **os.environ,
            "HARUMI_HOME": str(root / "home"),  # never read or write a real login
            "HARUMI_NO_UPDATE_CHECK": "1",
            "NO_COLOR": "1",
        }
        env.pop("PYTHONPATH", None)  # a checkout on the path would hide a module the wheel left out

        installed = run([python, "-m", "pip", "install", "--quiet", str(args.wheel)], env, timeout=600)
        if installed.returncode != 0:
            print(f"pip could not install {args.wheel.name}:\n{installed.stdout}{installed.stderr}", file=sys.stderr)
            return 1

        listing = run([python, "-m", "pip", "list", "--format=freeze"], env).stdout.lower()
        present = [name for name in MASKS if f"\n{name}==" in f"\n{listing}"]
        if present:
            print(f"the check environment is not clean: {present} is installed, which could hide a missing dependency", file=sys.stderr)
            return 1

        version = run([harumi, "--version"], env)
        if version.returncode != 0:
            print(f"`harumi --version` failed on Python {sys.version.split()[0]} with only the wheel installed:\n"
                  f"{version.stdout}{version.stderr}", file=sys.stderr)
            return 1
        reported = version.stdout.strip().split()[-1] if version.stdout.strip() else ""
        if args.expect_version and reported != args.expect_version:
            print(f"`harumi --version` printed {version.stdout.strip()!r}, expected {args.expect_version!r}", file=sys.stderr)
            return 1

        helped = run([harumi, "--help"], env)
        if helped.returncode != 0:
            print(f"`harumi --help` failed:\n{helped.stdout}{helped.stderr}", file=sys.stderr)
            return 1

        print(f"ok: {version.stdout.strip()} starts on Python {sys.version.split()[0]} from the wheel alone")
        return 0


if __name__ == "__main__":
    sys.exit(main())
