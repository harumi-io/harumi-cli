"""Configuration: base URL, credential/config file paths, org resolution.

Precedence for every setting: explicit constructor arg > environment
variable > ~/.harumi/config.json > hardcoded default.

Project binding (.harumi/config.json in the working directory) stores the
project_id and Gitea repo metadata written by `harumi new`/`harumi import`/
`harumi clone`/`harumi link`. It is searched from the cwd upward so it works
from subdirectories of the project.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

try:  # POSIX
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]


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
    # Public status page (Gatus) for this environment, linked from network-error
    # hints and read by `harumi status`.
    status_url: str
    # Internal envs are VPN-only, and `harumi login` only offers the choice to
    # people with a Harumi email (see `is_internal_email`). This is UX only —
    # real access is gated by needing an account in that environment's Supabase
    # plus the VPN.
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
        platform_url="https://app.harumi.io",
        status_url="https://status.harumi.io",
        internal=False,
        description="Public production environment.",
    ),
    "staging": Environment(
        name="staging",
        api_url="https://api.dev.harumi.io/api",
        git_url="https://git.dev.harumi.io",
        platform_url="https://app.dev.harumi.io",
        status_url="https://status.dev.harumi.io",
        internal=True,
        description="Internal staging/dev environment (VPN-only).",
    ),
}
DEFAULT_ENVIRONMENT = "production"

# Staff sign in with a Harumi address; clients never see an environment choice.
# A convenience for whom to ask, not a security boundary.
INTERNAL_EMAIL_DOMAIN = "harumi.io"


def is_internal_email(email: str) -> bool:
    """Whether `email` is a Harumi staff address (exactly `@harumi.io`)."""
    local, at, domain = email.strip().rpartition("@")
    return bool(local) and bool(at) and domain.lower() == INTERNAL_EMAIL_DOMAIN

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

# The environment one `Client` is working in, for the duration of one of its calls. The global above is "the
# last Config loaded", which is right for a CLI that runs one command and wrong for a library: a second `Client`
# for another environment replaced it, and the first then read (and refreshed) the second's login. A context
# variable is also per thread, so clients on different threads do not see each other's.
_ENV_SCOPE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("harumi_environment_scope", default=None)


@contextlib.contextmanager
def environment_scope(name: str) -> Iterator[None]:
    """Resolve credentials and settings against `name` inside the block, whatever the process-wide
    active environment is. Nests, and restores on the way out even if the block raises."""
    token = _ENV_SCOPE.set(name)
    try:
        yield
    finally:
        _ENV_SCOPE.reset(token)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write `data` to `path` so a reader (or a crash) never sees half a file.

    Writes to a temporary file in the same directory, then renames it over the
    target, which is atomic. `mkstemp` creates the file owner-only (0600) from the
    start, so the secrets inside are never readable by others, not even briefly.
    """
    # Write to what a symlink points at, so a credentials file kept in a dotfiles repo
    # and linked into ~/.harumi stays a link instead of being replaced by a copy.
    path = path.resolve() if path.is_symlink() else path
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, default=str)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        # Best-effort on platforms without POSIX permission bits (e.g. some Windows filesystems).
        path.chmod(0o600)


# One lock for the credentials file, shared by every harumi process on the machine.
# A refresh token can be used once, so two processes that refresh at the same moment
# would log each other out; this makes them take turns.
_lock_state = threading.local()
LOCK_TIMEOUT_SECONDS = 30.0


