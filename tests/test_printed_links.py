"""Every link the CLI prints must open the web app.

The built-in web hosts were `platform.harumi.io` (answers 503) and
`platform.dev.harumi.io` (404); the app is served from `app.` hosts. These tests
pin the hosts, the routes, and that overrides still win.
"""

from __future__ import annotations

import re

import pytest

from harumi import cli, config
from harumi.config import ENVIRONMENTS, active_platform_url


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("harumi.config.HARUMI_HOME", tmp_path)
    monkeypatch.setattr("harumi.config.CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr("harumi.config.CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr("harumi.config._ACTIVE_ENV", None)
    for var in ("HARUMI_ENV", "HARUMI_PLATFORM_URL"):
        monkeypatch.delenv(var, raising=False)


def test_the_built_in_hosts_are_the_working_web_app():
    assert ENVIRONMENTS["production"].platform_url == "https://app.harumi.io"
    assert ENVIRONMENTS["staging"].platform_url == "https://app.dev.harumi.io"


def test_no_platform_subdomain_is_left_anywhere_in_the_source():
    """The old hosts are dead ends. Catch one creeping back in, in code or docs."""
    import pathlib

    root = pathlib.Path(cli.__file__).resolve().parents[2]
    offenders = []
    for path in [*(root / "src").rglob("*.py"), root / "README.md", *(root / ".agents").rglob("*.md")]:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"platform(\.dev)?\.harumi\.io", line):
                offenders.append(f"{path.relative_to(root)}:{n}")
    assert offenders == []


@pytest.mark.parametrize(
    ("env", "host"), [("production", "https://app.harumi.io"), ("staging", "https://app.dev.harumi.io")]
)
def test_every_link_the_cli_builds_starts_with_the_active_hosts_app(monkeypatch, env, host):
    monkeypatch.setenv("HARUMI_ENV", env)
    config.Config.load()

    assert cli._billing_url() == f"{host}/settings?tab=billing"

    link = type("L", (), {"token": "tok123"})()
    assert cli._share_link_url(link) == f"{host}/share/tok123"


def test_the_environment_override_still_wins(monkeypatch):
    monkeypatch.setenv("HARUMI_PLATFORM_URL", "http://localhost:5173/")
    config.Config.load()

    assert active_platform_url() == "http://localhost:5173"
    assert cli._billing_url() == "http://localhost:5173/settings?tab=billing"


def test_a_per_environment_override_in_config_still_wins(tmp_path):
    config.Config.load()
    path = config.env_config_path("production")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"platform_url": "https://self-hosted.example.com"}')

    assert active_platform_url() == "https://self-hosted.example.com"
