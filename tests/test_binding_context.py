"""The project binding records which environment and workspace its project is in (harumi-cli#259).

`.harumi/config.json` used to hold only the project id and its repo, so a command run inside a
project folder had no way to know that the project belongs to another organization (or another
environment) than the one `harumi org use` / `harumi login` saved. These tests pin the file format
and the one-time backfill of bindings written before it; using the context is the next piece.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harumi.config import ProjectBinding, RepoBinding, environment_for_git_url, env_config_path, _write_json

REPO = RepoBinding(owner="u-1", name="solver", clone_url="https://git.harumi.io/u-1/solver.git")
ORG = "0b9c2f3e-1111-4222-8333-444455556666"


@pytest.fixture(autouse=True)
def home(tmp_path, isolate_harumi_home):
    return isolate_harumi_home(tmp_path / "home")


def _raw(folder: Path) -> dict:
    return json.loads((folder / ".harumi" / "config.json").read_text())


def _legacy(folder: Path, clone_url: str = REPO.clone_url, **extra) -> Path:
    (folder / ".harumi").mkdir(parents=True)
    data = {"project_id": "proj-1", "repo": {**REPO.to_dict(), "clone_url": clone_url}, **extra}
    (folder / ".harumi" / "config.json").write_text(json.dumps(data))
    return folder


# -- writing -----------------------------------------------------------------------------------


def test_a_new_binding_records_the_environment_and_the_owning_organization(tmp_path):
    ProjectBinding.write(tmp_path, "proj-1", REPO, environment="staging", customer_id=ORG, workspace_known=True)

    assert _raw(tmp_path) == {"project_id": "proj-1", "repo": REPO.to_dict(), "environment": "staging", "customer_id": ORG}
    loaded = ProjectBinding.load(tmp_path)
    assert (loaded.environment, loaded.customer_id, loaded.workspace_known) == ("staging", ORG, True)


def test_a_personal_project_is_recorded_as_null_not_left_out(tmp_path):
    """`null` says "personal"; a missing key says "an older CLI wrote this". They must not read alike."""
    ProjectBinding.write(tmp_path, "proj-1", REPO, environment="production", customer_id=None, workspace_known=True)

    assert _raw(tmp_path)["customer_id"] is None
    loaded = ProjectBinding.load(tmp_path)
    assert loaded.customer_id is None and loaded.workspace_known


def test_an_unknown_owner_leaves_the_key_out_so_it_is_filled_in_later(tmp_path):
    ProjectBinding.write(tmp_path, "proj-1", REPO, environment="production", customer_id=None, workspace_known=False)

    assert "customer_id" not in _raw(tmp_path)
    assert not ProjectBinding.load(tmp_path).workspace_known


def test_relinking_the_same_project_keeps_keys_this_version_does_not_know(tmp_path):
    _legacy(tmp_path, future_key={"kept": True})

    ProjectBinding.write(tmp_path, "proj-1", REPO, environment="production", customer_id=ORG, workspace_known=True)

    assert _raw(tmp_path)["future_key"] == {"kept": True}


def test_linking_another_project_starts_from_a_clean_file(tmp_path):
    _legacy(tmp_path, future_key="old project's", customer_id=ORG)

    ProjectBinding.write(tmp_path, "proj-2", REPO, environment="production", customer_id=None, workspace_known=True)

    data = _raw(tmp_path)
    assert data["project_id"] == "proj-2" and "future_key" not in data and data["customer_id"] is None


# -- reading what older CLIs wrote --------------------------------------------------------------


def test_a_legacy_binding_still_loads_and_says_its_workspace_is_unknown(tmp_path):
    loaded = ProjectBinding.load(_legacy(tmp_path))

    assert loaded.project_id == "proj-1"
    assert (loaded.environment, loaded.customer_id, loaded.workspace_known) == (None, None, False)


@pytest.mark.parametrize(
    "clone_url, expected",
    [
        ("https://git.harumi.io/u-1/solver.git", "production"),
        ("https://git.dev.harumi.io/u-1/solver.git", "staging"),
        ("https://GIT.DEV.HARUMI.IO:443/u-1/solver.git", "staging"),
        ("https://gitea.example.com/u-1/solver.git", None),
        ("not a url", None),
    ],
)
def test_a_legacy_binding_takes_its_environment_from_the_clone_url(tmp_path, clone_url, expected):
    assert ProjectBinding.load(_legacy(tmp_path, clone_url=clone_url)).effective_environment == expected


def test_a_self_hosted_git_url_override_places_the_binding_in_that_environment(tmp_path):
    _write_json(env_config_path("staging"), {"git_url": "http://localhost:3000"})

    assert environment_for_git_url("http://localhost:3000/u-1/solver.git") == "staging"


def test_a_recorded_environment_wins_over_the_clone_url(tmp_path):
    loaded = ProjectBinding.load(_legacy(tmp_path, environment="staging"))

    assert loaded.effective_environment == "staging"


@pytest.mark.parametrize("bad", [5, ["x"], {"a": 1}, "moon"])
def test_a_hand_edited_environment_of_the_wrong_kind_counts_as_not_recorded(tmp_path, bad):
    assert ProjectBinding.load(_legacy(tmp_path, environment=bad)).environment is None


@pytest.mark.parametrize("bad", [5, ["x"], {"a": 1}])
def test_a_hand_edited_owner_of_the_wrong_kind_is_unknown_not_a_crash(tmp_path, bad):
    loaded = ProjectBinding.load(_legacy(tmp_path, customer_id=bad))

    assert loaded.customer_id is None and not loaded.workspace_known


# -- backfilling --------------------------------------------------------------------------------


def test_backfill_asks_the_server_once_and_writes_the_answer_down(tmp_path):
    asked = []
    binding = ProjectBinding.load(_legacy(tmp_path, future_key=1))

    filled = binding.ensure_workspace(lambda pid: asked.append(pid) or ORG)

    assert asked == ["proj-1"]
    assert (filled.customer_id, filled.workspace_known, filled.environment) == (ORG, True, "production")
    assert _raw(tmp_path) == {
        "project_id": "proj-1", "repo": REPO.to_dict(), "future_key": 1, "customer_id": ORG, "environment": "production",
    }
    # Next time there is nothing to ask.
    ProjectBinding.load(tmp_path).ensure_workspace(lambda pid: pytest.fail("already known"))


def test_backfill_of_a_personal_project_records_null(tmp_path):
    filled = ProjectBinding.load(_legacy(tmp_path)).ensure_workspace(lambda pid: None)

    assert filled.workspace_known and filled.customer_id is None
    assert _raw(tmp_path)["customer_id"] is None


def test_a_failed_lookup_leaves_the_file_alone_and_is_not_an_error(tmp_path):
    before = (_legacy(tmp_path) / ".harumi" / "config.json").read_text()

    def offline(pid):
        raise RuntimeError("no network")

    binding = ProjectBinding.load(tmp_path)
    assert binding.ensure_workspace(offline) is binding
    assert (tmp_path / ".harumi" / "config.json").read_text() == before


def test_backfill_does_not_write_into_a_folder_relinked_meanwhile(tmp_path):
    binding = ProjectBinding.load(_legacy(tmp_path))

    def relink_then_answer(pid):
        ProjectBinding.write(tmp_path, "proj-2", REPO, environment="production", customer_id=None, workspace_known=True)
        return ORG

    binding.ensure_workspace(relink_then_answer)

    data = _raw(tmp_path)
    assert data["project_id"] == "proj-2" and data["customer_id"] is None


def test_a_read_only_binding_still_yields_the_answer_for_this_command(tmp_path, monkeypatch):
    binding = ProjectBinding.load(_legacy(tmp_path))

    def refuse(*a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr("harumi.config._write_json", refuse)

    filled = binding.ensure_workspace(lambda pid: ORG)
    assert filled.customer_id == ORG and filled.workspace_known
