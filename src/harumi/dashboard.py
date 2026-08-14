"""The `dashboard.toml` widget contract — a mirror of `WIDGET_SCHEMAS` in
harumi-platform's `packages/ui/src/dashboard/schema.ts`.

# ponytail: this is a hand-maintained mirror of `WIDGET_SCHEMAS` — harumi-cli
# has no dependency on that TS package (or on ai-solver, which keeps its own
# mirror), so there's no automated way to keep these in sync; a change to
# one that isn't ported to the others silently makes this module accept (or
# reject) a widget shape the platform disagrees with. Ceiling: three
# hand-synced copies (harumi-platform, ai-solver, harumi-cli), with the
# field contract (toml key + required + enum values) pinned by an identical
# literal in each repo's test suite (`schema.test.ts`,
# `tests/agents/test_dashboard_tools.py`, `tests/test_dashboard.py` here) so
# a field-level change fails every suite until it's ported — prose-only doc
# drift is still uncaught. Upgrade path: have harumi-api serve
# `WIDGET_SCHEMAS` as JSON (generated from schema.ts at build time) and have
# this module fetch that instead of hardcoding it. Whoever edits
# `WIDGET_SCHEMAS` in schema.ts must update this file (and its pinned test)
# in the same change — see harumi-platform's dashboard-widgets cursor rule.

Only the machine-checkable contract lives here (toml key, required, enum
values, and which fields are dot-paths into `output.json`). Prose
descriptions/examples for humans live in the CLI skill's
`references/dashboard.md` — drift there is a doc bug, not a broken
validator.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass(frozen=True)
class WidgetField:
    toml_key: str
    required: bool = False
    kind: str = "string"  # "string" | "enum" | "columns" | "series"
    values: Optional[Tuple[str, ...]] = None
    # True for fields that are a dot-path into the run's output.json (as
    # opposed to a field name *within* an already-resolved array item, e.g.
    # `x_key`/`series[].key`/gantt's `label_key` etc.).
    is_output_path: bool = False


_CHART_FIELDS: Tuple[WidgetField, ...] = (
    WidgetField("data_key", required=True, is_output_path=True),
    WidgetField("x_key", required=True),
    WidgetField("series", required=True, kind="series"),
)

WIDGET_SCHEMAS: Dict[str, Tuple[WidgetField, ...]] = {
    "metric": (
        WidgetField("value_key", required=True, is_output_path=True),
        WidgetField("delta_key", is_output_path=True),
        WidgetField("format", kind="enum", values=("number", "currency", "percent")),
        WidgetField("unit"),
    ),
    "table": (
        WidgetField("rows_key", required=True, is_output_path=True),
        WidgetField("columns", required=True, kind="columns"),
    ),
    "line-chart": _CHART_FIELDS,
    "bar-chart": _CHART_FIELDS,
    "gantt-chart": (
        WidgetField("tasks_key", required=True, is_output_path=True),
        WidgetField("resource_key"),
        WidgetField("label_key"),
        WidgetField("start_key"),
        WidgetField("end_key"),
        WidgetField("duration_key"),
        WidgetField("color_key"),
        WidgetField("time_unit"),
    ),
}


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
    """Raised when `dashboard.toml` itself isn't valid TOML."""


def validate_dashboard_toml(
    raw: str, output: Optional[Dict[str, Any]] = None
) -> Tuple[List[Dict[str, Any]], List[WidgetIssue]]:
    """Parses and validates `dashboard.toml`, mirroring
    `parseDashboardConfig` + `parseWidgetEntry`. Returns the widgets that
    would actually render, plus every issue found (dropped widgets first,
    then — only when `output` is given — output.json dot-paths that won't
    resolve, which the platform itself can't check ahead of time).
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
