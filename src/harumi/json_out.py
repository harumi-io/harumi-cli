"""What `--json` prints: named fields, not a dump of the API's model.

A script depends on whatever a command prints. Dumping the API model would make every field the
server ever adds part of that contract, and would publish the ones that are not meant to be: a
file's storage key, a run's signed output URL, a share link's token on a listing. So each record is
built from a list of field names, here, in one place.

Timestamps are ISO 8601 (`model_dump(mode="json")`); ids are whole; text is exactly as stored.
"""

from __future__ import annotations

import json
from datetime import timedelta, timezone
from typing import Any, Iterable, Optional

PROJECT = ("id", "name", "customer_id", "kernel_spec", "role_name", "created_at", "updated_at")
TRASHED_PROJECT = ("id", "name", "deleted_at")
RUN = (
    "id", "status", "exit_code", "source", "triggered_by", "git_branch", "git_tag", "git_commit", "command",
    "kernel_spec", "created_at", "started", "ended",
)
RUN_DETAIL = ("stdout", "stderr", "error")
FILE = ("name", "size", "last_modified")
SHARE = (
    "id", "label", "enabled", "password_set", "app_enabled", "chat_enabled", "run_history_enabled", "run_control_enabled",
)
DATASOURCE = ("id", "name", "type", "host", "port", "database", "username", "use_proxy", "proxy_host")
SCHEDULE = (
    "id", "cron", "start_at", "git_branch", "git_tag", "git_commit", "command", "kernel_spec", "output_format", "email_to", "status",
    "last_executed_at",
)
TAG = ("name", "commit_sha", "message", "created_at")
MEMORY = ("id", "scope", "content", "retrieval_mode", "source", "updated_at")
MEMBER = ("user_id", "email", "first_name", "last_name", "role", "pending")
BLUEPRINT = ("slug", "name", "description")
SKILL_PRESET = ("slug", "name", "description", "category", "tags", "version")
COMPARE = ("columns", "kpis", "winner_run_id", "no_winner_reason", "warnings", "fairness")
COMPARE_COLUMN = ("run_id", "branch", "commit", "status", "runtime_s", "source")
COMPARE_KPI = ("key", "label", "unit", "direction", "primary", "values", "deltas")
COMPARE_FAIRNESS = ("verdict", "files")
USAGE = (
    "plan_code", "tier", "unlimited", "balance_credits", "included_credits", "period_start", "period_end", "weekly_used_pct",
    "monthly_used_pct", "weekly_resets_at", "monthly_resets_at", "overage_allowed", "overage_enabled", "overage_paused",
    "overage_cap_cents", "overage_spent_cents", "subscription_status", "grace_ends_at",
)


def _pick(model: Any, fields: Iterable[str]) -> dict:
    data = model.model_dump(mode="json")
    return {name: data.get(name) for name in fields}


def project(p: Any) -> dict:
    return _pick(p, PROJECT)


def trashed_project(p: Any, *, days: int) -> dict:
    """A project in the Trash: when it was removed, and when the server is expected to delete it for good.

    `deleted_for_good_at` is `deleted_at` plus `days`, an ESTIMATE: harumi-api's window is a setting
    (`project_trash_retention_days`, default 7) and the trash response does not carry the deadline (asked for in
    harumi-io/harumi-api#654). It becomes exact when the API sends one; the field name stays."""
    record = _pick(p, TRASHED_PROJECT)
    deleted = p.deleted_at
    if deleted is None:
        record["deleted_for_good_at"] = None
        return record
    # One shape for a script: UTC with a Z. A time with no offset is UTC, as everywhere else in the CLI.
    utc = (deleted if deleted.tzinfo is not None else deleted.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    record["deleted_at"] = utc.isoformat().replace("+00:00", "Z")
    record["deleted_for_good_at"] = (utc + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return record


def run(r: Any, *, detail: bool = False) -> dict:
    record = _pick(r, RUN + (RUN_DETAIL if detail else ()))
    # The output URL is signed and expires; whether there is output, and `runs download`, are what a script needs.
    record["has_output"] = bool(r.output_url)
    return record


def file(f: Any) -> dict:
    return _pick(f, FILE)


def share(link: Any) -> dict:
    return _pick(link, SHARE)


def datasource(d: Any) -> dict:
    return _pick(d, DATASOURCE)


def schedule(s: Any) -> dict:
    return _pick(s, SCHEDULE)


def tag(t: Any) -> dict:
    return _pick(t, TAG)


def memory(m: Any) -> dict:
    return _pick(m, MEMORY)


def secret(s: Any) -> dict:
    return {"name": s.name}  # never the value: the API sends ciphertext, and a name is all a script may read


def organization(o: Any) -> dict:
    return {"id": o.id, "name": o.business_name, "role": o.role_name or o.role}


def member(m: Any) -> dict:
    return _pick(m, MEMBER)


def spec(k: Any) -> dict:
    return {
        "name": k.name, "display_name": k.display_name, "language": k.language, "description": k.description,
        "cpu": k.size.cpu, "memory": k.size.memory, "gpu": k.size.gpu, "subscription_required": k.subscription_required,
    }


def blueprint(b: Any) -> dict:
    return _pick(b, BLUEPRINT)


def skill_preset(p: Any) -> dict:
    """A library preset as a script sees it: never its content, which `skills add` writes to a file."""
    return _pick(p, SKILL_PRESET)


def _pick_dict(data: Any, fields: Iterable[str]) -> dict:
    """Like `_pick`, for a plain dict (the comparison is passed through untyped). A missing field is null, never absent."""
    data = data if isinstance(data, dict) else {}
    return {name: data.get(name) for name in fields}


def comparison(c: dict) -> dict:
    """`harumi compare --json`: the fields in COMPARE and the three records inside it, and nothing else the server
    may add. A field that is a list stays a list (empty when the server sent none), so a script can always loop."""
    out = _pick_dict(c, COMPARE)
    out["columns"] = [_pick_dict(col, COMPARE_COLUMN) for col in c.get("columns") or []]
    out["kpis"] = [_pick_dict(kpi, COMPARE_KPI) for kpi in c.get("kpis") or []]
    out["warnings"] = list(c.get("warnings") or [])
    fairness = _pick_dict(c.get("fairness"), COMPARE_FAIRNESS)
    fairness["files"] = list(fairness["files"] or [])
    fairness["verdict"] = fairness["verdict"] or "unknown"
    out["fairness"] = fairness
    return out


def usage(u: Any) -> dict:
    return _pick(u, USAGE)


def dumps(data: Any) -> str:
    """Pretty, and ASCII only: a non-ASCII or control character in a value is escaped, so printing a
    document can never send a terminal an escape sequence or a direction override."""
    return json.dumps(data, indent=2, default=str)


def workspace(org_id: Optional[str], name: Optional[str]) -> Optional[dict]:
    return {"id": org_id, "name": name} if org_id else None
