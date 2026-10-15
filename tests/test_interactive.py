"""Interactive menus: when they may appear, and what happens when they may not."""

from __future__ import annotations

import io
import types

import pytest

from harumi import interactive
from harumi.interactive import NonInteractiveError, Option, is_interactive, select


class _Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_interactive_only_with_a_real_terminal_on_both_ends():
    assert is_interactive(_Stream(True), _Stream(True), {})
    assert not is_interactive(_Stream(False), _Stream(True), {})  # input piped in
    assert not is_interactive(_Stream(True), _Stream(False), {})  # output piped out


@pytest.mark.parametrize(
    "environ",
    [{"CI": "true"}, {"TERM": "dumb"}, {"HARUMI_NO_INTERACTIVE": "1"}, {"HARUMI_NO_INTERACTIVE": "TRUE"}],
)
def test_interactive_off_in_ci_dumb_terminals_and_when_disabled(environ):
    assert not is_interactive(_Stream(True), _Stream(True), environ)


def test_a_stream_that_cannot_say_is_treated_as_not_a_terminal():
    class Broken:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    assert not is_interactive(Broken(), _Stream(True), {})


def test_select_without_a_terminal_fails_with_the_choices_and_the_flag(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)
    options = [Option("Acme", "org_1", hint="id org_1"), Option("Globex", "org_2")]

    with pytest.raises(NonInteractiveError) as exc:
        select("Which organization?", options, flag="--org")

    message = str(exc.value)
    assert "Which organization?" in message
    assert "Acme" in message and "Globex" in message
    assert "id org_1" in message
    assert "--org" in message


def test_select_never_touches_the_menu_library_without_a_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)

    def boom(*a, **k):
        raise AssertionError("must not prompt when there is no terminal")

    monkeypatch.setattr("questionary.select", boom)
    with pytest.raises(NonInteractiveError):
        select("Pick", [Option("a", 1)])


def test_select_returns_the_chosen_value(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)

    class Fake:
        application = types.SimpleNamespace(erase_when_done=False)

        def unsafe_ask(self):
            return "org_2"

    seen = {}

    def fake_select(message, choices, **kwargs):
        seen["choices"] = [c.value for c in choices]
        return Fake()

    monkeypatch.setattr("questionary.select", fake_select)
    value = select("Pick", [Option("Acme", "org_1"), Option("Globex", "org_2")])

    assert value == "org_2"
    assert seen["choices"] == ["org_1", "org_2"]


def test_ctrl_c_in_a_menu_returns_none_instead_of_raising(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)

    class Fake:
        application = types.SimpleNamespace(erase_when_done=False)

        def unsafe_ask(self):
            raise KeyboardInterrupt

    monkeypatch.setattr("questionary.select", lambda *a, **k: Fake())
    assert select("Pick", [Option("a", 1)]) is None


def test_select_needs_at_least_one_option():
    with pytest.raises(ValueError):
        select("Pick", [])


def test_importing_the_cli_does_not_load_the_menu_library():
    """questionary adds ~45 ms to startup; only a command that opens a menu
    should pay it. Checked in a fresh interpreter, since this process has
    already imported it."""
    import subprocess
    import sys

    code = "import sys, harumi.cli; sys.exit(1 if 'questionary' in sys.modules else 0)"
    result = subprocess.run([sys.executable, "-c", code])
    assert result.returncode == 0, "harumi.cli imported questionary at startup"


def test_long_lists_become_type_to_filter(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)
    seen = {}

    class Fake:
        application = types.SimpleNamespace(erase_when_done=False)

        def unsafe_ask(self):
            return 1

    def fake_select(message, choices, **kwargs):
        seen.update(kwargs)
        return Fake()

    monkeypatch.setattr("questionary.select", fake_select)

    select("Pick", [Option(str(i), i) for i in range(interactive.SEARCH_ABOVE + 1)])
    assert seen["use_search_filter"] is True
    assert seen["use_jk_keys"] is False  # the library forbids j/k together with filtering

    select("Pick", [Option(str(i), i) for i in range(3)])
    assert seen["use_search_filter"] is False
    assert seen["use_jk_keys"] is True


def test_filterable_rows_have_plain_string_titles(monkeypatch):
    """questionary's type-to-filter calls `title.lower()`. A styled-fragment title
    (what short lists use for the dimmed hint) crashes it, and mocks of
    `questionary.select` can't see that, so check the real filtering contract."""
    from questionary import Choice

    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)
    seen = {}

    class Fake:
        application = types.SimpleNamespace(erase_when_done=False)

        def unsafe_ask(self):
            return 0

    def fake_select(message, choices, **kwargs):
        seen["choices"] = choices
        return Fake()

    monkeypatch.setattr("questionary.select", fake_select)
    options = [Option(f"Project {i}", i, hint=f"proj-{i:02d}") for i in range(interactive.SEARCH_ABOVE + 1)]
    select("Pick", options)

    for choice in seen["choices"]:
        assert isinstance(choice, Choice)
        assert isinstance(choice.title, str)
    assert "proj-00" in seen["choices"][0].title  # the id is part of what you can filter on


