"""The way out for deprecated names: warn while they work, say so in their help, and explain once they are gone.

0.11 deprecated 28 names (`push`, `repo ...`, `outputs`, the `remove`/`delete` verbs, ...) and promised their
removal in 0.12.0. Each still runs and warns. What was missing is the day after: a script that calls `harumi push`
would get a bare "No such command". `harumi env` shows the model that works (it says it was removed and what to
use), so every retired name gets the same, from the same table that drives the warnings.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from harumi import __version__, cli

try:
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()

CHANGELOG = (pathlib.Path(cli.__file__).parent / "changelog.md").read_text()

# Hidden on purpose and not deprecated: `help` is `--help` spelt as a word; `env` already is a tombstone.
NOT_DEPRECATED = {("help",), ("env",)}


@pytest.fixture(autouse=True)
def quiet(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")


def hidden_leaves(root=None):
    """(path, command) for every command that is hidden itself or sits under a hidden group."""
    root = root or get_command(cli.app)
    found = []

    def walk(command, path, hidden):
        for name, sub in getattr(command, "commands", {}).items():
            here = hidden or sub.hidden
            if hasattr(sub, "commands"):
                walk(sub, path + (name,), here)
            elif here and path + (name,) not in NOT_DEPRECATED:
                found.append((path + (name,), sub))

    walk(root, (), False)
    return found


def required_args(command):
    """Just enough words for the parser to accept the command, so it reaches its warning."""
    out = []
    for param in command.params:
        if not getattr(param, "required", False):
            continue
        if param.param_type_name == "argument":
            out.append("x")
        else:
            out += [param.opts[0], "x"]
    return out


OLD_NAMES = [pytest.param(path, id=" ".join(path)) for path, _ in hidden_leaves()]


# -- one table ----------------------------------------------------------------------------------------------------


def test_the_audit_found_the_old_names():
    assert len(OLD_NAMES) >= 28, "discovery found too few hidden commands, so every test below would prove little"


def test_every_hidden_old_name_is_in_the_retired_table_and_nothing_else_is():
    leaves = {"harumi " + " ".join(path) for path, _ in hidden_leaves()}
    commands_in_table = {key for key in cli._RETIRED if " --" not in key}  # an option, not a command (see below)
    groups_in_table = {key for key in commands_in_table if key not in leaves}

    assert sorted(leaves - commands_in_table) == [], "hidden but nothing says where it went"
    # What is left is a group-level answer (`harumi repo`): it must cover commands that are really there.
    stale = [key for key in groups_in_table if not any(leaf.startswith(key + " ") for leaf in leaves)]
    assert stale == [], "in the table but there is no such command or group: a stale entry"


def test_the_removed_output_dir_option_is_in_the_table_too():
    assert "harumi run --output-dir" in cli._RETIRED


# -- while it still works -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", OLD_NAMES)
def test_an_old_name_warns_on_stderr_and_never_on_stdout(path):
    """A script pipes stdout; a warning there would corrupt what it parses."""
    result = runner.invoke(cli.app, [*path, *required_args(dict(hidden_leaves())[path])])

    assert "deprecated:" in result.stderr and cli._REMOVAL_VERSION in result.stderr
    assert "deprecated" not in result.stdout


@pytest.mark.parametrize("path", OLD_NAMES)
def test_an_old_name_says_in_its_help_that_it_is_going_away(path):
    """25 of 29 said nothing, so `harumi push --help` read as if push were the way to do it."""
    out = " ".join(runner.invoke(cli.app, [*path, "--help"]).stdout.split())
    use = cli._RETIRED["harumi " + " ".join(path)]

    assert f"Deprecated: removed in {cli._REMOVAL_VERSION}" in out
    assert " ".join(use.split()) in out  # all of it, brackets too: `[RUN_ID]` must not be swallowed as styling


def typer_version():
    from importlib.metadata import version

    return tuple(int(p) for p in version("typer").split(".")[:2])


def test_a_replacement_is_shown_as_written_not_read_as_styling(monkeypatch):
    """Help is rich markup, so a hint with `[bold]` in it would lose the words and their brackets. (The `[RUN_ID]`
    we have today starts with a capital and would survive anyway; this is the case that would not.)"""
    import typer

    monkeypatch.setitem(cli._RETIRED, "harumi zzz", "harumi new [bold]NAME[/bold] --x")
    probe = typer.Typer()

    @probe.command("zzz", help=cli._retired_help("harumi zzz", "Body."))
    def zzz():
        pass

    @probe.command("other")
    def other():
        pass

    out = " ".join(runner.invoke(probe, ["zzz", "--help"]).stdout.split())

    # typer 0.16 prints an escaped tag with its backslash (as in test_compare_picker); 0.23 and later do not.
    # Either way the words and brackets survive, which is what escaping is for: unescaped, `[bold]` is styling
    # and the text would read "harumi new NAME --x".
    shown = "harumi new \\[bold]NAME\\[/bold] --x" if typer_version() < (0, 23) else "harumi new [bold]NAME[/bold] --x"
    assert shown in out


def test_no_real_hint_has_a_bracket_that_rich_would_read_as_styling():
    """Why the escape test above may be skipped on typer 0.16 without leaving a hole: rich reads `[x]` as a tag
    only when it starts with a letter, `/`, `@` or `#`. `[RUN_ID]` starts with a capital and `[-- <path>]` with a dash,
    so no shipped hint is changed by escaping, on any typer. Fails if someone adds one that would be."""
    tags = re.compile(r"\[[a-z/@#][^\]]*\]")
    risky = {key: use for key, use in cli._RETIRED.items() if tags.search(use)}

    assert risky == {}


def harumi_commands_in(text: str):
    """The `harumi ...` command lines inside a replacement hint. A hint may also hold git commands."""
    for piece in re.split(r"&&|/ | \(", text):
        piece = piece.strip(" )")
        if piece.startswith("harumi "):
            yield piece


def resolves(line: str):
    words = re.sub(r"<[^>]*>|\[[^\]]*\]", "", line).split()
    command = get_command(cli.app)
    i = 1
    while i < len(words) and not words[i].startswith("-") and hasattr(command, "commands"):
        if words[i] not in command.commands:
            return f"no command {words[i]!r}"
        command = command.commands[words[i]]
        i += 1
    known = {"--help"} | {o for p in command.params for o in list(getattr(p, "opts", [])) + list(getattr(p, "secondary_opts", []))}
    return next((f"no option {w}" for w in words[i:] if w.startswith("-") and w not in known), None)


@pytest.mark.parametrize("key", sorted(cli._RETIRED))
def test_every_replacement_a_warning_names_exists(key):
    problems = [(line, why) for line in harumi_commands_in(cli._RETIRED[key]) if (why := resolves(line))]

    assert problems == []


def test_a_replacement_is_never_itself_retired():
    """Pointing at a name that is also going away would send a user round in a circle."""
    retired = {key.removeprefix("harumi ") for key in cli._RETIRED}
    loops = [(key, line) for key, use in cli._RETIRED.items() for line in harumi_commands_in(use) if line.removeprefix("harumi ") in retired]

    assert loops == []


# -- the day after it is removed -----------------------------------------------------------------------------------------


def with_the_old_names_gone():
    """The command tree as 0.12.0 will have it: every hidden deprecated command taken out."""
    root = get_command(cli.app)
    for path, _ in hidden_leaves(root):
        parent = root
        for name in path[:-1]:
            parent = parent.commands[name]
        parent.commands.pop(path[-1], None)
    for name in [n for n, c in root.commands.items() if c.hidden and hasattr(c, "commands") and not c.commands]:
        root.commands.pop(name)
    return root


def what_it_says(args):
    root = with_the_old_names_gone()
    try:
        root.main(args=list(args), standalone_mode=False)
    except Exception as exc:  # click's UsageError, whichever click typer vendors
        return getattr(exc, "message", str(exc))
    raise AssertionError(f"{args} did not fail")


@pytest.mark.parametrize("path", OLD_NAMES)
def test_once_removed_an_old_name_says_what_replaced_it(path):
    key = "harumi " + " ".join(path)
    message = what_it_says(path)

    assert f"`{key}` was removed in {cli._REMOVAL_VERSION}" in message
    assert cli._RETIRED[key] in message  # the whole hint, not a guess at it


def test_a_removed_command_in_a_group_is_answered_by_that_group():
    message = what_it_says(["projects", "create", "Demo"])

    assert "`harumi projects create` was removed in " + cli._REMOVAL_VERSION in message
    assert "harumi new <NAME> --no-clone" in message
    assert "Did you mean" not in message  # a guess would be worse than the answer


def test_a_removed_top_level_command_is_answered():
    message = what_it_says(["push", "."])

    assert "`harumi push` was removed in " + cli._REMOVAL_VERSION in message and "harumi import" in message


def test_the_whole_repo_group_is_answered_by_one_line_not_twelve():
    message = what_it_says(["repo", "ls"])

    assert "`harumi repo ls` was removed" in message and "git ls-files" in message
    bare = what_it_says(["repo"])
    assert "`harumi repo` was removed in " + cli._REMOVAL_VERSION in bare and "git" in bare
    assert bare.count("\n") <= 3  # the group, once; not a list of its twelve commands


@pytest.mark.parametrize("group, hint", [("skill", "harumi agent-skills install"), ("config", "harumi org use")])
def test_a_bare_retired_group_is_answered_too(group, hint):
    message = what_it_says([group])

    assert f"`harumi {group}` was removed" in message and hint in message


def test_a_flag_after_the_name_does_not_stop_it_being_recognised():
    message = what_it_says(["projects", "create", "Demo", "--no-such-flag"])

    assert "`harumi projects create` was removed" in message


def test_a_removed_name_gets_the_answer_even_when_it_is_close_to_a_real_command():
    """`outputs` is near `runs`; a spelling guess would hide the answer."""
    message = what_it_says(["outputs"])

    assert "was removed" in message and "harumi runs download" in message


def test_a_name_that_never_existed_is_not_given_a_made_up_history():
    message = what_it_says(["frobnicate"])

    assert "was removed" not in message


def test_a_name_that_is_still_a_real_command_is_untouched():
    result = runner.invoke(cli.app, ["runs", "list", "--help"])

    assert result.exit_code == 0 and "was removed" not in result.stdout


# -- the promise is kept --------------------------------------------------------------------------------------------------


def version_tuple(text: str):
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def still_here_after(removal_version: str):
    """The old names that should already be gone if `removal_version` has been reached."""
    if version_tuple(__version__) < version_tuple(removal_version):
        return []
    return [" ".join(path) for path, _ in hidden_leaves()]


def test_the_names_promised_gone_in_the_removal_version_are_gone_by_then():
    """The warning says "removed in 0.12.0". Bumping to that version with them still here would make it a lie.
    When this fails: delete the hidden commands and the `@_deprecated` wrappers, keep their `_RETIRED` entries
    (they are what answers a user afterwards), and drop the `hidden=True` groups."""
    assert still_here_after(cli._REMOVAL_VERSION) == []


def test_the_tripwire_does_fire_when_the_version_is_reached():
    """Today it passes only because 0.11 is before 0.12. This is the same check with the removal version set to
    the one we are on, to show it would catch the names being left behind."""
    assert len(still_here_after(__version__)) >= 28


def deprecating_release() -> str:
    """The changelog entry that announced the removals (newest releases come first, so the first `### Removed`
    in the file would be the wrong one as soon as a later release has its own)."""
    for block in re.split(r"\n(?=## )", CHANGELOG):
        if re.search(r"### Removed \(still works in [\d.]+x with a warning, removed in [\d.]+\)", block):
            return block
    raise AssertionError("no changelog entry announces the removal of the old names")


def test_the_removal_version_in_the_warning_is_the_one_the_changelog_promised():
    heading = re.search(r"### Removed \(still works in [\d.]+x with a warning, removed in ([\d.]+)\)", deprecating_release())

    assert heading and heading.group(1) == cli._REMOVAL_VERSION


def test_the_changelog_lists_every_old_name_it_promised_to_remove():
    """What users read must be the set that warns: no surprise removal."""
    block = deprecating_release()
    quoted = set(re.findall(r"`([^`]+)`", block))
    group_lines = {"repo", "skill", "config"}  # `harumi repo ...` covers all twelve
    unlisted = []
    for path, _ in hidden_leaves():
        full = " ".join(path)
        listed = full in quoted or path[-1] in quoted or (path[0] in group_lines and f"harumi {path[0]} ..." in quoted)
        if not listed:
            unlisted.append(full)

    assert unlisted == []
