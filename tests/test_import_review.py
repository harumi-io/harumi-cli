"""Gaps the automated review found in `harumi import`'s secret protection.

The protection used to cover only the working tree, and only when a person was there to
answer. History goes up verbatim, names are case-insensitive on the machines most people
use, and an agent or CI job has nobody to say no.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harumi import cli, git as hgit, interactive
from harumi.git import looks_like_secret, plan_import

from test_import_safety import FakeClient, Repo, at_terminal, home, sh, world  # noqa: F401  (fixtures; `home` is autouse and isolates ~/.harumi)

runner = CliRunner()


def repo_with_a_deleted_secret(root: Path) -> Path:
    f = root / "r"
    f.mkdir()
    sh("init", "-q", "-b", "main", ".", cwd=f)
    (f / "a.py").write_text("1\n")
    (f / ".env").write_text("KEY=sk-live\n")
    sh("add", ".", cwd=f)
    sh("commit", "-qm", "oops", cwd=f)
    sh("rm", "-q", "--cached", ".env", cwd=f)
    (f / ".env").unlink()
    sh("commit", "-qm", "remove it", cwd=f)
    return f


def repo_tracking_a_secret(root: Path) -> Path:
    f = root / "t"
    f.mkdir()
    sh("init", "-q", "-b", "main", ".", cwd=f)
    (f / "a.py").write_text("1\n")
    (f / "deploy.pem").write_text("k\n")
    sh("add", ".", cwd=f)
    sh("commit", "-qm", "init", cwd=f)
    return f


# -- history ----------------------------------------------------------------------------------------


def test_a_secret_committed_once_and_since_deleted_is_found_in_the_history_that_goes_up(tmp_path):
    plan = plan_import(repo_with_a_deleted_secret(tmp_path))

    assert plan.included_secrets == []  # it is not in the tree any more
    assert plan.history_secrets == [".env"]  # but every commit is pushed, and it is in one


def test_a_clean_history_reports_nothing(tmp_path):
    f = tmp_path / "c"
    f.mkdir()
    sh("init", "-q", "-b", "main", ".", cwd=f)
    (f / "a.py").write_text("1\n")
    sh("add", ".", cwd=f)
    sh("commit", "-qm", "init", cwd=f)

    assert plan_import(f).history_secrets == []


def test_the_preview_says_the_history_is_uploaded_as_it_is(world, tmp_path, at_terminal):  # noqa: F811
    result = runner.invoke(cli.app, ["import", str(repo_with_a_deleted_secret(tmp_path))], input="n\n")

    out = " ".join(result.output.split())
    assert ".env" in out and "history" in out and "uploaded" in out
    assert world.client.created == []


# -- nobody there to say no -------------------------------------------------------------------------


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


@pytest.mark.parametrize("make", [repo_tracking_a_secret, repo_with_a_deleted_secret])
def test_without_a_terminal_a_tracked_or_historical_secret_is_refused(world, tmp_path, no_terminal, make):  # noqa: F811
    result = runner.invoke(cli.app, ["import", str(make(tmp_path))])

    out = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "--include-tracked-secrets" in out
    assert world.client.created == [] and world.pushed == []


@pytest.mark.parametrize("make", [repo_tracking_a_secret, repo_with_a_deleted_secret])
def test_yes_does_not_mean_yes_to_uploading_a_secret(world, tmp_path, at_terminal, make):  # noqa: F811
    result = runner.invoke(cli.app, ["import", str(make(tmp_path)), "--yes"])

    assert result.exit_code == 1
    assert "--include-tracked-secrets" in " ".join(result.output.split())
    assert world.client.created == []


def test_the_explicit_flag_is_the_way_through(world, tmp_path, no_terminal):  # noqa: F811
    result = runner.invoke(cli.app, ["import", str(repo_tracking_a_secret(tmp_path)), "--include-tracked-secrets"])

    assert result.exit_code == 0, result.output
    assert world.client.created == ["t"]


def test_a_folder_with_nothing_secret_in_it_is_not_slowed_down_by_any_of_this(world, tmp_path, no_terminal):  # noqa: F811
    f = tmp_path / "ok"
    f.mkdir()
    (f / "a.py").write_text("1\n")

    assert runner.invoke(cli.app, ["import", str(f)]).exit_code == 0


def test_at_a_terminal_the_person_is_still_asked_and_the_default_is_no(world, tmp_path, at_terminal):  # noqa: F811
    result = runner.invoke(cli.app, ["import", str(repo_tracking_a_secret(tmp_path))], input="\n\n")

    assert world.client.created == []  # Enter on the prompt means no


# -- names ------------------------------------------------------------------------------------------


@pytest.fixture
def case_sensitive_git(monkeypatch):
    """Make git compare names exactly, as it does on Linux and on case-sensitive volumes.

    On the default macOS and Windows filesystems git folds case, so `.ENV` matched the
    `.env` exclude by accident and these tests passed there while failing on Linux CI.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.ignorecase")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")


