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
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class Environment:
    """A named Harumi backend environment (its api + git endpoints)."""

    name: str
    api_url: str
    git_url: str
    # Where the Harumi web app lives for this environment — used to print
    # user-facing project links. Never print `git_url` directly to the user;
    # Gitea is an implementation detail, not something we surface.
    platform_url: str
    # Internal envs are VPN-only and hidden from `harumi env list` unless the
    # user opts in (HARUMI_INTERNAL=1 or `--all`). This is UX only — real access
    # is gated by needing an account in that environment's Supabase + the VPN.
    internal: bool = False
    description: str = ""


# Built-in environments. Auth always flows through harumi-api (/users/otp,
# /users/refresh), so an environment is fully defined by its api + git URLs;
# each harumi-api is wired to its own Supabase.
ENVIRONMENTS: dict[str, Environment] = {
    "production": Environment(
        name="production",
        api_url="https://api.harumi.io/api",
        git_url="https://git.harumi.io",
        platform_url="https://platform.harumi.io",
        internal=False,
        description="Public production environment.",
    ),
    "staging": Environment(
        name="staging",
        api_url="https://api.dev.harumi.io/api",
        git_url="https://git.dev.harumi.io",
        platform_url="https://platform.dev.harumi.io",
        internal=True,
        description="Internal staging/dev environment (VPN-only).",
    ),
}
DEFAULT_ENVIRONMENT = "production"

HARUMI_HOME = Path(os.environ.get("HARUMI_HOME", Path.home() / ".harumi"))
# Global (cross-environment) config: only stores the selected environment name.
CONFIG_PATH = HARUMI_HOME / "config.json"
# Legacy flat credential/config files (pre-environments). Kept for one-time
# migration into the per-environment layout below.
CREDENTIALS_PATH = HARUMI_HOME / "credentials.json"

# Name of the per-project binding file, searched upward from cwd.
PROJECT_CONFIG_NAME = ".harumi/config.json"

# Active environment for this process. Set by Config.load() (which every CLI
# command funnels through); credential/env-config helpers resolve against it.
# ponytail: process-global mutable state — fine for a single-shot CLI process,
# and it keeps credential helpers callable without threading env everywhere.
_ACTIVE_ENV: Optional[str] = None


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


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------

def resolve_environment(explicit: Optional[str] = None) -> str:
    """Resolve the active environment name.

    Precedence: explicit arg > HARUMI_ENV env var > global config.json
    `environment` > DEFAULT_ENVIRONMENT. Raises ValueError on an unknown name.
    """
    name = (
        explicit
        or os.environ.get("HARUMI_ENV")
        or _read_json(CONFIG_PATH).get("environment")
        or DEFAULT_ENVIRONMENT
    )
    if name not in ENVIRONMENTS:
        known = ", ".join(sorted(ENVIRONMENTS))
        raise ValueError(f"Unknown environment {name!r}. Known environments: {known}.")
    return name


def set_active_environment(name: str) -> None:
    global _ACTIVE_ENV
    _ACTIVE_ENV = name


def active_environment() -> str:
    return _ACTIVE_ENV or resolve_environment()


def active_platform_url() -> str:
    """Harumi web app URL for the active environment (honors the same
    HARUMI_PLATFORM_URL / env config.json override as Config.load)."""
    name = active_environment()
    env_config = _read_json(env_config_path(name))
    url = (
        os.environ.get("HARUMI_PLATFORM_URL")
        or env_config.get("platform_url")
        or ENVIRONMENTS[name].platform_url
    )
    return url.rstrip("/")


def save_environment(name: str) -> None:
    """Persist the selected environment in the global config.json."""
    if name not in ENVIRONMENTS:
        known = ", ".join(sorted(ENVIRONMENTS))
        raise ValueError(f"Unknown environment {name!r}. Known environments: {known}.")
    global_config = _read_json(CONFIG_PATH)
    global_config["environment"] = name
    _write_json(CONFIG_PATH, global_config)


def _env_dir(name: Optional[str] = None) -> Path:
    return HARUMI_HOME / "environments" / (name or active_environment())


def credentials_path(name: Optional[str] = None) -> Path:
    return _env_dir(name) / "credentials.json"


