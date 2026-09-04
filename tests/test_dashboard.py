"""Tests for harumi.dashboard: the widget contract loaded from the vendored
schema artifact, dashboard spec discovery, and the spec validator.
"""

from __future__ import annotations

import importlib
import json
from unittest import mock

import pytest

from harumi.dashboard import (
    DashboardTomlError,
    describe_missing_key,
    local_dashboard_paths,
    parse_widget_entry,
    pick_dashboard_paths,
    resolve_path,
    validate_dashboard_toml,
    widget_schemas,
)

# The contract the CLI actually depends on, out of the vendored
# `dashboard-schema.json`. This is no longer a cross-repo hand-sync pin (the
# artifact is generated from harumi-platform's schema.ts, so the copies can't
# drift) — it's a guard on the *refresh*: re-vendoring an artifact that drops or
# renames a field the CLI validates against fails here instead of quietly
# validating less than it used to.
_EXPECTED_WIDGET_CONTRACT = {
    "metric": ["value_key!*", "delta_key*", "format[number|currency|percent]", "unit"],
    "table": ["rows_key!*", "columns!"],
    "line-chart": ["data_key!*", "x_key!", "series!"],
    "bar-chart": ["data_key!*", "x_key!", "series!"],
    "gantt-chart": [
        "tasks_key!*",
        "resource_key",
        "label_key",
        "start_key",
        "end_key",
        "duration_key",
        "color_key",
        "time_unit",
    ],
    "timeline": [
        "items_key!*",
        "resource_key",
        "label_key",
        "start_key",
        "end_key",
        "duration_key",
        "color_key",
        "id_key",
        "regions_key*",
        "region_start_key",
        "region_end_key",
        "region_label_key",
        "region_resource_key",
        "time_unit",
    ],
}


def test_widget_contract_matches_the_vendored_schema_artifact():
    """`!` marks required, `*` a dot-path into output.json, `[a|b]` an enum's values."""
    contract = {
        type_: [
            field.toml_key
            + ("!" if field.required else "")
            + ("*" if field.is_output_path else "")
            + (f"[{'|'.join(field.values)}]" if field.kind == "enum" else "")
            for field in fields
        ]
        for type_, fields in widget_schemas().items()
    }
    # Failing here means the re-vendored artifact changed the contract. Check
    # that the change is intended, then update this literal.
    assert contract == _EXPECTED_WIDGET_CONTRACT


def test_discovery_rule_matches_the_vendored_schema_artifact():
    """The discovery constants stay hardcoded so `harumi dashboard list` survives
    an unreadable artifact (see `test_schema_artifact_is_loaded_not_hardcoded`),
    which means they're a copy — and until this test existed, an unchecked one.

    The artifact carries the same two values under `discovery`, generated from
    harumi-platform's `packages/ui/src/dashboard/discovery.ts`. Pinning them
    against each other turns the copy into a fallback: re-vendoring an artifact
    that moved the rule fails here instead of leaving the CLI enumerating the
    old location while every other suite stays green.
    """
    from harumi.dashboard import DASHBOARD_DIR, ROOT_DASHBOARD_PATH, _artifact

    discovery = _artifact().get("discovery")
    assert isinstance(discovery, dict), "the artifact publishes the discovery rule"
    # Named explicitly so a renamed key reads as "the artifact stopped publishing
    # dashboardDir" rather than a bare KeyError from the comparison below.
    assert "dashboardDir" in discovery, "artifact's discovery block lost dashboardDir"
    assert "rootPath" in discovery, "artifact's discovery block lost rootPath"
    assert discovery["dashboardDir"] == DASHBOARD_DIR
    assert discovery["rootPath"] == ROOT_DASHBOARD_PATH