@pytest.mark.parametrize("path", [".ENV", ".Env.Local", "Server.PEM", "TLS/Server.Key", "ID_RSA", "Id_Ed25519.pub", "A.P12"])
def test_secret_names_are_matched_whatever_their_case(path):
    assert looks_like_secret(path)


def test_an_example_file_is_still_fine_in_any_case():
    assert not looks_like_secret(".ENV.EXAMPLE")


@pytest.mark.parametrize("name", [".ENV", "Server.PEM", "ID_RSA"])
def test_an_uppercase_secret_is_reported_and_not_uploaded(tmp_path, name, case_sensitive_git):
    """On a case-sensitive filesystem git's own matching would have uploaded these."""
    f = tmp_path / "p"
    f.mkdir()
    (f / name).write_text("k\n")
    (f / "a.py").write_text("1\n")

    plan = plan_import(f)

    assert plan.skipped_secrets == [name]
    assert plan.files == 1


@pytest.mark.parametrize("name", [".ENV", ".Env.Local", "Server.PEM", "ID_RSA", "Id_Ed25519", "Keys/Deploy.KEY", "A.P12"])
def test_an_uppercase_secret_is_not_in_the_commit_that_is_pushed(tmp_path, name, case_sensitive_git):
    """The preview and the push must agree. The names were being reported while the exclude
    patterns, which compare exactly, still let them through on a case-sensitive filesystem."""
    f = tmp_path / "p"
    (f / "Keys").mkdir(parents=True)
    (f / name).write_text("secret\n")
    (f / "a.py").write_text("1\n")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    sh_real = hgit.ensure_remote

    try:
        hgit.ensure_remote = lambda clone_url, name="harumi", cwd=None: sh("remote", "add", name, clone_url, cwd=cwd)
        hgit.push_folder(f, clone_url=remote.as_posix(), branch="main", exclude=hgit.import_excludes())
    finally:
        hgit.ensure_remote = sh_real
    pushed = subprocess.run(
        ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"], capture_output=True, text=True, check=True
    ).stdout.split()

    assert pushed == ["a.py"], f"a secret went up: {pushed}"


def test_an_example_file_is_uploaded_whatever_its_case(tmp_path, case_sensitive_git):
    f = tmp_path / "p"
    f.mkdir()
    (f / ".ENV.EXAMPLE").write_text("KEY=\n")
    (f / "a.py").write_text("1\n")

    plan = plan_import(f)

    assert plan.files == 2 and plan.skipped_secrets == []


def test_a_dot_env_directory_is_a_virtualenv_not_a_pile_of_secrets(tmp_path):
    f = tmp_path / "p"
    (f / ".env" / "lib" / "site").mkdir(parents=True)
    (f / ".env" / "pyvenv.cfg").write_text("x\n")
    (f / ".env" / "lib" / "site" / "cert.pem").write_text("k\n")
    (f / "a.py").write_text("1\n")

    plan = plan_import(f)

    assert plan.files == 1
    assert plan.skipped_secrets == []
    assert plan.skipped_dirs == [(".env", 2)]