def test_the_menu_erases_itself_and_leaves_one_clean_line(monkeypatch, capsys):
    """questionary redraws the chosen row as its answer, hint and alignment
    padding included, in yellow. We erase that and print our own line."""
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)
    asked = {}

    class Fake:
        application = types.SimpleNamespace(erase_when_done=False)

        def unsafe_ask(self):
            return "staging"

    def fake_select(message, choices, **kwargs):
        asked["question"] = Fake()
        return asked["question"]

    monkeypatch.setattr("questionary.select", fake_select)
    options = [
        Option("production", "production", hint="public"),
        Option("staging", "staging", hint="internal, needs the VPN"),
    ]

    value = select("Which environment?", options)

    assert value == "staging"
    assert asked["question"].application.erase_when_done is True
    out = capsys.readouterr().out
    assert "Which environment? staging" in out
    # None of the row's decoration leaks into what stays on screen.
    assert "internal" not in out and "VPN" not in out
    assert "  " not in out.replace("\n", "")  # no alignment padding


def test_a_label_with_brackets_is_printed_literally(monkeypatch, capsys):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)

    class Fake:
        application = types.SimpleNamespace(erase_when_done=False)

        def unsafe_ask(self):
            return "p1"

    monkeypatch.setattr("questionary.select", lambda *a, **k: Fake())

    select("Which project?", [Option("Routing [beta]", "p1", hint="p1")])

    assert "Routing [beta]" in capsys.readouterr().out


def test_quitting_leaves_nothing_behind(monkeypatch, capsys):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)

    class Fake:
        application = types.SimpleNamespace(erase_when_done=False)

        def unsafe_ask(self):
            raise KeyboardInterrupt

    monkeypatch.setattr("questionary.select", lambda *a, **k: Fake())

    assert select("Pick", [Option("a", 1)]) is None
    assert capsys.readouterr().out == ""


# -- "no matches" in type-to-filter ---------------------------------------------------
# These build a *real* questionary prompt, exactly as select() does, and read the
# search line it would draw. An earlier version of this hint passed tests that
# poked the control directly, yet never appeared on screen, because questionary
# binds the drawing method when the prompt is built. Only the real thing proves it.


def _real_prompt(titles):
    """A real questionary select built inside the hint's patch, plus its control."""
    import questionary
    from questionary import Choice

    with interactive._say_when_nothing_matches():
        question = questionary.select(
            "Pick", choices=[Choice(title=t, value=t) for t in titles], use_search_filter=True, use_jk_keys=False
        )
    return question


def _search_line(question) -> str:
    """What the search window of a built prompt would draw, found the way the
    library wires it: the FormattedTextControl bound to `get_search_string_tokens`."""
    from prompt_toolkit.layout.containers import Window

    def walk(container):
        yield container
        for child in getattr(container, "children", []) or []:
            yield from walk(child)
        content = getattr(container, "content", None)
        if content is not None and hasattr(content, "children"):
            yield from walk(content)
        elif content is not None and not hasattr(content, "text"):
            yield from walk(content)

    for node in walk(question.application.layout.container):
        control = getattr(node, "content", None)
        text = getattr(control, "text", None)
        owner = getattr(text, "__self__", None)
        if getattr(text, "__name__", "") in ("get_search_string_tokens", "with_note") and owner is not None:
            return "".join(t[1] for t in text() or [])
    raise AssertionError("search window not found in the prompt layout")


def _type(question, text):
    from prompt_toolkit.layout.containers import Window  # noqa: F401

    for node in _iter_controls(question):
        owner = getattr(getattr(node, "text", None), "__self__", None)
        if owner is not None and hasattr(owner, "search_filter"):
            owner.search_filter = text
            return
    raise AssertionError("control not found")


def _iter_controls(question):
    def walk(container):
        content = getattr(container, "content", None)
        if content is not None and hasattr(content, "text"):
            yield content
        for child in getattr(container, "children", []) or []:
            yield from walk(child)
        if content is not None and not hasattr(content, "text"):
            yield from walk(content)
        alt = getattr(container, "alternative_content", None)
        if alt is not None:
            yield from walk(alt)

    yield from walk(question.application.layout.container)


def test_a_filter_that_matches_something_adds_no_note():
    question = _real_prompt(["Routing", "Scheduling"])
    _type(question, "rout")
    assert "no matches" not in _search_line(question)


def test_a_filter_that_matches_nothing_says_so_in_words_on_the_real_prompt():
    question = _real_prompt(["Routing", "Scheduling"])
    _type(question, "zzz")
    line = _search_line(question)
    assert "zzz" in line
    assert "no matches" in line and "showing everything" in line


def test_the_note_ignores_case_like_the_filter_does():
    question = _real_prompt(["Routing"])
    _type(question, "ROUT")
    assert "no matches" not in _search_line(question)


def test_the_class_is_restored_afterwards_even_if_the_prompt_blows_up():
    from questionary.prompts.common import InquirerControl

    before = InquirerControl.get_search_string_tokens
    with pytest.raises(RuntimeError):
        with interactive._say_when_nothing_matches():
            assert InquirerControl.get_search_string_tokens is not before
            raise RuntimeError("boom")
    assert InquirerControl.get_search_string_tokens is before


def test_select_builds_the_prompt_inside_the_patch(monkeypatch):
    """The regression that mattered: the patch must be active while questionary
    *constructs* the prompt, not only while it runs."""
    from questionary.prompts.common import InquirerControl

    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)
    original = InquirerControl.get_search_string_tokens
    at_build = {}

    class Fake:
        application = types.SimpleNamespace(erase_when_done=False)

        def unsafe_ask(self):
            return 1

    def fake_select(*a, **k):
        at_build["patched"] = InquirerControl.get_search_string_tokens is not original
        return Fake()

    monkeypatch.setattr("questionary.select", fake_select)

    select("Pick", [Option(str(i), i) for i in range(interactive.SEARCH_ABOVE + 1)])

    assert at_build["patched"] is True
    assert InquirerControl.get_search_string_tokens is original
