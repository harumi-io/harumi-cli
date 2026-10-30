"""scripts/release_bump.py: the version bump that used to be a hand edit before each release.

It reads the newest changelog heading: if it has no date and its version is ahead of `__version__`, that is the release
being prepared. The script sets the version, dates the heading and regenerates cli-surface.json. A workflow runs it when
"Promote dev to main" is opened, so merging that PR is the only step left.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest

from scripts import release_bump as rb

ROOT = pathlib.Path(__file__).resolve().parent.parent

NOTES = """# Changelog

## 1.1.0

### New

- Something new.

## 1.0.0 - 2026-10-27

Intro.

### Fixed

- Something fixed.

## 0.11.1 - 2026-10-23

- A fix.
"""


def tree(tmp_path, *, version="1.0.0", notes=NOTES):
    (tmp_path / "src" / "harumi").mkdir(parents=True)
    (tmp_path / "src" / "harumi" / "__init__.py").write_text(
        f'"""doc"""\n\n__version__ = "{version}"\n\n__all__ = [\n    "__version__",\n]\n'
    )
    (tmp_path / "src" / "harumi" / "changelog.md").write_text(notes)
    (tmp_path / "cli-surface.json").write_text(json.dumps({"cli_version": version, "commands": []}))
    return tmp_path


def version_in(root):
    return re.search(r'^__version__ = "([^"]+)"$', (root / "src/harumi/__init__.py").read_text(), re.M).group(1)


# -- the decision ---------------------------------------------------------------------------------------------------


def test_an_undated_newer_heading_is_the_release_being_prepared():
    plan = rb.plan(NOTES, "1.0.0", today="2026-11-02")

    assert (plan.action, plan.version, plan.date) == ("bump", "1.1.0", "2026-11-02")


def test_a_dated_heading_that_equals_the_version_is_already_done():
    notes = NOTES.replace("## 1.1.0\n", "## 1.1.0 - 2026-11-01\n")

    plan = rb.plan(notes, "1.1.0", today="2026-11-02")

    assert plan.action == "noop" and plan.version == "1.1.0"


def test_running_twice_changes_nothing_the_second_time(tmp_path):
    root = tree(tmp_path)

    first = rb.run(root, today="2026-11-02", surface=False)
    after_first = (root / "src/harumi/changelog.md").read_text(), (root / "src/harumi/__init__.py").read_text()
    second = rb.run(root, today="2026-12-25", surface=False)  # a different day must not re-date it

    assert first.action == "bump" and second.action == "noop"
    assert after_first == ((root / "src/harumi/changelog.md").read_text(), (root / "src/harumi/__init__.py").read_text())


def test_the_version_matches_but_the_heading_has_no_date_just_gets_the_date():
    plan = rb.plan(NOTES, "1.1.0", today="2026-11-02")

    assert (plan.action, plan.version, plan.date) == ("date", "1.1.0", "2026-11-02")


def test_a_dated_heading_ahead_of_the_version_bumps_and_keeps_the_date_it_has():
    notes = NOTES.replace("## 1.1.0\n", "## 1.1.0 - 2026-11-01\n")

    plan = rb.plan(notes, "1.0.0", today="2026-11-02")

    assert (plan.action, plan.version, plan.date) == ("bump", "1.1.0", "2026-11-01")


@pytest.mark.parametrize(
    "notes, current, said",
    [
        ("# Changelog\n\nNo headings at all.\n", "1.0.0", "no release notes"),
        (NOTES.replace("## 1.1.0\n", "## 0.9.0\n"), "1.0.0", "behind"),
        (NOTES.replace("## 1.1.0\n", "## 1.1.0rc1\n"), "1.0.0", "no release notes"),  # not a heading the changelog parser reads
    ],
    ids=["no headings", "older than the version", "a prerelease"],
)
def test_it_refuses_anything_it_cannot_make_sense_of(notes, current, said):
    with pytest.raises(rb.ReleaseError) as exc:
        rb.plan(notes, current, today="2026-11-02")
    assert said in str(exc.value).lower()


def test_a_malformed_current_version_is_refused():
    with pytest.raises(rb.ReleaseError):
        rb.plan(NOTES, "not-a-version", today="2026-11-02")


@pytest.mark.parametrize("bad_date", ["11/02/2026", "2026-13-40", "", "tomorrow"])
def test_the_date_must_be_a_real_iso_date(bad_date):
    with pytest.raises(rb.ReleaseError):
        rb.plan(NOTES, "1.0.0", today=bad_date)


def test_versions_compare_as_numbers_not_text():
    notes = NOTES.replace("## 1.1.0\n", "## 1.10.0\n")

    assert rb.plan(notes, "1.9.0", today="2026-11-02").version == "1.10.0"
    with pytest.raises(rb.ReleaseError):
        rb.plan(notes.replace("## 1.10.0\n", "## 1.9.0\n"), "1.10.0", today="2026-11-02")


# -- what it writes -------------------------------------------------------------------------------------------------


def test_a_bump_sets_the_version_dates_the_heading_and_touches_nothing_else(tmp_path):
    root = tree(tmp_path)
    before_init = (root / "src/harumi/__init__.py").read_text()

    rb.run(root, today="2026-11-02", surface=False)

    assert version_in(root) == "1.1.0"
    notes = (root / "src/harumi/changelog.md").read_text()
    assert notes == NOTES.replace("## 1.1.0\n", "## 1.1.0 - 2026-11-02\n")
    assert (root / "src/harumi/__init__.py").read_text() == before_init.replace('"1.0.0"', '"1.1.0"', 1)
    assert '"__version__",' in (root / "src/harumi/__init__.py").read_text(), "the entry in __all__ is not the assignment"


def test_the_assignment_is_replaced_not_the_name_in___all__(tmp_path):
    """release.yml reads the version with an anchored regex; the bump must write exactly that line."""
    root = tree(tmp_path)

    rb.run(root, today="2026-11-02", surface=False)

    text = (root / "src/harumi/__init__.py").read_text()
    assert len(re.findall(r'^__version__ = "', text, re.M)) == 1 and version_in(root) == "1.1.0"


def test_check_mode_writes_nothing(tmp_path):
    root = tree(tmp_path)
    before = {p: p.read_text() for p in (root / "src/harumi/changelog.md", root / "src/harumi/__init__.py", root / "cli-surface.json")}

    plan = rb.run(root, today="2026-11-02", surface=True, check=True)

    assert plan.action == "bump"
    assert {p: p.read_text() for p in before} == before


def test_the_surface_is_regenerated_with_the_new_version(tmp_path, monkeypatch):
    root = tree(tmp_path)
    called = []

    def fake(cmd, **kw):
        called.append((cmd, kw.get("cwd"), (kw.get("env") or {}).get("PYTHONPATH")))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"cli_version": version_in(root), "commands": []}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake)

    rb.run(root, today="2026-11-02", surface=True)

    assert called and called[0][0][1:] == ["scripts/emit_cli_surface.py"] and called[0][1] == root
    assert json.loads((root / "cli-surface.json").read_text())["cli_version"] == "1.1.0"


def test_a_surface_that_fails_to_generate_leaves_the_tree_as_it_was(tmp_path, monkeypatch):
    root = tree(tmp_path)
    before = {p.name: p.read_text() for p in (root / "src/harumi").iterdir()}

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"))
    with pytest.raises(rb.ReleaseError) as exc:
        rb.run(root, today="2026-11-02", surface=True)

    assert "cli-surface" in str(exc.value).lower()
    assert {p.name: p.read_text() for p in (root / "src/harumi").iterdir()} == before, "half a release is worse than none"


def test_a_noop_does_not_touch_the_surface(tmp_path, monkeypatch):
    root = tree(tmp_path, version="1.1.0", notes=NOTES.replace("## 1.1.0\n", "## 1.1.0 - 2026-11-01\n"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("regenerated for nothing"))

    assert rb.run(root, today="2026-11-02", surface=True).action == "noop"


# -- the command line, which the workflow uses ----------------------------------------------------------------------


def test_main_reports_for_the_workflow_and_exits_zero(tmp_path, capsys):
    root = tree(tmp_path)

    code = rb.main(["--root", str(root), "--date", "2026-11-02", "--no-surface"])

    out = capsys.readouterr().out
    assert code == 0
    assert "action=bump" in out and "version=1.1.0" in out and "date=2026-11-02" in out


def test_main_says_noop_when_the_top_entry_is_already_the_version(tmp_path, capsys):
    root = tree(tmp_path, notes="# Changelog\n\n## 1.0.0 - 2026-10-27\n\nOld.\n")

    code = rb.main(["--root", str(root), "--date", "2026-11-02", "--no-surface"])

    assert code == 0 and "action=noop" in capsys.readouterr().out


def test_main_exits_one_with_the_reason_when_there_are_no_notes(tmp_path, capsys):
    root = tree(tmp_path, notes="# Changelog\n")

    assert rb.main(["--root", str(root), "--date", "2026-11-02", "--no-surface"]) == 1
    assert "no release notes" in capsys.readouterr().err.lower()


def test_main_check_prints_the_decision_and_changes_nothing(tmp_path, capsys):
    root = tree(tmp_path)

    code = rb.main(["--root", str(root), "--date", "2026-11-02", "--check"])

    assert code == 0 and "action=bump" in capsys.readouterr().out and version_in(root) == "1.0.0"


# -- against the real repository ------------------------------------------------------------------------------------


def test_the_real_changelog_and_version_are_understood_by_the_same_parser_the_cli_uses():
    """The script has its own heading pattern so the workflow needs no install to read it. It must agree with the CLI's."""
    from harumi import changelog

    text = (ROOT / "src/harumi/changelog.md").read_text()

    assert [(r.version, r.date) for r in rb.releases(text)] == [(r.version, r.date) for r in changelog.parse(text)]


def test_it_can_read_this_repository_without_changing_it():
    import harumi

    plan = rb.plan((ROOT / "src/harumi/changelog.md").read_text(), harumi.__version__, today="2026-11-02")

    assert plan.action in {"noop", "bump", "date"}


def test_release_yml_reads_the_version_with_the_line_the_bump_writes():
    """If these ever disagree a release is silently skipped, so pin the one anchored pattern both rely on."""
    workflow = (ROOT / ".github/workflows/release.yml").read_text()

    assert "^__version__ = \"([^\"]+)\"$" in workflow
    assert rb.VERSION_LINE.pattern == r'^__version__ = "([^"]+)"$'
