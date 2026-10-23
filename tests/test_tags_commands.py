"""harumi tags list / set / rm, and the tag flags on run and schedules."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

import harumi.cli as cli
from harumi.errors import ApiError
from tests.test_cli import (  # noqa: F401 - fixtures
    QUEUED,
    SCHEDULE,
    at_a_terminal,
    api,
    bound_dir,
    git,
    isolated_harumi_home,
)

runner = CliRunner()
SHA = "3f2a9c1e5b7d4a8c9e0f1a2b3c4d5e6f7a8b9c0d"
TAG = {"name": "prod", "commit_sha": SHA, "message": "release 4", "created_at": "2026-10-20T10:00:00Z"}


# -- tags list -------------------------------------------------------------------------


def test_tags_list_shows_the_name_and_a_short_commit(api):
    api.route("GET", "/api/projects/proj-1/repo/tags", [TAG])

    result = runner.invoke(cli.app, ["tags", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "prod" in result.output and "3f2a9c1e" in result.output and "release 4" in result.output
    assert SHA not in result.output
    assert "harumi run --tag" in result.output


def test_tags_list_with_none_says_how_to_add_one(api):
    api.route("GET", "/api/projects/proj-1/repo/tags", [])

    result = runner.invoke(cli.app, ["tags", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "No tags yet" in result.output and "harumi tags set" in result.output


def test_tags_list_json_is_named_fields_only(api):
    api.route("GET", "/api/projects/proj-1/repo/tags", [{**TAG, "internal": "x"}])

    result = runner.invoke(cli.app, ["tags", "list", "--json", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [TAG]


def test_a_tag_name_that_looks_like_markup_is_printed_literally(api):
    api.route("GET", "/api/projects/proj-1/repo/tags", [{**TAG, "name": "v[/x]", "message": "a [bold]b[/bold]"}])

    result = runner.invoke(cli.app, ["tags", "list", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "v[/x]" in result.output and "a [bold]b[/bold]" in result.output


# -- tags set --------------------------------------------------------------------------


def test_tags_set_with_to_sends_the_target(api):
    api.route("PUT", "/api/projects/proj-1/repo/tags/prod", TAG)

    result = runner.invoke(
        cli.app, ["tags", "set", "prod", "--to", "release-2", "-m", "release 4", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    assert api.body_for("PUT", "/api/projects/proj-1/repo/tags/prod") == {"target": "release-2", "message": "release 4"}
    assert "Tagged" in result.output and "3f2a9c1e" in result.output
    assert "harumi run --tag prod" in result.output


def test_tags_set_defaults_to_the_current_branch_when_it_is_pushed(api, git):
    git["branch"] = "release-2"
    api.route("PUT", "/api/projects/proj-1/repo/tags/prod", TAG)

    result = runner.invoke(cli.app, ["tags", "set", "prod", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert api.body_for("PUT", "/api/projects/proj-1/repo/tags/prod") == {"target": "release-2"}


def test_tags_set_refuses_a_branch_with_unpushed_commits_and_sends_nothing(api, git):
    """The tag lives on the server: tagging the branch name would label older code than the user sees."""
    git["unpushed"] = True

    result = runner.invoke(cli.app, ["tags", "set", "prod", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "git push harumi main" in " ".join(result.output.split())
    assert api.requests == []


def test_tags_set_in_a_detached_head_asks_what_to_tag(api, git):
    git["branch"] = None

    result = runner.invoke(cli.app, ["tags", "set", "prod", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "--to" in result.output
    assert api.requests == []


def test_tags_set_with_to_does_not_look_at_the_working_tree(api, git, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git was consulted")

    monkeypatch.setattr(cli, "has_unpushed_commits", boom)
    monkeypatch.setattr(cli, "current_branch", boom)
    api.route("PUT", "/api/projects/proj-1/repo/tags/prod", TAG)

    result = runner.invoke(cli.app, ["tags", "set", "prod", "--to", SHA, "--project", "proj-1"])

    assert result.exit_code == 0, result.output


def test_a_tag_named_like_a_branch_says_so(api):
    api.route(
        "PUT", "/api/projects/proj-1/repo/tags/main", {"detail": "'main' is a version (branch) name; a tag cannot share it"}, status=409
    )

    result = runner.invoke(cli.app, ["tags", "set", "main", "--to", "dev", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "cannot share" in result.output


def test_a_slash_in_a_tag_name_stays_a_path(api):
    api.route("PUT", "/api/projects/proj-1/repo/tags/rel/v1", {**TAG, "name": "rel/v1"})

    result = runner.invoke(cli.app, ["tags", "set", "rel/v1", "--to", "main", "--project", "proj-1"])

    assert result.exit_code == 0, result.output


# -- tags rm ---------------------------------------------------------------------------


def test_tags_rm_with_yes_deletes(api):
    api.route("DELETE", "/api/projects/proj-1/repo/tags/prod", None, status=204)

    result = runner.invoke(cli.app, ["tags", "rm", "prod", "--yes", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output and "prod" in result.output


def test_tags_rm_declined_changes_nothing_and_exits_1(api, at_a_terminal):
    result = runner.invoke(cli.app, ["tags", "rm", "prod", "--project", "proj-1"], input="n\n")

    assert result.exit_code == 1, result.output
    assert "Aborted." in result.output
    assert api.requests == []


def test_tags_rm_without_a_terminal_refuses_and_names_yes(api):
    result = runner.invoke(cli.app, ["tags", "rm", "prod", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "--yes" in result.output
    assert api.requests == []


def test_tags_rm_of_a_tag_a_schedule_follows_surfaces_the_reason(api):
    api.route(
        "DELETE",
        "/api/projects/proj-1/repo/tags/prod",
        {"detail": "Tag 'prod' is used by a schedule; delete the schedule first"},
        status=409,
    )

    result = runner.invoke(cli.app, ["tags", "rm", "prod", "--yes", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "used by a schedule" in result.output


def test_tags_rm_of_an_unknown_tag_points_at_the_list(api):
    api.route("DELETE", "/api/projects/proj-1/repo/tags/nope", {"detail": "not found"}, status=404)

    result = runner.invoke(cli.app, ["tags", "rm", "nope", "--yes", "--project", "proj-1"])

    assert result.exit_code == 1
    assert "harumi tags list" in result.output


# -- run --tag -------------------------------------------------------------------------


def test_run_with_a_tag_sends_only_the_tag_and_pushes_nothing(api, git, bound_dir):
    git["dirty"] = True  # a dirty tree would normally push a scratch branch
    api.route("POST", "/api/projects/proj-bound/execute", QUEUED, status=202)

    result = runner.invoke(cli.app, ["run", "--tag", "prod"])

    assert result.exit_code == 0, result.output
    body = api.body_for("POST", "/api/projects/proj-bound/execute")
    assert body["tag"] == "prod"
    assert "branch" not in body and "commit" not in body
    assert git["deleted"] == []  # no scratch branch existed to clean up
    assert "tag" in result.output and "prod" in result.output


@pytest.mark.parametrize("other", [["--branch", "dev"], ["--commit", "abc123"]])
def test_run_refuses_a_tag_with_a_branch_or_commit_before_any_request(api, git, bound_dir, other):
    result = runner.invoke(cli.app, ["run", "--tag", "prod", *other])

    assert result.exit_code == 1
    assert "--tag cannot be combined" in result.output
    assert api.requests == []


def test_run_with_a_missing_tag_shows_the_server_reason(api, git, bound_dir):
    api.route("POST", "/api/projects/proj-bound/execute", {"detail": "Tag 'nope' does not exist"}, status=404)

    result = runner.invoke(cli.app, ["run", "--tag", "nope"])

    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_runs_list_and_get_show_the_tag_of_a_run_from_a_tag(api):
    row = {
        "id": "7f3a91c2-0000-4000-8000-000000000001",
        "project_id": "proj-1",
        "status": "completed",
        "git_branch": None,
        "git_tag": "prod",
        "git_commit": SHA,
        "created_at": "2026-10-15T12:00:00Z",
    }
    api.route("GET", "/api/projects/proj-1/runs", {"runs": [row]})

    listed = runner.invoke(cli.app, ["runs", "list", "--project", "proj-1"])
    assert listed.exit_code == 0, listed.output
    assert "tag prod" in listed.output

    api.route("GET", f"/api/projects/proj-1/runs/{row['id']}", row)
    got = runner.invoke(cli.app, ["runs", "get", row["id"], "--project", "proj-1"])
    assert got.exit_code == 0, got.output
    assert "tag prod @ 3f2a9c1e" in got.output

    as_json = runner.invoke(cli.app, ["runs", "list", "--json", "--project", "proj-1"])
    assert json.loads(as_json.stdout)[0]["git_tag"] == "prod"


# -- schedules --git-tag ---------------------------------------------------------------

TAG_SCHEDULE = {**SCHEDULE, "git_branch": None, "git_tag": "prod"}


def test_schedules_add_with_a_tag_sends_the_tag_and_no_branch(api):
    api.route("POST", "/api/projects/proj-1/schedules", TAG_SCHEDULE, status=201)

    result = runner.invoke(
        cli.app, ["schedules", "add", "--cron", "0 9 * * *", "--git-tag", "prod", "--project", "proj-1"]
    )

    assert result.exit_code == 0, result.output
    body = api.body_for("POST", "/api/projects/proj-1/schedules")
    assert body["git_tag"] == "prod"
    assert "git_branch" not in body


@pytest.mark.parametrize("other", [["--git-branch", "dev"], ["--git-commit", "abc123"]])
def test_schedules_add_refuses_a_tag_with_a_branch_or_commit_before_any_request(api, other):
    result = runner.invoke(
        cli.app, ["schedules", "add", "--cron", "0 9 * * *", "--git-tag", "prod", *other, "--project", "proj-1"]
    )

    assert result.exit_code == 1
    assert "--git-tag cannot be combined" in result.output
    assert api.requests == []


def test_schedules_add_without_a_tag_still_defaults_to_main(api):
    api.route("POST", "/api/projects/proj-1/schedules", SCHEDULE, status=201)

    runner.invoke(cli.app, ["schedules", "add", "--cron", "0 9 * * *", "--project", "proj-1"])

    body = api.body_for("POST", "/api/projects/proj-1/schedules")
    assert body["git_branch"] == "main" and "git_tag" not in body


def test_schedules_update_can_move_a_schedule_onto_a_tag(api):
    sid = "5c4e1b2a-0000-4000-8000-000000000001"
    api.route("PUT", f"/api/projects/proj-1/schedules/{sid}", TAG_SCHEDULE)

    result = runner.invoke(cli.app, ["schedules", "update", sid, "--git-tag", "prod", "--project", "proj-1"])

    assert result.exit_code == 0, result.output
    assert api.body_for("PUT", f"/api/projects/proj-1/schedules/{sid}") == {"git_tag": "prod"}


def test_schedules_update_refuses_a_tag_with_a_branch(api):
    sid = "5c4e1b2a-0000-4000-8000-000000000001"

    result = runner.invoke(
        cli.app, ["schedules", "update", sid, "--git-tag", "prod", "--git-branch", "dev", "--project", "proj-1"]
    )

    assert result.exit_code == 1
    assert api.requests == []


def test_schedules_list_and_get_show_the_tag_instead_of_a_blank_branch(api):
    sid = TAG_SCHEDULE["id"]
    api.route("GET", "/api/projects/proj-1/schedules", {"schedules": [TAG_SCHEDULE]})
    api.route("GET", f"/api/projects/proj-1/schedules/{sid}", TAG_SCHEDULE)

    listed = runner.invoke(cli.app, ["schedules", "list", "--project", "proj-1"])
    assert listed.exit_code == 0, listed.output
    assert "tag prod" in listed.output and "NULL" not in listed.output

    got = runner.invoke(cli.app, ["schedules", "get", sid, "--project", "proj-1"])
    assert got.exit_code == 0, got.output
    assert "git_tag" in got.output and "prod" in got.output
    assert "git_branch" not in got.output  # it follows the tag, so no empty branch row

    as_json = runner.invoke(cli.app, ["schedules", "list", "--json", "--project", "proj-1"])
    assert json.loads(as_json.stdout)[0]["git_tag"] == "prod"
