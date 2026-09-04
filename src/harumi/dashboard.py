"""The dashboard spec widget contract, loaded from the generated schema artifact.

`widget_schemas()` reads ``dashboard-schema.json``, which harumi-platform
generates from ``packages/ui/src/dashboard/schema.ts`` (the canonical source of
truth) and vendors here. That replaces what used to be a hand-maintained mirror:
three copies of the same contract in three repos, kept in step only by an
identical literal pinned in each repo's test suite, which caught a field change
only once someone ran the other repo's tests and never caught prose drift at all.

It's read on first use rather than at import, because ``cli.py`` imports this
module at module level — an eager load would let a corrupt artifact break every
command, including ones that never touch a dashboard.

Refreshing it is a copy from a harumi-platform checkout::

    cp <harumi-platform>/packages/ui/dashboard-schema.json src/harumi/dashboard-schema.json

or, without one, from a running deployment (harumi-api ≥ the release that added
``GET /api/public/dashboard-schema``; older ones 404, in which case use the
``cp`` above)::

    curl -fsSL https://api.harumi.io/api/public/dashboard-schema \
      -o src/harumi/dashboard-schema.json

Either way the result is checked, not trusted: ``tests/test_dashboard.py`` pins
the contract the CLI needs out of it — including the ``discovery`` block and the
fields the validator reads — so a refresh that fetched something unusable, or a
platform change that removed a field the CLI depends on, fails here rather than
silently degrading validation.

Only the machine-checkable contract is used here (toml key, required, enum
values, and which fields are dot-paths into ``output.json``). The artifact also
carries prose docs and examples for the agent's reference tool; those are
ignored — human-facing prose for the CLI lives in the skill's
``references/dashboard.md``, and drift there is a doc bug, not a broken
validator.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


SCHEMA_ARTIFACT_PATH = Path(__file__).with_name("dashboard-schema.json")


@dataclass(frozen=True)
class WidgetField:
    toml_key: str
    required: bool = False
    kind: str = "string"  # "string" | "number" | "enum" | "columns" | "series"
    values: Optional[Tuple[str, ...]] = None
    # True for fields that are a dot-path into the run's output.json (as
    # opposed to a field name *within* an already-resolved array item, e.g.
    # `x_key`/`series[].key`/gantt's `label_key` etc.).
    is_output_path: bool = False


class DashboardSchemaError(RuntimeError):
    """Raised when the vendored schema artifact is missing or unusable.

    Fatal for the dashboard commands rather than falling back to a built-in
    contract: validating against a guessed schema would report a spec as fine
    while the platform drops half its widgets, which is worse than refusing.

    Deliberately *not* fatal for the rest of the CLI — see `widget_schemas()`.
    """


# The field kinds `_coerce_field` below knows how to check. A kind outside this
# set would fall through to "no value is ever valid", quietly making a required
# field impossible to satisfy and an optional one impossible to use — so a typo
# in the artifact is rejected at load rather than silently weakening validation.
_KNOWN_FIELD_KINDS = frozenset({"string", "number", "enum", "columns", "series"})


@lru_cache(maxsize=1)
def _artifact() -> Dict[str, Any]:
    try:
        raw = SCHEMA_ARTIFACT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise DashboardSchemaError(f"cannot read {SCHEMA_ARTIFACT_PATH.name}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DashboardSchemaError(f"{SCHEMA_ARTIFACT_PATH.name} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DashboardSchemaError(f"{SCHEMA_ARTIFACT_PATH.name} is not a JSON object")
    if not isinstance(parsed.get("widgetTypes"), list):
        raise DashboardSchemaError(f"{SCHEMA_ARTIFACT_PATH.name} has no widgetTypes array")
    return parsed


def _widget_field(widget_type: str, field: Any) -> WidgetField:
    if not isinstance(field, dict):
        raise DashboardSchemaError(f'{SCHEMA_ARTIFACT_PATH.name}: widget "{widget_type}" has a non-object field')
    try:
        toml_key = field["tomlKey"]
    except (KeyError, TypeError) as exc:
        raise DashboardSchemaError(f'{SCHEMA_ARTIFACT_PATH.name}: widget "{widget_type}" has a field with no tomlKey') from exc

    kind = field.get("type", "string")
    if kind not in _KNOWN_FIELD_KINDS:
        raise DashboardSchemaError(
            f'{SCHEMA_ARTIFACT_PATH.name}: widget "{widget_type}" field "{toml_key}" has unknown type "{kind}" '
            f"(known: {', '.join(sorted(_KNOWN_FIELD_KINDS))})"
        )
    if kind == "enum" and not field.get("values"):
        raise DashboardSchemaError(
            f'{SCHEMA_ARTIFACT_PATH.name}: widget "{widget_type}" field "{toml_key}" is an enum with no values'
        )

    return WidgetField(
        toml_key=toml_key,
        required=bool(field.get("required")),
        kind=kind,
        values=tuple(field["values"]) if field.get("values") else None,
        is_output_path=bool(field.get("isOutputPath")),
    )


@lru_cache(maxsize=1)
def widget_schemas() -> Dict[str, Tuple[WidgetField, ...]]:
    """The widget contract, read from the vendored artifact on first use.

    Lazy on purpose. ``cli.py`` imports this module at module level, so loading
    the artifact at import would mean a corrupt or missing JSON file takes down
    *every* command — ``harumi --version`` and ``harumi login`` included — for a
    file only the dashboard commands need. (The hardcoded schema this replaced
    couldn't fail, so eager loading would have been a real regression; see the
    click/typer note in pyproject.toml for the last time a startup-time failure
    bit this CLI.) Raises `DashboardSchemaError` here instead, where only the
    dashboard commands are affected.
    """
    schemas: Dict[str, Tuple[WidgetField, ...]] = {}
    for widget in _artifact()["widgetTypes"]:
        if not isinstance(widget, dict):
            raise DashboardSchemaError(f"{SCHEMA_ARTIFACT_PATH.name}: widgetTypes contains a non-object entry")
        widget_type = widget.get("type")
        if not isinstance(widget_type, str):
            raise DashboardSchemaError(f"{SCHEMA_ARTIFACT_PATH.name}: a widget type entry has no string type")
        fields = widget.get("fields")
        if not isinstance(fields, list):
            raise DashboardSchemaError(f'{SCHEMA_ARTIFACT_PATH.name}: widget "{widget_type}" has no fields array')
        schemas[widget_type] = tuple(_widget_field(widget_type, field) for field in fields)
    return schemas


def schema_version() -> int:
    """Version of the vendored artifact. Lazy for the same reason as
    `widget_schemas()`."""
    version = _artifact().get("version", 0)
    if isinstance(version, bool) or not isinstance(version, int):
        raise DashboardSchemaError(f"{SCHEMA_ARTIFACT_PATH.name}: version must be an integer, got {version!r}")
    return version


def _coerce_columns(value: Any) -> Optional[List[Dict[str, str]]]:
    if not isinstance(value, list):
        return None
    columns = []
    for col in value:
        if not isinstance(col, dict) or not isinstance(col.get("key"), str):
            continue
        columns.append({"key": col["key"], "label": col.get("label") if isinstance(col.get("label"), str) else col["key"]})
    return columns or None


def _coerce_series(value: Any) -> Optional[List[Dict[str, str]]]:
    if not isinstance(value, list):
        return None
    series = []
    for s in value:
        if not isinstance(s, dict) or not isinstance(s.get("key"), str):
            continue
        entry = {"key": s["key"], "label": s.get("label") if isinstance(s.get("label"), str) else s["key"]}
        if isinstance(s.get("color"), str):
            entry["color"] = s["color"]
        series.append(entry)
    return series or None


def _coerce_field(value: Any, field: WidgetField) -> Any:
    if value is None:
        return None
    if field.kind == "string":
        return value if isinstance(value, str) else None
    if field.kind == "number":
        # bool is an int subclass in Python; `true` is not a number here.
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if field.kind == "enum":
        return value if isinstance(value, str) and field.values and value in field.values else None
    if field.kind == "columns":
        return _coerce_columns(value)
    if field.kind == "series":
        return _coerce_series(value)
    return None


@dataclass(frozen=True)
class WidgetIssue:
    """A problem found while validating `dashboard.toml`.

    `dropped` mirrors `parseDashboardConfig`'s behavior: the platform never
    fails the whole dashboard for a bad widget, it just silently omits it.
    `dropped=False` issues (unresolved output paths) are CLI-only extras —
    the widget still renders, just empty.
    """

    widget_id: Optional[str]
    message: str
    dropped: bool = True


def parse_widget_entry(entry: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[WidgetIssue]]:
    """Validate one raw `[[widgets]]` table entry. Mirrors `parseWidgetEntry`
    in schema.ts: returns either a coerced widget dict, or the (single,
    first-found) reason it was dropped."""
    type_ = entry.get("type")
    id_ = entry.get("id")
    title = entry.get("title")
    if not isinstance(type_, str) or not isinstance(id_, str) or not isinstance(title, str):
        return None, WidgetIssue(id_ if isinstance(id_, str) else None, "missing or invalid type/id/title")

    schema = widget_schemas().get(type_)
    if schema is None:
        return None, WidgetIssue(id_, f'widget "{id_}": unknown type "{type_}"')

    widget: Dict[str, Any] = {"type": type_, "id": id_, "title": title}
    for field in schema:
        coerced = _coerce_field(entry.get(field.toml_key), field)
        if coerced is not None:
            widget[field.toml_key] = coerced
        elif field.required:
            return None, WidgetIssue(id_, f'widget "{id_}" ({type_}): missing or invalid "{field.toml_key}"')

    return widget, None


def resolve_path(data: Dict[str, Any], path: str) -> Any:
    """Dot-path lookup, e.g. `resolve_path(data, "totals.revenue")`."""
    value: Any = data
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value


def describe_missing_key(data: Dict[str, Any], path: str) -> str:
    """Mirrors `describeMissingKey`: explains why a dot-path came back empty,
    surfacing the sibling keys available at the point resolution broke down."""
    current: Any = data
    for key in path.split("."):
        if not isinstance(current, dict):
            return f'"{path}" isn\'t in this run\'s output'
        if key not in current:
            available = list(current.keys())
            if available:
                return f'"{path}" isn\'t in this run\'s output — found: {", ".join(available)}'
            return f'"{path}" isn\'t in this run\'s output'
        current = current[key]
    if isinstance(current, list):
        return f'"{path}" is an empty list in this run\'s output' if not current else ""
    if current is not None:
        return f'"{path}" is a {type(current).__name__}, not a list, in this run\'s output'
    return f'"{path}" isn\'t in this run\'s output'


class DashboardTomlError(ValueError):
    """Raised when a dashboard spec isn't valid TOML."""