def test_schema_artifact_is_loaded_not_hardcoded():
    """The whole point of vendoring: an unusable artifact must fail loudly
    rather than fall back to a stale built-in contract that reports a broken
    spec as fine."""
    from harumi.dashboard import SCHEMA_ARTIFACT_PATH, DashboardSchemaError, _artifact, schema_version

    assert SCHEMA_ARTIFACT_PATH.is_file(), "the artifact ships inside the package"
    assert schema_version() >= 1

    # Patch the artifact path's own `read_text`, not `Path.read_text` globally,
    # so an unrelated file read inside this test can't fail too.
    _artifact.cache_clear()
    try:
        with mock.patch.object(type(SCHEMA_ARTIFACT_PATH), "read_text", side_effect=OSError("boom")):
            with pytest.raises(DashboardSchemaError):
                _artifact()
    finally:
        _artifact.cache_clear()


@pytest.mark.parametrize(
    ("artifact", "expected_fragment"),
    [
        ("not json at all {{{", "not valid JSON"),
        ('["a", "list"]', "not a JSON object"),
        ('{"version": 1}', "no widgetTypes"),
        ('{"version": 1, "widgetTypes": ["nope"]}', "non-object entry"),
        ('{"version": 1, "widgetTypes": [{"fields": []}]}', "no string type"),
        ('{"version": 1, "widgetTypes": [{"type": "metric"}]}', "no fields array"),
        (
            '{"version": 1, "widgetTypes": [{"type": "metric", "fields": [{"required": true}]}]}',
            "no tomlKey",
        ),
        # A typo in a field's type would otherwise make that field impossible to
        # satisfy — validation would silently accept widgets the platform drops,
        # the exact failure this contract exists to catch.
        (
            '{"version": 1, "widgetTypes": [{"type": "metric", "fields": '
            '[{"tomlKey": "value_key", "type": "strnig"}]}]}',
            'unknown type "strnig"',
        ),
        (
            '{"version": 1, "widgetTypes": [{"type": "metric", "fields": '
            '[{"tomlKey": "format", "type": "enum"}]}]}',
            "enum with no values",
        ),
    ],
)
def test_malformed_artifact_raises_a_clear_error(artifact, expected_fragment, tmp_path, monkeypatch):
    """A broken artifact must produce DashboardSchemaError, not a KeyError or a
    quietly weakened contract."""
    import harumi.dashboard as dashboard_module
    from harumi.dashboard import DashboardSchemaError

    path = tmp_path / "dashboard-schema.json"
    path.write_text(artifact, encoding="utf-8")
    monkeypatch.setattr(dashboard_module, "SCHEMA_ARTIFACT_PATH", path)

    dashboard_module._artifact.cache_clear()
    dashboard_module.widget_schemas.cache_clear()
    try:
        with pytest.raises(DashboardSchemaError) as exc:
            dashboard_module.widget_schemas()
        assert expected_fragment in str(exc.value)
    finally:
        dashboard_module._artifact.cache_clear()
        dashboard_module.widget_schemas.cache_clear()


def test_bad_version_raises_rather_than_coercing(tmp_path, monkeypatch):
    """`int()` would turn 1.9 into 1 and blow up on "v1" with a TypeError."""
    import harumi.dashboard as dashboard_module
    from harumi.dashboard import DashboardSchemaError

    path = tmp_path / "dashboard-schema.json"
    path.write_text('{"version": "v1", "widgetTypes": []}', encoding="utf-8")
    monkeypatch.setattr(dashboard_module, "SCHEMA_ARTIFACT_PATH", path)

    dashboard_module._artifact.cache_clear()
    try:
        with pytest.raises(DashboardSchemaError, match="must be an integer"):
            dashboard_module.schema_version()
    finally:
        dashboard_module._artifact.cache_clear()


