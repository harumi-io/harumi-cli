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
    parse_clock_entry,
    parse_dataset_entry,
    parse_metric_entry,
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
    "kpi-rail": ["items!"],
    "table": ["rows_key!*", "columns!"],
    "detail": ["items_key!*", "id_key!", "fields"],
    "filter": ["items_key!*", "id_key!", "label_key"],
    "treemap": ["items_key!*", "value_key!", "name_key!", "color_key"],
    "line-chart": ["data_key!*", "x_key!", "series!"],
    "bar-chart": ["data_key!*", "x_key!", "series!"],
    "chart": ["variant![line|bar]", "data_key!*", "x_key!", "series!"],
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
            "kpi-rail": {
                "type": "kpi-rail",
                "id": "k",
                "title": "K",
                "items": [{"label": "Cost", "value_key": "totals.cost"}],
            },
            "table": {
                "type": "table",
                "id": "t",
                "title": "T",
                "rows_key": "rows",
                "columns": [{"key": "name", "label": "Name"}],
            },
            "detail": {"type": "detail", "id": "d", "title": "D", "items_key": "jobs", "id_key": "id"},
            "filter": {"type": "filter", "id": "f", "title": "F", "items_key": "jobs", "id_key": "id"},
            "treemap": {
                "type": "treemap",
                "id": "tm",
                "title": "TM",
                "items_key": "categories",
                "value_key": "cost",
                "name_key": "name",
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
            "chart": {
                "type": "chart",
                "id": "c",
                "title": "C",
                "variant": "line",
                "data_key": "series",
                "x_key": "label",
                "series": [{"key": "value"}],
            },
            "gantt-chart": {"type": "gantt-chart", "id": "g", "title": "G", "tasks_key": "schedule"},
            "timeline": {"type": "timeline", "id": "tl", "title": "TL", "items_key": "schedule"},
        }
        for type_, entry in entries.items():
            widget, issue = parse_widget_entry(entry)
            assert issue is None, f"{type_} should parse cleanly"
            assert widget is not None and widget["type"] == type_
        # Every type the artifact declares must have a minimal entry above —
        # missing one here would mean this "every type" test silently stopped
        # covering a type without anyone noticing, the same drift the
        # re-vendor step above exists to catch.
        assert set(entries) == set(widget_schemas())

    def test_rejects_unknown_widget_type(self):
        widget, issue = parse_widget_entry({"type": "pie-chart", "id": "p", "title": "P"})
        assert widget is None
        assert issue is not None and issue.entity_id == "p"
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

    def test_kpi_rail_drops_invalid_items_but_keeps_valid_ones(self):
        entry = {
            "type": "kpi-rail",
            "id": "k",
            "title": "K",
            "items": [
                {"label": "Cost", "value_key": "totals.cost", "format": "currency"},
                {"label": "Bad"},  # missing value_key
                {"value_key": "totals.x"},  # missing label
                {"label": "Not a dict"},  # placeholder to keep list realistic
            ],
        }
        widget, issue = parse_widget_entry(entry)
        assert issue is None
        assert widget is not None
        assert widget["items"] == [{"label": "Cost", "value_key": "totals.cost", "format": "currency"}]

    def test_kpi_rail_with_only_invalid_items_is_dropped(self):
        entry = {"type": "kpi-rail", "id": "k", "title": "K", "items": [{"label": "Bad"}]}
        widget, issue = parse_widget_entry(entry)
        assert widget is None
        assert issue is not None and issue.dropped is True

    def test_kpi_rail_item_ignores_unknown_format_but_keeps_item(self):
        entry = {
            "type": "kpi-rail",
            "id": "k",
            "title": "K",
            "items": [{"label": "Cost", "value_key": "totals.cost", "format": "scientific-notation"}],
        }
        widget, issue = parse_widget_entry(entry)
        assert issue is None
        assert widget is not None
        assert widget["items"] == [{"label": "Cost", "value_key": "totals.cost"}]


