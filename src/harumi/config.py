"""Configuration: base URL, credential/config file paths, org resolution.

Precedence for every setting: explicit constructor arg > environment
variable > ~/.harumi/config.json > hardcoded default.

Project binding (.harumi/config.json in the working directory) stores the
project_id and Gitea repo metadata written by `harumi init`. It is searched
from the cwd upward so it works from subdirectories of the project.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_API_URL = "https://api.harumi.io/api"
DEFAULT_GIT_URL = "https://git.dev.harumi.io"

HARUMI_HOME = Path(os.environ.get("HARUMI_HOME", Path.home() / ".harumi"))
CREDENTIALS_PATH = HARUMI_HOME / "credentials.json"
CONFIG_PATH = HARUMI_HOME / "config.json"

# Name of the per-project binding file, searched upward from cwd.
PROJECT_CONFIG_NAME = ".harumi/config.json"


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


def _find_project_config(start: Optional[Path] = None) -> Optional[Path]:
    """Walk upward from `start` (default: cwd) looking for .harumi/config.json."""
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / PROJECT_CONFIG_NAME
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


@dataclass
class RepoBinding:
    """Gitea repo metadata stored in the per-project .harumi/config.json."""

    owner: str
    name: str
    clone_url: str
    default_branch: str = "main"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RepoBinding":
        return cls(
            owner=d["owner"],
            name=d["name"],
            clone_url=d["clone_url"],
            default_branch=d.get("default_branch", "main"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "name": self.name,
            "clone_url": self.clone_url,
            "default_branch": self.default_branch,
        }


@dataclass
class ProjectBinding:
    """Contents of .harumi/config.json in the working directory."""

    project_id: str
    repo: RepoBinding
    # Absolute path of the .harumi/config.json that was loaded.
    config_path: Path = field(default_factory=Path)

    @classmethod
    def load(cls, start: Optional[Path] = None) -> Optional["ProjectBinding"]:
        path = _find_project_config(start)
        if path is None:
            return None
        data = _read_json(path)
        if not data.get("project_id") or not data.get("repo"):
            return None
        try:
            return cls(
                project_id=data["project_id"],
                repo=RepoBinding.from_dict(data["repo"]),
                config_path=path,
            )
        except (KeyError, TypeError):
            return None

    @classmethod
    def write(cls, directory: Path, project_id: str, repo: RepoBinding) -> "ProjectBinding":
        path = directory / PROJECT_CONFIG_NAME
        _write_json(
            path,
            {"project_id": project_id, "repo": repo.to_dict()},
        )
        return cls(project_id=project_id, repo=repo, config_path=path)


@dataclass
class Config:
    """Resolved runtime configuration for the harumi client/CLI."""

    api_url: str
    git_url: str = DEFAULT_GIT_URL
    org_id: Optional[str] = None

    @classmethod
    def load(
        cls,
        api_url: Optional[str] = None,
        git_url: Optional[str] = None,
        org_id: Optional[str] = None,
    ) -> "Config":
        file_config = _read_json(CONFIG_PATH)

        resolved_api_url = (
            api_url
            or os.environ.get("HARUMI_API_URL")
            or file_config.get("api_url")
            or DEFAULT_API_URL
        )
        resolved_git_url = (
            git_url
            or os.environ.get("HARUMI_GIT_URL")
            or file_config.get("git_url")
            or DEFAULT_GIT_URL
        )
        resolved_org_id = (
            org_id
            or os.environ.get("HARUMI_ORG")
            or file_config.get("org_id")
        )
        return cls(
            api_url=resolved_api_url.rstrip("/"),
            git_url=resolved_git_url.rstrip("/"),
            org_id=resolved_org_id,
        )

    def save_org_id(self, org_id: str) -> None:
        """Persist the resolved org id so future invocations don't need it."""
        file_config = _read_json(CONFIG_PATH)
        file_config["org_id"] = org_id
        _write_json(CONFIG_PATH, file_config)
        self.org_id = org_id


def load_credentials() -> Optional[dict[str, Any]]:
    data = _read_json(CREDENTIALS_PATH)
    return data or None


def save_credentials(
    access_token: str,
    refresh_token: str,
    git_token: Optional[str] = None,
    **extra: Any,
) -> None:
    data: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        **extra,
    }
    if git_token is not None:
        data["git_token"] = git_token
    _write_json(CREDENTIALS_PATH, data)


def save_git_token(git_token: str) -> None:
    """Persist a Gitea personal access token for the current user."""
    creds = _read_json(CREDENTIALS_PATH)
    creds["git_token"] = git_token
    _write_json(CREDENTIALS_PATH, creds)


def load_git_token() -> Optional[str]:
    creds = _read_json(CREDENTIALS_PATH)
    return creds.get("git_token") or None


def clear_credentials() -> None:
    if CREDENTIALS_PATH.exists():
        CREDENTIALS_PATH.unlink()
