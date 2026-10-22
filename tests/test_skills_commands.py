"""`skills library` and `skills add` (US-26): what they print is true, safe and usable from a script.

Walked against a fake API with the real payload shapes. The install itself (frontmatter provenance, line endings,
refusing to overwrite) was already right; these are the messages around it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config
from harumi.client import Client

try:
    runner = CliRunner(mix_stderr=False)  # type: ignore[call-arg]
except TypeError:
    runner = CliRunner()

PRESETS = [
    {"slug": "data-cleaning", "name": "Data cleaning", "description": "Find and fix messy columns.", "category": "data", "tags": ["pandas"], "version": 3},
    {"slug": "routing-basics", "name": "Routing basics", "description": "Vehicle routing with OR-Tools.", "category": "optimization", "tags": ["ortools", "routing"], "version": 1},
    {"slug": "gurobi-tips", "name": "Gurobi tips", "description": "", "category": "optimization", "tags": [], "version": 2},
]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    config.save_credentials(access_token="a", refresh_token="r")


@pytest.fixture
def library(monkeypatch):
    """The skill library endpoints. `presets` is a list the test may extend."""
    state = {"presets": [dict(p) for p in PRESETS], "fail": None, "requests": []}

    def handler(request):
        state["requests"].append(request.url.path)
        if state["fail"]:
            return httpx.Response(state["fail"], json={"detail": "boom"})
        if request.url.path.endswith("/skill-library"):
            return httpx.Response(200, json={"skills": state["presets"]})
        slug = request.url.path.rsplit("/", 1)[1]
        for preset in state["presets"]:
            if preset["slug"] == slug:
                body = f"---\nname: {preset['name']}\n---\n\n# {preset['name']}\n"
                return httpx.Response(200, json={**preset, "content": body})
        return httpx.Response(404, json={"detail": "Skill not found"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli, "_get_client", lambda api_url=None, git_url=None, org=None: Client(api_url="https://api.harumi.io/api", transport=transport))
    return state


def make_project(base, name="proj"):
    folder = base / name
    (folder / ".harumi").mkdir(parents=True)
    repo = {"clone_url": "https://x/y.git", "owner": "o", "name": "n", "default_branch": "main"}
    (folder / ".harumi" / "config.json").write_text(json.dumps({"project_id": "p1", "repo": repo}))
    return folder


def in_dir(path, args):
    old = os.getcwd()
    os.chdir(path)
    try:
        return runner.invoke(cli.app, args)
    finally:
        os.chdir(old)


# -- skills library: --json ------------------------------------------------------------------------------------------


def test_library_json_is_one_document_and_nothing_else(library):
    result = runner.invoke(cli.app, ["skills", "library", "--json"])

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert [p["slug"] for p in document] == ["data-cleaning", "gurobi-tips", "routing-basics"]  # sorted, like the table
    assert set(document[0]) == {"slug", "name", "description", "category", "tags", "version"}
    assert result.stderr == ""


def test_library_json_respects_the_filters(library):
    result = runner.invoke(cli.app, ["skills", "library", "--category", "Optimization", "-q", "ortools", "--json"])

    assert [p["slug"] for p in json.loads(result.stdout)] == ["routing-basics"]


def test_library_json_with_nothing_found_is_an_empty_list_not_a_sentence(library):
    result = runner.invoke(cli.app, ["skills", "library", "-q", "no-such-thing", "--json"])

    assert result.exit_code == 0 and json.loads(result.stdout) == []


def test_library_json_fails_like_any_other_call_with_stdout_empty(library):
    library["fail"] = 500
    result = runner.invoke(cli.app, ["skills", "library", "--json"])

    assert result.exit_code == 1 and result.stdout == "" and "boom" in result.stderr


def test_library_json_never_publishes_a_skills_content(library):
    """The list endpoint does not carry a body, but the detail model does; a field added to the summary later
    must not appear in a script's contract by accident."""
    library["presets"][0]["content"] = "SECRET BODY"
    library["presets"][0]["internal_note"] = "not for scripts"

    document = json.loads(runner.invoke(cli.app, ["skills", "library", "--json"]).stdout)

    assert "SECRET BODY" not in json.dumps(document) and "internal_note" not in json.dumps(document)


# -- skills library: the table ------------------------------------------------------------------------------------------


def test_library_table_shows_text_as_stored(library):
    library["presets"].append({"slug": "odd", "name": "Odd [/x] name", "description": "see [link](url) and [case-1]", "category": "[bold]c[/bold]", "tags": [], "version": 1})

    result = runner.invoke(cli.app, ["skills", "library"])

    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert "Odd [/x] name" in out and "[case-1]" in out and "[bold]c[/bold]" in out


def test_library_table_does_not_cut_a_slug_at_a_narrow_width(library, monkeypatch):
    monkeypatch.setattr(cli.console, "_width", 50)
    monkeypatch.setattr(cli.console, "_height", 40)

    result = runner.invoke(cli.app, ["skills", "library"])

    assert "routing-basics" in result.stdout and "data-cleaning" in result.stdout


