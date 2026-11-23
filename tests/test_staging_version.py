from pathlib import Path

import pytest
from packaging.version import Version

from scripts.staging_version import stamp


def test_staging_versions_are_unique_and_between_stable_releases(tmp_path: Path):
    path = tmp_path / "version.py"

    def make(run, attempt):
        path.write_text('__version__ = "1.2.3"\n', encoding="utf-8")
        version = stamp(path, run, attempt)
        assert path.read_text(encoding="utf-8") == f'__version__ = "{version}"\n'
        return Version(version)

    first, retry, next_run = make(12, 1), make(12, 2), make(13, 1)
    assert Version("1.2.3") < first < retry < next_run < Version("1.2.4")
    assert first.is_prerelease


@pytest.mark.parametrize("text,run,attempt", [
    ('__version__ = "1.2.3.dev1"\n', 1, 1),
    ('__version__ = "1.2.3"\n__version__ = "1.2.4"\n', 1, 1),
    ('__version__ = "1.2.3"\n', 0, 1),
    ('__version__ = "1.2.3"\n', 1, 1000),
])
def test_invalid_inputs_fail_without_modifying_source(tmp_path, text, run, attempt):
    path = tmp_path / "version.py"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        stamp(path, run, attempt)
    assert path.read_text(encoding="utf-8") == text
