"""`agent-skills install` (US-27): says where the skills went and why, in words a person can read.

It writes into a user's home, so every test points the agents' folders at a temp directory. The copy itself
(idempotent, refuses to clobber a folder that is not a skill) was already right; the output and the errors were not:
the first lines were the package's internal source path (91 characters on a pipx install) and a destination broke
mid-word at 80 columns, and a file in the way printed `[Errno 17] File exists: '...'`.
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from harumi import cli, skills

try:
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()

SKILLS = ["harumi-cli", "harumi-cli-setup"]


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    agents = [
        skills.Agent("cursor", "Cursor", home / ".cursor" / "skills"),
        skills.Agent("claude-code", "Claude Code", home / ".claude" / "skills"),
        skills.Agent("codex", "Codex", home / ".codex" / "skills"),
        skills.Agent("universal", "Universal (~/.agents/skills)", home / ".agents" / "skills"),
    ]
    monkeypatch.setattr(skills, "AGENTS", agents)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli.console, "_width", 80)
    monkeypatch.setattr(cli.console, "_height", 40)
    monkeypatch.setattr(cli.err_console, "_width", 80)
    monkeypatch.setattr(cli.err_console, "_height", 40)
    return home


def said(args, cwd=None):
    old = os.getcwd()
    if cwd:
        os.chdir(cwd)
    try:
        result = runner.invoke(cli.app, args)
    finally:
        os.chdir(old)
    return result.exit_code, result.stdout, result.stderr


def installed(home, folder):
    return sorted(p.name for p in (home / folder).iterdir()) if (home / folder).is_dir() else []


# -- what it says ------------------------------------------------------------------------------------------------------------


def test_it_names_each_agent_and_where_its_skills_went(fake_home):
    (fake_home / ".cursor").mkdir()
    (fake_home / ".claude").mkdir()

    code, out, _ = said(["agent-skills", "install"])

    assert code == 0
    assert "Cursor" in out and "Claude Code" in out
    flat = out.replace("\n", " ")
    assert "~/.cursor/skills" in flat and "~/.claude/skills" in flat
    assert installed(fake_home, ".cursor/skills") == SKILLS and installed(fake_home, ".claude/skills") == SKILLS


def test_it_does_not_print_the_packages_internal_source_path(fake_home):
    """The reader cannot act on `.../site-packages/harumi/_skills/harumi-cli`; it is noise that pushed the answer off screen."""
    (fake_home / ".cursor").mkdir()

    _, out, _ = said(["agent-skills", "install"])

    assert "_skills" not in out and ".agents/skills/harumi-cli" not in out and "site-packages" not in out
    assert str(skills.bundled_skill_dirs()[0].parent) not in out.replace("\n", "")


def test_no_line_of_the_output_breaks_a_path_at_80_columns(fake_home):
    """It broke `harumi-cli` into `h` / `arumi-cli` at 80 columns: a destination must stay in one piece."""
    for name in (".cursor", ".claude", ".codex"):
        (fake_home / name).mkdir()

    _, out, _ = said(["agent-skills", "install"])

    for path in ("~/.cursor/skills", "~/.claude/skills", "~/.codex/skills"):
        assert path in out, f"{path} was split across lines"


def test_it_says_how_many_skills_not_how_many_directories(fake_home):
    (fake_home / ".cursor").mkdir()

    _, out, _ = said(["agent-skills", "install"])

    assert "harumi-cli" in out and "harumi-cli-setup" in out and "directories" not in out


def test_a_dry_run_says_nothing_was_written_and_writes_nothing(fake_home):
    (fake_home / ".cursor").mkdir()

    code, out, _ = said(["agent-skills", "install", "--dry-run"])

    assert code == 0 and "Would" in out and "Installed" not in out
    assert installed(fake_home, ".cursor/skills") == []


def test_with_no_agent_found_it_says_so_and_where_it_went_instead(fake_home):
    code, out, _ = said(["agent-skills", "install"])

    assert code == 0
    assert "No Cursor, Claude Code or Codex folder" in out and "~/.agents/skills" in out
    assert "--agent" in out
    assert installed(fake_home, ".agents/skills") == SKILLS


def test_choosing_an_agent_does_not_print_the_no_agent_found_note(fake_home):
    code, out, _ = said(["agent-skills", "install", "--agent", "codex"])

    assert code == 0 and "No Cursor" not in out and "Codex" in out
    assert installed(fake_home, ".codex/skills") == SKILLS


def test_project_install_names_the_folder_it_wrote_to(fake_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    code, out, _ = said(["agent-skills", "install", "--project"], cwd=repo)

    assert code == 0 and "this project" in out and ".agents/skills" in out
    assert sorted(p.name for p in (repo / ".agents" / "skills").iterdir()) == SKILLS


def test_running_it_again_is_the_same_and_still_says_what_it_did(fake_home):
    (fake_home / ".cursor").mkdir()
    said(["agent-skills", "install"])

    code, out, _ = said(["agent-skills", "install"])

    assert code == 0 and "Cursor" in out and installed(fake_home, ".cursor/skills") == SKILLS


# -- when it cannot ------------------------------------------------------------------------------------------------------------


def test_a_file_where_the_folder_should_be_is_explained(fake_home):
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "skills").write_text("i am a file")

    code, _, err = said(["agent-skills", "install", "--agent", "claude-code"])

    assert code == 1
    assert "Errno" not in err
    assert "~/.claude/skills" in err.replace("\n", "") and "file" in err and "move" in err.lower()


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions and a non-root user")
def test_a_folder_we_cannot_write_to_says_so_and_what_to_do(fake_home):
    folder = fake_home / ".cursor" / "skills"
    folder.mkdir(parents=True)
    folder.chmod(0o500)
    try:
        code, _, err = said(["agent-skills", "install", "--agent", "cursor"])
    finally:
        folder.chmod(0o700)

    assert code == 1
    flat = err.replace("\n", " ")
    assert "Traceback" not in err and "Errno" not in err
    # The folder a person can fix, not the file inside it that the OS failed to create.
    assert "No permission to write to ~/.cursor/skills." in flat.replace("  ", " ")
    assert "harumi-cli" not in flat
    assert "--agent" in flat  # the way out, which a bare "Permission denied" does not give


def test_an_unknown_agent_lists_the_known_ones_and_writes_nothing(fake_home):
    code, _, err = said(["agent-skills", "install", "--agent", "bogus"])

    assert code == 1 and "bogus" in err and "cursor" in err
    assert not (fake_home / ".agents").exists()


def test_a_misspelt_agent_gets_the_nearest_name(fake_home):
    _, _, err = said(["agent-skills", "install", "--agent", "cursur"])

    assert "Did you mean cursor" in err


def test_a_folder_that_is_not_a_skill_is_not_overwritten_without_force(fake_home):
    target = fake_home / ".cursor" / "skills" / "harumi-cli"
    target.mkdir(parents=True)
    (target / "mine.txt").write_text("keep me")

    code, _, err = said(["agent-skills", "install", "--agent", "cursor"])

    assert code == 1 and "--force" in err
    assert (target / "mine.txt").read_text() == "keep me"
