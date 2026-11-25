"""The CLI validates declarations only; it never opens the named sources."""

import json
import os

import pytest
from typer.testing import CliRunner

import harumi.cli as cli
from harumi.canvas_manifest import CanvasManifestError, read_manifest, validate_manifest

VALID = {
    "version": 1,
    "entry": "main.tsx",
    "files": ["main.tsx", "styles/app.css"],
    "capabilities": ["data.read"],
}


def test_valid_contract_matches_the_portable_examples():
    validate_manifest(VALID)
    validate_manifest({**VALID, "version": 1.0, "capabilities": []})
    validate_manifest({"version": 1, "entry": "__proto__", "files": ["__proto__"], "capabilities": []})


@pytest.mark.parametrize("invalid", [
    None, [], {}, {**VALID, "version": True}, {**VALID, "version": 2},
    {**VALID, "extra": "unknown"}, {**VALID, "files": []},
    {**VALID, "files": ["main.tsx", "main.tsx"]},
    {**VALID, "files": ["main.tsx", "MAIN.tsx"]},
    {**VALID, "files": ["main.tsx", "main.tsx/child"]},
    {**VALID, "files": ["main.tsx", "../secret"]},
    {**VALID, "files": ["main.tsx", "CON.txt"]},
    {**VALID, "files": ["main.tsx", "dir/name."]},
    {**VALID, "files": ["main.tsx", "a\\b"]},
    {**VALID, "files": ["main.tsx", "café.tsx"]},
    {**VALID, "files": ["main.tsx", "x" * 201]},
    {**VALID, "files": ["main.tsx"] + [f"f{i}.ts" for i in range(32)]},
    {**VALID, "files": ["main.tsx", 1]},
    {**VALID, "entry": "MAIN.tsx"},
    {**VALID, "capabilities": ["run"]},
    {**VALID, "capabilities": ["data.read", "data.read"]},
])
def test_invalid_declarations_are_rejected(invalid):
    with pytest.raises(CanvasManifestError):
        validate_manifest(invalid)


@pytest.mark.parametrize("raw", [b"\xff", b"{", b"{\"version\":NaN}", b"x" * 64_001])
def test_local_read_rejects_invalid_or_oversized_json(tmp_path, raw):
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    with pytest.raises(CanvasManifestError):
        read_manifest(path)


def test_named_pipe_is_rejected_without_waiting_for_a_writer(tmp_path):
    if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("requires nonblocking POSIX FIFO")
    path = tmp_path / "pipe"
    os.mkfifo(path)
    with pytest.raises(CanvasManifestError, match="regular file"):
        read_manifest(path)


def test_command_is_explicit_about_what_it_did_not_check(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    # Named source files deliberately do not exist: no source read is implied.
    monkeypatch.setattr(cli, "_get_client", lambda **_: pytest.fail("network client must not be created"))
    result = CliRunner().invoke(cli.app, ["canvas", "validate-manifest", str(path)])
    assert result.exit_code == 0, result.output
    assert "Source files were not checked, compiled, rendered or published" in result.output


def test_command_reports_invalid_manifest_without_a_traceback(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"version": 2}', encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["canvas", "validate-manifest", str(path)])
    assert result.exit_code == 1
    assert "Error:" in result.output and "Traceback" not in result.output