@pytest.mark.parametrize(
    "version",
    [1.9, True, False, [1], {"v": 1}, None],
    ids=["float", "true", "false", "list", "dict", "null"],
)
def test_non_integer_version_types_are_rejected(version, tmp_path, monkeypatch):
    """`test_bad_version_raises_rather_than_coercing` only pins the string case.
    `True` is the one that needs its own guard: `bool` is an `int` subclass, so
    the `isinstance(version, int)` check alone would accept `true` as version 1.
    """
    import harumi.dashboard as dashboard_module
    from harumi.dashboard import DashboardSchemaError

    path = tmp_path / "dashboard-schema.json"
    path.write_text(
        json.dumps({"version": version, "widgetTypes": []}), encoding="utf-8"
    )
    monkeypatch.setattr(dashboard_module, "SCHEMA_ARTIFACT_PATH", path)

    dashboard_module._artifact.cache_clear()
    try:
        with pytest.raises(DashboardSchemaError, match="must be an integer"):
            dashboard_module.schema_version()
    finally:
        dashboard_module._artifact.cache_clear()


def test_importing_the_cli_does_not_read_the_artifact():
    """Regression guard. `cli.py` imports `harumi.dashboard` at module level, so
    loading the contract eagerly would make a corrupt artifact break *every*
    command — `harumi --version`, `harumi login` — not just the dashboard ones.
    The schema this replaced was a hardcoded dict that couldn't fail, so eager
    loading would be a real regression.
    """
    import harumi.dashboard as dashboard_module

    dashboard_module._artifact.cache_clear()
    dashboard_module.widget_schemas.cache_clear()
    with mock.patch.object(
        type(dashboard_module.SCHEMA_ARTIFACT_PATH), "read_text", side_effect=AssertionError("read at import")
    ):
        importlib.reload(importlib.import_module("harumi.cli"))

    # And the discovery rule keeps working without the artifact, so
    # `harumi dashboard list` survives a bad JSON file.
    assert dashboard_module.pick_dashboard_paths(["dashboard.toml"]) == ["dashboard.toml"]


class TestParseWidgetEntry:
    def test_parses_a_minimal_valid_entry_for_every_type(self):
        entries = {
            "metric": {"type": "metric", "id": "m", "title": "M", "value_key": "totals.x"},
            "table": {
                "type": "table",
                "id": "t",
                "title": "T",
                "rows_key": "rows",
                "columns": [{"key": "name", "label": "Name"}],
            },
            "line-chart": {
                "type": "line-chart",
                "id": "l",
                "title": "L",
                "data_key": "series",
                "x_key": "label",
                "series": [{"key": "value"}],
            },
            "bar-chart": {
                "type": "bar-chart",
                "id": "b",
                "title": "B",
                "data_key": "series",
                "x_key": "label",
                "series": [{"key": "value"}],
            },
            "gantt-chart": {"type": "gantt-chart", "id": "g", "title": "G", "tasks_key": "schedule"},
        }
        for type_, entry in entries.items():
            widget, issue = parse_widget_entry(entry)
            assert issue is None, f"{type_} should parse cleanly"
            assert widget is not None and widget["type"] == type_

    def test_rejects_unknown_widget_type(self):
        widget, issue = parse_widget_entry({"type": "pie-chart", "id": "p", "title": "P"})
        assert widget is None
        assert issue is not None and issue.widget_id == "p"
        assert "unknown type" in issue.message

    def test_rejects_missing_type_id_title(self):
        widget, issue = parse_widget_entry({"type": "metric", "title": "M", "value_key": "x"})
        assert widget is None and issue is not None

    def test_reports_missing_required_field(self):
        widget, issue = parse_widget_entry({"type": "metric", "id": "m", "title": "M"})
        assert widget is None
        assert issue is not None and "value_key" in issue.message

    def test_camel_case_typo_is_treated_as_missing(self):
        # The exact real-world failure mode this module exists to catch: a
        # renamed/camelCase key silently drops the widget on the platform.
        widget, issue = parse_widget_entry(
            {"type": "metric", "id": "revenue", "title": "Revenue", "valueKey": "totals.revenue"}
        )
        assert widget is None
        assert issue is not None and "value_key" in issue.message

    def test_unknown_enum_value_is_dropped_not_fatal(self):
        widget, issue = parse_widget_entry(
            {"type": "metric", "id": "m", "title": "M", "value_key": "x", "format": "scientific-notation"}
        )
        assert issue is None
        assert widget is not None and "format" not in widget

    def test_column_label_defaults_to_key(self):
        widget, issue = parse_widget_entry(
            {"type": "table", "id": "t", "title": "T", "rows_key": "rows", "columns": [{"key": "name"}]}
        )
        assert issue is None
        assert widget is not None
        assert widget["columns"] == [{"key": "name", "label": "name"}]


