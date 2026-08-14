# Dashboards & widgets

Each Harumi project's dashboard renders from a repo-committed `dashboard.toml`, bound by dot-path keys to `output/output.json` — the file a run writes (see `[output]` in `harumi.toml`). There is no dedicated backend endpoint for this: `dashboard.toml` is just a file in the project's Gitea repo, read with `harumi repo cat dashboard.toml` / written with `harumi repo put`, same as any other file.

A project with no `dashboard.toml` still renders — the platform falls back to a generic default dashboard. `harumi projects create` gets a starter `dashboard.toml` seeded server-side; `harumi import` does **not** (it overwrites the scaffold), so an imported project has no dashboard until one is committed.

## The failure mode this exists to catch

The platform's parser (`parseDashboardConfig` in harumi-platform) is deliberately forgiving: a widget with an unknown `type`, a missing required key, or a renamed key (e.g. `valueKey` instead of `value_key`) is **silently dropped** — the dashboard renders everything else and the bad widget just doesn't appear, with no error surfaced to whoever edited the file. A widget whose dot-path doesn't resolve against a real run's `output.json` renders, but empty.

Always run `harumi dashboard validate` before `harumi repo put dashboard.toml` (or before telling the user a `dashboard.toml` edit is done) — it's the only place in the toolchain that fails loudly.

## The five widget types

Get the live, in-code reference with `harumi dashboard widgets` (add `--type metric` to filter to one). Summary:

| type | required keys | optional keys |
|---|---|---|
| `metric` | `value_key` | `delta_key`, `format` (`number`\|`currency`\|`percent`), `unit` |
| `table` | `rows_key`, `columns` | — |
| `line-chart` | `data_key`, `x_key`, `series` | — |
| `bar-chart` | `data_key`, `x_key`, `series` | — |
| `gantt-chart` | `tasks_key` | `resource_key`, `label_key`, `start_key`, `end_key`, `duration_key`, `color_key`, `time_unit` |

Every widget entry also needs `type`, `id` (unique), and `title`. `*_key` fields (except chart `x_key`/gantt's per-task field names) are **dot-paths into `output.json`**, e.g. `"totals.revenue"`.

### `metric` — a single KPI tile

```toml
[[widgets]]
type = "metric"
id = "revenue"
title = "Total revenue"
value_key = "totals.revenue"
format = "currency"
```

Matching `output.json`: `{"totals": {"revenue": 812300}}`.

### `table` — a sortable grid

```toml
[[widgets]]
type = "table"
id = "breakdown"
title = "Breakdown"
rows_key = "breakdown"
columns = [
  { key = "name", label = "Name" },
  { key = "value", label = "Value" },
]
```

Matching `output.json`: `{"breakdown": [{"name": "Item A", "value": 52400}]}`. `columns[].key` is a field **within each row object**, not a dot-path.

### `line-chart` / `bar-chart` — trend or comparison

```toml
[[widgets]]
type = "line-chart"
id = "trend"
title = "Objective value over time"
data_key = "timeseries"
x_key = "label"
series = [{ key = "value", label = "Objective value" }]
```

Matching `output.json`: `{"timeseries": [{"label": "Mon", "value": 412}, {"label": "Tue", "value": 398}]}`. `x_key` and `series[].key` are fields within each data point, not dot-paths. Multiple `series` entries render as multiple lines/bars.

### `gantt-chart` — resource-row schedule

The shape for job-shop / scheduling solver output: one row per resource/machine, one bar per task.

```toml
[[widgets]]
type = "gantt-chart"
id = "schedule"
title = "Machine schedule"
tasks_key = "schedule"
time_unit = "min"
```

Matching `output.json`:

```json
{
  "schedule": [
    { "resource": "Machine 1", "task": "Job A op1", "start": 0, "end": 45 },
    { "resource": "Machine 1", "task": "Job B op1", "start": 45, "end": 90 },
    { "resource": "Machine 2", "task": "Job A op2", "start": 45, "end": 120, "group": "Job A" }
  ]
}
```

Semantics worth knowing:

- `resource_key`/`label_key`/`start_key`/`end_key` default to `resource`/`task`/`start`/`end` and are fields within each task object, not dot-paths.
- Set either `end_key` or `duration_key` (added to the start). If both are set, `end_key` wins. A task resolving neither is dropped from the chart.
- `color_key` names a field grouping tasks into a categorical color (e.g. tasks belonging to the same job).

## Layout

```toml
[layout]
columns = 2
```

`columns` is the only layout hint; defaults to 2 when omitted.

## Validating

```bash
harumi dashboard widgets                        # the reference table above, always current
harumi dashboard validate                        # ./dashboard.toml
harumi dashboard validate --ref feature/solver-v2  # the repo's copy on a branch
harumi dashboard validate --against ./output.json  # + check dot-paths against a local file
harumi dashboard validate --latest                 # + check dot-paths against the latest run's output.json
harumi dashboard validate --run <RUN_ID>           # + a specific run
```

Exits non-zero if any widget would be dropped, or (when checking dot-paths) any widget would render empty.
