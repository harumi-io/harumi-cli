from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest
from packaging.version import Version

from scripts.staging_version import stamp


def test_staging_versions_are_unique_and_between_stable_releases(tmp_path: Path):
    path = tmp_path / "version.py"

    def make(run, attempt):
        path.write_text('__version__ = "1.2.3"\n', encoding="utf-8")
        version = stamp(path, run, attempt, "1.2.3")
        assert path.read_text(encoding="utf-8") == f'__version__ = "{version}"\n'
        return Version(version)

    first, retry, next_run = make(12, 1), make(12, 2), make(13, 1)
    assert Version("1.2.3") < first < retry < next_run < Version("1.2.4")
    assert first.is_prerelease


def test_staging_version_exceeds_a_newer_stable_hotfix(tmp_path):
    path = tmp_path / "version.py"
    path.write_text('__version__ = "1.1.0"\n', encoding="utf-8")
    assert stamp(path, 1, 1, "1.1.1") == "1.1.2.dev1001"


def test_staging_version_uses_source_when_ahead_of_pypi(tmp_path):
    path = tmp_path / "version.py"
    path.write_text('__version__ = "1.2.0"\n', encoding="utf-8")
    assert stamp(path, 1, 1, "1.1.5") == "1.2.1.dev1001"


def test_pip_prerelease_specifier_does_not_enable_dependency_prereleases(tmp_path):
    # A local index exercises the documented pip resolver policy without contacting PyPI.
    for name, version, requirement in [
        ("stagingprobe", "1.0.0", "Requires-Dist: dep-probe>=1\n"),
        ("stagingprobe", "1.0.1.dev1", "Requires-Dist: dep-probe>=1\n"),
        ("dep_probe", "1.0.0", ""),
        ("dep_probe", "2.0.0.dev1", ""),
    ]:
        info = f"{name}-{version}.dist-info"
        with ZipFile(tmp_path / f"{name}-{version}-py3-none-any.whl", "w") as wheel:
            wheel.writestr(f"{info}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{requirement}\n")
            wheel.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            wheel.writestr(f"{info}/RECORD", "")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "--isolated", "install", "--dry-run", "--ignore-installed",
         "--no-index", "--find-links", str(tmp_path), "stagingprobe>=0.0.0.dev0"],
        capture_output=True, text=True, check=True,
    )
    assert "stagingprobe-1.0.1.dev1" in result.stdout
    assert "dep_probe-1.0.0" in result.stdout
    assert "dep_probe-2.0.0.dev1" not in result.stdout


def test_stamp_accepts_the_real_source_version(tmp_path):
    source = Path(__file__).resolve().parent.parent / "src/harumi/__init__.py"
    copy = tmp_path / "__init__.py"
    copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    assert Version(stamp(copy, 1, 1, "0.0.0")).is_devrelease


@pytest.mark.parametrize("text,run,attempt,floor", [
    ('__version__ = "1.2.3.dev1"\n', 1, 1, "1.2.3"),
    ('__version__ = "1.2.3"\n__version__ = "1.2.4"\n', 1, 1, "1.2.3"),
    ('__version__ = "1.2.3"\n', 0, 1, "1.2.3"),
    ('__version__ = "1.2.3"\n', 1, 1000, "1.2.3"),
    ('__version__ = "1.2.3"\n', 1, 1, "1.3.0.dev1"),
    ('__version__ = "1.2.3"\n', 1, 1, ""),
])
def test_invalid_inputs_fail_without_modifying_source(tmp_path, text, run, attempt, floor):
    path = tmp_path / "version.py"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        stamp(path, run, attempt, floor)
    assert path.read_text(encoding="utf-8") == text