class TestResolvePath:
    def test_resolves_nested_dot_path(self):
        assert resolve_path({"totals": {"revenue": 100}}, "totals.revenue") == 100

    def test_missing_path_is_none(self):
        assert resolve_path({"totals": {}}, "totals.revenue") is None

    def test_describe_missing_key_lists_siblings(self):
        message = describe_missing_key({"objective": 1, "rows": []}, "totals.revenue")
        assert "totals.revenue" in message
        assert "objective" in message and "rows" in message


class TestValidateDashboardToml:
    def test_valid_toml_produces_no_issues(self):
        raw = """
[[widgets]]
type = "metric"
id = "objective"
title = "Objective"
value_key = "objective"
"""
        widgets, issues = validate_dashboard_toml(raw)
        assert len(widgets) == 1
        assert issues == []

    def test_invalid_toml_raises(self):
        with pytest.raises(DashboardTomlError):
            validate_dashboard_toml("not = [valid toml")

    def test_dropped_widget_is_reported(self):
        raw = """
[[widgets]]
type = "metric"
id = "revenue"
title = "Revenue"
valueKey = "totals.revenue"
"""
        widgets, issues = validate_dashboard_toml(raw)
        assert widgets == []
        assert len(issues) == 1 and issues[0].dropped is True

    def test_unresolved_dot_path_is_reported_but_not_dropped(self):
        raw = """
[[widgets]]
type = "metric"
id = "objective"
title = "Objective"
value_key = "totals.objective"
"""
        widgets, issues = validate_dashboard_toml(raw, output={"objective": 42})
        assert len(widgets) == 1
        assert len(issues) == 1 and issues[0].dropped is False
        assert "totals.objective" in issues[0].message

    def test_resolved_dot_path_has_no_issues(self):
        raw = """
[[widgets]]
type = "metric"
id = "objective"
title = "Objective"
value_key = "objective"
"""
        widgets, issues = validate_dashboard_toml(raw, output={"objective": 42})
        assert len(widgets) == 1
        assert issues == []

    def test_top_level_title_and_layout_are_accepted_and_ignored(self):
        """`title` is the dashboard's picker label when a project has several
        specs — the validator only cares about widgets, so it must not treat
        either non-widget table as a problem."""
        raw = """
title = "Machine schedule"

[layout]
columns = 3

[[widgets]]
type = "metric"
id = "objective"
title = "Objective"
value_key = "objective"
"""
        widgets, issues = validate_dashboard_toml(raw)
        assert len(widgets) == 1
        assert issues == []


class TestValidateTimeline:
    """The point of re-vendoring: before the refresh `harumi dashboard validate`
    called a valid timeline spec an unknown type and dropped it."""

    def test_a_full_timeline_spec_validates(self):
        raw = """
[[widgets]]
type = "timeline"
id = "schedule"
title = "Machine schedule"
items_key = "schedule"
id_key = "op"
regions_key = "breaks"
region_resource_key = "maquina"
time_unit = "h"
"""
        widgets, issues = validate_dashboard_toml(
            raw, output={"schedule": [{"resource": "M1"}], "breaks": []}
        )
        assert len(widgets) == 1
        assert issues == []

    def test_a_timeline_missing_its_required_key_is_dropped(self):
        raw = """
[[widgets]]
type = "timeline"
id = "schedule"
title = "Machine schedule"
"""
        widgets, issues = validate_dashboard_toml(raw)
        assert widgets == []
        assert len(issues) == 1 and issues[0].dropped is True

    def test_an_unresolved_regions_path_is_reported(self):
        """`regions_key` is a dot-path into output.json, so a typo in it must be
        caught the same way `items_key` is — not silently render zero bands."""
        raw = """
[[widgets]]
type = "timeline"
id = "schedule"
title = "Machine schedule"
items_key = "schedule"
regions_key = "paradas"
"""
        _, issues = validate_dashboard_toml(raw, output={"schedule": [{"resource": "M1"}]})
        assert any("paradas" in issue.message for issue in issues)


