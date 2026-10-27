"""The git token must not be written into a project's `.git/config` (issue #126).

`harumi clone`, `new`, `link` and `import` used to set the `harumi` remote to `https://user:token@host/...`. Anyone who
could read the folder, or a screen share of `git remote -v`, had a token with access to the user's repositories. The remote
is now the plain URL and git asks `harumi git-credential` for the password. These tests use real git.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from harumi import config, git as hgit

CLONE_URL = "https://git.harumi.io/u-abc/repo.git"
TOKEN = "SECRET-TOKEN-0123"
USER = "u-abc"


def git(*args, cwd, env=None, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True, check=check
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway HARUMI_HOME with a logged-in production user, and a git that ignores the real machine's config."""
    harumi_home = tmp_path / "harumi-home"
    monkeypatch.setattr("harumi.config.HARUMI_HOME", harumi_home)
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    path = config.credentials_path("production")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"git_token": TOKEN, "git_username": USER}))
    # The user's own git setup: a global credential helper that would answer with an old token.
    gitconfig = tmp_path / "gitconfig"
    stale = tmp_path / "stale-helper.sh"
    stale.write_text('#!/bin/sh\ncase "$1" in get) echo username=old-user; echo password=STALE-TOKEN;; store) cat >> "$0.stored";; esac\n')
    stale.chmod(stale.stat().st_mode | stat.S_IEXEC)
    gitconfig.write_text(f"[credential]\n\thelper = {stale}\n[user]\n\temail = t@example.com\n\tname = t\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HARUMI_HOME", str(harumi_home))
    return harumi_home


@pytest.fixture
def harumi_on_path(tmp_path, monkeypatch):
    """A real `harumi` executable on PATH, as installed, so git can run `harumi git-credential`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "harumi"
    src = Path(__file__).resolve().parent.parent / "src"
    script.write_text(
        f'#!/bin/sh\nPYTHONPATH="{src}" exec "{sys.executable}" -c "from harumi.cli import main; main()" "$@"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return script


@pytest.fixture
def repo(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    git("init", "-q", cwd=folder)
    return folder


def config_text(folder):
    return (folder / ".git" / "config").read_text()


def fill(folder, host="git.harumi.io"):
    request = f"protocol=https\nhost={host}\n\n"
    return subprocess.run(["git", "credential", "fill"], cwd=folder, input=request, capture_output=True, text=True)


# -- the remote -----------------------------------------------------------------------------------------------------


def test_the_remote_is_the_plain_url_and_the_token_is_nowhere_in_the_repo(home, repo):
    hgit.ensure_remote(CLONE_URL, cwd=repo)

    assert git("remote", "get-url", "harumi", cwd=repo).stdout.strip() == CLONE_URL
    assert TOKEN not in config_text(repo) and TOKEN not in git("remote", "-v", cwd=repo).stdout
    assert TOKEN not in git("config", "--list", cwd=repo).stdout


def test_a_url_that_already_has_credentials_is_cleaned(home, repo):
    hgit.ensure_remote(f"https://{USER}:{TOKEN}@git.harumi.io/u-abc/repo.git", cwd=repo)

    assert git("remote", "get-url", "harumi", cwd=repo).stdout.strip() == CLONE_URL
    assert TOKEN not in config_text(repo)


def test_setting_the_remote_twice_does_not_stack_helpers(home, repo):
    hgit.ensure_remote(CLONE_URL, cwd=repo)
    hgit.ensure_remote(CLONE_URL, cwd=repo)

    helpers = git("config", "--local", "--get-all", "credential.https://git.harumi.io.helper", cwd=repo).stdout.splitlines()
    assert helpers == ["", hgit.CREDENTIAL_HELPER]


# -- git gets the password from harumi, for Harumi's host only -------------------------------------------------------


def test_git_asks_harumi_for_the_password_and_gets_the_token(home, repo, harumi_on_path):
    hgit.ensure_remote(CLONE_URL, cwd=repo)

    answer = fill(repo)

    assert f"username={USER}" in answer.stdout and f"password={TOKEN}" in answer.stdout


def test_a_stale_helper_the_user_already_has_does_not_answer_first(home, repo, harumi_on_path):
    """A macOS Keychain or credential-manager entry for this host would otherwise win, with an old token."""
    hgit.ensure_remote(CLONE_URL, cwd=repo)

    answer = fill(repo)

    assert "STALE-TOKEN" not in answer.stdout and "old-user" not in answer.stdout


def test_the_users_helper_still_serves_every_other_host(home, repo, harumi_on_path):
    hgit.ensure_remote(CLONE_URL, cwd=repo)

    answer = fill(repo, host="github.com")

    assert "STALE-TOKEN" in answer.stdout and TOKEN not in answer.stdout


def test_a_good_push_is_not_copied_into_the_users_other_helper(home, repo, harumi_on_path, tmp_path):
    """After success git tells every helper to `store`. Ours ignores it, and the user's must not be told at all."""
    hgit.ensure_remote(CLONE_URL, cwd=repo)

    subprocess.run(
        ["git", "credential", "approve"], cwd=repo, capture_output=True, text=True,
        input=f"protocol=https\nhost=git.harumi.io\nusername={USER}\npassword={TOKEN}\n\n",
    )

    assert not (tmp_path / "stale-helper.sh.stored").exists()


