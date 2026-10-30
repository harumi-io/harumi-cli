"""Command grammar: the words people guess, the help they ask for, and the completion they press TAB for.

The verb vocabulary is mostly regular (`list`, `get`, `add`, `rm`, `update`), with four irregular groups
(`files ls/put`, `org create`, `memories edit`, `secrets/profile set`). Renaming them would need a
deprecation window, so a wrong guess is answered instead: with the command that exists, and the list.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from harumi import cli

try:  # click 8.1 mixes stderr into the output unless told not to; 8.2 removed the switch
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()


@pytest.fixture(autouse=True)
def quiet(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr(cli.console, "_width", 100)
    monkeypatch.setattr(cli.console, "_height", 50)
    monkeypatch.setattr(cli.err_console, "_width", 100)
    monkeypatch.setattr(cli.err_console, "_height", 50)


def plain(text: str) -> str:
    return " ".join(text.split())


def said(args) -> tuple[int, str]:
    result = runner.invoke(cli.app, args)
    return result.exit_code, plain(result.stdout + result.stderr)


# -- -h -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("args", [["-h"], ["projects", "-h"], ["projects", "rm", "-h"], ["share", "update", "-h"], ["org", "role", "-h"]])
def test_dash_h_asks_for_help_everywhere(args):
    code, out = said(args)

    assert code == 0 and "Usage:" in out and "No such option" not in out


def test_dash_h_does_not_steal_a_flag_that_means_something_else():
    """No command may use -h for anything but help; if one ever does, this fails and says which."""
    clashes: list[str] = []

    def walk(command, path):
        for name, sub in getattr(command, "commands", {}).items():
            if hasattr(sub, "commands"):
                walk(sub, path + [name])
            elif any("-h" in getattr(p, "opts", []) for p in sub.params):
                clashes.append(" ".join(path + [name]))

    walk(get_command(cli.app), [])
    assert clashes == []


# -- wrong verbs ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, suggestion",
    [
        (["secrets", "create"], "set"),
        (["secrets", "add"], "set"),
        (["memories", "update"], "edit"),
        (["memories", "change"], "edit"),
        (["files", "list"], "ls"),
        (["files", "add"], "put"),
        (["files", "upload"], "put"),
        (["files", "download"], "get"),
        (["files", "delete"], "rm"),
        (["share", "show"], "get"),
        (["runs", "show"], "get"),
        (["runs", "stop"], "cancel"),
        (["skills", "list"], "library"),
        (["skills", "install"], "add"),
        (["datasources", "show"], "get"),
    ],
)
def test_a_wrong_verb_is_answered_with_the_command_that_exists(args, suggestion):
    code, out = said(args)

    assert code == 2 and "No such command" in out
    assert f"Did you mean '{suggestion}'" in out


def test_org_add_is_ambiguous_so_it_names_both_meanings():
    code, out = said(["org", "add"])

    assert code == 2 and "'create'" in out and "'invite'" in out


def test_the_error_lists_the_commands_that_do_exist():
    code, out = said(["secrets", "frobnicate"])

    assert code == 2 and "Commands:" in out and all(word in out for word in ("list", "set", "rm"))


def test_the_list_of_commands_does_not_offer_names_that_were_removed():
    code, out = said(["projects", "frobnicate"])

    listed = out.split("Commands:")[1]
    assert "rm" in listed and "delete" not in listed and "create" not in listed  # both were removed in 1.0.0


@pytest.mark.parametrize(
    "args, answer",
    [
        (["projects", "delete", "p1"], "harumi projects rm"),
        (["org", "delete", "o1"], "harumi org rm"),
        (["schedules", "remove", "s1"], "harumi schedules rm"),
    ],
)
def test_a_removed_verb_is_answered_not_guessed_at(args, answer):
    """`delete` is close to nothing real, so a spelling guess would be wrong; the removal answer names the verb."""
    code, out = said(args)

    assert code == 2 and "was removed in 1.0.0" in out and answer in out
    assert "Did you mean" not in out


def test_blueprints_is_answered_like_every_other_removed_name():
    """Removed in 1.1.0, after a release of warnings: it gets the removal answer, not a spelling guess."""
    code, out = said(["blueprints"])

    assert code == 2 and "was removed in 1.1.0" in out and "harumi examples" in out
    assert "Did you mean" not in out


def test_a_close_typo_still_gets_the_spelling_suggestion_and_not_a_wrong_synonym():
    code, out = said(["projects", "lst"])

    assert "Did you mean 'list'" in out


def test_asking_for_the_version_the_word_way_points_at_the_flag():
    code, out = said(["version"])

    assert code == 2 and "--version" in out


def test_dash_v_is_the_version_too():
    result = runner.invoke(cli.app, ["-v"])

    assert result.exit_code == 0 and result.stdout.strip().split()[-1] == cli.__version__


def test_a_top_level_typo_is_unchanged():
    code, out = said(["shar"])

    assert code == 2 and "Did you mean" in out and "share" in out


def test_a_wrong_verb_never_runs_anything(monkeypatch):
    """A hint is not an alias: `secrets create X` must not create a secret by guessing."""
    called = []
    monkeypatch.setattr(cli, "_get_client", lambda *a, **k: called.append("client") or (_ for _ in ()).throw(AssertionError("ran")))

    code, _ = said(["secrets", "create", "API_KEY"])

    assert code == 2 and called == []


# -- the help list ----------------------------------------------------------------------------------------------


def test_the_command_list_is_grouped_not_one_flat_list():
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    for panel in ("Get started", "Run and results", "Project", "Account and team"):
        assert f"─ {panel} ─" in result.stdout, panel


def test_every_visible_command_is_in_a_named_group():
    """Typer's "no panel" is a placeholder object, not None, so a command left out of `_PANELS` would be listed
    under a default "Commands" heading; this makes that an error instead of an untidy help."""
    root = get_command(cli.app)
    ungrouped = [
        name for name, cmd in root.commands.items()
        if not cmd.hidden and not isinstance(getattr(cmd, "rich_help_panel", None), str)
    ]

    assert ungrouped == []


def test_the_panel_table_names_only_commands_that_exist():
    root = get_command(cli.app)
    named = [name for names in cli._PANELS.values() for name in names]

    assert sorted(set(named) - set(root.commands)) == [], "a typo in _PANELS silently groups nothing"
    assert len(named) == len(set(named)), "a command is listed under two groups"


def test_the_visible_commands_are_exactly_the_ones_in_the_panel_table():
    root = get_command(cli.app)
    visible = {name for name, cmd in root.commands.items() if not cmd.hidden}

    assert visible == {name for names in cli._PANELS.values() for name in names}


def test_what_a_new_person_needs_comes_before_everything_else():
    out = runner.invoke(cli.app, ["--help"]).stdout
    listed = [line.split()[1] for line in out.splitlines() if line.startswith("│ ") and len(line.split()) > 1 and line.split()[1].isidentifier() or line.startswith("│ whats")]

    first = out.index("Get started")
    assert first < out.index("Run and results") < out.index("Project ─") < out.index("Account and team")
    commands = out[first:]
    assert commands.index("│ login") < commands.index("│ start")  # signing in is where it starts
    assert listed  # the parse found commands, so the order above is about something


def test_each_summary_is_one_sentence_that_fits_a_line():
    root = get_command(cli.app)
    long_ones = []
    for name, cmd in root.commands.items():
        if cmd.hidden:
            continue
        first = (cmd.help or "").strip().split("\n\n")[0]
        if "\n" in first or len(first) > 100:
            long_ones.append((name, len(first)))

    assert long_ones == []


# -- the options nobody needs every day ---------------------------------------------------------------------------


def test_api_url_and_org_are_in_an_advanced_panel_not_the_everyday_options():
    _, out = said(["projects", "rm", "--help"])

    everyday, advanced = out.split("Advanced", 1)
    assert "--yes" in everyday and "--api-url" not in everyday and "--org" not in everyday
    assert "--api-url" in advanced and "--org" in advanced


def test_they_are_still_part_of_the_contract_and_still_work():
    """Hidden flags would be dropped from cli-surface.json, which harumi-docs diffs against. These are only
    moved to another panel in the help, so the contract still lists them."""
    root = get_command(cli.app)
    opts = {o for p in root.commands["projects"].commands["rm"].params for o in getattr(p, "opts", [])}

    assert {"--api-url", "--org"} <= opts
    assert not any(getattr(p, "hidden", False) for p in root.commands["projects"].commands["rm"].params)


def test_org_is_described_without_the_header_name():
    _, out = said(["projects", "rm", "--help"])

    assert "X-Organization" not in out and "organization" in out.lower()


def test_every_command_that_has_these_flags_puts_them_in_the_same_place():
    root = get_command(cli.app)
    stray = []

    def walk(command, path):
        for name, sub in getattr(command, "commands", {}).items():
            if hasattr(sub, "commands"):
                walk(sub, path + [name])
                continue
            for p in sub.params:
                if any(o in ("--api-url", "--org", "--git-url") for o in getattr(p, "opts", [])) and getattr(p, "rich_help_panel", None) != "Advanced":
                    stray.append(f"{' '.join(path + [name])} {p.opts}")

    walk(root, [])
    # `usage --org` is the subject of the command ("show this organization's allowance"), not an override of
    # where it runs, so it is deliberately an everyday option. Anything else is a flag that was missed.
    assert stray == ["usage ['--org']"]


# -- completion --------------------------------------------------------------------------------------------------


def complete(line: str) -> list[str]:
    """What the zsh completion script would be shown for `line` + TAB, run in a real subprocess."""
    # The real entry point, not `python -c`: Typer decides the program name (and so which variable to read)
    # from how it was started, and under `-c` it is "-c", so completion silently runs the normal command.
    entry = os.path.join(os.path.dirname(sys.executable), "harumi")
    env = {**os.environ, "_HARUMI_COMPLETE": "complete_zsh", "_TYPER_COMPLETE_ARGS": f"harumi {line}", "HARUMI_NO_UPDATE_CHECK": "1"}
    out = subprocess.run([entry], env=env, capture_output=True, text=True, timeout=60).stdout
    return [piece.split('":"')[0].strip('"( ') for piece in out.replace("_arguments '*: :((", "").replace("))'", "").split("\n") if piece.strip()]


@pytest.mark.parametrize(
    "line, expected",
    [
        ("datasources add wh --type ", {"postgresql", "mysql", "sqlserver", "oracle"}),
        ("datasources update wh --type ", {"postgresql", "mysql", "sqlserver", "oracle"}),
        ("datasources test --type ", {"postgresql", "mysql", "sqlserver", "oracle"}),
        ("org invite acme --email a@b.c --role ", {"admin", "member", "viewer"}),
        ("org role acme bo --role ", {"admin", "member", "viewer"}),
        ("memories add hi --mode ", {"always", "on_demand"}),
        ("memories edit abc --mode ", {"always", "on_demand"}),
        ("memories list --mode ", {"always", "on_demand"}),
    ],
)
def test_a_value_with_a_fixed_set_completes_to_that_set(line, expected):
    got = set(complete(line))

    assert expected <= got and "_files" not in got


@pytest.mark.parametrize(
    "line, expected, left_out",
    [
        ("datasources add wh --type my", {"mysql"}, {"postgresql", "sqlserver", "oracle"}),
        ("org invite acme --email a@b.c --role v", {"viewer"}, {"admin", "member"}),
        ("memories add hi --mode on", {"on_demand"}, {"always"}),
    ],
)
def test_completion_narrows_to_what_has_been_typed(line, expected, left_out):
    got = set(complete(line))

    assert expected <= got and not (left_out & got)


def test_commands_and_flags_still_complete():
    assert "share" in complete("sh")
    assert {"--yes", "--api-url"} <= set(complete("projects rm --"))


def test_completion_never_asks_the_network_or_needs_a_login():
    """A TAB press must be instant and work logged out: so no value is looked up from the API."""
    import harumi.cli as module
    import inspect

    source = inspect.getsource(module)
    assert "autocompletion=" in source
    for line in source.splitlines():
        if "autocompletion=" in line:
            assert "client" not in line.lower() and "api" not in line.lower()


@pytest.mark.parametrize(
    "typed, expected",
    [("shar", "share"), ("scedules", "schedules"), ("secrts", "secrets"), ("statsu", "status"), ("logn", "login"), ("dashbord", "dashboard")],
)
def test_a_misspelt_command_gets_the_one_obvious_suggestion_on_every_typer(typed, expected):
    """Typer 0.16 has no spelling suggestions and later ones add them, so they are made here, identically."""
    code, out = said([typed])

    assert code == 2 and f"Did you mean '{expected}'?" in out
    assert " or " not in out.split("Did you mean")[1].split("?")[0]  # not padded with weak guesses


def test_something_nothing_like_a_command_gets_no_made_up_suggestion():
    code, out = said(["zzzzzz"])

    assert code == 2 and "Did you mean" not in out and "harumi --help" in out
