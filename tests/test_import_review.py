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

from test_import_safety import FakeClient, Repo, at_terminal, sh, world  # noqa: F401  (fixtures)

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


@pytest.mark.parametrize("path", [".ENV", ".Env.Local", "Server.PEM", "TLS/Server.Key", "ID_RSA", "Id_Ed25519.pub", "A.P12"])
def test_secret_names_are_matched_whatever_their_case(path):
    assert looks_like_secret(path)


def test_an_example_file_is_still_fine_in_any_case():
    assert not looks_like_secret(".ENV.EXAMPLE")


@pytest.mark.parametrize("name", [".ENV", "Server.PEM", "ID_RSA"])
def test_an_uppercase_secret_is_reported_and_not_uploaded(tmp_path, name):
    """On a case-sensitive filesystem git's own matching would have uploaded these."""
    f = tmp_path / "p"
    f.mkdir()
    (f / name).write_text("k\n")
    (f / "a.py").write_text("1\n")

    plan = plan_import(f)

    assert plan.skipped_secrets == [name]
    assert plan.files == 1


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

    result = runner.invoke(cli.app, ["start"], input=f"2\n{f}\nRouting\ny\n")

    out = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "Left out" in out and ".env" in out  # the preview ran
    assert world.client.created == ["Routing"]
    assert ".env" in world.pushed[0]["exclude"]


def test_start_option_two_will_not_import_a_tracked_secret_just_because_the_flow_is_menu_driven(world, tmp_path, at_terminal):  # noqa: F811
    folder = repo_tracking_a_secret(tmp_path)

    result = runner.invoke(cli.app, ["start"], input=f"2\n{folder}\nRouting\n\n")  # Enter at the confirmation = no

    assert world.client.created == []