def test_git_never_waits_for_a_password_on_the_terminal(home, repo, monkeypatch):
    """With no helper to answer (not logged in), the push must fail, not hang on a prompt nobody can type into."""
    seen = {}
    real_run = subprocess.run

    def spy(args, **kwargs):
        seen["env"] = kwargs.get("env") or {}
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    hgit._run(["status"], cwd=repo)

    assert seen["env"].get("GIT_TERMINAL_PROMPT") == "0"


# -- clone ----------------------------------------------------------------------------------------------------------


def test_clone_runs_with_the_helper_and_leaves_a_remote_without_the_token(home, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hgit, "_run", lambda args, **kw: calls.append(args))

    hgit.clone_repo(f"https://{USER}:{TOKEN}@git.harumi.io/u-abc/repo.git", tmp_path / "dest")

    (args,) = calls
    assert TOKEN not in " ".join(args) and "clone" in args
    assert args.count("credential.https://git.harumi.io.helper=") == 1  # the reset
    assert f"credential.https://git.harumi.io.helper={hgit.CREDENTIAL_HELPER}" in args
    assert args[args.index("clone") + 3] == CLONE_URL  # `clone --origin harumi <url>`: the URL given to git is the plain one


def test_a_real_clone_goes_through_the_helper(home, tmp_path, harumi_on_path):
    """End to end with real git: a local bare repo served over a file path cannot ask for a password, so the clone only
    proves the arguments are accepted and the remote ends up clean."""
    bare = tmp_path / "bare.git"
    git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", cwd=seed)
    (seed / "a.txt").write_text("a")
    git("add", "a.txt", cwd=seed)
    git("commit", "-q", "-m", "x", cwd=seed)
    git("push", "-q", str(bare), "HEAD:refs/heads/main", cwd=seed)
    dest = tmp_path / "dest"

    hgit.clone_repo(bare.as_posix(), dest)

    assert (dest / "a.txt").exists()
    assert git("remote", "get-url", "harumi", cwd=dest).stdout.strip() == bare.as_posix()


# -- clones made before this fix -------------------------------------------------------------------------------------


def old_clone(folder):
    git("remote", "add", "harumi", f"https://{USER}:{TOKEN}@git.harumi.io/u-abc/repo.git", cwd=folder)


def test_an_old_clone_is_detected_and_cleaned(home, repo, harumi_on_path):
    old_clone(repo)
    assert TOKEN in config_text(repo) and hgit.remote_has_credentials(cwd=repo)

    assert hgit.scrub_remote(cwd=repo) is True

    assert TOKEN not in config_text(repo)
    assert git("remote", "get-url", "harumi", cwd=repo).stdout.strip() == CLONE_URL
    assert f"password={TOKEN}" in fill(repo).stdout  # and it still authenticates, now through the helper


def test_cleaning_a_clean_clone_does_nothing_and_says_so(home, repo):
    hgit.ensure_remote(CLONE_URL, cwd=repo)
    before = config_text(repo)

    assert hgit.scrub_remote(cwd=repo) is False and config_text(repo) == before


def test_cleaning_a_folder_with_no_harumi_remote_does_nothing(home, repo):
    assert hgit.scrub_remote(cwd=repo) is False


def test_a_scratch_push_cleans_an_old_clone_before_it_pushes(home, repo, monkeypatch):
    old_clone(repo)
    (repo / "main.py").write_text("print(1)\n")
    git("add", "main.py", cwd=repo)
    git("commit", "-q", "-m", "x", cwd=repo)
    seen = {}
    real_run = hgit._run

    def spy(args, **kw):
        if args[:1] == ["push"]:
            seen["config_at_push"] = config_text(repo)
        return real_run(args, **kw) if args[:1] != ["push"] else subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(hgit, "_run", spy)
    hgit.push_scratch(username=USER, cwd=repo)

    assert TOKEN not in seen["config_at_push"]


def test_an_error_from_git_never_contains_the_token(home, repo):
    """Nothing puts the token in argv any more, so there is nothing to leak into GitError."""
    with pytest.raises(hgit.GitError) as exc:
        hgit._run(["push", "harumi", "HEAD:refs/heads/x"], cwd=repo)

    assert TOKEN not in str(exc.value)