# The discovery rule (which files are dashboard specs, in what display order) is
# structural rather than part of the widget contract, so it stays a plain
# constant: `harumi dashboard list` keeps working even when the artifact is
# unreadable, and this file's import can't fail. The artifact publishes the same
# two values under `discovery` for consumers that have no copy of their own;
# harumi-platform's packages/ui/src/dashboard/discovery.ts is the source.
#
# Deliberately not read from the artifact at runtime, even though it carries the
# values: that would either move the read to import (breaking the guarantee
# above) or add a lazy accessor whose fallback branch is the only one that ever
# behaves differently. Instead `tests/test_dashboard.py` pins these two against
# the vendored artifact's `discovery` block, so re-vendoring an artifact that
# moved the rule fails there rather than leaving the CLI quietly enumerating the
# old location. The copy is a fallback, not a second definition.
DASHBOARD_DIR = "dashboard"
ROOT_DASHBOARD_PATH = "dashboard.toml"


def is_dashboard_path(path: str) -> bool:
    """Whether `path` is a dashboard spec the platform would render:
    `dashboard/<name>.toml` (one level deep) or the legacy root `dashboard.toml`."""
    if path == ROOT_DASHBOARD_PATH:
        return True
    prefix = f"{DASHBOARD_DIR}/"
    if not (path.startswith(prefix) and path.endswith(".toml")):
        return False
    return "/" not in path[len(prefix) :]


