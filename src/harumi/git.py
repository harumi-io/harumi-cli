"""Git helpers for harumi-dev-cli.

All operations shell out to the system `git` binary via subprocess — no
extra Python dependency.  Most functions here assume the caller is inside a
git working tree that is already bound to a Harumi project (i.e. `harumi
link` — or `harumi new`/`harumi clone`, which bind as part of creating the
checkout — has been run).  Any function that requires a bound repo raises
`NotAHarumiRepoError` with a clear next-step message when the precondition
is not met.

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
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Optional, Sequence
from urllib.parse import quote

from harumi.errors import HarumiError


class NotAHarumiRepoError(HarumiError):
    """Raised when a git operation is attempted outside a directory `harumi`
    has bound to a project."""

    def __init__(self) -> None:
        super().__init__(
            "No Harumi project found in this directory (or any parent). "
            "Run `harumi new` to start one, `harumi clone <PROJECT_ID>` to fetch "
            "an existing one, or `harumi link --project <PROJECT_ID>` if this "
            "directory already has the project's code."
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
    redact: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git sub-command, returning the CompletedProcess on success.

    `redact`, when given, is scrubbed from the command/stderr embedded in a
    raised `GitError` — a token-bearing authenticated URL in `args` must
    never end up verbatim in a message that gets printed to the terminal or
    logged. Callers pass the raw token, but `args` may actually embed its
    percent-encoded form (see `_authenticated_url`), so both forms are
    scrubbed — otherwise a token with URL-reserved characters (`+`, `/`,
    `=`, ...) would survive redaction in its (trivially reversible)
    encoded form.
    """
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
        command = " ".join(args)
        stderr = exc.stderr
        if redact:
            for needle in {redact, quote(redact, safe="")}:
                command = command.replace(needle, "***")
                stderr = stderr.replace(needle, "***")
        raise GitError(command, stderr) from exc
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

    Both `username` and `token` are percent-encoded: an unescaped `@`, `:`,
    or `/` in either would re-split the URL's authority section (e.g. an
    email-like username `user@harumi.io` yields
    `https://user@harumi.io:token@host/...`, whose authority becomes
    `harumi.io:token@host` — `:token` is then parsed as a bogus port,
    producing "Port number was not a decimal number").
    """
    if not clone_url.startswith("https://"):
        return clone_url
    host_and_path = clone_url[len("https://"):]
    # Strip any existing credentials to avoid doubling them.
    host_and_path = re.sub(r"^[^@]+@", "", host_and_path)
    safe_username = quote(username, safe="")
    safe_token = quote(token, safe="")
    return f"https://{safe_username}:{safe_token}@{host_and_path}"


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
        _run(["remote", "set-url", name, authed_url], cwd=cwd, redact=token)
    else:
        _run(["remote", "add", name, authed_url], cwd=cwd, redact=token)


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
# Clone: fetch an existing project's repo into a fresh local directory
# ---------------------------------------------------------------------------

def clone_repo(
    clone_url: str,
    username: str,
    token: str,
    dest: Path,
    remote: str = "harumi",
) -> None:
    """Clone `clone_url` into `dest` (must not already exist), authenticated
    with `username`/`token`, and name the resulting remote `harumi`.

    Used by `harumi clone`/`harumi new` to fetch a project's existing repo
    (which always has at least a scaffold commit — see `harumi-api`'s
    `provision_from_blueprint`/`_seed_repo_files`) rather than trying to push
    into it, which would fail as a non-fast-forward push against history the
    caller doesn't have.
    """
    authed_url = _authenticated_url(clone_url, username, token)
    _run(
        ["clone", "--origin", remote, authed_url, str(dest)],
        redact=token,
    )



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


# ---------------------------------------------------------------------------
# Import: push a plain folder as the initial commit of a project's repo
# ---------------------------------------------------------------------------

# What `harumi import` leaves out. Directories nobody wants uploaded, and files that
# look like credentials. They are excluded through the repo's *local*
# `.git/info/exclude`, never by editing the user's own `.gitignore`.
JUNK_DIRS = (
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ipynb_checkpoints",
)
SECRET_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
)
# Look like a secret by name but are meant to be shared (a template, not a value).
SECRET_OK = (".env.example", ".env.sample", ".env.template", ".env.dist")
# Excluded without comment: noise, not a decision.
_QUIET_EXCLUDES = ("*.pyc", ".DS_Store")
LARGE_FILE_BYTES = 50 * 1024 * 1024
_MARKER = "# harumi import (local; not part of your project)"


def import_excludes() -> list[str]:
    """The gitignore-syntax patterns `harumi import` adds, in order."""
    patterns = [f"{d}/" for d in JUNK_DIRS] + list(SECRET_GLOBS)
    patterns += [f"!{name}" for name in SECRET_OK]
    return patterns + list(_QUIET_EXCLUDES)


def looks_like_secret(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in SECRET_OK:
        return False
    return any(fnmatch(name, g) for g in SECRET_GLOBS)


@dataclass
class ImportPlan:
    """What an import would upload and what it would leave out."""

    files: int = 0
    total_bytes: int = 0
    largest: list[tuple[str, int]] = field(default_factory=list)  # biggest first
    skipped_secrets: list[str] = field(default_factory=list)
    skipped_dirs: list[tuple[str, int]] = field(default_factory=list)  # (dir, file count)
    # Look like secrets but git already tracks them, so they ARE included.
    included_secrets: list[str] = field(default_factory=list)
    # Commits already in the folder's git history; the push sends all of them.
    history_commits: int = 0


def _is_repo_root(folder: Path) -> bool:
    """Whether `folder` is itself the top of a git repo (not merely inside one).

    Compares resolved paths: git reports the real location, so a symlinked path
    (macOS's /tmp is one) would otherwise never match.
    """
    root = repo_root(cwd=folder)
    return root is not None and root.resolve() == folder.resolve()


def _ls_files(folder: Path, extra_excludes: Sequence[str]) -> tuple[list[str], list[str]]:
    """What `git add --all` would stage in `folder`: (already tracked, new).

    Tracked files are always staged: an exclude pattern keeps a *new* file out, it
    cannot un-track one git already has. So they are reported separately and
    never subject to `extra_excludes`. The new files honour the user's own ignore
    files plus `extra_excludes`.

    Works whether or not `folder` is already a repo, without creating anything in
    it: a folder that is not a repo is listed through a throwaway git dir with
    `folder` as its work tree (the same thing `push_folder` will do for real).
    """
    new_args = ["ls-files", "-z", "--others", "--exclude-standard"]
    new_args += [f"--exclude={p}" for p in extra_excludes]
    if _is_repo_root(folder):
        tracked = _run(["ls-files", "-z", "--cached"], cwd=folder).stdout
        new = _run(new_args, cwd=folder).stdout
    else:
        tracked = ""
        with tempfile.TemporaryDirectory() as scratch:
            _run(["init", "-q", "--bare", scratch])
            new = _run(
                new_args, cwd=folder, env={"GIT_DIR": scratch, "GIT_WORK_TREE": str(folder)}
            ).stdout
    split = lambda out: sorted({p for p in out.split("\0") if p})  # noqa: E731
    return split(tracked), split(new)


def history_size(folder: Path) -> int:
    """How many commits of history already exist in `folder` (0 if it is not a repo or has none)."""
    if not _is_repo_root(folder):
        return 0
    result = _run(["rev-list", "--count", "HEAD"], cwd=folder, check=False)
    try:
        return int(result.stdout.strip()) if result.returncode == 0 else 0
    except ValueError:
        return 0


def plan_import(folder: Path) -> ImportPlan:
    """Work out, without touching `folder`, what `harumi import` will upload."""
    tracked, new_kept = _ls_files(folder, import_excludes())
    _, new_all = _ls_files(folder, [])
    kept = sorted(set(tracked) | set(new_kept))

    plan = ImportPlan(history_commits=history_size(folder))
    sizes: list[tuple[str, int]] = []
    for rel in kept:
        try:
            size = (folder / rel).stat().st_size
        except OSError:
            continue  # vanished or a dangling symlink: git will cope, we just don't count it
        sizes.append((rel, size))
        plan.total_bytes += size
        if looks_like_secret(rel) and rel in tracked:
            # Not skippable from here: git already has it, so `add --all` keeps it.
            plan.included_secrets.append(rel)
    plan.files = len(sizes)
    plan.largest = sorted(sizes, key=lambda x: x[1], reverse=True)[:3]

    dirs: dict[str, int] = {}
    for rel in sorted(set(new_all) - set(new_kept)):
        parts = rel.split("/")
        junk = next((i for i, part in enumerate(parts[:-1]) if part in JUNK_DIRS), None)
        if junk is not None:
            key = "/".join(parts[: junk + 1])
            dirs[key] = dirs.get(key, 0) + 1
        elif looks_like_secret(rel):
            plan.skipped_secrets.append(rel)
        # anything else was skipped quietly (*.pyc, .DS_Store)
    plan.skipped_dirs = sorted(dirs.items())
    return plan


def _write_local_excludes(folder: Path, patterns: Sequence[str]) -> None:
    """Add `patterns` to the repo's local exclude file, once, under a marker."""
    result = _run(["rev-parse", "--git-path", "info/exclude"], cwd=folder)
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = folder / path
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    if _MARKER in existing:
        return
    block = "\n".join([_MARKER, *patterns, ""])
    path.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + block)