def _lock_file(path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return open(path, "a+b")
    except OSError:
        return None


def _try_lock(handle, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                return False
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def _unlock(handle) -> None:
    with contextlib.suppress(OSError):
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def credentials_lock(timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold the credentials lock. Re-entrant within a thread.

    If the lock cannot be taken (read-only home, an unsupported platform, or a wait
    longer than `timeout`) this carries on without it rather than hanging: the
    lock removes a race, it is not worth refusing to work for.
    """
    depth = getattr(_lock_state, "depth", 0)
    if depth:
        _lock_state.depth = depth + 1
        try:
            yield
        finally:
            _lock_state.depth -= 1
        return

    handle = _lock_file(credentials_path().with_name("credentials.json.lock"))
    acquired = handle is not None and _try_lock(handle, timeout)
    _lock_state.depth = 1
    try:
        yield
    finally:
        _lock_state.depth = 0
        if handle is not None:
            if acquired:
                _unlock(handle)
            handle.close()


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
    # `isinstance` first: a hand-edited non-string in config.json (a list is unhashable)
    # would otherwise raise a TypeError from the `in` test instead of this message.
    if not isinstance(name, str) or name not in ENVIRONMENTS:
        known = ", ".join(sorted(ENVIRONMENTS))
        raise ValueError(f"Unknown environment {name!r}. Known environments: {known}.")
    return name


# The API URL the most recent `Config.load` resolved, *with the environment it was resolved
# for* -- including a one-off `--api-url` flag, which never touches the environment or
# config.json. The unreachable-API hint reads it to tell a stock stack from a local one.
# Keyed to the environment so switching environments can't judge one env's URL against
# another's stock URL.
_RESOLVED_API_URL: Optional[tuple[str, str]] = None


def set_active_environment(name: str) -> None:
    global _ACTIVE_ENV
    _ACTIVE_ENV = name


def active_environment() -> str:
    return _ENV_SCOPE.get() or _ACTIVE_ENV or resolve_environment()


def active_platform_url() -> str:
    """Harumi web app URL for the active environment (honors the same
    HARUMI_PLATFORM_URL / env config.json override as Config.load)."""
    name = active_environment()
    env_config = _read_json(env_config_path(name))
    url = (
        os.environ.get("HARUMI_PLATFORM_URL")
        or _str_setting(env_config, "platform_url")
        or ENVIRONMENTS[name].platform_url
    )
    return url.rstrip("/")


def _str_setting(env_config: dict[str, Any], key: str) -> Optional[str]:
    """A string from an env config.json, or `None` for anything else -- a hand-edited
    `"status_url": 5` should fall back to the default, not crash the error path."""
    value = env_config.get(key)
    return value if isinstance(value, str) and value else None


def active_status_url() -> str:
    """Status page URL for the active environment (honors the same
    HARUMI_STATUS_URL / env config.json `status_url` override as
    `active_platform_url`)."""
    name = active_environment()
    env_config = _read_json(env_config_path(name))
    url = (
        os.environ.get("HARUMI_STATUS_URL")
        or _str_setting(env_config, "status_url")
        or ENVIRONMENTS[name].status_url
    )
    return url.rstrip("/")


def status_hint_url() -> Optional[str]:
    """The status page to point an unreachable-API error at, or `None`.

    The stock status page says nothing about a self-hosted or local stack, so when the API
    URL in use is not the environment's own (`--api-url`, HARUMI_API_URL, or `api_url` in
    config.json) the hint is only shown if a `status_url` override says where that stack's
    status lives.
    """
    name = active_environment()
    env_config = _read_json(env_config_path(name))
    resolved = _RESOLVED_API_URL[1] if _RESOLVED_API_URL and _RESOLVED_API_URL[0] == name else None
    api_in_use = resolved or os.environ.get("HARUMI_API_URL") or _str_setting(env_config, "api_url")
    api_is_stock = not api_in_use or api_in_use.rstrip("/") == ENVIRONMENTS[name].api_url.rstrip("/")
    status_override = os.environ.get("HARUMI_STATUS_URL") or _str_setting(env_config, "status_url")
    return active_status_url() if (api_is_stock or status_override) else None


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
            or _str_setting(env_config, "api_url")
            or env.api_url
        )
        global _RESOLVED_API_URL
        _RESOLVED_API_URL = (env_name, resolved_api_url.rstrip("/"))
        resolved_git_url = (
            git_url
            or os.environ.get("HARUMI_GIT_URL")
            or _str_setting(env_config, "git_url")
            or env.git_url
        )
        resolved_org_id = (
            org_id
            or os.environ.get("HARUMI_ORG")
            or _str_setting(env_config, "org_id")
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

    def clear_org_id(self) -> None:
        """Forget the saved organization, so requests go to the personal workspace."""
        path = env_config_path(self.environment)
        env_config = _read_json(path)
        if "org_id" in env_config:
            del env_config["org_id"]
            _write_json(path, env_config)
        self.org_id = None


def load_credentials() -> Optional[dict[str, Any]]:
    data = _read_json(credentials_path())
    return data or None


def save_credentials(
    access_token: str,
    refresh_token: str,
    git_token: Optional[str] = None,
    *,
    replace: bool = False,
    **extra: Any,
) -> None:
    """Store the session.

    By default only the session keys are updated and everything else in the file
    (the git token, its URL and username) is kept, so a token refresh does not
    log the user out of git. A fresh login passes `replace=True` so nothing from
    a previous account survives.
    """
    with credentials_lock():
        data: dict[str, Any] = {} if replace else _read_json(credentials_path())
        data.update({"access_token": access_token, "refresh_token": refresh_token, **extra})
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
    with credentials_lock():
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
