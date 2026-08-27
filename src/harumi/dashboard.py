"""The dashboard spec widget contract, loaded from the generated schema artifact.

`WIDGET_SCHEMAS` below is built at import from ``dashboard-schema.json``, which
harumi-platform generates from ``packages/ui/src/dashboard/schema.ts`` (the
canonical source of truth) and vendors here. That replaces what used to be a
hand-maintained mirror: three copies of the same contract in three repos, kept
in step only by an identical literal pinned in each repo's test suite, which
caught a field change only once someone ran the other repo's tests and never
caught prose drift at all.

Refreshing it is a copy: ``cp <harumi-platform>/packages/ui/dashboard-schema.json
src/harumi/dashboard-schema.json``. ``tests/test_dashboard.py`` pins the
contract the CLI needs out of it, so a platform change that removes a field the
CLI depends on fails here rather than silently degrading validation.

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
    """Raised when the vendored schema artifact is missing or unreadable.

    Fatal rather than falling back to a built-in default: validating against a
    guessed contract would report a spec as fine while the platform drops half
    its widgets, which is worse than refusing to validate.
    """


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


def _load_widget_schemas() -> Dict[str, Tuple[WidgetField, ...]]:
    schemas: Dict[str, Tuple[WidgetField, ...]] = {}
    for widget in _artifact()["widgetTypes"]:
        fields = tuple(
            WidgetField(
                toml_key=field["tomlKey"],
                required=bool(field.get("required")),
                kind=field.get("type", "string"),
                values=tuple(field["values"]) if field.get("values") else None,
                is_output_path=bool(field.get("isOutputPath")),
            )
            for field in widget["fields"]
        )
        schemas[widget["type"]] = fields
    return schemas


WIDGET_SCHEMAS: Dict[str, Tuple[WidgetField, ...]] = _load_widget_schemas()

SCHEMA_VERSION: int = int(_artifact().get("version", 0))


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

    schema = WIDGET_SCHEMAS.get(type_)
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


DASHBOARD_DIR: str = _artifact().get("discovery", {}).get("dashboardDir", "dashboard")
ROOT_DASHBOARD_PATH: str = _artifact().get("discovery", {}).get("rootPath", "dashboard.toml")


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
    picker shows them: `dashboard/*.toml` (alphabetical) then the legacy root
    `dashboard.toml`. Mirrors `pickDashboardPaths` in harumi-platform's
    `apps/web/src/lib/dashboard-files.ts`."""
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
            schema = WIDGET_SCHEMAS[widget["type"]]
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
