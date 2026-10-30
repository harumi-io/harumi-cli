"""`harumi org leave` and `harumi org invitations`: the two things harumi-api offers that the CLI could not do.

The server's own error for removing yourself says "use leave organization", which the CLI had no command for, and an
invitation could only be answered in the web app. Transfer of ownership stays there: it needs a second-factor code.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.client import Client

runner = CliRunner()
O1 = "0a1b2c3d-1111-4222-8333-444455556666"  # I own it
O2 = "0a1b9999-2222-4333-8444-555566667777"  # I am an admin
O3 = "9f8e7d6c-3333-4444-8555-666677778888"  # I am a member
I1 = "aaaa1111-1111-4222-8333-444455556666"  # invited to
I2 = "bbbb2222-2222-4333-8444-555566667777"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path / "home")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli.console, "_width", 100)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")


class Api:
    def __init__(self):
        self.orgs = [
            {"id": O1, "business_name": "Acme Logistics", "role": "owner"},
            {"id": O2, "business_name": "Acme Labs [beta]", "role": "admin"},
            {"id": O3, "business_name": "Zeta", "role": "member"},
        ]
        self.invitations = [
            {"customer_id": I1, "business_name": "Globex", "role": "member"},
            {"customer_id": I2, "business_name": "Initech [x]", "role": "admin"},
        ]
        self.calls: list[str] = []
        self.fail: tuple[int, object] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        p, m = request.url.path, request.method
        self.calls.append(f"{m} {p}")
        if m != "GET" and self.fail:
            status, body = self.fail
            return httpx.Response(status, json=body)
        if p.endswith("/users/organizations") and m == "GET":
            return httpx.Response(200, json=self.orgs)
        if p.endswith("/users/me") and m == "DELETE":
            return httpx.Response(204)
        if p.endswith("/users/invitations") and m == "GET":
            return httpx.Response(200, json=self.invitations)
        if "/users/invitations/" in p and m == "POST":
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": f"not routed: {m} {p}"})


@pytest.fixture
def api(monkeypatch):
    a = Api()
    monkeypatch.setattr(
        cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(a.handler))
    )
    return a


@pytest.fixture
def at_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


def said(result):
    return " ".join((result.output or "").split())


def sent(api, method):
    return [c for c in api.calls if c.startswith(method)]


# -- org leave ------------------------------------------------------------------------------------------------------


def test_leave_asks_first_and_names_what_you_lose(api, at_terminal, monkeypatch):
    asked = []
    monkeypatch.setattr(cli.typer, "confirm", lambda text, **k: asked.append(text) or False)

    result = runner.invoke(cli.app, ["org", "leave", "Zeta"])

    assert asked and "Zeta" in asked[0] and "projects" in asked[0].lower(), asked
    assert sent(api, "DELETE") == [] and result.exit_code == 1  # declined: nothing sent, and a script is told


def test_leave_with_yes_goes_ahead_and_says_so(api, no_terminal):
    result = runner.invoke(cli.app, ["org", "leave", "Zeta", "--yes"])

    assert result.exit_code == 0, result.output
    assert sent(api, "DELETE") == [f"DELETE /api/users/organizations/{O3}/users/me"]
    assert "Left" in said(result) and "Zeta" in said(result)


def test_leave_is_found_by_the_start_of_the_id(api, no_terminal):
    runner.invoke(cli.app, ["org", "leave", O2[:8], "--yes"])

    assert sent(api, "DELETE") == [f"DELETE /api/users/organizations/{O2}/users/me"]


def test_leave_with_no_terminal_and_no_yes_refuses_and_names_the_flag(api, no_terminal):
    result = runner.invoke(cli.app, ["org", "leave", "Zeta"])

    assert result.exit_code == 1 and "--yes" in said(result) and sent(api, "DELETE") == []


def test_an_owner_is_told_before_asking_that_they_cannot_leave(api, at_terminal, monkeypatch):
    """The server refuses an owner ('Transfer ownership first'); say that up front instead of asking a question whose
    answer cannot matter, and point at the two things that do work."""
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: pytest.fail("asked an owner to confirm leaving"))

    result = runner.invoke(cli.app, ["org", "leave", "Acme Logistics"])

    out = said(result)
    assert result.exit_code == 1 and sent(api, "DELETE") == []
    assert "owner" in out.lower() and "transfer" in out.lower() and "harumi org rm" in out


def test_leaving_does_not_fall_back_to_the_workspace_you_are_in(api, no_terminal):
    """Like `org rm`: with no name, `--yes` must not leave whatever organization happens to be selected."""
    config.Config.load().save_org_id(O3)

    result = runner.invoke(cli.app, ["org", "leave", "--yes"])

    assert result.exit_code == 1 and sent(api, "DELETE") == []
    assert "Say which organization to leave" in said(result), "tells you to name one instead of leaving the current workspace"


def test_without_a_name_a_terminal_picks_from_your_organizations(api, at_terminal, monkeypatch):
    shown = {}

    def pick(question, options, **kw):
        shown.update(question=question, options=options)
        return O3

    monkeypatch.setattr(interactive, "select", pick)
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)

    runner.invoke(cli.app, ["org", "leave"])

    assert "leave" in shown["question"].lower()
    assert sent(api, "DELETE") == [f"DELETE /api/users/organizations/{O3}/users/me"]


def test_leaving_the_workspace_you_are_in_puts_you_back_in_your_personal_one(api, no_terminal):
    config.Config.load().save_org_id(O3)

    result = runner.invoke(cli.app, ["org", "leave", "Zeta", "--yes"])

    assert config.Config.load().org_id in (None, "")
    assert "personal workspace" in said(result)


def test_leaving_another_organization_keeps_your_workspace(api, no_terminal):
    config.Config.load().save_org_id(O2)

    runner.invoke(cli.app, ["org", "leave", "Zeta", "--yes"])

    assert config.Config.load().org_id == O2


def test_leaving_still_succeeds_when_the_saved_workspace_cannot_be_updated(api, no_terminal, monkeypatch):
    """The leave already happened on the server. A read-only home must not turn that into a failure, or a retry would
    try to leave an organization that is no longer there."""
    config.Config.load().save_org_id(O3)

    def refuse(self):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(config.Config, "clear_org_id", refuse)

    result = runner.invoke(cli.app, ["org", "leave", "Zeta", "--yes"])

    out = said(result)
    assert result.exit_code == 0, result.output
    assert sent(api, "DELETE") == [f"DELETE /api/users/organizations/{O3}/users/me"]
    assert "Left" in out and "could not update your saved workspace" in out and "org use --personal" in out


def test_a_server_refusal_is_shown_not_a_traceback(api, no_terminal):
    api.fail = (403, {"detail": "The organization owner cannot leave."})

    result = runner.invoke(cli.app, ["org", "leave", "Zeta", "--yes"])

    assert result.exit_code == 1 and "owner cannot leave" in said(result) and "Traceback" not in result.output


def test_a_name_with_brackets_is_shown_as_typed(api, no_terminal):
    result = runner.invoke(cli.app, ["org", "leave", "Acme Labs [beta]", "--yes"])

    assert result.exit_code == 0 and "Acme Labs [beta]" in said(result)


# -- org invitations ------------------------------------------------------------------------------------------------


def test_invitations_lists_who_invited_you_and_as_what(api, no_terminal):
    result = runner.invoke(cli.app, ["org", "invitations"])

    out = said(result)
    assert result.exit_code == 0 and "Globex" in out and "member" in out and "Initech [x]" in out and "admin" in out
    assert I1[:8] in out, "the start of the id, which accept and decline take"


def test_no_invitations_says_so(api, no_terminal):
    api.invitations = []

    result = runner.invoke(cli.app, ["org", "invitations"])

    assert result.exit_code == 0 and "no" in said(result).lower() and "invitation" in said(result).lower()


def test_invitations_json_is_named_fields_and_nothing_else(api, no_terminal):
    api.invitations[0]["internal_note"] = "not for scripts"

    result = runner.invoke(cli.app, ["org", "invitations", "--json"])

    data = json.loads(result.stdout)
    assert [set(d) for d in data] == [{"id", "name", "role"}] * 2
    assert data[0] == {"id": I1, "name": "Globex", "role": "member"}
    assert "internal_note" not in result.output


def test_an_empty_json_list_is_an_empty_array(api, no_terminal):
    api.invitations = []

    assert json.loads(runner.invoke(cli.app, ["org", "invitations", "--json"]).stdout) == []


def test_accept_sends_the_accept_call_for_that_invitation(api, no_terminal):
    result = runner.invoke(cli.app, ["org", "invitations", "accept", "Globex"])

    assert result.exit_code == 0, result.output
    assert sent(api, "POST") == [f"POST /api/users/invitations/{I1}/accept"]
    assert "Joined" in said(result) and "Globex" in said(result)


def test_decline_sends_the_decline_call_and_does_not_join(api, no_terminal):
    result = runner.invoke(cli.app, ["org", "invitations", "decline", I2[:8], "--yes"])

    assert result.exit_code == 0, result.output
    assert sent(api, "POST") == [f"POST /api/users/invitations/{I2}/decline"]
    assert "Declined" in said(result) and "Initech [x]" in said(result)


@pytest.mark.parametrize("typed", ["b", "bbb", "bbbb222"])
def test_a_short_start_of_an_id_cannot_decline_an_invitation(api, no_terminal, typed):
    """The bot's finding. `org rm` and `org leave` need 8+ characters of an id for an irreversible step; declining is
    irreversible too, so `decline b --yes` must not decline whichever single invitation happens to start with b."""
    result = runner.invoke(cli.app, ["org", "invitations", "decline", typed, "--yes"])

    assert result.exit_code == 1 and sent(api, "POST") == []
    assert "too short" in said(result) and "8" in said(result)


def test_a_name_still_declines_however_short_it_is(api, no_terminal):
    """The guard is about ids: a name is typed in full, so it names exactly one invitation."""
    api.invitations[1]["business_name"] = "X"

    result = runner.invoke(cli.app, ["org", "invitations", "decline", "x", "--yes"])

    assert result.exit_code == 0 and sent(api, "POST") == [f"POST /api/users/invitations/{I2}/decline"]


def test_accepting_with_a_short_start_still_works(api, no_terminal):
    """Joining is not destructive, so the short start stays convenient for `accept`."""
    result = runner.invoke(cli.app, ["org", "invitations", "accept", "aa"])

    assert result.exit_code == 0 and sent(api, "POST") == [f"POST /api/users/invitations/{I1}/accept"]


def test_decline_asks_first_because_it_cannot_be_taken_back(api, at_terminal, monkeypatch):
    asked = []
    monkeypatch.setattr(cli.typer, "confirm", lambda text, **k: asked.append(text) or False)

    runner.invoke(cli.app, ["org", "invitations", "decline", "Globex"])

    assert asked and "Globex" in asked[0] and sent(api, "POST") == []


def test_decline_with_no_terminal_and_no_yes_refuses(api, no_terminal):
    result = runner.invoke(cli.app, ["org", "invitations", "decline", "Globex"])

    assert result.exit_code == 1 and "--yes" in said(result) and sent(api, "POST") == []


def test_accept_never_asks(api, at_terminal, monkeypatch):
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: pytest.fail("asked before accepting an invitation"))

    result = runner.invoke(cli.app, ["org", "invitations", "accept", "Globex"])

    assert result.exit_code == 0 and sent(api, "POST")


def test_without_a_name_a_terminal_picks_the_invitation(api, at_terminal, monkeypatch):
    shown = {}

    def pick(question, options, **kw):
        shown.update(question=question, values=[o.value for o in options])
        return I2

    monkeypatch.setattr(interactive, "select", pick)

    runner.invoke(cli.app, ["org", "invitations", "accept"])

    assert "accept" in shown["question"].lower() and shown["values"] == [I1, I2]
    assert sent(api, "POST") == [f"POST /api/users/invitations/{I2}/accept"]


def test_without_a_name_and_no_terminal_it_lists_what_there_is(api, no_terminal):
    result = runner.invoke(cli.app, ["org", "invitations", "accept"])

    out = said(result)
    assert result.exit_code == 1 and "Globex" in out and "Initech" in out and sent(api, "POST") == []


def test_an_unknown_invitation_points_at_the_list(api, no_terminal):
    result = runner.invoke(cli.app, ["org", "invitations", "accept", "Nowhere"])

    assert result.exit_code == 1 and "harumi org invitations" in said(result) and sent(api, "POST") == []


def test_two_invitations_sharing_a_start_are_not_guessed_between(api, no_terminal):
    api.invitations[1]["customer_id"] = I1[:6] + "ffff" + I1[10:]

    result = runner.invoke(cli.app, ["org", "invitations", "accept", I1[:6]])

    assert result.exit_code == 1 and "more than one" in said(result) and sent(api, "POST") == []


# -- where the server's own advice now leads ------------------------------------------------------------------------


MEMBERS = [
    {"user_id": "11111111-aaaa-4bbb-8ccc-000000000001", "email": "ana@example.com", "role": "member", "pending": False},
    {"user_id": "22222222-aaaa-4bbb-8ccc-000000000002", "email": "bo@example.com", "role": "admin", "pending": False},
]


def test_removing_yourself_points_at_the_command_that_exists(api, no_terminal, monkeypatch):
    """harumi-api answers that removing yourself is not allowed and to use 'leave organization'. There is a command for
    that now, so the answer says its name instead of leaving the user to look for it."""
    original = api.handler

    def handler(request):
        if request.url.path.endswith("/users") and request.method == "GET":
            return httpx.Response(200, json=MEMBERS)
        return original(request)

    monkeypatch.setattr(
        cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(handler))
    )
    api.fail = (403, {"detail": "You cannot remove yourself. Use leave organization instead."})

    result = runner.invoke(cli.app, ["org", "rm-member", "Zeta", "ana@example.com", "--yes"])

    out = said(result)
    assert result.exit_code == 1 and "Traceback" not in result.output
    assert "You cannot remove yourself" in out, "the server's own words stay"
    assert "harumi org leave Zeta" in out, "and so does the command that does it"


def test_other_refusals_to_remove_a_member_get_no_leave_advice(api, no_terminal, monkeypatch):
    original = api.handler

    def handler(request):
        if request.url.path.endswith("/users") and request.method == "GET":
            return httpx.Response(200, json=MEMBERS)
        return original(request)

    monkeypatch.setattr(
        cli, "_get_client", lambda *x, **k: Client(api_url="https://api.harumi.io/api", transport=httpx.MockTransport(handler))
    )
    api.fail = (403, {"detail": "Only an admin can remove members."})

    result = runner.invoke(cli.app, ["org", "rm-member", "Zeta", "bo@example.com", "--yes"])

    assert result.exit_code == 1 and "Only an admin" in said(result) and "org leave" not in said(result)


def test_the_help_lists_both():
    out = runner.invoke(cli.app, ["org", "--help"]).output

    assert "leave" in out and "invitations" in out


def test_the_skill_reference_documents_both_commands_and_their_guards():
    """The test used to promise this in its name and never read the reference."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".agents/skills/harumi-cli/references/commands.md").read_text(encoding="utf-8")

    assert "harumi org leave" in text and "harumi org invitations" in text
    assert "second-factor" in text or "web app" in text, "says transfer of ownership stays in the web app"