class TestPickDashboardPaths:
    def test_folder_specs_first_alphabetically_then_the_legacy_root(self):
        assert pick_dashboard_paths(
            [
                "main.py",
                "dashboard/schedule.toml",
                "dashboard.toml",
                "dashboard/costs.toml",
            ]
        ) == ["dashboard/costs.toml", "dashboard/schedule.toml", "dashboard.toml"]

    def test_orders_by_code_point_like_the_platform_picker(self):
        """`discovery.ts` sorts by code point, matching `sorted()`.

        It used `localeCompare` until that was aligned, and these are exactly
        the names the two disagreed on — a leading underscore, and a `-`/`_`
        pair. `harumi dashboard list` numbers specs in this order, so a
        disagreement meant `harumi dashboard show 1` and the browser's first tab
        could be different files.
        """
        assert pick_dashboard_paths(
            [
                "dashboard/costs_v2.toml",
                "dashboard/Alpha.toml",
                "dashboard/costs-v2.toml",
                "dashboard/_draft.toml",
                "dashboard/beta.toml",
            ]
        ) == [
            "dashboard/Alpha.toml",
            "dashboard/_draft.toml",
            "dashboard/beta.toml",
            "dashboard/costs-v2.toml",
            "dashboard/costs_v2.toml",
        ]

    def test_local_working_copy_discovery_agrees_on_those_names(self, tmp_path):
        """`local_dashboard_paths` sorts `Path` objects while
        `pick_dashboard_paths` sorts strings. Equivalent only because the
        directory prefix is constant, which is worth pinning rather than
        assuming — `harumi dashboard validate` uses the local path and
        `harumi dashboard list` the remote one, and they're expected to line up.
        """
        (tmp_path / "dashboard").mkdir()
        names = [
            "costs_v2.toml",
            "Alpha.toml",
            "costs-v2.toml",
            "_draft.toml",
            "beta.toml",
        ]
        for name in names:
            (tmp_path / "dashboard" / name).write_text("", encoding="utf-8")

        assert local_dashboard_paths(tmp_path) == pick_dashboard_paths(
            f"dashboard/{name}" for name in names
        )

    def test_ignores_non_toml_nested_and_unrelated_paths(self):
        assert pick_dashboard_paths(
            [
                "dashboard/README.md",
                "dashboard/archive/old.toml",
                "dashboards/other.toml",
                "harumi.toml",
            ]
        ) == []

    def test_root_only_project_is_unchanged(self):
        assert pick_dashboard_paths(["dashboard.toml", "main.py"]) == ["dashboard.toml"]


class TestLocalDashboardPaths:
    def test_finds_folder_specs_then_the_root_file(self, tmp_path):
        (tmp_path / "dashboard").mkdir()
        (tmp_path / "dashboard" / "schedule.toml").write_text("")
        (tmp_path / "dashboard" / "costs.toml").write_text("")
        (tmp_path / "dashboard" / "notes.md").write_text("")
        (tmp_path / "dashboard.toml").write_text("")

        assert local_dashboard_paths(tmp_path) == [
            "dashboard/costs.toml",
            "dashboard/schedule.toml",
            "dashboard.toml",
        ]

    def test_empty_when_nothing_is_committed(self, tmp_path):
        assert local_dashboard_paths(tmp_path) == []