def env_config_path(name: Optional[str] = None) -> Path:
    return _env_dir(name) / "config.json"


def _migrate_legacy_credentials(name: str) -> None:
    """One-time migration of the pre-environments flat files into `name`'s dir.

    Existing installs stored credentials at ~/.harumi/credentials.json and
    org_id/api_url in ~/.harumi/config.json, all pointed at production. Move
    them into the default environment so upgrading users stay logged in.
    """
    if name != DEFAULT_ENVIRONMENT:
        return
    new_creds = credentials_path(name)
    if new_creds.exists() or not CREDENTIALS_PATH.exists():
        return

    new_creds.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(CREDENTIALS_PATH), str(new_creds))
    try:
        new_creds.chmod(0o600)
    except OSError:
        pass

    # Carry over any legacy org_id from the old global config into the env config.
    legacy_global = _read_json(CONFIG_PATH)
    legacy_org = legacy_global.get("org_id")
    if legacy_org:
        env_cfg = _read_json(env_config_path(name))
        env_cfg.setdefault("org_id", legacy_org)
        _write_json(env_config_path(name), env_cfg)
    # Drop stale keys from the global config; it only tracks `environment` now.
    for stale in ("org_id", "api_url", "git_url"):
        legacy_global.pop(stale, None)
    _write_json(CONFIG_PATH, legacy_global)


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
    git_url: str
    platform_url: str
    org_id: Optional[str] = None
    environment: str = DEFAULT_ENVIRONMENT

    @classmethod
    def load(
        cls,
        api_url: Optional[str] = None,
        git_url: Optional[str] = None,
        org_id: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> "Config":
        env_name = resolve_environment(environment)
        set_active_environment(env_name)
        _migrate_legacy_credentials(env_name)

        env = ENVIRONMENTS[env_name]
        env_config = _read_json(env_config_path(env_name))

        resolved_api_url = (
            api_url
            or os.environ.get("HARUMI_API_URL")
            or env_config.get("api_url")
            or env.api_url
        )
        resolved_git_url = (
            git_url
            or os.environ.get("HARUMI_GIT_URL")
            or env_config.get("git_url")
            or env.git_url
        )
        resolved_org_id = (
            org_id
            or os.environ.get("HARUMI_ORG")
            or env_config.get("org_id")
        )
        return cls(
            api_url=resolved_api_url.rstrip("/"),
            git_url=resolved_git_url.rstrip("/"),
            platform_url=active_platform_url(),
            org_id=resolved_org_id,
            environment=env_name,
        )

    def save_org_id(self, org_id: str) -> None:
        """Persist the resolved org id (scoped to this environment) so future
        invocations don't need it."""
        path = env_config_path(self.environment)
        env_config = _read_json(path)
        env_config["org_id"] = org_id
        _write_json(path, env_config)
        self.org_id = org_id


def load_credentials() -> Optional[dict[str, Any]]:
    data = _read_json(credentials_path())
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
    _write_json(credentials_path(), data)


def save_git_token(git_token: str, git_url: Optional[str] = None, username: Optional[str] = None) -> None:
    """Persist a Gitea personal access token (and its git_url/username) for the
    current user, scoped to the active environment.

    `username` is the real Gitea account name (e.g. `u-<user_id>`) — the git
    credential username. Callers must never substitute the Harumi account
    email here: emails contain `@`, which breaks unescaped basic-auth URLs
    (`https://user:token@host/...`) by introducing a second `@`.
    """
    path = credentials_path()
    creds = _read_json(path)
    creds["git_token"] = git_token
    if git_url:
        creds["git_url"] = git_url
    if username:
        creds["git_username"] = username
    _write_json(path, creds)


def load_git_token() -> Optional[str]:
    creds = _read_json(credentials_path())
    return creds.get("git_token") or None


def load_git_username() -> Optional[str]:
    """Return the persisted Gitea username, or None if not yet provisioned
    (e.g. a token saved by a CLI version predating this field)."""
    creds = _read_json(credentials_path())
    return creds.get("git_username") or None


def clear_credentials() -> None:
    path = credentials_path()
    if path.exists():
        path.unlink()