def push_folder(
    folder: Path,
    clone_url: str,
    username: str,
    token: str,
    branch: str = "main",
    remote: str = "harumi",
    message: str = "Import project",
    exclude: Sequence[str] = (),
) -> str:
    """Commit everything in ``folder`` and push it to ``remote/branch``.

    Used by ``harumi import`` to seed a freshly-provisioned project repo from a
    local folder. Initializes a git repo in-place if the folder is
    not already one, force-updates the target branch to a single commit of the
    current tree, and pushes. Returns the commit SHA.

    Force-push is intentional: the provisioned repo only contains a scaffold
    commit, and the exported folder is the authoritative content. ponytail:
    overwrites the target branch — fine for import (fresh repo), not a general
    push primitive.
    """
    if not folder.is_dir():
        raise HarumiError(f"Not a directory: {folder}")

    # Initialize a repo in the folder if it isn't one already. If the folder is
    # a clone of the user's GitHub repo, it already is one — reuse it.
    if not _is_repo_root(folder):
        _run(["init"], cwd=folder)

    ensure_remote(clone_url, username, token, name=remote, cwd=folder)

    if exclude:
        _write_local_excludes(folder, exclude)
    _run(["add", "--all"], cwd=folder)
    # Commit; allow an empty diff to still produce a commit so push has a ref.
    _run(
        ["-c", "user.email=cli@harumi.io", "-c", "user.name=harumi-cli",
         "commit", "--allow-empty", "-m", message],
        cwd=folder,
    )
    sha = head_sha(cwd=folder)
    _run(["push", "--force", remote, f"HEAD:refs/heads/{branch}"], cwd=folder, redact=token)
    return sha