def pick_dashboard_paths(paths: Iterable[str]) -> List[str]:
    """The dashboard specs in a flat repo listing, in the order the platform's
    picker shows them: ``dashboard/*.toml`` (code-point sorted) then the legacy
    root ``dashboard.toml``. Mirrors ``pickDashboardPaths`` in harumi-platform's
    ``packages/ui/src/dashboard/discovery.ts``.

    Plain ``sorted()`` is the shared order. The frontend used ``localeCompare``
    until it was aligned to code point, which disagreed for names like
    ``costs-v2.toml`` / ``costs_v2.toml``, so ``harumi dashboard list`` could
    number specs differently from the browser's picker.
    """
    all_paths = list(paths)
    in_dir = sorted(p for p in all_paths if p != ROOT_DASHBOARD_PATH and is_dashboard_path(p))
    if ROOT_DASHBOARD_PATH in all_paths:
        in_dir.append(ROOT_DASHBOARD_PATH)
    return in_dir


def local_dashboard_paths(root: Path) -> List[str]:
    """The same discovery against a working copy — `<root>/dashboard/*.toml`
    then `<root>/dashboard.toml`. Returns repo-relative paths (posix), so the
    result is directly comparable with `pick_dashboard_paths`."""
    candidates = [p.name for p in sorted((root / DASHBOARD_DIR).glob("*.toml"))]
    found = [f"{DASHBOARD_DIR}/{name}" for name in candidates]
    if (root / ROOT_DASHBOARD_PATH).is_file():
        found.append(ROOT_DASHBOARD_PATH)
    return found


