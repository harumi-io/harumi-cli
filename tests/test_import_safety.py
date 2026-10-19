"""`harumi import` must not upload secrets or junk, must say what it is uploading,
and must not create a second project from a folder that already is one.

The git side runs real git in temporary folders: ignore and tracking rules are the
thing under test, and a mock of git would only agree with the code that wrote it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harumi import cli, config, git as hgit, interactive
from harumi.git import SECRET_OK, import_excludes, looks_like_secret, plan_import, push_folder

runner = CliRunner()


def sh(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True
    )


def messy_folder(root: Path) -> Path:
    """A folder as people really have them: code, data, secrets and tooling junk."""
    f = root / "proj"
    for d in ("data", ".venv/lib", "node_modules/x", "src/__pycache__"):
        (f / d).mkdir(parents=True)
    (f / "main.py").write_text("print(1)\n")
    (f / "src" / "m.py").write_text("x = 1\n")
    (f / "data" / "big.csv").write_bytes(b"0" * 3_000_000)
    (f / ".env").write_text("API_KEY=sk-live-123\n")
    (f / ".env.example").write_text("API_KEY=\n")
    (f / "deploy.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    (f / ".venv/lib/a.py").write_text("x\n")
    (f / "node_modules/x/i.js").write_text("y\n")
    (f / "src/__pycache__/m.pyc").write_text("z\n")
    (f / ".DS_Store").write_text("o\n")
    return f


# -- what looks like a secret ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [".env", ".env.local", ".env.production", "config/.env", "deploy.pem", "tls/server.key", "id_rsa", "id_ed25519.pub", "a.p12", "a.pfx", "store.keystore"],
)
def test_these_look_like_secrets(path):
    assert looks_like_secret(path)


@pytest.mark.parametrize("path", ["main.py", "keyboard.py", "environment.md", "data/keys.csv", "monkey.txt", *SECRET_OK])
def test_these_do_not(path):
    assert not looks_like_secret(path)


# -- the plan --------------------------------------------------------------------------


def test_the_plan_leaves_out_secrets_and_junk_and_keeps_the_rest(tmp_path):
    plan = plan_import(messy_folder(tmp_path))

    assert plan.files == 4  # main.py, src/m.py, data/big.csv, .env.example
    assert sorted(plan.skipped_secrets) == [".env", "deploy.pem"]
    assert dict(plan.skipped_dirs) == {".venv": 1, "node_modules": 1, "src/__pycache__": 1}
    assert plan.largest[0] == ("data/big.csv", 3_000_000)
    assert plan.included_secrets == []
    assert plan.history_commits == 0


def test_an_example_env_file_is_uploaded_because_it_holds_no_secret(tmp_path):
    folder = messy_folder(tmp_path)
    plan = plan_import(folder)
    assert ".env.example" not in plan.skipped_secrets
    assert plan.files == 4


def test_the_users_own_gitignore_is_honoured(tmp_path):
    folder = messy_folder(tmp_path)
    (folder / ".gitignore").write_text("data/\n")

    plan = plan_import(folder)

    assert plan.largest[0][0] != "data/big.csv"
    assert plan.files == 4  # main.py, src/m.py, .env.example, .gitignore


def test_planning_leaves_the_folder_exactly_as_it_was(tmp_path):
    folder = messy_folder(tmp_path)
    before = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))

    plan_import(folder)

    after = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))
    assert after == before
    assert not (folder / ".git").exists()


def test_a_secret_that_git_already_tracks_is_reported_as_going_up_with_its_history(tmp_path):
    """Excluding a pattern cannot un-track a file git already has, so the preview
    must not claim it was skipped. This is the case a naive exclude gets wrong."""
    folder = tmp_path / "r"
    folder.mkdir()
    sh("init", "-q", "-b", "main", ".", cwd=folder)
    (folder / "main.py").write_text("print(1)\n")
    (folder / ".env").write_text("SECRET=1\n")
    sh("add", ".", cwd=folder)
    sh("commit", "-qm", "init", cwd=folder)
    (folder / "new.pem").write_text("k\n")

    plan = plan_import(folder)

    assert plan.included_secrets == [".env"]
    assert plan.skipped_secrets == ["new.pem"]  # untracked, so it really is left out
    assert plan.history_commits == 1


def test_a_symlinked_path_gives_the_same_answer_as_the_real_one(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    sh("init", "-q", "-b", "main", ".", cwd=real)
    (real / "a.py").write_text("x\n")
    sh("add", ".", cwd=real)
    sh("commit", "-qm", "init", cwd=real)
    link = tmp_path / "link"
    link.symlink_to(real)

    assert plan_import(link).history_commits == plan_import(real).history_commits == 1


# -- what is actually pushed -------------------------------------------------------------


def push_to_bare_remote(folder: Path, tmp: Path, **kw) -> list[str]:
    """Run the real push_folder against a local bare repo; return the files in the pushed commit."""
    remote = tmp / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    push_folder(folder, clone_url=remote.as_posix(), username="u", token="t", branch="main", **kw)
    out = subprocess.run(
        ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"], capture_output=True, text=True, check=True
    )
    return sorted(out.stdout.split())


def test_the_pushed_commit_contains_no_secrets_or_junk(tmp_path, monkeypatch):
    monkeypatch.setattr(hgit, "ensure_remote", lambda clone_url, username, token, name="harumi", cwd=None: sh("remote", "add", name, clone_url, cwd=cwd))
    folder = messy_folder(tmp_path)

    pushed = push_to_bare_remote(folder, tmp_path, exclude=import_excludes())

    assert pushed == [".env.example", "data/big.csv", "main.py", "src/m.py"]


def test_the_exclusions_are_written_locally_and_never_touch_the_users_gitignore(tmp_path, monkeypatch):
    monkeypatch.setattr(hgit, "ensure_remote", lambda clone_url, username, token, name="harumi", cwd=None: sh("remote", "add", name, clone_url, cwd=cwd))
    folder = messy_folder(tmp_path)
    (folder / ".gitignore").write_text("build/\n")

    push_to_bare_remote(folder, tmp_path, exclude=import_excludes())

    assert (folder / ".gitignore").read_text() == "build/\n"
    local = (folder / ".git" / "info" / "exclude").read_text()
    assert "[eE][nN][vV]" in local and ".venv/" in local and "not part of your project" in local


def test_importing_twice_does_not_duplicate_the_local_exclude_block(tmp_path):
    folder = tmp_path / "f"
    folder.mkdir()
    sh("init", "-q", ".", cwd=folder)

    hgit._write_local_excludes(folder, [".env"])
    hgit._write_local_excludes(folder, [".env"])

    assert (folder / ".git" / "info" / "exclude").read_text().count("harumi import") == 1


def test_without_exclusions_push_folder_behaves_as_it_always_did(tmp_path, monkeypatch):
    monkeypatch.setattr(hgit, "ensure_remote", lambda clone_url, username, token, name="harumi", cwd=None: sh("remote", "add", name, clone_url, cwd=cwd))
    folder = messy_folder(tmp_path)

    pushed = push_to_bare_remote(folder, tmp_path)

    assert ".env" in pushed and "deploy.pem" in pushed  # the old, unsafe default, kept for library callers


# -- the command --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")
    config.save_git_token("gitea", username="u-1")


class Repo:
    owner, name, clone_url, default_branch = "acme", "s", "https://g/acme/s.git", "main"


class FakeClient:
    config = type("C", (), {"org_id": None, "environment": "production"})()

    def __init__(self):
        self.created = []

    def create_project(self, name, **kw):
        self.created.append(name)
        return type("P", (), {"id": "proj-new", "name": name, "customer_id": None, "repo": Repo()})()


@pytest.fixture
def world(monkeypatch):
    client = FakeClient()
    pushed = []
    monkeypatch.setattr(cli, "_get_client", lambda *a, **k: client)
    monkeypatch.setattr(cli, "_require_entitled_workspace", lambda *a, **k: None)
    monkeypatch.setattr(cli, "push_folder", lambda folder, **kw: pushed.append({"folder": folder, **kw}))
    monkeypatch.setattr(cli, "_bind_and_configure_remote", lambda *a, **k: False)
    return type("W", (), {"client": client, "pushed": pushed})


def test_import_refuses_a_folder_that_is_already_a_harumi_project(world, tmp_path):
    folder = messy_folder(tmp_path)
    (folder / ".harumi").mkdir()
    (folder / ".harumi" / "config.json").write_text(
        json.dumps({"project_id": "proj-existing", "repo": {"owner": "a", "name": "b", "clone_url": "u", "default_branch": "main"}})
    )

    result = runner.invoke(cli.app, ["import", str(folder)])

    assert result.exit_code == 1
    out = " ".join(result.output.split())
    assert "already a Harumi project" in out and "proj-existing" in out
    assert "git push harumi" in out
    assert world.client.created == [] and world.pushed == []


def test_import_shows_what_it_uploads_and_what_it_leaves_out(world, tmp_path):
    result = runner.invoke(cli.app, ["import", str(messy_folder(tmp_path)), "--yes"])

    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "5 files" in out  # the 4 real ones plus the harumi.toml written for it
    assert ".venv/ (1 file)" in out and "node_modules/ (1 file)" in out
    assert ".env" in out and "deploy.pem" in out and "look like secrets" in out
    assert "harumi secrets set" in out


def test_import_passes_the_exclusions_to_the_push(world, tmp_path):
    runner.invoke(cli.app, ["import", str(messy_folder(tmp_path)), "--yes"])

    assert world.pushed and world.pushed[0]["exclude"] == import_excludes()
    assert ".venv/" in world.pushed[0]["exclude"]


def test_a_tracked_secret_is_a_loud_warning(world, tmp_path):
    folder = tmp_path / "r"
    folder.mkdir()
    sh("init", "-q", "-b", "main", ".", cwd=folder)
    (folder / "main.py").write_text("x\n")
    (folder / ".env").write_text("S=1\n")
    sh("add", ".", cwd=folder)
    sh("commit", "-qm", "i", cwd=folder)

    result = runner.invoke(cli.app, ["import", str(folder), "--yes"])

    out = " ".join(result.output.split())
    assert "will be uploaded" in out and ".env" in out and "git rm --cached" in out


def test_large_files_are_called_out_and_pointed_at_files_put(world, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LARGE_FILE_BYTES", 1_000_000)
    result = runner.invoke(cli.app, ["import", str(messy_folder(tmp_path)), "--yes"])

    out = " ".join(result.output.split())
    assert "Large files" in out and "data/big.csv" in out and "harumi files put" in out


# -- asking, only where there is someone to ask ---------------------------------------


@pytest.fixture
def at_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)


def test_at_a_terminal_it_asks_for_the_name_and_confirms(world, tmp_path, at_terminal):
    result = runner.invoke(cli.app, ["import", str(messy_folder(tmp_path))], input="Routing demo\ny\n")

    assert result.exit_code == 0, result.output
    assert world.client.created == ["Routing demo"]


def test_saying_no_creates_nothing_and_leaves_the_folder_alone(world, tmp_path, at_terminal):
    folder = messy_folder(tmp_path)
    before = sorted(p.name for p in folder.iterdir())

    result = runner.invoke(cli.app, ["import", str(folder)], input="\nn\n")

    assert result.exit_code == 0
    assert "Nothing was created" in result.output
    assert world.client.created == [] and world.pushed == []
    assert sorted(p.name for p in folder.iterdir()) == before  # not even a harumi.toml was written


def test_enter_alone_accepts_the_folder_name(world, tmp_path, at_terminal):
    runner.invoke(cli.app, ["import", str(messy_folder(tmp_path))], input="\ny\n")
    assert world.client.created == ["proj"]


def test_a_given_name_is_not_asked_for_again(world, tmp_path, at_terminal):
    runner.invoke(cli.app, ["import", str(messy_folder(tmp_path)), "--project-name", "Given"], input="y\n")
    assert world.client.created == ["Given"]


def test_the_confirmation_defaults_to_no_when_a_secret_will_be_uploaded(world, tmp_path, at_terminal):
    folder = tmp_path / "r"
    folder.mkdir()
    sh("init", "-q", "-b", "main", ".", cwd=folder)
    (folder / ".env").write_text("S=1\n")
    sh("add", ".", cwd=folder)
    sh("commit", "-qm", "i", cwd=folder)

    runner.invoke(cli.app, ["import", str(folder)], input="\n\n")  # Enter at the name, Enter at the question

    assert world.client.created == []


def test_yes_skips_every_question(world, tmp_path, at_terminal):
    result = runner.invoke(cli.app, ["import", str(messy_folder(tmp_path)), "--yes"])

    assert result.exit_code == 0, result.output
    assert world.client.created == ["proj"]


def test_without_a_terminal_it_never_asks_and_goes_ahead_as_it_always_did(world, tmp_path, monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)

    result = runner.invoke(cli.app, ["import", str(messy_folder(tmp_path))])

    assert result.exit_code == 0, result.output
    assert world.client.created == ["proj"]
    assert "5 files" in " ".join(result.output.split())  # the summary is still logged


def test_the_summary_counts_exactly_what_ends_up_in_the_pushed_commit(tmp_path, monkeypatch):
    """End to end, with the real `push_folder` into a local bare repo. The preview
    once said 3 files while 4 arrived, because the harumi.toml written afterwards
    was not counted. What is promised must be what is sent."""
    monkeypatch.setattr(
        hgit, "ensure_remote", lambda clone_url, username, token, name="harumi", cwd=None: sh("remote", "add", name, clone_url, cwd=cwd)
    )
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)

    class LocalRepo(Repo):
        clone_url = remote.as_posix()

    client = FakeClient()
    client.create_project = lambda name, **kw: type(
        "P", (), {"id": "proj-new", "name": name, "customer_id": None, "repo": LocalRepo()}
    )()
    monkeypatch.setattr(cli, "_get_client", lambda *a, **k: client)
    monkeypatch.setattr(cli, "_require_entitled_workspace", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_bind_and_configure_remote", lambda *a, **k: False)
    monkeypatch.setattr(cli, "push_folder", hgit.push_folder)  # the real one, not the stub

    result = runner.invoke(cli.app, ["import", str(messy_folder(tmp_path)), "--yes"])

    assert result.exit_code == 0, result.output
    listed = subprocess.run(
        ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"], capture_output=True, text=True, check=True
    ).stdout.split()
    promised = int(re.search(r"(\d+) files", " ".join(result.output.split())).group(1))
    assert promised == len(listed), f"summary said {promised}, the repo received {sorted(listed)}"
    assert ".env" not in listed and "deploy.pem" not in listed
