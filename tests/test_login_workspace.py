"""After login: which workspace (an organization or personal), and what next."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from harumi import cli, config, interactive
from harumi.config import Config
from harumi.models import Organization

runner = CliRunner()

ORG_A = Organization(id="1af71c9b-98dd-4aaa-892f-5f690dd2d6d5", business_name="Harumi DEV Team", role_name="admin")
ORG_B = Organization(id="302d52ba-4033-4bca-ba12-b2d8da3b9b83", business_name="IVECO - Test")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.setattr("harumi.config._RESOLVED_API_URL", None)
    for var in ("HARUMI_ENV", "HARUMI_API_URL", "HARUMI_GIT_URL", "HARUMI_ORG"):
        monkeypatch.delenv(var, raising=False)
    config.save_credentials(access_token="a", refresh_token="r", email="ana@example.com")
    return tmp_path


def saved_org() -> "str | None":
    path = config.env_config_path("production")
    return json.loads(path.read_text()).get("org_id") if path.exists() else None


class FakeClient:
    def __init__(self, orgs):
        self.orgs = orgs

    def list_organizations(self):
        return self.orgs


@pytest.fixture
def memberships(monkeypatch):
    holder = {"orgs": []}
    monkeypatch.setattr(cli, "_get_client", lambda *a, **k: FakeClient(holder["orgs"]))
    return holder


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: True)


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda *a, **k: False)


def _menu(monkeypatch, answer):
    asked = []

    def select(message, options, *, flag=None, default=None):
        asked.append({"message": message, "options": options, "flag": flag, "default": default})
        return answer

    monkeypatch.setattr(interactive, "select", select)
    return asked


def _settle() -> None:
    cli._resolve_and_store_org(Config.load())


# -- the decision -------------------------------------------------------------


def test_no_organizations_means_personal_automatically(memberships, terminal, monkeypatch, capsys):
    memberships["orgs"] = []
    monkeypatch.setattr(interactive, "select", lambda *a, **k: pytest.fail("nothing to choose between"))

    _settle()

    assert saved_org() is None
    assert "personal" in capsys.readouterr().out


def test_belonging_to_an_org_asks_with_personal_as_the_last_choice(memberships, terminal, monkeypatch):
    memberships["orgs"] = [ORG_A, ORG_B]
    asked = _menu(monkeypatch, ORG_B.id)

    _settle()

    assert [o.value for o in asked[0]["options"]] == [ORG_A.id, ORG_B.id, cli.PERSONAL_WORKSPACE]
    assert asked[0]["default"] == ORG_A.id
    assert saved_org() == ORG_B.id


def test_one_org_is_still_a_question_because_personal_is_the_other_answer(memberships, terminal, monkeypatch):
    memberships["orgs"] = [ORG_A]
    asked = _menu(monkeypatch, ORG_A.id)

    _settle()

    assert len(asked) == 1
    assert len(asked[0]["options"]) == 2
    assert saved_org() == ORG_A.id


def test_picking_personal_clears_a_previously_saved_org(memberships, terminal, monkeypatch):
    memberships["orgs"] = [ORG_A]
    Config.load().save_org_id("an-org-you-left")  # stale: not in the list
    _menu(monkeypatch, cli.PERSONAL_WORKSPACE)

    _settle()

    assert saved_org() is None


def test_a_saved_org_you_still_belong_to_is_kept_without_asking(memberships, terminal, monkeypatch, capsys):
    memberships["orgs"] = [ORG_A, ORG_B]
    Config.load().save_org_id(ORG_B.id)
    monkeypatch.setattr(interactive, "select", lambda *a, **k: pytest.fail("already decided"))

    _settle()

    assert saved_org() == ORG_B.id
    out = capsys.readouterr().out
    assert "IVECO - Test" in out
    assert "harumi org use" in out


def test_a_saved_org_you_have_left_is_dropped_when_you_now_have_none(memberships, terminal):
    memberships["orgs"] = []
    Config.load().save_org_id("an-org-you-left")

    _settle()

    assert saved_org() is None


def test_cancelling_the_menu_keeps_you_logged_in_and_changes_nothing(memberships, terminal, monkeypatch, capsys):
    memberships["orgs"] = [ORG_A]
    _menu(monkeypatch, None)

    _settle()

    assert saved_org() is None
    assert "harumi org use" in capsys.readouterr().out


def test_an_explicit_HARUMI_ORG_is_left_alone(memberships, terminal, monkeypatch):
    monkeypatch.setenv("HARUMI_ORG", "from-the-shell")
    memberships["orgs"] = [ORG_A]
    monkeypatch.setattr(interactive, "select", lambda *a, **k: pytest.fail("explicitly set"))

    _settle()

    assert saved_org() is None


# -- no terminal: predictable, never a prompt -------------------------------------


def test_without_a_terminal_a_single_org_is_used_as_it_always_was(memberships, no_terminal, monkeypatch):
    memberships["orgs"] = [ORG_A]
    monkeypatch.setattr(interactive, "select", lambda *a, **k: pytest.fail("no terminal"))

    _settle()

    assert saved_org() == ORG_A.id


def test_without_a_terminal_several_orgs_mean_personal_and_the_list_is_printed(
    memberships, no_terminal, monkeypatch, capsys
):
    memberships["orgs"] = [ORG_A, ORG_B]
    monkeypatch.setattr(interactive, "select", lambda *a, **k: pytest.fail("no terminal"))

    _settle()

    assert saved_org() is None
    out = capsys.readouterr().out
    assert "personal" in out
    assert ORG_A.id in out and "IVECO" in out
    assert "harumi org use" in out


# -- harumi org use -----------------------------------------------------------------


def test_org_use_with_an_id_still_works_as_before():
    result = runner.invoke(cli.app, ["org", "use", "org-9"])

    assert result.exit_code == 0, result.output
    assert saved_org() == "org-9"


def test_org_use_personal_returns_to_the_personal_workspace():
    Config.load().save_org_id("org-9")

    result = runner.invoke(cli.app, ["org", "use", "--personal"])

    assert result.exit_code == 0, result.output
    assert saved_org() is None
    assert "personal" in result.output


def test_org_use_rejects_an_id_together_with_personal():
    result = runner.invoke(cli.app, ["org", "use", "org-9", "--personal"])

    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
    assert saved_org() is None


def test_org_use_without_an_id_opens_the_picker(memberships, terminal, monkeypatch):
    memberships["orgs"] = [ORG_A, ORG_B]
    asked = _menu(monkeypatch, ORG_B.id)

    result = runner.invoke(cli.app, ["org", "use"])

    assert result.exit_code == 0, result.output
    assert saved_org() == ORG_B.id
    assert asked[0]["flag"] == "the ORG_ID argument (or --personal)"


def test_org_use_without_an_id_and_without_a_terminal_lists_the_choices(memberships, no_terminal):
    memberships["orgs"] = [ORG_A, ORG_B]

    result = runner.invoke(cli.app, ["org", "use"])

    assert result.exit_code == 1
    assert "Harumi DEV Team" in result.output and "IVECO - Test" in result.output
    assert "ORG_ID" in result.output
    assert saved_org() is None


def test_org_use_picker_for_someone_with_no_orgs_says_they_are_already_personal(memberships, terminal):
    memberships["orgs"] = []

    result = runner.invoke(cli.app, ["org", "use"])

    assert result.exit_code == 1
    assert "already in your personal" in " ".join(result.output.split())  # the terminal may wrap the line


# -- login ends by offering next steps ---------------------------------------------------------


@pytest.fixture
def logged_in_login(monkeypatch):
    """Run the real `login` with the network replaced and a session already on file."""
    monkeypatch.setattr(cli, "_session_still_works", lambda cfg, email: True)
    monkeypatch.setattr(cli, "_resolve_and_store_org", lambda config: None)
    monkeypatch.setattr(cli, "_provision_git_token", lambda config: None)
    offered = []
    monkeypatch.setattr(cli, "_quickstart", lambda **k: offered.append(k))
    return offered


def test_login_at_a_terminal_ends_with_the_next_steps_menu(logged_in_login, terminal):
    result = runner.invoke(cli.app, ["login", "--email", "ana@example.com"])

    assert result.exit_code == 0, result.output
    # The login screen already said who is logged in; the menu must not repeat it.
    assert logged_in_login == [{"show_status": False}]


def test_login_without_a_terminal_just_finishes(logged_in_login, no_terminal):
    result = runner.invoke(cli.app, ["login", "--email", "ana@example.com"])

    assert result.exit_code == 0, result.output
    assert logged_in_login == []


def test_a_failed_login_never_reaches_the_next_steps_menu(monkeypatch, terminal):
    from harumi.errors import ApiUnreachableError

    offered = []
    monkeypatch.setattr(cli, "_session_still_works", lambda cfg, email: False)
    monkeypatch.setattr(cli, "_quickstart", lambda **k: offered.append(k))

    def request_otp(*a, **k):
        raise ApiUnreachableError("Could not reach harumi-api")

    monkeypatch.setattr(cli.auth, "request_otp", request_otp)

    result = runner.invoke(cli.app, ["login", "--email", "ana@example.com"])

    assert result.exit_code == 1
    assert offered == []


def test_the_gitea_username_is_no_longer_printed_on_success(monkeypatch, capsys):
    from harumi.models import GitCredentials

    class Client:
        def get_git_token(self):
            return GitCredentials(token="t", git_url="https://git.example", username="u-440ac9f5")

    monkeypatch.setattr(cli, "_get_client", lambda *a, **k: Client())
    monkeypatch.setattr(cli, "ProjectBinding", type("B", (), {"load": staticmethod(lambda: None)}))

    cli._provision_git_token(Config.load())

    out = capsys.readouterr().out
    assert "u-440ac9f5" not in out
    assert "provisioned" not in out
