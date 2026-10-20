"""An unknown widget type says what the valid ones are.

`harumi dashboard validate` told you a widget was dropped for `unknown type "charts"` and
left you to run `harumi dashboard widgets` to find out what "charts" should have been.
`harumi dashboard widgets --type pie` already lists them; validate now does too, and
suggests the nearest one when it is a typo.
"""

from __future__ import annotations

from harumi.dashboard import parse_widget_entry, widget_schemas


def issue_for(type_: str):
    widget, issue = parse_widget_entry({"type": type_, "id": "w1", "title": "T"})
    assert widget is None and issue is not None
    return issue.message


def test_the_message_lists_every_valid_type():
    message = issue_for("pie")

    for name in widget_schemas():
        assert name in message, f"{name!r} missing from: {message}"
    assert 'unknown type "pie"' in message  # the original wording is kept


def test_a_typo_suggests_the_nearest_type():
    assert 'Did you mean "chart"?' in issue_for("charts")
    assert 'Did you mean "treemap"?' in issue_for("tree-map")


def test_nothing_close_gets_the_list_but_no_guess():
    message = issue_for("zzzzzz")

    assert "Did you mean" not in message and "metric" in message


def test_the_validate_command_shows_it(tmp_path):
    from typer.testing import CliRunner

    from harumi import cli

    spec = tmp_path / "d.toml"
    spec.write_text('[[widgets]]\nid = "x"\ntype = "charts"\ntitle = "X"\n')

    result = CliRunner().invoke(cli.app, ["dashboard", "validate", str(spec)])

    out = " ".join(result.output.split())
    assert result.exit_code == 1
    assert 'unknown type "charts"' in out and 'Did you mean "chart"?' in out and "Valid types:" in out
