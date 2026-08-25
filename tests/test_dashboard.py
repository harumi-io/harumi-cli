"""Tests for harumi.dashboard: the drift-pinned widget contract, dashboard
spec discovery, and the spec validator.
"""

from __future__ import annotations

import pytest

from harumi.dashboard import (
    WIDGET_SCHEMAS,
    DashboardTomlError,
    describe_missing_key,
    local_dashboard_paths,
    parse_widget_entry,
    pick_dashboard_paths,
    resolve_path,
    validate_dashboard_toml,
)

# The identical literal pinned in harumi-platform's `schema.test.ts`
# (`AI_SOLVER_MIRROR_CONTRACT`) and ai-solver's `test_dashboard_tools.py`
# (`_EXPECTED_WIDGET_CONTRACT`). A field-level change to `WIDGET_SCHEMAS` in
# any of the three repos fails that repo's copy of this literal until the
# change is ported to the other two — see the `ponytail:` comment in
# harumi.dashboard and harumi-platform's dashboard-widgets cursor rule.
_EXPECTED_WIDGET_CONTRACT = {
    "metric": ["value_key!", "delta_key", "format[number|currency|percent]", "unit"],
    "table": ["rows_key!", "columns!"],
    "line-chart": ["data_key!", "x_key!", "series!"],
    "bar-chart": ["data_key!", "x_key!", "series!"],
    "gantt-chart": [
        "tasks_key!",
        "resource_key",
        "label_key",
        "start_key",
        "end_key",
        "duration_key",
        "color_key",
        "time_unit",
    ],
}


def test_widget_contract_matches_the_cross_repo_pinned_literal():
    contract = {
        type_: [
            field.toml_key
            + ("!" if field.required else "")
            + (f"[{'|'.join(field.values)}]" if field.kind == "enum" else "")
            for field in fields
        ]
        for type_, fields in WIDGET_SCHEMAS.items()
    }
    # Failing here means: port the change to harumi-platform's schema.ts and
    # ai-solver's dashboard_tools.py (and their pinned test literals) too.
    assert contract == _EXPECTED_WIDGET_CONTRACT


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
