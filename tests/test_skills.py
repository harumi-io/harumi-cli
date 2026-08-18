"""Tests for harumi.skills: bundled-skill discovery and local install."""

from __future__ import annotations

import re

import pytest

import harumi.skills as skills


def _frontmatter(skill_md_path):
    text = skill_md_path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, f"{skill_md_path} has no YAML frontmatter block"
    return match.group(1)


def test_bundled_skills_have_valid_frontmatter():
    dirs = skills.bundled_skill_dirs()
    names = {d.name for d in dirs}
    assert {"harumi-cli", "harumi-cli-setup"}.issubset(names)

    for skill_dir in dirs:
        front = _frontmatter(skill_dir / "SKILL.md")
        name_match = re.search(r"^name:\s*(\S+)\s*$", front, re.MULTILINE)
        assert name_match, f"{skill_dir}/SKILL.md missing `name:`"
        assert name_match.group(1) == skill_dir.name, (
            f"frontmatter name {name_match.group(1)!r} must match folder {skill_dir.name!r}"
        )
        assert re.search(r"^description:", front, re.MULTILINE), (
            f"{skill_dir}/SKILL.md missing `description:`"
        )


def test_install_writes_every_bundled_skill_to_every_target(tmp_path, monkeypatch):
    fake_home_dir = tmp_path / "cursor-skills"
    monkeypatch.setattr(
        skills,
        "AGENTS",
        [skills.Agent("cursor", "Cursor", fake_home_dir)],
    )
    monkeypatch.setattr(skills, "AGENT_KEYS", ["cursor"])

    written = skills.install(agent_keys=["cursor"])

    expected_names = {d.name for d in skills.bundled_skill_dirs()}
    assert {p.name for p in written} == expected_names
    for path in written:
        assert (path / "SKILL.md").exists()


def test_install_is_idempotent(tmp_path, monkeypatch):
    fake_home_dir = tmp_path / "cursor-skills"
    monkeypatch.setattr(skills, "AGENTS", [skills.Agent("cursor", "Cursor", fake_home_dir)])
    monkeypatch.setattr(skills, "AGENT_KEYS", ["cursor"])

    skills.install(agent_keys=["cursor"])
    written_again = skills.install(agent_keys=["cursor"])  # should not raise
    for path in written_again:
        assert (path / "SKILL.md").exists()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    fake_home_dir = tmp_path / "cursor-skills"
    monkeypatch.setattr(skills, "AGENTS", [skills.Agent("cursor", "Cursor", fake_home_dir)])
    monkeypatch.setattr(skills, "AGENT_KEYS", ["cursor"])

    written = skills.install(agent_keys=["cursor"], dry_run=True)
    assert written  # reports what it *would* write
    assert not fake_home_dir.exists()


def test_project_scope_writes_agents_skills_dir(tmp_path):
    skills.install(project=True, cwd=tmp_path)
    project_dir = tmp_path / skills.PROJECT_SKILLS_DIR
    assert (project_dir / "harumi-cli" / "SKILL.md").exists()
    assert (project_dir / "harumi-cli-setup" / "SKILL.md").exists()


def test_unknown_agent_key_raises(tmp_path):
    with pytest.raises(ValueError, match="Unknown agent"):
        skills.install(agent_keys=["not-a-real-agent"])


def test_refuses_to_overwrite_non_skill_directory_without_force(tmp_path, monkeypatch):
    fake_home_dir = tmp_path / "cursor-skills"
    monkeypatch.setattr(skills, "AGENTS", [skills.Agent("cursor", "Cursor", fake_home_dir)])
    monkeypatch.setattr(skills, "AGENT_KEYS", ["cursor"])

    collision = fake_home_dir / "harumi-cli"
    collision.mkdir(parents=True)
    (collision / "not-a-skill.txt").write_text("mine")

    with pytest.raises(FileExistsError):
        skills.install(agent_keys=["cursor"])

    # --force overwrites it.
    skills.install(agent_keys=["cursor"], force=True)
    assert (collision / "SKILL.md").exists()
