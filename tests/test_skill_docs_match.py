"""The agent skills tell an agent which error strings to look for. They must match.

An agent reads `.agents/skills/**` and string-matches what the CLI prints. When the
messages changed ("Not logged in. Run harumi login first." became two friendlier ones),
the skills kept the old text, so an agent matching on it would have missed the new
errors. This pins each message the skills quote to what the CLI really prints.
"""

from __future__ import annotations

import pathlib

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
    assert "Not logged in. Run harumi login first." not in skills_text()
    assert "Not logged in. Run `harumi login` first." not in skills_text()


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