def test_a_nothing_found_sentence_says_what_to_try(library):
    result = runner.invoke(cli.app, ["skills", "library", "--category", "nope"])

    assert result.exit_code == 0
    assert "No skills found" in result.stdout and "harumi skills library" in result.stdout


def test_a_category_with_no_match_lists_the_categories_that_exist(library):
    result = runner.invoke(cli.app, ["skills", "library", "--category", "nope"])

    assert "data" in result.stdout and "optimization" in result.stdout


# -- skills add: the messages around the install ------------------------------------------------------------------------------


def test_adding_a_skill_that_does_not_exist_points_at_the_library(library, tmp_path):
    project = make_project(tmp_path)

    result = in_dir(project, ["skills", "add", "routing-basic"])

    assert result.exit_code == 1
    assert "routing-basic" in result.stderr and "harumi skills library" in result.stderr
    assert "HTTP 404" not in result.stderr


def test_a_misspelt_slug_suggests_the_nearest_one(library, tmp_path):
    result = in_dir(make_project(tmp_path), ["skills", "add", "routing-basic"])

    assert "routing-basics" in result.stderr


def test_a_slug_nothing_like_any_gets_no_invented_suggestion(library, tmp_path):
    result = in_dir(make_project(tmp_path), ["skills", "add", "zzzzzz"])

    assert "Did you mean" not in result.stderr and "harumi skills library" in result.stderr


def test_a_wrong_slug_writes_nothing(library, tmp_path):
    project = make_project(tmp_path)

    in_dir(project, ["skills", "add", "routing-basic"])

    assert not (project / "skills").exists()


def test_a_server_error_is_not_called_a_missing_skill(library, tmp_path):
    """A 500 says nothing about the slug: pointing at the library would send the user the wrong way."""
    library["fail"] = 500

    result = in_dir(make_project(tmp_path), ["skills", "add", "routing-basics"])

    assert result.exit_code == 1 and "boom" in result.stderr and "harumi skills library" not in result.stderr


def installed_line(stdout: str) -> str:
    """The "Installed ... at <path>." sentence, joined: it is the first thing printed and ends at the full stop
    before the next sentence. The command line below it also names the folder, so asserting on all of the output
    would be satisfied by the wrong line."""
    flat = re.sub(r"\s*\n\s*", " ", stdout)
    return flat.split("Commit and push")[0]


def test_the_install_message_survives_brackets_in_the_folder_and_the_preset_name(library, tmp_path):
    library["presets"].append({"slug": "odd", "name": "Odd [/x] name", "description": "d", "category": "c", "tags": [], "version": 1})
    project = make_project(tmp_path, "route [v2]")

    result = in_dir(project, ["skills", "add", "odd"])

    assert result.exit_code == 0, result.stderr
    line = installed_line(result.stdout)
    assert "Odd [/x] name" in line and "route [v2]" in line


def test_a_folder_that_looks_like_markup_is_shown_as_it_is(library, tmp_path):
    result = in_dir(make_project(tmp_path, "[bold]x[/bold]"), ["skills", "add", "gurobi-tips"])

    assert result.exit_code == 0 and "[bold]x[/bold]" in installed_line(result.stdout)


def test_the_printed_git_command_actually_stages_the_skill_from_a_subfolder(library, tmp_path):
    """Run it. Printing a command nobody ran is how this one shipped broken from a subfolder."""
    project = make_project(tmp_path)
    (project / "src").mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)

    result = in_dir(project / "src", ["skills", "add", "gurobi-tips"])
    text = re.sub(r"\s*\n\s*", " ", result.stdout)
    # Only the staging half is run: committing needs a git identity this machine may not have, and pushing needs
    # a server. The `cd` is the part that matters; it is what makes `git add` work from a subfolder.
    printed = re.search(r"(cd .+? && git add \S+)(?= && git commit)", text)
    assert printed, text

    ran = subprocess.run(printed.group(1), shell=True, cwd=project / "src", capture_output=True, text=True)
    staged = subprocess.run(["git", "-C", str(project), "diff", "--cached", "--name-only"], capture_output=True, text=True).stdout.split()

    assert ran.returncode == 0, ran.stderr
    assert "skills/gurobi-tips/SKILL.md" in staged


def test_dashboard_validate_prints_a_file_name_with_brackets_as_it_is(tmp_path):
    """`dashboard/[v2].toml` printed as `dashboard/.toml`: the file looked missing. Found while walking the other
    commands that print a name somebody else chose."""
    folder = tmp_path / "dashboard"
    folder.mkdir()
    (folder / "[v2].toml").write_text('[[widget]]\ntype = "metric"\nlabel = "x"\n')
    (folder / "ok.toml").write_text('[[widget]]\ntype = "metric"\nlabel = "x"\n')

    result = in_dir(tmp_path, ["dashboard", "validate"])

    assert "dashboard/[v2].toml" in result.stdout and "dashboard/.toml" not in result.stdout
