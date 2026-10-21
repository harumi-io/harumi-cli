"""org, profile and whoami: pick by name, say what a delete takes, and keep the saved workspace true.

Driven against a fake API that honours ids, with the real payload shapes, at 80 columns.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.client import Client

runner = CliRunner()
O1 = "0a1b2c3d-1111-4222-8333-444455556666"
O2 = "0a1b9999-2222-4333-8444-555566667777"
O3 = "9f8e7d6c-3333-4444-8555-666677778888"
U1 = "11111111-aaaa-4bbb-8ccc-000000000001"
U2 = "22222222-aaaa-4bbb-8ccc-000000000002"
U3 = "33333333-aaaa-4bbb-8ccc-000000000003"
MFA = {"code": "mfa_required", "message": "Two-factor authentication is required for this action."}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "home" / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli.console, "_width", 80)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


class Api:
    def __init__(self):
        self.orgs = [
            {"id": O1, "business_name": "Acme Logistics", "role": "owner"},
            {"id": O2, "business_name": "Acme Labs [beta]", "role": "admin"},
            {"id": O3, "business_name": "Zeta", "role": "member"},
        ]
        self.members = [
            {"user_id": U1, "role": "owner", "email": "ana@example.com", "first_name": "Ana", "last_name": "Souza"},
            {"user_id": U2, "role": "admin", "email": "bo@example.com", "first_name": "Bo", "last_name": None},
            {"user_id": U3, "role": "member", "email": "cy@example.com", "pending": True},
        ]
        self.calls: list[str] = []
        self.sent: list[dict] = []
        self.fail: tuple[int, object] | None = None
        self.profile = {"id": U1, "email": "ana@example.com", "first_name": "Ana", "last_name": "Souza", "bio": None}

    def handler(self, request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        self.calls.append(f"{m} {p}")
        if self.fail and m != "GET":
            return httpx.Response(self.fail[0], json={"detail": self.fail[1]})
        body = json.loads(request.content) if request.content else {}
        if m != "GET":
            self.sent.append(body)
        if p.endswith("/users/organizations") and m == "GET":
            return httpx.Response(200, json=self.orgs)
        if p.endswith("/users/organizations") and m == "POST":
            return httpx.Response(200, json={"id": "c0ffee00-0000-4000-8000-000000000000", "role": "owner", **body})
        if p.endswith("/users/profile") and m == "GET":
            return httpx.Response(200, json=self.profile)
        if p.endswith("/users/profile") and m == "POST":
            return httpx.Response(200, json={**self.profile, **body})
        if p.endswith("/users") and "/organizations/" in p and m == "GET":
            return httpx.Response(200, json=self.members)
        if p.endswith("/users") and m == "POST":
            return httpx.Response(200, json={"user_id": U3, "role": body["role"], "email": body["email"], "pending": True})
        if "/users/" in p.split("/organizations/")[-1]:
            return httpx.Response(204) if m == "DELETE" else httpx.Response(200, json={**self.members[1], **body})
        if "/organizations/" in p and m in ("PATCH",):
            return httpx.Response(200, json={**self.orgs[0], **body})
        if "/organizations/" in p and m == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "not routed " + p})


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler)))
    return a


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


def pick(monkeypatch, value):
    seen = {}

    def select(message, options, *, flag=None, default=None):
        seen.update(message=message, values=[o.value for o in options], labels=[o.label for o in options], flag=flag)
        return value

    monkeypatch.setattr(interactive, "select", select)
    return seen


def plain(text: str) -> str:
    return " ".join(text.split())


def saved_org() -> str | None:
    return config.Config.load().org_id


# -- errors the API sends as structure ----------------------------------------------------------------------


def test_a_validation_error_names_the_field_and_never_echoes_what_was_typed(api):
    api.fail = (422, [{"type": "value_error", "loc": ["body", "email"], "msg": "value is not a valid email address", "input": "hunter2-secret"}])

    result = runner.invoke(cli.app, ["org", "invite", O1, "--email", "hunter2-secret"])

    out = plain(result.output)
    assert result.exit_code == 1 and "email: value is not a valid email address" in out
    assert "hunter2-secret" not in out and "{'" not in out


def test_a_second_factor_refusal_says_what_to_do_instead_of_printing_a_dict(api):
    api.fail = (403, MFA)

    out = plain(runner.invoke(cli.app, ["org", "rm", O1, "--yes"]).output)

    assert "Two-factor authentication is required" in out and "web app" in out and "{'" not in out and "mfa_required" not in out


# -- picking an organization --------------------------------------------------------------------------------


def test_an_organization_is_found_by_name_ignoring_case(api):
    assert runner.invoke(cli.app, ["org", "members", "zeta"]).exit_code == 0
    assert api.calls[-1].endswith(f"/organizations/{O3}/users")


def test_an_organization_is_found_by_the_start_of_its_id(api):
    assert runner.invoke(cli.app, ["org", "members", "9f8e"]).exit_code == 0
    assert api.calls[-1].endswith(f"/organizations/{O3}/users")


def test_an_organization_name_that_is_also_a_prefix_of_another_is_refused_not_guessed(api):
    result = runner.invoke(cli.app, ["org", "members", "0a1b"])  # starts both O1 and O2

    assert result.exit_code == 1 and "more than one" in plain(result.output)
    assert not any("/users" in c and "/organizations/0a1b" in c for c in api.calls)


def test_an_unknown_organization_points_at_the_list(api):
    result = runner.invoke(cli.app, ["org", "members", "nope"])

    assert result.exit_code == 1 and "harumi org list" in plain(result.output)


def test_members_without_an_argument_uses_the_workspace_you_are_in(api):
    config.Config.load().save_org_id(O3)

    assert runner.invoke(cli.app, ["org", "members"]).exit_code == 0
    assert api.calls[-1].endswith(f"/organizations/{O3}/users")


def test_members_without_an_argument_or_workspace_picks_at_a_terminal(api, monkeypatch):
    seen = pick(monkeypatch, O2)

    assert runner.invoke(cli.app, ["org", "members"]).exit_code == 0
    assert api.calls[-1].endswith(f"/organizations/{O2}/users") and seen["values"] == [O1, O2, O3]


def test_members_without_an_argument_and_without_a_terminal_lists_the_choices(api, no_terminal):
    result = runner.invoke(cli.app, ["org", "members"])

    out = plain(result.output)
    assert result.exit_code == 1 and "no interactive terminal" in out and "Zeta" in out


def test_org_list_says_which_workspace_you_are_in(api):
    config.Config.load().save_org_id(O3)

    out = plain(runner.invoke(cli.app, ["org", "list"]).output)

    assert "Working in: Zeta" in out and "harumi org use" in out


def test_org_list_says_personal_when_no_organization_is_chosen(api):
    assert "personal workspace" in plain(runner.invoke(cli.app, ["org", "list"]).output)


def test_names_with_brackets_are_shown_as_they_are(api):
    out = plain(runner.invoke(cli.app, ["org", "list"]).output)

    assert "Acme Labs [beta]" in out
    assert runner.invoke(cli.app, ["org", "create", "Lab [/x]"]).exit_code == 0
    assert runner.invoke(cli.app, ["org", "rename", "zeta", "Zeta [/y]"]).exit_code == 0


def test_creating_an_organization_says_how_to_work_in_it(api):
    out = plain(runner.invoke(cli.app, ["org", "create", "New Co"]).output)

    assert "harumi org use" in out


# -- roles --------------------------------------------------------------------------------------------------


def test_owner_is_refused_before_any_request_and_points_at_transfer(api):
    for args in (["org", "invite", "zeta", "--email", "d@example.com", "--role", "owner"], ["org", "role", "zeta", "bo", "--role", "owner"]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 1 and "Transfer ownership" in plain(result.output)
    assert not any(c.startswith(("POST", "PUT")) for c in api.calls)


def test_a_mistyped_role_gets_a_suggestion_before_any_request(api):
    result = runner.invoke(cli.app, ["org", "invite", "zeta", "--email", "d@example.com", "--role", "admn"])

    assert result.exit_code == 1 and "Did you mean admin" in plain(result.output) and api.sent == []


def test_a_role_in_the_wrong_case_is_sent_lower_cased(api):
    assert runner.invoke(cli.app, ["org", "invite", "zeta", "--email", "d@example.com", "--role", "Viewer"]).exit_code == 0
    assert api.sent[-1]["role"] == "viewer"


@pytest.mark.parametrize("who", ["bo@example.com", "BO@example.com", "22222222", U2])
def test_a_member_is_found_by_email_id_or_start_of_id(api, who):
    assert runner.invoke(cli.app, ["org", "role", "zeta", who, "--role", "viewer"]).exit_code == 0
    assert api.calls[-1].endswith(f"/users/{U2}")


def test_a_member_who_is_not_in_the_organization_points_at_the_member_list(api):
    result = runner.invoke(cli.app, ["org", "role", "zeta", "nobody@example.com", "--role", "viewer"])

    assert result.exit_code == 1 and "harumi org members" in plain(result.output)


def test_role_without_a_member_picks_one(api, monkeypatch):
    seen = pick(monkeypatch, U3)

    assert runner.invoke(cli.app, ["org", "role", "zeta", "--role", "viewer"]).exit_code == 0
    assert api.calls[-1].endswith(f"/users/{U3}") and U1 not in seen["values"]  # the owner can't be changed


def test_removing_a_member_names_them_in_the_question(api):
    out = plain(runner.invoke(cli.app, ["org", "rm-member", "zeta", "22222222"], input="n\n").output)

    assert "bo@example.com" in out and U2 not in out and "Aborted" in out
    assert not any(c.startswith("DELETE") for c in api.calls)


def test_members_lists_names_and_pending_invitations(api):
    out = plain(runner.invoke(cli.app, ["org", "members", "zeta"]).output)

    assert "Ana Souza" in out and "cy@example.com" in out and "invited" in out


# -- deleting an organization -------------------------------------------------------------------------------


def test_deleting_an_organization_says_what_goes_with_it_and_names_it(api):
    out = plain(runner.invoke(cli.app, ["org", "rm", "acme logistics"], input="n\n").output)

    assert "Acme Logistics" in out and "Every project in it is deleted" in out and "subscription" in out and O1 not in out.split("[y/N]")[0]
    assert not any(c.startswith("DELETE") for c in api.calls)


def test_deleting_without_naming_the_organization_is_refused_with_yes(api):
    result = runner.invoke(cli.app, ["org", "rm", "--yes"])

    assert result.exit_code == 1 and "Say which" in plain(result.output) and not any(c.startswith("DELETE") for c in api.calls)


def test_deleting_without_naming_an_organization_never_falls_back_to_the_one_you_are_in(api, monkeypatch):
    """Members and the like default to the current workspace. A delete must not: it would delete it."""
    config.Config.load().save_org_id(O1)
    seen = pick(monkeypatch, None)  # the user backs out of the picker

    result = runner.invoke(cli.app, ["org", "rm"])

    assert result.exit_code == 0 and seen["values"] == [O1, O2, O3]  # asked, with every organization offered
    assert not any(c.startswith("DELETE") for c in api.calls)


def test_removing_a_member_with_yes_but_no_member_is_refused(api):
    result = runner.invoke(cli.app, ["org", "rm-member", "zeta", "--yes"])

    assert result.exit_code == 1 and "Say who" in plain(result.output) and not any(c.startswith("DELETE") for c in api.calls)


def test_deleting_the_workspace_you_are_in_puts_you_back_in_your_personal_one(api):
    config.Config.load().save_org_id(O1)

    out = plain(runner.invoke(cli.app, ["org", "rm", "acme logistics", "--yes"]).output)

    assert saved_org() is None and "personal workspace" in out


def test_deleting_another_organization_leaves_your_workspace_alone(api):
    config.Config.load().save_org_id(O3)

    assert runner.invoke(cli.app, ["org", "rm", "acme logistics", "--yes"]).exit_code == 0
    assert saved_org() == O3


def test_a_refused_delete_does_not_touch_the_saved_workspace(api):
    config.Config.load().save_org_id(O1)
    api.fail = (403, "Only the organization owner can delete it.")

    assert runner.invoke(cli.app, ["org", "rm", "acme logistics", "--yes"]).exit_code == 1
    assert saved_org() == O1


# -- profile and whoami -------------------------------------------------------------------------------------


def test_a_last_name_can_be_cleared(api):
    result = runner.invoke(cli.app, ["profile", "set", "--last-name", ""])

    assert result.exit_code == 0 and api.sent[-1] == {"last_name": ""}


def test_a_bio_can_be_cleared_and_keeps_its_brackets(api):
    assert runner.invoke(cli.app, ["profile", "set", "--bio", ""]).exit_code == 0 and api.sent[-1] == {"bio": ""}
    assert runner.invoke(cli.app, ["profile", "set", "--bio", "I use [/x]"]).exit_code == 0 and api.sent[-1] == {"bio": "I use [/x]"}


def test_an_empty_first_name_is_refused_before_any_request(api):
    result = runner.invoke(cli.app, ["profile", "set", "--first-name", "  "])

    assert result.exit_code == 1 and "first name" in plain(result.output).lower() and api.sent == []


def test_whoami_names_the_workspace_instead_of_printing_its_id(api):
    config.Config.load().save_org_id(O3)

    out = plain(runner.invoke(cli.app, ["whoami"]).output)

    assert "Zeta" in out


def test_whoami_still_works_when_the_saved_workspace_cannot_be_looked_up(api):
    config.Config.load().save_org_id("77777777-0000-4000-8000-000000000000")  # not one of yours any more

    result = runner.invoke(cli.app, ["whoami"])

    out = plain(result.output)
    assert result.exit_code == 0 and "77777777" in out and "harumi org use" in out


# -- org use ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["zeta", "ZETA", "9f8e", O3])
def test_org_use_saves_the_real_id_whatever_you_typed(api, typed):
    """Saving the name or a cut id would make every later request carry an organization that does not exist."""
    result = runner.invoke(cli.app, ["org", "use", typed])

    assert result.exit_code == 0 and saved_org() == O3 and "Zeta" in plain(result.output)


def test_org_use_refuses_an_organization_you_do_not_belong_to_and_saves_nothing(api):
    config.Config.load().save_org_id(O1)

    result = runner.invoke(cli.app, ["org", "use", "99999999-0000-4000-8000-000000000000"])

    assert result.exit_code == 1 and "harumi org list" in plain(result.output) and saved_org() == O1


def test_org_use_with_an_ambiguous_start_of_an_id_saves_nothing(api):
    assert runner.invoke(cli.app, ["org", "use", "0a1b"]).exit_code == 1 and saved_org() is None
