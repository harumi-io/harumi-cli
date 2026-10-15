"""Interactive menus, shared by every command that offers them.

The rules (so each command behaves the same way):

* A menu appears only when a real person is at a real terminal
  (`is_interactive`). Pipes, CI, `TERM=dumb` and coding agents never get one.
* Every menu has a flag or argument that does the same thing; the menu is a
  convenience on top of it, never the only way in.
* Where there is no terminal, `select` raises `NonInteractiveError`, whose
  message lists the valid choices and the flag to pass. The caller fails with
  that message; it never hangs waiting for input nobody can give.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Mapping, Optional, Sequence, TextIO, TypeVar

from harumi.errors import HarumiError

# `questionary` (and the prompt_toolkit under it) is imported inside `select`,
# never here: it costs ~45 ms, and nearly every command starts without ever
# showing a menu. `tests/test_interactive.py` pins that this module stays cheap
# to import.

T = TypeVar("T")

DISABLE_ENV = "HARUMI_NO_INTERACTIVE"
SEARCH_ABOVE = 8  # lists longer than this become type-to-filter
_TRUTHY = {"1", "true", "yes"}


class NonInteractiveError(HarumiError):
    """A choice was needed but there is no terminal to ask in."""


@dataclass(frozen=True)
class Option:
    """One row of a menu.

    `hint` is shown dimmed beside the row; use it to say which command the row
    runs, so the menu doubles as a lesson in the CLI.
    """

    label: str
    value: object
    hint: Optional[str] = None


def is_interactive(
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """Whether it is safe to show a menu and wait for a key press."""
    environ = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    if environ.get(DISABLE_ENV, "").lower() in _TRUTHY:
        return False
    if environ.get("CI"):
        return False
    if environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stdin.isatty() and stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _pointer() -> str:
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower()
    return "❯" if "utf" in encoding else ">"


def _style():
    """Matches the banner's magenta so the menu reads as part of the same CLI."""
    from questionary import Style

    return Style(
        [
            ("qmark", "fg:ansimagenta bold"),
            ("question", "bold"),
            ("pointer", "fg:ansimagenta bold"),
            ("highlighted", "fg:ansimagenta bold"),
            ("selected", "fg:ansimagenta"),
            ("instruction", "fg:ansibrightblack"),
            ("text", ""),
        ]
    )


def select(
    message: str,
    options: Sequence[Option],
    *,
    flag: Optional[str] = None,
    default: Optional[object] = None,
) -> Optional[object]:
    """Ask the user to pick one option; return its `value`, or None if they quit.

    Arrow keys (or j/k) move, Enter confirms, Ctrl-C quits quietly.

    `flag` is how to make this choice without a menu (for example `--org`). It
    goes into the error raised when there is no terminal. `default` is the value
    whose row starts highlighted.
    """
    if not options:
        raise ValueError("select() needs at least one option")

    if not is_interactive():
        raise NonInteractiveError(non_interactive_message(message, options, flag))

    import questionary
    from questionary import Choice

    # A long list is scanned by typing, not by arrowing: type to filter. The
    # library forbids combining that with j/k navigation, so long lists give up j/k.
    searchable = len(options) > SEARCH_ABOVE
    width = max(len(o.label) for o in options)
    choices = [Choice(title=_title(o, width, searchable), value=o.value) for o in options]
    start = next((c for c in choices if default is not None and c.value == default), None)
    # The "no matches" note (see `_say_when_nothing_matches`) must be in place while
    # the prompt is *built*: questionary binds the method it draws the search line
    # with at that moment, so patching after construction would change nothing.
    try:
        with _say_when_nothing_matches():
            question = questionary.select(
                message,
                choices=choices,
                qmark="",
                pointer=_pointer(),
                instruction=(
                    "(type to filter, ↑/↓ to move, enter to select, ctrl-c to quit)"
                    if searchable
                    else "(↑/↓ to move, enter to select, ctrl-c to quit)"
                ),
                use_jk_keys=not searchable,
                use_search_filter=searchable,
                use_arrow_keys=True,
                default=start,
                style=_style(),
            )
            # The library redraws the chosen row as its "answer", with the hint and
            # the padding we added for alignment, in a loud colour. Erase the whole
            # menu when it closes and leave one tidy line of our own instead.
            question.application.erase_when_done = True
            value = question.unsafe_ask()
    except KeyboardInterrupt:
        return None
    _echo_answer(message, next((o.label for o in options if o.value == value), str(value)))
    return value


def _plain_title(choice) -> str:
    title = getattr(choice, "title", "")
    return "".join(t[1] for t in title) if isinstance(title, list) else str(title)


@contextmanager
def _say_when_nothing_matches() -> Iterator[None]:
    """Make type-to-filter say so when a search finds nothing.

    questionary falls back to showing the whole list when a filter matches
    nothing, and signals it only by recolouring the typed text. With no colour
    (NO_COLOR, some palettes) that reads as "the filter did nothing". This adds
    words to that line. It leans on questionary internals (`get_search_string_tokens`,
    `found_in_search`), so if they change in a future version it quietly does
    nothing and the menu still works.
    """
    try:
        from questionary.prompts.common import InquirerControl

        original = InquirerControl.get_search_string_tokens
    except (ImportError, AttributeError):
        yield
        return

    def with_note(self):
        tokens = original(self)
        if tokens is None:
            return None  # nothing typed: no search line at all
        # Ask the filter directly rather than trusting `found_in_search`, which
        # the library updates only as a side effect of drawing the list.
        if self.search_filter and not any(
            self.search_filter.lower() in _plain_title(c).lower() for c in self.choices
        ):
            tokens = [*tokens, ("class:search_none", "  no matches - showing everything")]
        return tokens

    InquirerControl.get_search_string_tokens = with_note
    try:
        yield
    finally:
        InquirerControl.get_search_string_tokens = original


def _echo_answer(question: str, answer: str) -> None:
    """The line a menu leaves behind: what was asked and what was chosen."""
    from rich.console import Console
    from rich.markup import escape

    Console().print(f"[magenta]?[/magenta] {escape(question)} [bold magenta]{escape(answer)}[/bold magenta]")


def _title(option: Option, width: int, searchable: bool):
    """The text of one row.

    Label, then the typed-by-hand command beside it so the menu teaches the CLI.
    Normally the hint is dimmed, which needs styled fragments; but type-to-filter
    matches against the title as a plain string and crashes on fragments, so
    filterable lists use plain text (which also lets you filter by the hint, e.g.
    a project id).
    """
    if searchable:
        return f"{option.label.ljust(width)}   {option.hint}" if option.hint else option.label
    return [("", option.label.ljust(width)), ("fg:ansibrightblack", f"   {option.hint}" if option.hint else "")]


def non_interactive_message(message: str, options: Sequence[Option], flag: Optional[str]) -> str:
    """What to tell someone (or some agent) who can't be shown a menu."""
    lines = [f"{message} Cannot ask here: no interactive terminal."]
    lines.append("Choose one of:")
    lines.extend(f"  {o.label}" + (f"  ({o.hint})" if o.hint else "") for o in options)
    if flag:
        lines.append(f"Pass it with {flag}.")
    return "\n".join(lines)