class TestParseDatasetEntry:
    def test_parses_a_minimal_intervals_dataset(self):
        dataset, issue = parse_dataset_entry(
            {"id": "schedule", "kind": "intervals", "source_key": "schedule", "roles": {"start": "start", "end": "end"}}
        )
        assert issue is None
        assert dataset == {
            "id": "schedule",
            "kind": "intervals",
            "source_key": "schedule",
            "roles": {"start": "start", "end": "end"},
        }

    def test_records_and_scalars_need_no_roles(self):
        dataset, issue = parse_dataset_entry({"id": "rows", "kind": "records", "source_key": "rows"})
        assert issue is None
        assert dataset is not None and dataset["roles"] == {}

    def test_missing_id_is_rejected(self):
        dataset, issue = parse_dataset_entry({"kind": "records", "source_key": "rows"})
        assert dataset is None
        assert issue is not None and 'missing or invalid "id"' in issue.message

    def test_unknown_kind_is_rejected(self):
        dataset, issue = parse_dataset_entry({"id": "d", "kind": "graph", "source_key": "rows"})
        assert dataset is None
        assert issue is not None and 'unknown kind "graph"' in issue.message

    def test_missing_source_key_is_rejected(self):
        dataset, issue = parse_dataset_entry({"id": "d", "kind": "records"})
        assert dataset is None
        assert issue is not None and "source_key" in issue.message

    def test_intervals_needs_start_and_end_or_duration(self):
        dataset, issue = parse_dataset_entry({"id": "d", "kind": "intervals", "source_key": "s", "roles": {"start": "s"}})
        assert dataset is None
        assert issue is not None and '"end" or "duration"' in issue.message

    def test_intervals_accepts_duration_instead_of_end(self):
        dataset, issue = parse_dataset_entry(
            {"id": "d", "kind": "intervals", "source_key": "s", "roles": {"start": "s", "duration": "dur"}}
        )
        assert issue is None and dataset is not None

    def test_timeline_needs_an_instant_and_a_value(self):
        dataset, issue = parse_dataset_entry({"id": "d", "kind": "timeline", "source_key": "s", "roles": {"at": "t"}})
        assert dataset is None
        assert issue is not None and '"value"' in issue.message

    def test_timeline_accepts_start_instead_of_at(self):
        dataset, issue = parse_dataset_entry(
            {"id": "d", "kind": "timeline", "source_key": "s", "roles": {"start": "t", "value": "v"}}
        )
        assert issue is None and dataset is not None


class TestParseMetricEntry:
    def test_parses_a_minimal_metric(self):
        metric, issue = parse_metric_entry({"id": "makespan", "sql": "SELECT max(end) FROM schedule"})
        assert issue is None
        assert metric == {"id": "makespan", "sql": "SELECT max(end) FROM schedule"}

    def test_title_is_carried_when_present(self):
        metric, issue = parse_metric_entry({"id": "m", "sql": "SELECT 1", "title": "M"})
        assert issue is None
        assert metric is not None and metric["title"] == "M"

    def test_missing_id_is_rejected(self):
        metric, issue = parse_metric_entry({"sql": "SELECT 1"})
        assert metric is None
        assert issue is not None and 'missing or invalid "id"' in issue.message

    def test_missing_sql_is_rejected(self):
        metric, issue = parse_metric_entry({"id": "m"})
        assert metric is None
        assert issue is not None and 'missing or invalid "sql"' in issue.message

    def test_a_write_query_is_rejected_by_the_sql_guard(self):
        metric, issue = parse_metric_entry({"id": "m", "sql": "DROP TABLE t"})
        assert metric is None
        assert issue is not None and issue.entity_id == "m"
        assert "read-only" in issue.message


class TestParseClockEntry:
    def test_a_valid_clock_parses(self):
        clock, message = parse_clock_entry({"dataset": "schedule", "speed": 60}, {"schedule": "intervals"})
        assert message is None
        assert clock == {"dataset": "schedule", "speed": 60}

    def test_speed_defaults_to_absent_when_not_given(self):
        clock, message = parse_clock_entry({"dataset": "schedule"}, {"schedule": "intervals"})
        assert message is None
        assert clock == {"dataset": "schedule"}

    def test_missing_dataset_is_rejected(self):
        clock, message = parse_clock_entry({}, {"schedule": "intervals"})
        assert clock is None
        assert message is not None and 'missing or invalid "dataset"' in message

    def test_unknown_dataset_is_rejected(self):
        clock, message = parse_clock_entry({"dataset": "nope"}, {"schedule": "intervals"})
        assert clock is None
        assert message is not None and 'no dataset "nope"' in message

    def test_a_non_intervals_dataset_is_rejected(self):
        clock, message = parse_clock_entry({"dataset": "rows"}, {"rows": "records"})
        assert clock is None
        assert message is not None and "is records, but a clock needs an intervals dataset" in message

    @pytest.mark.parametrize("speed", [0, -1, float("inf"), float("nan"), "fast", True])
    def test_invalid_speed_is_rejected(self, speed):
        clock, message = parse_clock_entry({"dataset": "schedule", "speed": speed}, {"schedule": "intervals"})
        assert clock is None
        assert message is not None and '"speed" must be a positive finite number' in message

    def test_an_oversized_integer_speed_is_rejected_not_a_crash(self):
        # tomllib parses a TOML integer into an arbitrary-precision Python int
        # with no 64-bit bound check, so `speed = 10**400` parses fine.
        # math.isfinite() converts its argument to a C double and raises
        # OverflowError for anything outside float range instead of
        # returning False — this used to crash instead of reporting a
        # rejection.
        clock, message = parse_clock_entry({"dataset": "schedule", "speed": 10**400}, {"schedule": "intervals"})
        assert clock is None
        assert message is not None and '"speed" must be a positive finite number' in message

    def test_a_speed_just_under_float_max_is_accepted_not_rejected_by_an_imprecise_bound(self):
        # A guard against OverflowError that used a round-number threshold
        # below the real float ceiling (~1.7976931348623157e308) would wrongly
        # reject this legitimately finite value.
        speed = 15 * 10**307
        assert speed < 1.8e308  # still comfortably finite as a float
        clock, message = parse_clock_entry({"dataset": "schedule", "speed": speed}, {"schedule": "intervals"})
        assert message is None
        assert clock == {"dataset": "schedule", "speed": speed}


