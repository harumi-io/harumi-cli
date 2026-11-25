"""Local validation of a portable canvas declaration, not source or publication.

The v1 shape and path rules track harumi-platform's canvas-core SOURCE.md.
This module does not open the declared source files or execute code.
"""

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

MAX_MANIFEST_BYTES = 64_000
MAX_FILES = 32
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
# Match canvas-core v1 and Microsoft's documented COM1–9 / LPT1–9 names.
_WINDOWS_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.IGNORECASE)


class CanvasManifestError(ValueError):
    """An invalid or unsupported canvas source declaration."""


def _reject_constant(_: str) -> None:
    raise ValueError("Non-JSON number")


def _valid_path(path: Any) -> bool:
    return (
        isinstance(path, str)
        and 0 < len(path) <= 200
        and all(
            _SEGMENT.fullmatch(part) is not None and not part.endswith(".") and _WINDOWS_DEVICE.match(part) is None
            for part in path.split("/")
        )
    )


def validate_manifest(value: Any) -> None:
    """Check the declaration only. Actual file bytes and permissions are not inspected."""
    if not isinstance(value, dict) or set(value) != {"version", "entry", "files", "capabilities"}:
        raise CanvasManifestError("Expected version, entry, files and capabilities only.")
    version = value["version"]
    if type(version) not in (int, float) or version != 1:
        raise CanvasManifestError("Unsupported canvas source manifest version.")
    entry, files, capabilities = value["entry"], value["files"], value["capabilities"]
    if not _valid_path(entry) or not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise CanvasManifestError("Entry and file list must contain valid source-relative paths (1–32 files).")
    if any(not _valid_path(path) for path in files):
        raise CanvasManifestError("Source paths must be normalized, relative and portable.")
    folded = [path.lower() for path in files]
    if len(set(folded)) != len(folded):
        raise CanvasManifestError("Source paths must be unique ignoring case.")
    all_paths = set(folded)
    for path in folded:
        parts = path.split("/")
        if any("/".join(parts[:index]) in all_paths for index in range(1, len(parts))):
            raise CanvasManifestError("A source path cannot be both a file and a directory.")
    if entry not in files:
        raise CanvasManifestError("The entry must be one of the declared files.")
    if not isinstance(capabilities, list) or len(capabilities) > 1 or any(c != "data.read" for c in capabilities):
        raise CanvasManifestError("Only the data.read capability is supported.")


def read_manifest(path: Path) -> None:
    """Read at most the local declaration limit; never follow its declared paths."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise CanvasManifestError("The manifest must be a regular file.")
        raw = file.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise CanvasManifestError("The manifest exceeds 64000 bytes.")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise CanvasManifestError("The manifest starts with a UTF-8 byte-order mark; save it as UTF-8 without BOM.")
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CanvasManifestError("The manifest must be valid UTF-8 JSON.") from None
    validate_manifest(value)
