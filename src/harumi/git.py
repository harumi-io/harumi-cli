"""Git helpers for harumi-dev-cli.

All operations shell out to the system `git` binary via subprocess — no
extra Python dependency.  The functions here assume the caller is inside a
git working tree that is already bound to a Harumi project (i.e. `harumi
init` has been run).  Any function that requires a bound repo raises
`NotAHarumiRepoError` with a clear "run harumi init" message when the
precondition is not met.

The scratch-branch flow (used by `harumi run` on an un-pushed/dirty tree):

1. `push_scratch(git_url, token, username)` creates a throwaway branch off
   the current HEAD, stages the *entire* working tree (tracked + untracked,
   obeying .gitignore) into a temporary index without touching the user's
   real index or branch, commits it, pushes to the `harumi` remote, then
   returns `(branch_name, commit_sha)`.
2. The caller runs the job.
3. `delete_remote_scratch(branch)` deletes the remote ref when the run
   finishes (best-effort: failure is logged, not raised).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from harumi.errors import HarumiError


class NotAHarumiRepoError(HarumiError):
    """Raised when a git operation is attempted outside a harumi-init'd repo."""

    def __init__(self) -> None:
        super().__init__(
            "No Harumi project found in this directory (or any parent). "
            "Run `harumi init --project <PROJECT_ID>` first."
        )


class GitError(HarumiError):
    """Raised when a git subprocess call fails."""

    def __init__(self, command: str, stderr: str) -> None:
        self.command = command
        self.stderr = stderr
        super().__init__(f"git {command} failed: {stderr.strip()}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(
    args: list[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git sub-command, returning the CompletedProcess on success."""
    full_args = ["git"] + args
    merged_env = {**os.environ, **(env or {})}
    try:
        return subprocess.run(
            full_args,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            capture_output=True,
            text=True,
            check=check,
        )
    except subprocess.CalledProcessError as exc:
        raise GitError(" ".join(args), exc.stderr) from exc
    except FileNotFoundError:
        raise HarumiError(
            "git not found. Install git and make sure it is on your PATH."
        )


# ---------------------------------------------------------------------------
# Repo state queries
# ---------------------------------------------------------------------------

def repo_root(cwd: Optional[Path] = None) -> Optional[Path]:
    """Return the absolute path of the git repo root, or None if not inside one."""
    result = _run(
        ["rev-parse", "--show-toplevel"],
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def is_dirty(cwd: Optional[Path] = None) -> bool:
    """True when the working tree has uncommitted changes (tracked or untracked,
    excluding files that match .gitignore)."""
    tracked = _run(["status", "--porcelain"], cwd=cwd).stdout.strip()
    return bool(tracked)


def has_unpushed_commits(remote: str = "harumi", cwd: Optional[Path] = None) -> bool:
    """True when HEAD has commits not yet on `remote/<current-branch>`."""
    branch = current_branch(cwd=cwd)
    if branch is None:
        return True
    result = _run(
        ["rev-list", "--count", f"{remote}/{branch}..HEAD"],
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        # Remote branch doesn't exist yet → everything is unpushed.
        return True
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return True


def current_branch(cwd: Optional[Path] = None) -> Optional[str]:
    """Return the current branch name, or None if in detached HEAD state."""
    result = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, check=False)
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return None if name == "HEAD" else name


def head_sha(cwd: Optional[Path] = None) -> str:
    """Return the full SHA of the current HEAD commit."""
    return _run(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()


# ---------------------------------------------------------------------------
# Remote management
# ---------------------------------------------------------------------------

def _authenticated_url(clone_url: str, username: str, token: str) -> str:
    """Embed basic-auth credentials into a https:// clone URL.

    Gitea expects `https://<user>:<token>@<host>/...` for git-over-HTTPS
    with token auth.  We never embed credentials in the stored clone_url —
    only in the ephemeral URL passed to individual git operations.
    """
    if not clone_url.startswith("https://"):
        return clone_url
    host_and_path = clone_url[len("https://"):]
    # Strip any existing credentials to avoid doubling them.
    host_and_path = re.sub(r"^[^@]+@", "", host_and_path)
    return f"https://{username}:{token}@{host_and_path}"


def ensure_remote(
    clone_url: str,
    username: str,
    token: str,
    name: str = "harumi",
    cwd: Optional[Path] = None,
) -> None:
    """Add or update the `harumi` remote to use an authenticated HTTPS URL.

    The URL with embedded credentials is set only for the local repo config;
    it is never stored in the binding file so the token doesn't leak into
    committed config.
    """
    authed_url = _authenticated_url(clone_url, username, token)

    result = _run(["remote", "get-url", name], cwd=cwd, check=False)
    if result.returncode == 0:
        _run(["remote", "set-url", name, authed_url], cwd=cwd)
    else:
        _run(["remote", "add", name, authed_url], cwd=cwd)


def refresh_remote_token(
    clone_url: str,
    username: str,
    token: str,
    name: str = "harumi",
    cwd: Optional[Path] = None,
) -> None:
    """Re-embed a fresh token into an existing remote URL (e.g. after token rotation)."""
    ensure_remote(clone_url, username, token, name=name, cwd=cwd)


# ---------------------------------------------------------------------------
# Scratch-branch push
# ---------------------------------------------------------------------------

def push_scratch(
    username: str,
    token: str,
    remote: str = "harumi",
    message: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> tuple[str, str]:
    """Commit the entire working tree to a throwaway branch and push it.

    Uses a temporary GIT_INDEX_FILE so the user's real staging area and
    current branch are completely untouched.  Returns ``(branch_name, sha)``.

    The branch is named ``harumi-scratch/<username>/<yyyymmdd-HHMMSS>`` so
    all scratch refs are easily grouped and pruned server-side.

    Untracked files that are *not* in .gitignore are included (via
    ``git add --all``).  Ignored files are never included.
    """
    root = repo_root(cwd=cwd)
    if root is None:
        raise NotAHarumiRepoError()

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_user = re.sub(r"[^a-zA-Z0-9._-]", "-", username)
    branch = f"harumi-scratch/{safe_user}/{ts}"

    commit_msg = message or f"harumi scratch run {ts}"

    with tempfile.NamedTemporaryFile(suffix=".harumi-index", delete=False) as tmp:
        tmp_index = tmp.name

    try:
        env = {"GIT_INDEX_FILE": tmp_index}

        # Copy the real index into our scratch index as a starting point so
        # tracked files already staged are included at their latest state.
        _run(["read-tree", "HEAD"], cwd=root, env=env)

        # Add everything in the working tree (respects .gitignore, adds
        # untracked non-ignored files).
        _run(["add", "--all"], cwd=root, env=env)

        # Write the tree object from the scratch index.
        tree_sha = _run(["write-tree"], cwd=root, env=env).stdout.strip()

        # Create the commit object, parented on HEAD.
        parent_sha = head_sha(cwd=root)
        commit_sha = _run(
            [
                "commit-tree",
                tree_sha,
                "-p", parent_sha,
                "-m", commit_msg,
            ],
            cwd=root,
            env=env,
        ).stdout.strip()

        # Push directly to the remote ref without touching a local branch.
        _run(
            ["push", remote, f"{commit_sha}:refs/heads/{branch}"],
            cwd=root,
        )

    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass

    return branch, commit_sha


def delete_remote_scratch(
    branch: str,
    remote: str = "harumi",
    cwd: Optional[Path] = None,
) -> None:
    """Delete the scratch branch from the remote (best-effort; never raises)."""
    try:
        _run(["push", remote, f":refs/heads/{branch}"], cwd=cwd, check=False)
    except Exception:
        pass
