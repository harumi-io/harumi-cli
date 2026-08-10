"""Offline tests for the named-environment machinery in harumi.config."""

from __future__ import annotations

import json

import pytest

import harumi.config as config
from harumi.config import Config


@pytest.fixture(autouse=True)
def isolated_harumi_home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    monkeypatch.delenv("HARUMI_ENV", raising=False)
    monkeypatch.delenv("HARUMI_API_URL", raising=False)
    monkeypatch.delenv("HARUMI_GIT_URL", raising=False)
    monkeypatch.delenv("HARUMI_ORG", raising=False)
    yield


def test_default_environment_is_production():
    cfg = Config.load()
    assert cfg.environment == "production"
    assert cfg.api_url == "https://api.harumi.io/api"
    # Regression: git_url must follow the same environment as api_url (they used
    # to default to mismatched prod-api / staging-git).
    assert cfg.git_url == "https://git.harumi.io"
    assert cfg.platform_url == "https://platform.harumi.io"


def test_staging_environment_urls():
    cfg = Config.load(environment="staging")
    assert cfg.environment == "staging"
    assert cfg.api_url == "https://api.dev.harumi.io/api"
    assert cfg.git_url == "https://git.dev.harumi.io"
    assert cfg.platform_url == "https://platform.dev.harumi.io"


def test_active_platform_url_follows_active_environment():
    # Regression: user-facing output (e.g. `harumi import`) must link to the
    # Harumi platform, never the underlying Gitea git_url.
    Config.load(environment="production")
    assert config.active_platform_url() == "https://platform.harumi.io"

    Config.load(environment="staging")
    assert config.active_platform_url() == "https://platform.dev.harumi.io"


def test_environment_precedence_env_var_over_saved(monkeypatch):
    config.save_environment("production")
    monkeypatch.setenv("HARUMI_ENV", "staging")
    assert Config.load().environment == "staging"


def test_explicit_arg_beats_env_var(monkeypatch):
    monkeypatch.setenv("HARUMI_ENV", "staging")
    assert Config.load(environment="production").environment == "production"


def test_unknown_environment_raises():
    with pytest.raises(ValueError, match="Unknown environment"):
        Config.load(environment="nope")


def test_credentials_are_isolated_per_environment():
    Config.load(environment="production")
    config.save_credentials(access_token="prod-tok", refresh_token="prod-ref")

    Config.load(environment="staging")
    config.save_credentials(access_token="stag-tok", refresh_token="stag-ref")

    Config.load(environment="production")
    assert config.load_credentials()["access_token"] == "prod-tok"

    Config.load(environment="staging")
    assert config.load_credentials()["access_token"] == "stag-tok"


def test_org_id_is_scoped_per_environment():
    prod = Config.load(environment="production")
    prod.save_org_id("org-prod")
    stag = Config.load(environment="staging")
    stag.save_org_id("org-stag")

    assert Config.load(environment="production").org_id == "org-prod"
    assert Config.load(environment="staging").org_id == "org-stag"


def test_legacy_credentials_migrate_into_production(tmp_path):
    # Simulate a pre-environments install: a flat credentials.json + org_id.
    config.CREDENTIALS_PATH.write_text(json.dumps({"access_token": "old-tok", "refresh_token": "old-ref"}))
    config.CONFIG_PATH.write_text(json.dumps({"org_id": "legacy-org", "api_url": "http://old"}))

    cfg = Config.load()  # resolves + migrates production

    assert cfg.environment == "production"
    assert cfg.org_id == "legacy-org"
    assert config.load_credentials()["access_token"] == "old-tok"
    # Legacy flat file is moved, not left behind.
    assert not config.CREDENTIALS_PATH.exists()
    # Global config keeps only the environment selection now.
    global_cfg = json.loads(config.CONFIG_PATH.read_text())
    assert "api_url" not in global_cfg and "org_id" not in global_cfg


def test_save_environment_rejects_unknown():
    with pytest.raises(ValueError, match="Unknown environment"):
        config.save_environment("nope")


def test_save_environment_persists_selection():
    config.save_environment("staging")
    assert json.loads(config.CONFIG_PATH.read_text())["environment"] == "staging"
    # A fresh resolve (no explicit arg / env var) picks it up.
    config.set_active_environment(config.resolve_environment())
    assert config.active_environment() == "staging"


def test_save_git_token_persists_username_for_url_auth():
    # Regression: the git remote's basic-auth username must be the real Gitea
    # account name, never the Harumi login email — an email contains '@',
    # which breaks unescaped basic-auth URLs. save_git_token/load_git_username
    # is the persistence half of that fix.
    config.save_git_token("tok123", git_url="https://git.dev.harumi.io", username="u-abc123")
    assert config.load_git_token() == "tok123"
    assert config.load_git_username() == "u-abc123"


def test_load_git_username_is_none_when_absent():
    config.save_git_token("tok123")  # no username — simulates a pre-fix credentials.json
    assert config.load_git_username() is None
