"""The way out for deprecated names: warn while they work, say so in their help, and explain once they are gone.

0.11 deprecated 28 names (`push`, `repo ...`, `outputs`, the `remove`/`delete` verbs, ...) and promised their
removal in 1.0.0, which removed them. A script that still calls `harumi push` is told it was removed and what to
use, from the same table (`_RETIRED`) that drove the warnings, instead of a bare "No such command". `harumi env`
was the model. `blueprints` and `new --blueprint` were deprecated in 1.0.0 and are removed in 1.1.0: until then
they run and warn, which is what the "while it still works" tests below check.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from harumi import __version__, cli

try:
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()

CHANGELOG = (pathlib.Path(cli.__file__).parent / "changelog.md").read_text()

# Hidden on purpose and not deprecated: `help` is `--help` spelt as a word; `env` already is a tombstone; `git-credential`
# is the helper git runs for the project's remote.
NOT_DEPRECATED = {("help",), ("env",), ("git-credential",)}  # git-credential is run by git, not by people


@pytest.fixture(autouse=True)
def quiet(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")


def version_tuple(text: str):
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def due_keys(release: str):
    """Every entry in `_RETIRED` (commands, groups and options) that `release` has removed, or removes."""
    return [key for key in cli._RETIRED if version_tuple(cli._removal_version(key)) <= version_tuple(release)]


def removed_names(by=None):
    """The paths of the retired *commands* (not options) that release `by` removes: ("push",), ("repo", "ls"), ("repo",).
    Taken from the table, because the commands themselves are deleted: there is nothing left to scan for."""
    return [tuple(key.split()[1:]) for key in due_keys(by or cli._REMOVAL_VERSION) if " --" not in key]


def exists_in_tree(path) -> bool:
    command = get_command(cli.app)
    for name in path:
        if name not in getattr(command, "commands", {}):
            return False
        command = command.commands[name]
    return True


def due_by(path, release: str) -> bool:
    """Is the old name at `path` removed by `release`? Most go at the default removal version; a few, deprecated later
    (`blueprints`), go at their own, and are still here after the release that renamed them."""
    return version_tuple(cli._removal_version("harumi " + " ".join(path))) <= version_tuple(release)


def hidden_leaves(root=None, by=None):
    """(path, command) for every command that is hidden itself or sits under a hidden group. With `by`, only the
    ones the release `by` removes."""
    root = root or get_command(cli.app)
    found = []

    def walk(command, path, hidden):
        for name, sub in getattr(command, "commands", {}).items():
            here = hidden or sub.hidden
            if hasattr(sub, "commands"):
                walk(sub, path + (name,), here)
            elif here and path + (name,) not in NOT_DEPRECATED and (by is None or due_by(path + (name,), by)):
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


OLD_NAMES = [pytest.param(path, id=" ".join(path)) for path, _ in hidden_leaves()]  # still registered: deprecated, not yet removed
REMOVED_IN_THIS_RELEASE = [pytest.param(path, id=" ".join(path)) for path in removed_names()]


# -- one table ----------------------------------------------------------------------------------------------------


def test_the_audit_found_the_old_names():
    assert len(REMOVED_IN_THIS_RELEASE) >= 28, "the table lost names, so every test below would prove little"
    assert [p.id for p in OLD_NAMES] == ["blueprints"], "the one deprecated name left; it goes in 1.1.0"


def test_every_hidden_old_name_is_in_the_retired_table_and_the_removed_ones_are_really_gone():
    leaves = {"harumi " + " ".join(path) for path, _ in hidden_leaves()}
    commands_in_table = {key for key in cli._RETIRED if " --" not in key}  # an option, not a command (see below)
    removed = {"harumi " + " ".join(path) for path in removed_names()}

    assert sorted(leaves - commands_in_table) == [], "hidden but nothing says where it went"
    assert leaves.isdisjoint(removed), "removed in 1.0.0 but still registered as a command"
    still_registered = [key for key in removed if exists_in_tree(tuple(key.split()[1:]))]
    assert still_registered == [], "in the table as removed, but the command (or group) is still there"


def test_the_removed_output_dir_option_is_in_the_table_too():
    assert "harumi run --output-dir" in cli._RETIRED


# -- while it still works -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", OLD_NAMES)
def test_an_old_name_warns_on_stderr_and_never_on_stdout(path):
    """A script pipes stdout; a warning there would corrupt what it parses."""
    result = runner.invoke(cli.app, [*path, *required_args(dict(hidden_leaves())[path])])

    assert "deprecated:" in result.stderr and cli._removal_version("harumi " + " ".join(path)) in result.stderr
    assert "deprecated" not in result.stdout


@pytest.mark.parametrize("path", OLD_NAMES)
def test_an_old_name_says_in_its_help_that_it_is_going_away(path):
    """25 of 29 said nothing, so `harumi push --help` read as if push were the way to do it."""
    out = " ".join(runner.invoke(cli.app, [*path, "--help"]).stdout.split())
    key = "harumi " + " ".join(path)
    use = cli._RETIRED[key]

    assert f"Deprecated: removed in {cli._removal_version(key)}" in out
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


def with_the_old_names_gone(release=None):
    """The command tree as `release` (default: 1.0.0) will have it: every hidden command that release removes
    taken out. The ones renamed in 1.0.0 itself (`blueprints`) are still there, warning, until 1.1.0."""
    root = get_command(cli.app)
    for path, _ in hidden_leaves(root, by=release or cli._REMOVAL_VERSION):
        parent = root
        for name in path[:-1]:
            parent = parent.commands[name]
        parent.commands.pop(path[-1], None)
    for name in [n for n, c in root.commands.items() if c.hidden and hasattr(c, "commands") and not c.commands]:
        root.commands.pop(name)
    return root


def what_it_says(args, release=None):
    root = with_the_old_names_gone(release)
    try:
        root.main(args=list(args), standalone_mode=False)
    except Exception as exc:  # click's UsageError, whichever click typer vendors
        return getattr(exc, "message", str(exc))
    raise AssertionError(f"{args} did not fail")


@pytest.mark.parametrize("path", REMOVED_IN_THIS_RELEASE)
def test_once_removed_an_old_name_says_what_replaced_it(path):
    key = "harumi " + " ".join(path)
    message = what_it_says(path)

    assert f"`{key}` was removed in {cli._removal_version(key)}" in message
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


def still_here_after(removal_version: str):
    """The old names that should already be gone if `removal_version` has been reached."""
    if version_tuple(__version__) < version_tuple(removal_version):
        return []
    return [" ".join(path) for path, _ in hidden_leaves(by=removal_version)]


def test_a_name_renamed_in_this_release_is_still_there_after_it():
    """`blueprints` is deprecated in 1.0.0 and removed in 1.1.0. A tree built for 1.0.0 must still have it,
    working and warning, or its users would meet the removal in the same release as the warning."""
    root = with_the_old_names_gone()

    assert "blueprints" in root.commands and root.commands["blueprints"].hidden
    assert cli._removal_version("harumi blueprints") == "1.1.0" != cli._REMOVAL_VERSION


def test_once_0_13_removes_blueprints_it_says_0_13_and_what_replaced_it():
    """The answer a script gets the day after, built for the release that removes it. Tested with the version a
    name is due at, not the default one, or a tombstone quoting the wrong release would pass."""
    message = what_it_says(["blueprints"], release="1.1.0")

    assert "`harumi blueprints` was removed in 1.1.0" in message and "harumi examples" in message
    assert "1.0.0" not in message


def test_in_the_0_12_tree_blueprints_still_works_rather_than_being_a_tombstone():
    root = with_the_old_names_gone()

    assert "blueprints" in root.commands


def test_every_name_removed_later_is_deprecated_in_an_earlier_release_than_it_is_removed_in():
    """A name cannot be deprecated and removed by the same release. Anything in `_REMOVED_IN` must be later than the
    default (those were announced in 0.11), and later than the version now being built (nobody saw a warning)."""
    for key, version in cli._REMOVED_IN.items():
        assert version_tuple(version) > version_tuple(cli._REMOVAL_VERSION), key
        assert key in cli._RETIRED, f"{key} has a removal version but is not in _RETIRED"


def test_the_names_promised_gone_in_the_removal_version_are_gone_by_then():
    """The warning says "removed in 1.0.0". Bumping to that version with them still here would make it a lie.
    When this fails: delete the hidden commands and the `@_deprecated` wrappers, keep their `_RETIRED` entries
    (they are what answers a user afterwards), and drop the `hidden=True` groups."""
    assert still_here_after(cli._REMOVAL_VERSION) == []


def test_the_tripwire_does_fire_when_the_version_is_reached(monkeypatch):
    """Today it passes only because 1.0 is before 1.1. The same check, run as if this were the release that
    removes `blueprints` (the installed version set to 1.1.0), must report it left behind."""
    monkeypatch.setattr(sys.modules[__name__], "__version__", "1.1.0")

    assert still_here_after("1.1.0") == ["blueprints"]


def release_notes(version: str):
    """The parsed changelog entry for `version`, or None."""
    from harumi import changelog

    return next((r for r in changelog.load() if r.version == version), None)


def release_gaps(removal_version: str, current_version: str):
    """What the release notes still lack once `current_version` has reached `removal_version`.

    The 1.0.0 notes are drafted ahead of the release without a date and without the removed names (they are
    still here, deprecated, until the release commit deletes them). Bumping the version is the moment both
    must be filled in; an empty list before that, so the draft can sit in `dev` meanwhile.
    """
    if version_tuple(current_version) < version_tuple(removal_version):
        return []
    entry = release_notes(removal_version)
    if entry is None:
        return [f"no changelog entry for {removal_version}"]
    gaps = [] if entry.date else [f"the {removal_version} heading has no date"]
    removed = entry.notes.split("### Removed")[-1] if "### Removed" in entry.notes else ""
    spans = re.findall(r"`([^`]+)`", removed)
    quoted = {normalised_name(span) for span in spans}
    # Only a span written with a trailing "..." is a group (`harumi repo ...`). A stray `projects` in the prose is
    # not: it would otherwise stand in for every name that starts with it.
    groups = {normalised_name(span) for span in spans if span.strip().endswith("...")}
    for key in due_keys(removal_version):
        name = normalised_name(key)
        # The full name, or the group it belongs to. A bare last word is not enough: `delete` is the end of two
        # names, so one mention would stand in for both.
        if name not in quoted and not any(name.startswith(group + " ") for group in groups):
            gaps.append(f"the notes do not list `{name}` as removed")
    return gaps


def normalised_name(text: str) -> str:
    """`harumi repo ...`, `repo ...` and `repo` are the same group; `harumi push` and `push` the same command."""
    return re.sub(r"\s+\.\.\.$", "", text.strip()).removeprefix("harumi ").strip()


def test_the_release_notes_are_complete_by_the_time_the_version_is_bumped():
    """When this fails: give the 1.0.0 heading its date, and list every name in `_RETIRED` under its `### Removed`
    (write `harumi repo ...` once for the group)."""
    assert release_gaps(cli._REMOVAL_VERSION, __version__) == []


def test_the_release_check_does_fire_when_the_notes_are_left_as_drafted(monkeypatch):
    """The check must be able to fail: notes drafted ahead of a release (no date, none of the removed names listed)
    are reported on both counts. Built here rather than read from the changelog, which is complete by now."""
    from harumi.changelog import Release

    drafted = Release(cli._REMOVAL_VERSION, None, "### Removed\n\n- `harumi env`.\n")
    monkeypatch.setattr(sys.modules[__name__], "release_notes", lambda version: drafted)

    gaps = release_gaps(cli._REMOVAL_VERSION, cli._REMOVAL_VERSION)

    assert any("no date" in g for g in gaps)
    assert sum("do not list" in g for g in gaps) == len(due_keys(cli._REMOVAL_VERSION))  # none of them, whatever the table grows to


def test_a_complete_release_entry_satisfies_the_check(monkeypatch):
    """So the release step can go green: a dated entry that lists every retired name passes."""
    from harumi.changelog import Release

    names = ", ".join(f"`{key.removeprefix('harumi ')}`" for key in due_keys(cli._REMOVAL_VERSION))
    complete = Release(cli._REMOVAL_VERSION, "2026-11-01", f"### Removed\n\n- {names}\n")
    monkeypatch.setattr(sys.modules[__name__], "release_notes", lambda version: complete)

    assert release_gaps(cli._REMOVAL_VERSION, cli._REMOVAL_VERSION) == []


def all_flags_anywhere():
    found = {"--version", "-v", "-h", "--help", "--install-completion", "--show-completion"}

    def walk(command):
        for param in command.params:
            found.update(getattr(param, "opts", []))
            found.update(getattr(param, "secondary_opts", []))
        for sub in getattr(command, "commands", {}).values():
            walk(sub)

    walk(get_command(cli.app))
    return found


def unresolved_in_notes(notes: str):
    """Every `harumi ...` command and every `--flag` quoted in release notes, that the CLI does not have."""
    root = get_command(cli.app)
    flags = all_flags_anywhere()
    bad = []
    for span in re.findall(r"`([^`]+)`", notes):
        words = re.sub(r"\s+\.\.\.|<[^>]*>", "", span).split()
        if not words:
            continue
        named = " ".join(words)
        if any(named == key or named.startswith(key + " ") for key in cli._RETIRED if " --" not in key):
            continue  # a removed name, quoted to say it was removed: it is meant not to exist
        if words[0] == "harumi":
            command, i = root, 1
            while i < len(words) and not words[i].startswith("-") and hasattr(command, "commands"):
                if words[i] not in command.commands:
                    bad.append((span, f"no command {words[i]!r}"))
                    break
                command = command.commands[words[i]]
                i += 1
            else:
                known = {o for p in command.params for o in list(getattr(p, "opts", [])) + list(getattr(p, "secondary_opts", []))}
                known |= {"--help", "-h"} | ({"--version", "-v", "--install-completion"} if command is root else set())
                bad += [(span, f"no option {w}") for w in words[i:] if w.startswith("-") and w not in known]
        else:
            # A command quoted without the program name (`share add|update --comparisons`) or a bare flag: the
            # flags in it must exist somewhere, which is what a reader can check.
            bad += [(span, f"no flag {w!r} anywhere") for w in words if w.startswith("-") and w.split("=")[0] not in flags]
    return bad


def test_the_draft_names_only_commands_and_flags_that_exist():
    """Release notes are read by people deciding whether to upgrade. A flag that was renamed before release, or a
    command that never shipped, would send them looking for it."""
    notes = release_notes(cli._REMOVAL_VERSION).notes

    assert unresolved_in_notes(notes) == []
    assert len(re.findall(r"`harumi ", notes)) >= 8  # the scan found the commands, so the line above says something


@pytest.mark.parametrize(
    "wrong",
    [
        "`harumi begin` starts it",
        "pass `--comparisonz`",
        "`share add|update --comparisonz`",
        "`harumi runs frobnicate`",
        "`harumi run --no-such-flag`",
    ],
    ids=["unknown command", "unknown bare flag", "unknown flag after a command without its program name", "unknown subcommand", "unknown flag on a real command"],
)
def test_the_notes_check_catches_a_wrong_command_or_flag(wrong):
    assert unresolved_in_notes(wrong) != []


@pytest.fixture
def gaps_for(monkeypatch):
    """A function giving the names the release notes fail to list, for a given "Removed" section."""
    from harumi.changelog import Release

    def check(removed_text: str):
        entry = Release(cli._REMOVAL_VERSION, "2026-11-01", "### Removed\n\n" + removed_text)
        monkeypatch.setattr(sys.modules[__name__], "release_notes", lambda version: entry)
        return [g for g in release_gaps(cli._REMOVAL_VERSION, cli._REMOVAL_VERSION) if "do not list" in g]

    return check


def test_names_written_with_the_program_name_count_as_listed(gaps_for):
    """The draft already writes `harumi env`; a release author who writes `harumi push` must not be told it is
    missing and then reword correct notes to satisfy a test."""
    every_name = ", ".join(f"`{key}`" for key in due_keys(cli._REMOVAL_VERSION))

    assert gaps_for(every_name) == []


def test_names_written_without_the_program_name_count_as_listed(gaps_for):
    every_name = ", ".join(f"`{key.removeprefix('harumi ')}`" for key in due_keys(cli._REMOVAL_VERSION))

    assert gaps_for(every_name) == []


def test_a_group_written_once_covers_its_commands(gaps_for):
    leaves = [key for key in due_keys(cli._REMOVAL_VERSION) if key.startswith("harumi repo ")]
    others = ", ".join(f"`{key}`" for key in due_keys(cli._REMOVAL_VERSION) if not key.startswith("harumi repo"))

    assert len(leaves) >= 10 and gaps_for(others + ", `harumi repo ...`") == []
    assert len(gaps_for(others)) == len(leaves) + 1  # without it, the group and each of its commands are missing


def test_one_shared_last_word_does_not_stand_in_for_every_name_that_ends_with_it(gaps_for):
    """`delete` ends `projects delete` and `org delete`; listing the word once must not satisfy both."""
    missing = gaps_for("`delete`, `remove`, `create`, `list`")

    assert len(missing) == len(due_keys(cli._REMOVAL_VERSION)), "a bare word counted as listing a name"


def test_a_stray_word_in_the_prose_is_not_a_group(gaps_for):
    """`projects` and `run` mentioned in passing must not stand in for `projects create`, `projects delete` and
    `run --output-dir`."""
    listed = ", ".join(f"`{key}`" for key in due_keys(cli._REMOVAL_VERSION) if not key.startswith(("harumi projects", "harumi run")))
    missing = gaps_for(listed + ", and `projects` and `run` were touched")

    assert sorted(missing) == [
        "the notes do not list `projects create` as removed",
        "the notes do not list `projects delete` as removed",
        "the notes do not list `run --output-dir` as removed",
    ]


def test_the_draft_notes_exist_ahead_of_the_release_and_are_newest_first():
    from harumi import changelog

    versions = [r.version for r in changelog.load()]
    assert versions[0] == cli._REMOVAL_VERSION and __version__ in versions


def deprecating_release() -> str:
    """The changelog entry that announced the removals (newest releases come first, so the first `### Removed`
    in the file would be the wrong one as soon as a later release has its own)."""
    for block in re.split(r"\n(?=## )", CHANGELOG):
        if re.search(r"### Removed \(still works in [\d.]+x with a warning, removed in [\d.]+\)", block):
            return block
    raise AssertionError("no changelog entry announces the removal of the old names")


def test_the_names_are_removed_no_earlier_than_the_changelog_promised():
    """0.11 promised "removed in 0.12.0" and that text cannot be edited now. Removing later (it shipped as 1.0.0) keeps
    the promise; removing earlier would break it, so the check is "not before", not "equal"."""
    heading = re.search(r"### Removed \(still works in [\d.]+x with a warning, removed in ([\d.]+)\)", deprecating_release())

    assert heading and version_tuple(cli._REMOVAL_VERSION) >= version_tuple(heading.group(1))


def test_the_changelog_lists_every_old_name_it_promised_to_remove():
    """What users read must be the set that warns: no surprise removal."""
    block = deprecating_release()
    quoted = set(re.findall(r"`([^`]+)`", block))
    group_lines = {"repo", "skill", "config"}  # `harumi repo ...` covers all twelve
    unlisted = []
    # The names that release promised to remove, not the ones renamed since (those are announced in their own).
    for path, _ in hidden_leaves(by=cli._REMOVAL_VERSION):
        full = " ".join(path)
        listed = full in quoted or path[-1] in quoted or (path[0] in group_lines and f"harumi {path[0]} ..." in quoted)
        if not listed:
            unlisted.append(full)

    assert unlisted == []