def validate_dashboard_toml(
    raw: str, output: Optional[Dict[str, Any]] = None
) -> Tuple[List[Dict[str, Any]], List[WidgetIssue]]:
    """Parses and validates a dashboard spec, mirroring
    `parseDashboardConfig` + `parseWidgetEntry`. Returns the widgets that
    would actually render, plus every issue found (dropped widgets first,
    then — only when `output` is given — output.json dot-paths that won't
    resolve, which the platform itself can't check ahead of time).

    A top-level `title` (the picker label when a project has several
    dashboards) and a `[layout]` table are both accepted and ignored here —
    neither affects whether a widget renders.
    """
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise DashboardTomlError(str(exc)) from exc

    raw_widgets = parsed.get("widgets")
    if not isinstance(raw_widgets, list):
        raw_widgets = []

    widgets: List[Dict[str, Any]] = []
    issues: List[WidgetIssue] = []
    for entry in raw_widgets:
        if not isinstance(entry, dict):
            issues.append(WidgetIssue(None, "widget entry is not a table"))
            continue
        widget, issue = parse_widget_entry(entry)
        if widget is not None:
            widgets.append(widget)
        else:
            assert issue is not None
            issues.append(issue)

    if output is not None:
        for widget in widgets:
            schema = widget_schemas()[widget["type"]]
            for field in schema:
                if not field.is_output_path:
                    continue
                path = widget.get(field.toml_key)
                if not isinstance(path, str):
                    continue
                resolved = resolve_path(output, path)
                if resolved is None:
                    issues.append(
                        WidgetIssue(
                            widget["id"],
                            f'widget "{widget["id"]}" ({widget["type"]}): '
                            f'{describe_missing_key(output, path)} (from "{field.toml_key}")',
                            dropped=False,
                        )
                    )

    return widgets, issues
