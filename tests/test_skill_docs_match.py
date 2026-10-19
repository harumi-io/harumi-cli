"""The agent skills tell an agent which error strings to look for. They must match.

An agent reads `.agents/skills/**` and string-matches what the CLI prints. When the
messages changed ("Not logged in. Run harumi login first." became two friendlier ones),
the skills kept the old text, so an agent matching on it would have missed the new
errors. This pins each message the skills quote to what the CLI really prints.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from harumi.errors import NotAuthenticatedError, SessionRefreshError

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL_FILES = [
    ROOT / ".agents/skills/harumi-cli/SKILL.md",
    ROOT / ".agents/skills/harumi-cli-setup/SKILL.md",
    ROOT / ".agents/skills/harumi-cli/references/commands.md",
]


def skills_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in SKILL_FILES)


def test_the_old_login_message_is_gone_from_every_skill():
    """The whole sentence, in any spelling of its second half. ("Not logged in" alone is still
    a phrase a person might type, so the setup skill lists it as a trigger.)"""
    assert not re.search(r"Not logged in\. ?Run", skills_text())
    assert "login first" not in skills_text()


def code_spans_outside_fences(text: str):
    """Inline code spans in the body: not the front matter (a description there wraps across
    lines, so a span can legitimately start on one and end on the next) and not fenced blocks."""
    lines = text.splitlines()
    start = 0
    if lines and lines[0] == "---":
        start = lines.index("---", 1) + 1
    in_fence = False
    for n, line in enumerate(lines[start:], start + 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            for span in re.findall(r"(?<!`)`([^`\n]+)`(?!`)", line):
                yield n, span


def test_no_inline_code_span_has_stray_spaces_at_its_edges():
    """Nesting a backtick pair inside one, as in `Error: ... Run `harumi login` ...`, renders
    as three pieces: a span ending in a space, loose text, and a span starting with one."""
    broken = []
    for path in SKILL_FILES:
        for n, span in code_spans_outside_fences(path.read_text(encoding="utf-8")):
            if span != span.strip():
                broken.append(f"{path.name}:{n}: `{span}`")
    assert broken == []


@pytest.mark.parametrize(
    "message",
    [
        str(NotAuthenticatedError()).split(" Run")[0],  # "You're not logged in."
        str(NotAuthenticatedError(expired=True)).split(" Run")[0],  # "Your Harumi session has expired."
        str(SessionRefreshError(403, "x")).split(":")[0],  # "Could not renew your login"
    ],
)
def test_every_message_the_skills_quote_is_what_the_cli_prints(message):
    assert message in skills_text(), f"the skills never mention what the CLI says: {message!r}"


def test_the_messages_the_cli_prints_still_start_the_way_the_skills_say():
    """The reverse: if someone rewords a message, this is where it shows up."""
    assert str(NotAuthenticatedError()).startswith("You're not logged in.")
    assert str(NotAuthenticatedError(expired=True)).startswith("Your Harumi session has expired.")
    assert str(SessionRefreshError(403, "x")).startswith("Could not renew your login")


def test_the_skills_say_no_env_line_means_production():
    assert "no `env:` means production" in skills_text()
