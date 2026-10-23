"""Every third-party module the package imports is declared as a dependency.

The 0.11.0 wheel did not start on Python 3.9: `dashboard.py` imports `tomli` there (the standard library only has
`tomllib` from 3.11), and `tomli` was in no dependency list. Nothing noticed, because every CI job installs the dev
extras and pytest depends on `tomli` on 3.9, so it was always present. This reads the source and the declared
dependencies, and does not care what happens to be installed.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "harumi"

# The standard library, without needing `sys.stdlib_module_names` (3.10 and later only): a module is the standard
# library's if it is built in or lives in the interpreter's own library folder rather than in site-packages.
STDLIB_ROOTS = {pathlib.Path(sys.modules["os"].__file__).resolve().parent}


# Standard-library modules that exist only on newer Pythons, so this interpreter cannot find them. They are
# imported behind a version check (the test below pins that), and what stands in for them is a dependency.
NEWER_STANDARD_LIBRARY = {"tomllib"}

# Standard-library modules that exist only on some operating systems, so this interpreter may not find them. config.py
# imports msvcrt (Windows file locking) behind a try, and the stdlib needs no declaring.
PLATFORM_STANDARD_LIBRARY = {"msvcrt"}


def is_standard_library(module: str) -> bool:
    if module in sys.builtin_module_names or module == "__future__" or module in NEWER_STANDARD_LIBRARY or module in PLATFORM_STANDARD_LIBRARY:
        return True
    spec = __import__("importlib.util", fromlist=["util"]).find_spec(module)
    if spec is None:
        return False
    origin = spec.origin
    if origin in (None, "built-in", "frozen"):
        return True
    path = pathlib.Path(origin).resolve()
    return any(root in path.parents or root == path.parent for root in STDLIB_ROOTS) and "site-packages" not in path.parts


def declared_dependencies() -> dict[str, str]:
    """{distribution name: its requirement line}, from `[project] dependencies` in pyproject.toml."""
    text = (ROOT / "pyproject.toml").read_text()
    block = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.S | re.M)
    assert block, "could not find [project] dependencies in pyproject.toml"
    lines = [ln.split("#")[0].strip() for ln in block.group(1).splitlines()]
    requirements = re.findall(r'"([^"]+)"', "\n".join(lines))
    return {re.split(r"[<>=!~;\s\[]", req, maxsplit=1)[0].lower().replace("_", "-"): req for req in requirements}


# A module's import name is not always its distribution's name. Only the ones this package uses are listed; a new
# third-party import that is not here fails the test below, which is the point.
DISTRIBUTION_OF = {
    "httpx": "httpx",
    "typer": "typer",
    "rich": "rich",
    "pydantic": "pydantic",
    "questionary": "questionary",
    "tomli": "tomli",
}


def third_party_imports() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name != "harumi" and not is_standard_library(name):
                    found.setdefault(name, set()).add(f"{path.name}:{node.lineno}")
    return found


def test_the_scan_finds_the_known_third_party_imports():
    """If the scan found nothing it would pass for the wrong reason."""
    found = third_party_imports()

    assert {"httpx", "typer", "rich", "pydantic"} <= set(found)
    assert "os" not in found and "json" not in found and "pathlib" not in found


def undeclared_imports(imports: dict[str, set[str]], declared: dict[str, str]) -> dict[str, str]:
    """The third-party modules in `imports` that no dependency in `declared` accounts for, with where and why."""
    undeclared = {}
    for module, where in imports.items():
        distribution = DISTRIBUTION_OF.get(module)
        if distribution is None:
            undeclared[module] = f"{sorted(where)[0]} (not in DISTRIBUTION_OF: add it, and declare it)"
        elif distribution not in declared:
            undeclared[module] = f"{sorted(where)[0]} (imports {module}, but {distribution} is not in [project] dependencies)"
    return undeclared


def test_every_third_party_import_is_a_declared_dependency():
    assert undeclared_imports(third_party_imports(), declared_dependencies()) == {}


def test_tomli_is_declared_for_the_pythons_that_have_no_tomllib():
    """`tomllib` is standard from 3.11; below that the source imports `tomli`, so it must be installed with the package."""
    requirement = declared_dependencies().get("tomli")

    assert requirement is not None, "tomli is imported on Python < 3.11 and is not a dependency"
    assert re.search(r'python_version\s*<\s*[\'"]3\.11[\'"]', requirement), requirement  # not installed where unneeded


def test_the_import_is_guarded_the_way_the_dependency_is():
    """The dependency is conditional on Python < 3.11, so the import must be too: an unconditional `import tomli`
    would fail on 3.11 and later, where it is not installed."""
    source = (SRC / "dashboard.py").read_text()

    assert re.search(r"if sys\.version_info >= \(3, 11\):\s+import tomllib\s+else:\s+import tomli as tomllib", source)


def test_the_check_fails_for_an_import_nobody_declared(tmp_path, monkeypatch):
    """The test above is not a rubber stamp: run the same check on a package that imports something nobody declared,
    and on one that imports a known library whose dependency line is missing (the original `tomli` bug)."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "mod.py").write_text("import httpx\nimport some_unlisted_library\nimport tomli\n")
    monkeypatch.setitem(globals(), "SRC", package)

    found = undeclared_imports(third_party_imports(), declared={"httpx": "httpx>=0.27"})

    assert set(found) == {"some_unlisted_library", "tomli"}
    assert "not in DISTRIBUTION_OF" in found["some_unlisted_library"]
    assert "tomli is not in [project] dependencies" in found["tomli"]
    assert "httpx" not in found


def test_the_check_passes_when_everything_is_declared(tmp_path, monkeypatch):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "mod.py").write_text("import httpx\nimport json\n")
    monkeypatch.setitem(globals(), "SRC", package)

    assert undeclared_imports(third_party_imports(), declared={"httpx": "httpx>=0.27"}) == {}


@pytest.mark.parametrize("module", ["os", "json", "pathlib", "re", "tomllib", "typing", "subprocess", "urllib"])
def test_the_standard_library_is_recognised_without_sys_stdlib_module_names(module):
    assert is_standard_library(module)


@pytest.mark.parametrize("module", ["httpx", "typer", "rich", "pydantic"])
def test_third_party_modules_are_not_mistaken_for_the_standard_library(module):
    assert not is_standard_library(module)
