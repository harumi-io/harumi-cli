"""Seed the bundled Harumi skills (`harumi-cli`, `harumi-cli-setup`) onto
local coding agents, so a `pip`/`pipx`/`uv` install of the CLI can also hand
the agent the docs that drive it.

Source of truth for skill content is `.agents/skills/` in this repo; the
wheel bundles a verbatim copy at `harumi/_skills` via the `force-include` in
pyproject.toml, so there is nothing to keep in sync by hand here (the
separate GitHub-hosted `harumi-io/harumi-skills` distribution — for
`npx skills add`, Claude Code, and Cursor plugin installs — is kept in sync
by CI on release instead; see release.yml).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_SKILLS_DIR = ".agents/skills"


@dataclass(frozen=True)
class Agent:
    key: str
    label: str
    global_dir: Path


AGENTS: list[Agent] = [
    Agent("cursor", "Cursor", Path.home() / ".cursor" / "skills"),
    Agent("claude-code", "Claude Code", Path.home() / ".claude" / "skills"),
    Agent("codex", "Codex", Path.home() / ".codex" / "skills"),
    Agent("universal", "Universal (~/.agents/skills)", Path.home() / ".agents" / "skills"),
]
AGENT_KEYS = [a.key for a in AGENTS]


def _bundled_skills_root() -> Path:
    """Directory containing the shipped skill folders.

    Normal installs (pip/pipx/uv): `harumi/_skills`, populated by the wheel's
    `force-include`. Editable installs (`pip install -e .`) don't run that
    build step, so fall back to the repo's own `.agents/skills/`.
    """
    bundled = Path(__file__).parent / "_skills"
    if bundled.is_dir():
        return bundled
    repo_fallback = Path(__file__).resolve().parents[2] / ".agents" / "skills"
    if repo_fallback.is_dir():
        return repo_fallback
    raise FileNotFoundError(
        "Bundled skill docs not found. Reinstall `harumi`, or run this from "
        "a harumi-cli checkout that has .agents/skills/."
    )


def bundled_skill_dirs() -> list[Path]:
    """Every skill folder shipped with this install (e.g. harumi-cli,
    harumi-cli-setup), sorted by name."""
    root = _bundled_skills_root()
    dirs = sorted((p for p in root.iterdir() if p.is_dir() and (p / "SKILL.md").exists()), key=lambda p: p.name)
    if not dirs:
        raise FileNotFoundError(f"No skills found under {root}")
    return dirs


def detect_agents() -> list[Agent]:
    """Agents whose config directory already exists on this machine.

    ponytail: presence of `~/.cursor`, `~/.claude`, `~/.codex` is a
    heuristic, not a real "is this agent installed" check — it misses an
    agent installed under a non-default HOME, and can't detect one that has
    never been run. Ceiling: this is a directory-existence guess, nothing
    more. Upgrade path: delegate real per-agent detection to `npx skills`
    (it already does this across ~75 agents) instead of growing this list.
    """
    return [a for a in AGENTS if a.key != "universal" and a.global_dir.parent.is_dir()]


def agents_missing_skill() -> list[Agent]:
    """Detected agents (see `detect_agents`) that don't have the `harumi-cli`
    skill in their global skills directory yet.

    Powers a first-run nudge toward `harumi skill install` (see
    `cli._print_skill_install_hint`). `pip`/`pipx`/`uv` have no post-install
    hook to seed the skill automatically during the install itself, so the
    CLI's own next bare invocation is the closest automatic moment — and it
    only *recommends* running the command rather than writing files
    unprompted, since `install()` already treats seeding an agent's config
    directory as something the user should ask for.
    """
    return [a for a in detect_agents() if not (a.global_dir / "harumi-cli" / "SKILL.md").exists()]


def install(
    *,
    agent_keys: Optional[list[str]] = None,
    project: bool = False,
    dry_run: bool = False,
    force: bool = False,
    cwd: Optional[Path] = None,
) -> list[Path]:
    """Copy every bundled skill folder into the chosen install targets.

    Returns the list of skill directories written (or that would be written,
    under --dry-run). Re-running is idempotent — it overwrites in place.
    """
    skill_dirs = bundled_skill_dirs()

    if project:
        target_dirs = [(cwd or Path.cwd()) / PROJECT_SKILLS_DIR]
    else:
        if agent_keys:
            unknown = set(agent_keys) - set(AGENT_KEYS)
            if unknown:
                raise ValueError(
                    f"Unknown agent(s): {', '.join(sorted(unknown))}. "
                    f"Known: {', '.join(AGENT_KEYS)}."
                )
            chosen = [a for a in AGENTS if a.key in agent_keys]
        else:
            chosen = detect_agents() or [a for a in AGENTS if a.key == "universal"]
        target_dirs = [a.global_dir for a in chosen]

    written: list[Path] = []
    for target_dir in target_dirs:
        for skill_dir in skill_dirs:
            dest = target_dir / skill_dir.name
            written.append(dest)
            if dry_run:
                continue
            if dest.exists() and not (dest / "SKILL.md").exists() and not force:
                raise FileExistsError(
                    f"{dest} already exists and doesn't look like a skill "
                    "(no SKILL.md). Pass --force to overwrite it."
                )
            if dest.is_symlink() or dest.is_file():
                dest.unlink()
            elif dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill_dir, dest)
    return written
