"""The release notes behind `harumi whats-new`.

`changelog.md` ships inside the wheel (hatch includes every file under
`src/harumi`), so the notes are available offline and always match the installed
version. Format: one `## <version> - <date>` heading per release, newest first,
followed by the notes as plain Markdown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from typing import Optional


@dataclass(frozen=True)
class Release:
    version: str
    date: Optional[str]
    notes: str


_HEADING = re.compile(r"^## (\d+(?:\.\d+)*)(?:\s+-\s+(\S.*))?\s*$", re.M)


def parse(text: str) -> list[Release]:
    matches = list(_HEADING.finditer(text))
    releases = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        releases.append(Release(match.group(1), match.group(2), text[match.end():end].strip()))
    return releases


def load() -> list[Release]:
    return parse(resources.files("harumi").joinpath("changelog.md").read_text(encoding="utf-8"))