def test_a_dot_env_file_is_still_a_secret_and_not_a_directory(tmp_path):
    f = tmp_path / "p"
    f.mkdir()
    (f / ".env").write_text("KEY=1\n")

    assert plan_import(f).skipped_secrets == [".env"]


# -- the rest ---------------------------------------------------------------------------------------


def test_the_next_step_can_be_pasted_when_the_folder_has_a_space_or_a_bracket(tmp_path, monkeypatch, capsys):
    where = tmp_path / "My Project [v2]"
    where.mkdir()
    monkeypatch.chdir(tmp_path)

    cli._next_steps(where)

    out = capsys.readouterr().out
    assert "cd 'My Project [v2]'" in out


def test_a_blank_project_name_is_asked_again_not_sent_to_the_server(world, tmp_path, at_terminal):  # noqa: F811
    f = tmp_path / "p"
    f.mkdir()
    (f / "a.py").write_text("1\n")

    result = runner.invoke(cli.app, ["import", str(f)], input="   \nRouting\ny\n")

    assert result.exit_code == 0, result.output
    assert world.client.created == ["Routing"]


def test_start_option_two_imports_the_folder_it_is_pointed_at(world, tmp_path, at_terminal, monkeypatch):  # noqa: F811
    """`harumi start`, "a folder of code on this machine": asks for the path, then goes
    through the same preview and confirmation as `harumi import`, with the secret
    protection, instead of a separate unchecked path."""
    f = tmp_path / "mine"
    f.mkdir()
    (f / "a.py").write_text("1\n")
    (f / ".env").write_text("KEY=1\n")

    monkeypatch.setattr(cli.interactive, "select", lambda *a, **k: "2")  # the arrow menu: "a folder of code"
    result = runner.invoke(cli.app, ["start"], input=f"{f}\nRouting\ny\n")

    out = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "Left out" in out and ".env" in out  # the preview ran
    assert world.client.created == ["Routing"]
    assert world.pushed[0]["exclude"] == hgit.import_excludes()  # the push is told what to leave out


def test_start_option_two_will_not_import_a_tracked_secret_just_because_the_flow_is_menu_driven(world, tmp_path, at_terminal, monkeypatch):  # noqa: F811
    folder = repo_tracking_a_secret(tmp_path)

    monkeypatch.setattr(cli.interactive, "select", lambda *a, **k: "2")  # the arrow menu: "a folder of code"
    result = runner.invoke(cli.app, ["start"], input=f"{folder}\nRouting\n\n")  # Enter at the confirmation = no

    assert world.client.created == []


def test_a_windows_folder_with_a_space_is_double_quoted_not_single_quoted():
    """cmd.exe and PowerShell do not treat single quotes as quoting."""
    assert cli._quote_for_shell("My Project", windows=True) == '"My Project"'
    assert cli._quote_for_shell("My Project", windows=False) == "'My Project'"
    assert cli._quote_for_shell("plain", windows=True) == "plain" == cli._quote_for_shell("plain", windows=False)


def test_every_option_of_import_is_in_the_skill_reference():
    """The reference is what an AI agent reads to know what the CLI can do. A new flag that
    is missing from it is a flag the agent will not use, or will mis-use."""
    from typer.main import get_command

    reference = (Path(__file__).resolve().parents[1] / ".agents/skills/harumi-cli/references/commands.md").read_text(encoding="utf-8")
    start = reference.index("### `import`")
    section = reference[start : reference.index("### `clone`", start)]
    options = [o for p in get_command(cli.app).commands["import"].params for o in p.opts if o.startswith("--") and not p.hidden]

    assert "--include-tracked-secrets" in options  # the parse found the real options
    missing = [o for o in options if o not in section]
    assert missing == [], f"documented in the skill reference? missing: {missing}"
