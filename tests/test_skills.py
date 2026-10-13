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


def test_agents_missing_skill_reports_detected_agent_without_it(tmp_path, monkeypatch):
    fake_home_dir = tmp_path / "cursor-home"
    fake_home_dir.mkdir()
    agent = skills.Agent("cursor", "Cursor", fake_home_dir / "skills")
    monkeypatch.setattr(skills, "AGENTS", [agent])

    assert skills.agents_missing_skill() == [agent]

    skills.install(agent_keys=["cursor"])
    assert skills.agents_missing_skill() == []


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


# -- library presets installed into a project (`harumi skills add`) -----------


def test_provenance_replaces_existing_source_and_version_and_keeps_the_body():
    preset = "---\nname: X\nversion: 1\nsource: other/x\ndescription: d\n---\n\n# Body\n"

    out = skills.with_install_provenance(preset, "x", 4)

    assert out == "---\nname: X\ndescription: d\nsource: harumi/x\nversion: 4\n---\n\n# Body\n"


def test_provenance_adds_a_frontmatter_to_a_preset_without_one():
    out = skills.with_install_provenance("# Just a body\n", "x", 2)

    assert out == "---\nsource: harumi/x\nversion: 2\n---\n\n# Just a body\n"


def test_provenance_rejects_unclosed_frontmatter():
    with pytest.raises(ValueError):
        skills.with_install_provenance("---\nname: X\n# never closed\n", "x", 1)


def test_frontmatter_source_reads_what_an_install_wrote():
    installed = skills.with_install_provenance("---\nname: X\n---\nbody\n", "vrp-modeling", 1)

    assert skills.frontmatter_source(installed) == "harumi/vrp-modeling"
    assert skills.frontmatter_source("---\nname: mine\n---\n") is None
    assert skills.frontmatter_source("no frontmatter") is None


def test_provenance_leaves_nested_version_and_source_keys_alone():
    preset = "---\nname: X\nmetadata:\n  version: 2\n  source: internal\nversion: 1\n---\nbody\n"

    out = skills.with_install_provenance(preset, "x", 5)

    assert "metadata:\n  version: 2\n  source: internal\n" in out
    assert out.count("\nversion: 5\n") == 1
    assert "version: 1" not in out


def test_provenance_keeps_crlf_line_endings_consistent():
    out = skills.with_install_provenance("---\r\nname: X\r\n---\r\nbody\r\n", "x", 1)

    assert "\r\n" in out
    assert out.replace("\r\n", "") .count("\n") == 0
