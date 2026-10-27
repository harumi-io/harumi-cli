"""Help that shows how to use a command, and docs that cannot drift from the CLI.

Three guards, each shown to fail when its fix is taken out:
- an example in `--help` is parsed by the real command (so a renamed flag breaks the build, not a user);
- no help text strands a word on a line of its own;
- every `harumi ...` invocation in the README and the skills names a command and flags that exist.
"""

from __future__ import annotations

import pathlib
import re
import shlex

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from harumi import cli

try:
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def quiet(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")


def leaves():
    """(path, click command) for every visible command that takes no sub-command."""
    out = []

    def walk(command, path):
        for name, sub in getattr(command, "commands", {}).items():
            if sub.hidden:
                continue
            if hasattr(sub, "commands"):
                walk(sub, path + [name])
            else:
                out.append((" ".join(path + [name]), sub))

    walk(get_command(cli.app), [])
    return out


def split_shell(line: str) -> list[str]:
    """The words of one example, without a trailing comment, pipe or redirect."""
    line = re.sub(r"\s+#.*$", "", line)
    line = re.split(r"\s+(?:\||>|>>|2>)\s+", line)[0]
    return shlex.split(line)


def parse_with_click(line: str) -> None:
    """Have the real command parse the example: unknown flags, missing required values and bad
    choices all raise. The command body never runs."""
    root = get_command(cli.app)
    words = split_shell(line)[1:]
    ctx = root.make_context("harumi", [], resilient_parsing=True)
    command = root
    name = "harumi"
    while words and hasattr(command, "commands") and words[0] in command.commands:
        name = words[0]
        command = command.commands[words.pop(0)]
        if not hasattr(command, "commands"):
            break
    command.make_context(name, list(words), parent=ctx)


# -- examples in --help -----------------------------------------------------------------------------------------


EVERYDAY = [
    "login", "new", "import", "clone", "link", "examples", "run", "runs list", "runs get", "runs cancel", "runs download",
    "compare", "check", "files put", "files get", "files ls", "files rm", "datasources add", "datasources query",
    "schedules add", "secrets set", "memories add", "org invite", "org role", "org use", "usage",
    "dashboard validate", "skills add", "agent-skills install", "profile set", "projects rm",
]


@pytest.mark.parametrize("path", EVERYDAY)
def test_the_commands_people_reach_for_first_show_examples_in_their_help(path):
    result = runner.invoke(cli.app, [*path.split(), "--help"])

    assert result.exit_code == 0
    assert "Examples:" in result.stdout, f"`harumi {path} --help` has no example"
    assert f"harumi {path}" in result.stdout.split("Examples:")[1]


def examples():
    out = []
    for path, example, _comment in cli._iter_examples():
        out.append(pytest.param(path, example, id=example[:70]))
    return out


@pytest.mark.parametrize("path, example", examples())
def test_every_example_parses_against_the_real_command(path, example):
    assert example.startswith(f"harumi {path}"), "an example sits in the help of the command it shows"
    parse_with_click(example)


@pytest.mark.parametrize(
    "wrong",
    ["harumi runs get --jsno", "harumi datasources add wh", "harumi files put", "harumi org invite Acme"],
    ids=["unknown flag", "missing required option", "missing required argument", "missing required --email"],
)
def test_a_wrong_example_would_fail_the_parse(wrong):
    """The parse is a real check, not a rubber stamp."""
    with pytest.raises(Exception):
        parse_with_click(wrong)


def test_the_examples_in_help_are_exactly_the_examples_in_the_table():
    """What the user reads is what was tested: same lines, none lost to wrapping."""
    for path, command in leaves():
        listed = [e for p, e, _ in cli._iter_examples() if p == path]
        if not listed:
            continue
        shown = runner.invoke(cli.app, [*path.split(), "--help"]).stdout
        for line in listed:
            assert line in shown, f"{line!r} is not in the help of `harumi {path}` as printed"


# Help prose is drawn at most 80 columns wide, with one column of margin on each side, so a line longer than 78
# wraps mid-command on the most common terminal width. (A 120-column terminal is fine; 80 is what people have.)
MOST_COMMON_TERMINAL_WIDTH = 80
LONGEST_LINE_THAT_FITS = MOST_COMMON_TERMINAL_WIDTH - 2


def test_examples_fit_the_most_common_terminal_and_hold_no_markup():
    """The lines as they are printed, comment included. This once said 88 and was measured on the unwrapped string
    with COLUMNS pinned to 200, so it could not see that four examples wrapped on an 80-column terminal."""
    printed = [row for lines in cli._EXAMPLES.values() for row in cli._examples_block(lines).splitlines()[3:]]
    too_wide = [row.strip() for row in printed if len(row) > LONGEST_LINE_THAT_FITS]
    markup = [e for _, e, c in cli._iter_examples() if "[" in e or "[" in c]

    assert len(printed) > 40  # the slice above really found the example lines
    assert too_wide == [] and markup == []  # a "[" would be read by rich as styling and vanish


def test_the_examples_really_stay_on_one_line_when_drawn_at_80_columns(monkeypatch):
    """The measurement above, checked against what is drawn: render every help at 80 columns and look for an
    example that came out on two lines (the comment's tail alone on its own line)."""
    wrapped = []
    for path in cli._EXAMPLES:
        text = help_text(path.split(), MOST_COMMON_TERMINAL_WIDTH, monkeypatch)
        block = text.split("Examples:")[1].split("\n")[1:]
        for line, nxt in zip(block, block[1:]):
            if line.strip().startswith("harumi") and nxt.strip() and not nxt.strip().startswith(("harumi", "╭", "│")):
                wrapped.append((path, line.strip()[:50], nxt.strip()[:30]))

    assert wrapped == []


def test_an_example_never_shows_a_secret_on_the_command_line():
    """Secrets, tokens and passwords are prompted for; an example must not teach typing one into a shell."""
    for _, example, _ in cli._iter_examples():
        assert not re.search(r"--(password|token|secret|value)\b|glpat-|sk-", example), example


def test_a_command_with_no_example_in_the_table_is_not_a_surprise():
    """Hidden aliases and groups stay out of the table; if a name in it stops existing, this says so."""
    known = {path for path, _ in leaves()}
    unknown = sorted({p for p, _, _ in cli._iter_examples()} - known)

    assert unknown == []


def test_the_examples_block_is_a_block_and_not_reflowed_into_the_text():
    out = runner.invoke(cli.app, ["files", "put", "--help"]).stdout
    lines = [ln.strip() for ln in out.split("Examples:")[1].splitlines() if ln.strip().startswith("harumi")]

    assert len(lines) >= 2 and all(ln.startswith("harumi files put") for ln in lines)


# -- no stranded words ----------------------------------------------------------------------------------------


def help_text(args, width, monkeypatch=None):
    """The help as printed at `width` columns. Typer's help is drawn by rich, which reads COLUMNS; the
    `terminal_width=` argument of `invoke` is a click setting that rich ignores, and conftest pins COLUMNS=200,
    so a test that only passes `terminal_width` is silently always 200 wide."""
    if monkeypatch is not None:
        monkeypatch.setenv("COLUMNS", str(width))
    return runner.invoke(cli.app, [*args, "--help"]).stdout


def test_the_width_a_test_asks_for_is_the_width_it_gets(monkeypatch):
    narrow = max(len(line) for line in help_text(["compare"], 60, monkeypatch).splitlines())
    wide = max(len(line) for line in help_text(["compare"], 120, monkeypatch).splitlines())

    assert narrow <= 60 and 100 < wide <= 120


def all_visible():
    root = get_command(cli.app)

    def walk(command, path):
        for name, sub in getattr(command, "commands", {}).items():
            if sub.hidden:
                continue
            yield " ".join(path + [name]), sub
            if hasattr(sub, "commands"):
                yield from walk(sub, path + [name])

    return list(walk(root, []))


def test_no_help_paragraph_keeps_the_hard_line_breaks_of_the_source():
    """The cause of ragged help. Typer joins the single newlines of a help text's first paragraph and leaves
    every later one as written, so a body paragraph wrapped at the width of the source file is wrapped again at
    the terminal's, and on anything narrower the line ends are uneven ("...With no run ids, / at / a terminal").
    A paragraph that is a block (starts with \\b) is meant to keep its lines."""
    ragged = []
    for path, command in all_visible():
        for paragraph in (command.help or "").split("\n\n")[1:]:
            if "\n" in paragraph.strip() and not paragraph.lstrip().startswith("\b"):
                ragged.append((path, paragraph.strip().split("\n")[0][:50]))

    assert ragged == []


def test_unwrap_joins_prose_and_keeps_paragraphs_apart():
    text = "Summary line.\n\nFirst part of a\nwrapped sentence.\n\nSecond paragraph."

    assert cli._unwrap(text) == "Summary line.\n\nFirst part of a wrapped sentence.\n\nSecond paragraph."


@pytest.mark.parametrize(
    "block",
    [
        "\b\nline one\nline two",  # marked as a block
        "Steps:\n- first\n- second",  # a bullet list
        "Steps:\n* first\n* second",
        "Steps:\n1. first\n2. second",  # a numbered list
        "Run:\n    harumi run\n    harumi check",  # an indented block
    ],
    ids=["marked block", "dash list", "star list", "numbered list", "indented block"],
)
def test_unwrap_leaves_a_block_whose_line_breaks_mean_something(block):
    """Joining these would turn a list or a shell snippet into one run-on line."""
    text = "Summary.\n\n" + block

    assert cli._unwrap(text) == text


def test_unwrap_treats_a_run_of_blank_lines_like_one_paragraph_break():
    """`"" in " \\t"` is True in Python, so an empty first line once made a whole paragraph look indented."""
    text = "Summary.\n\n\nwrapped\nsentence."

    joined = cli._unwrap(text)
    assert joined.split("\n\n")[-1] == "wrapped sentence."  # joined, and no space left where the empty line was
    assert joined.split() == text.split()


def test_unwrap_is_idempotent_and_never_loses_a_word():
    text = "Summary.\n\nOne two\nthree four\nfive.\n\nSteps:\n- a\n- b"
    once = cli._unwrap(text)

    assert cli._unwrap(once) == once
    assert once.split() == text.split()


def test_a_block_that_is_meant_to_keep_its_lines_keeps_them():
    """The examples are such a block; unwrapping must not run them together."""
    out = runner.invoke(cli.app, ["runs", "list", "--help"]).stdout
    lines = [ln.strip() for ln in out.split("Examples:")[1].splitlines() if ln.strip().startswith("harumi")]

    assert len(lines) == 3 and all(ln.startswith("harumi runs list") for ln in lines)


def stranded_words(text: str, width: int):
    """Lines of prose that hold one short word although the first word of the next line would have fitted
    beside it: a break somebody wrote, not one the terminal made. (A short word is also left alone by a
    natural wrap when what follows is too long to fit; that is not a fault and is not reported.)"""
    lines = text.split("\n")
    end = next((i for i, line in enumerate(lines) if line.startswith(("╭", "Options", "Arguments"))), len(lines))
    prose = lines[:end]
    found = []
    for before, line, after in zip(prose, prose[1:], prose[2:]):
        word = line.strip()
        if re.fullmatch(r"[A-Za-z]{1,3}", word) and before.strip() and after.strip() and not before.strip().endswith(":"):
            next_word = after.split()[0]
            if len(word) + 1 + len(next_word) + 1 <= width:  # one column of margin on the left
                found.append(word)
    return found


@pytest.mark.parametrize("width", [60, 70, 80, 100])
def test_no_help_text_strands_a_word_on_a_line_of_its_own(width, monkeypatch):
    stranded = []
    for path in [""] + [p for p, _ in all_visible()]:
        for word in stranded_words(help_text(path.split(), width, monkeypatch), width):
            stranded.append((path or "harumi", word))

    assert stranded == []


def test_the_stranded_word_detector_sees_a_forced_break_and_leaves_a_natural_one():
    forced = "text one two three,\n at\n a terminal, you pick\n"
    natural = "text that runs right up to the edge of the\n or\n averyveryveryverylongwordthatdoesnotfit\n"

    assert stranded_words(forced, 80) == ["at"]
    assert stranded_words(natural, 40) == []


# -- the docs name commands and flags that exist ---------------------------------------------------------------


DOCS = [
    "README.md",
    ".agents/skills/harumi-cli/SKILL.md",
    ".agents/skills/harumi-cli/references/commands.md",
    ".agents/skills/harumi-cli/references/dashboard.md",
    ".agents/skills/harumi-cli-setup/SKILL.md",
]

# Invocations that are deliberately not real: the wrong spellings that show what the CLI says back.
DELIBERATELY_WRONG = {"harumi secrets create", "harumi version", "harumi shar"}


def all_flags(command) -> set[str]:
    flags = {"--help", "-h"}
    for param in command.params:
        flags |= set(getattr(param, "opts", [])) | set(getattr(param, "secondary_opts", []))
    return flags


def why_not_real(span: str):
    """None when the invocation names a command and flags that exist (or is shorthand), else the reason."""
    span = re.sub(r"\s+#.*$", "", span)
    span = re.split(r"\s+(?:\||>|>>)\s+", span)[0]
    if any(span.startswith(wrong) for wrong in DELIBERATELY_WRONG):
        return None
    if any(span == name or span.startswith(name + " ") for name in cli._RETIRED if " --" not in name):
        # A name that was removed, mentioned to say so ("There is no `harumi repo`"). Taken from the table that
        # answers a script that still calls it, so a typo in a live command is still caught.
        return None
    span = re.sub(r"<[^>]*>|\[[^\]]*\]", "\0", span).replace("...", "\0")
    words = span.replace("|", " ").split()
    command = get_command(cli.app)
    i = 1
    while i < len(words) and not words[i].startswith("-") and hasattr(command, "commands"):
        if "\0" in words[i]:
            return None  # `harumi <command> --help`: the command is a placeholder
        if words[i] not in command.commands:
            return f"no command {words[i]!r} under `{' '.join(words[:i])}`"
        command = command.commands[words[i]]
        i += 1
    known = all_flags(command)
    if command is get_command(cli.app):
        known |= {"--version", "-v", "--install-completion", "--show-completion"}
    for word in words[i:]:
        if word.startswith("-") and "\0" not in word and word.split("=")[0] not in known:
            return f"`{' '.join(words[:i])}` has no option {word.split('=')[0]}"
    return None


def invocations(path):
    for number, line in enumerate((ROOT / path).read_text().splitlines(), 1):
        spans = re.findall(r"`(harumi [^`]+)`", line)
        if line.strip().startswith(("harumi ", "$ harumi ")):
            spans.append(line.strip().removeprefix("$ "))
        for span in spans:
            yield number, span


@pytest.mark.parametrize("doc", DOCS)
def test_every_command_and_flag_the_docs_show_exists(doc):
    wrong = [(n, span[:70], reason) for n, span in invocations(doc) if (reason := why_not_real(span))]

    assert wrong == [], f"{doc} documents something the CLI does not have"


def test_the_drift_check_catches_a_renamed_flag_and_a_missing_command():
    assert why_not_real("harumi runs get --jsno")
    assert why_not_real("harumi pushh")  # a typo is not a removed name
    assert why_not_real("harumi push") is None and why_not_real("harumi repo ls") is None  # removed in 1.0.0, in the table
    assert why_not_real("harumi runs frobnicate")
    assert why_not_real("harumi share update ID --no-password") is None  # a secondary spelling of --password
    assert why_not_real("harumi run --watch --json | jq -e .succeeded") is None  # `-e` is jq's


def test_the_docs_are_not_empty_of_invocations():
    """A parser that finds nothing would pass every file, so it has to find a lot."""
    found = {doc: sum(1 for _ in invocations(doc)) for doc in DOCS}

    assert found[".agents/skills/harumi-cli/references/commands.md"] > 100 and found["README.md"] > 10
