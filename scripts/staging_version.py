#!/usr/bin/env python3
"""Stamp a CI checkout with a unique, opt-in PEP 440 development version."""

import argparse
import re
from pathlib import Path


def stamp(path: Path, run_number: int, attempt: int) -> str:
    if run_number < 1 or not 1 <= attempt < 1000:
        raise ValueError("run number must be positive and attempt must be between 1 and 999")
    text = path.read_text(encoding="utf-8")
    pattern = r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$'
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError("expected exactly one stable X.Y.Z source version")
    major, minor, patch = map(int, matches[0].groups())
    version = f"{major}.{minor}.{patch + 1}.dev{run_number * 1000 + attempt}"
    text = text[:matches[0].start()] + f'__version__ = "{version}"' + text[matches[0].end():]
    path.write_text(text, encoding="utf-8")
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_number", type=int)
    parser.add_argument("attempt", type=int)
    parser.add_argument("--path", type=Path, default=Path("src/harumi/__init__.py"))
    args = parser.parse_args()
    print(stamp(args.path, args.run_number, args.attempt))
