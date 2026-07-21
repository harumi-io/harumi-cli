"""Configuration: base URL, credential/config file paths, org resolution.

Precedence for every setting: explicit constructor arg > environment
variable > ~/.harumi/config.json > hardcoded default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_API_URL = "https://api.harumi.io/api"

HARUMI_HOME = Path(os.environ.get("HARUMI_HOME", Path.home() / ".harumi"))
CREDENTIALS_PATH = HARUMI_HOME / "credentials.json"
CONFIG_PATH = HARUMI_HOME / "config.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))
    try:
        path.chmod(0o600)
    except OSError:
        # Best-effort on platforms without POSIX permission bits (e.g. some
        # Windows filesystems). The file is still written.
        pass


@dataclass
class Config:
    """Resolved runtime configuration for the harumi client/CLI."""

    api_url: str
    org_id: Optional[str] = None

    @classmethod
    def load(
        cls,
        api_url: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> "Config":
        file_config = _read_json(CONFIG_PATH)

        resolved_api_url = (
            api_url
            or os.environ.get("HARUMI_API_URL")
            or file_config.get("api_url")
            or DEFAULT_API_URL
        )
        resolved_org_id = (
            org_id
            or os.environ.get("HARUMI_ORG")
            or file_config.get("org_id")
        )
        return cls(api_url=resolved_api_url.rstrip("/"), org_id=resolved_org_id)

    def save_org_id(self, org_id: str) -> None:
        """Persist the resolved org id so future invocations don't need it."""
        file_config = _read_json(CONFIG_PATH)
        file_config["org_id"] = org_id
        _write_json(CONFIG_PATH, file_config)
        self.org_id = org_id


def load_credentials() -> Optional[dict[str, Any]]:
    data = _read_json(CREDENTIALS_PATH)
    return data or None


def save_credentials(access_token: str, refresh_token: str, **extra: Any) -> None:
    data: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        **extra,
    }
    _write_json(CREDENTIALS_PATH, data)


def clear_credentials() -> None:
    if CREDENTIALS_PATH.exists():
        CREDENTIALS_PATH.unlink()