class TestValidateDashboardTomlDatasetsMetricsClock:
    """`validate_dashboard_toml` used to only look at `[[widgets]]` — a bad
    `[[datasets]]`/`[[metrics]]`/`[clock]` entry was invisible to `harumi
    dashboard validate` and only surfaced as an error banner on the platform,
    later and to a different audience."""

    def test_a_full_spec_with_all_four_sections_validates_clean(self):
        raw = """
[[datasets]]
id = "schedule"
kind = "intervals"
source_key = "schedule"

[datasets.roles]
start = "start"
end = "end"

[[metrics]]
id = "makespan"
sql = "SELECT max(end) - min(start) AS value FROM schedule"

[clock]
dataset = "schedule"
speed = 30

[[widgets]]
type = "metric"
id = "objective"
title = "Objective"
value_key = "objective"
"""
        widgets, issues = validate_dashboard_toml(raw)
        assert len(widgets) == 1
        assert issues == []

    def test_a_bad_dataset_is_reported(self):
        raw = """
[[datasets]]
id = "schedule"
kind = "not-a-kind"
source_key = "schedule"
"""
        _, issues = validate_dashboard_toml(raw)
        assert len(issues) == 1
        assert issues[0].entity_id == "schedule"
        assert "unknown kind" in issues[0].message

    def test_a_duplicate_dataset_id_is_reported(self):
        raw = """
[[datasets]]
id = "schedule"
kind = "records"
source_key = "a"

[[datasets]]
id = "schedule"
kind = "records"
source_key = "b"
"""
        _, issues = validate_dashboard_toml(raw)
        assert len(issues) == 1 and "duplicate id" in issues[0].message

    def test_a_non_select_metric_is_reported(self):
        raw = """
[[metrics]]
id = "danger"
sql = "DELETE FROM schedule"
"""
        _, issues = validate_dashboard_toml(raw)
        assert len(issues) == 1
        assert issues[0].entity_id == "danger"
        assert "DELETE" in issues[0].message

    def test_a_duplicate_metric_id_is_reported(self):
        raw = """
[[metrics]]
id = "m"
sql = "SELECT 1"

[[metrics]]
id = "m"
sql = "SELECT 2"
"""
        _, issues = validate_dashboard_toml(raw)
        assert len(issues) == 1 and "duplicate id" in issues[0].message

    def test_a_clock_naming_an_undeclared_dataset_is_reported(self):
        raw = """
[clock]
dataset = "nope"
"""
        _, issues = validate_dashboard_toml(raw)
        assert len(issues) == 1 and 'no dataset "nope"' in issues[0].message

    def test_a_clock_can_name_a_timeline_widgets_synthesized_dataset(self):
        """No `[[datasets]]` entry at all — the clock names the dataset a bare
        `timeline` widget implies, the same way the platform's
        `synthesizeDatasets` lets it without an explicit declaration."""
        raw = """
[[widgets]]
type = "timeline"
id = "schedule"
title = "Schedule"
items_key = "ops"

[clock]
dataset = "schedule__source"
"""
        _, issues = validate_dashboard_toml(raw)
        assert issues == []

    def test_a_non_table_clock_is_reported_not_silently_skipped(self):
        """`[[clock]]` (double-bracketed, an easy slip since every other
        section here — widgets, datasets, metrics — really is a list) parses
        as a list, and `clock = "off"` parses as a string; either way this
        used to hit `isinstance(raw_clock, dict)` as False and vanish with no
        issue, unlike every other malformed-shape case in this file."""
        raw = """
[[clock]]
dataset = "schedule"
"""
        _, issues = validate_dashboard_toml(raw)
        assert len(issues) == 1 and "clock entry is not a table" in issues[0].message



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

    def test_kpi_rail_item_with_unresolved_value_key_is_reported_but_not_dropped(self):
        # kpi-rail items get the same value_key/delta_key contract as a
        # standalone metric — a typo here used to pass validate cleanly and
        # only show up as a blank tile in the browser.
        raw = """
[[widgets]]
type = "kpi-rail"
id = "summary"
title = "Summary"
items = [
  { label = "Cost", value_key = "totals.cost" },
  { label = "Makespan", value_key = "totals.makespan", delta_key = "deltas.makespan" },
]
"""
        widgets, issues = validate_dashboard_toml(raw, output={"totals": {"cost": 1}})
        assert len(widgets) == 1
        messages = [issue.message for issue in issues]
        assert len(issues) == 2
        assert all(issue.dropped is False for issue in issues)
        assert any("totals.makespan" in m and "items[2].value_key" in m for m in messages)
        assert any("deltas.makespan" in m and "items[2].delta_key" in m for m in messages)

    def test_kpi_rail_item_with_resolved_value_key_has_no_issues(self):
        raw = """
[[widgets]]
type = "kpi-rail"
id = "summary"
title = "Summary"
items = [{ label = "Cost", value_key = "totals.cost" }]
"""
        widgets, issues = validate_dashboard_toml(raw, output={"totals": {"cost": 1}})
        assert len(widgets) == 1
        assert issues == []

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
