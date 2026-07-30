"""One authoritative, server-timestamped dashboard snapshot.

Contract v1 (agreed 2026-07-30, Forven Development thread 6691da7a…):

- ``GET /api/dashboard/snapshot`` returns the immutable cached snapshot only.
  All refresh work happens in a background producer loop; the HTTP handler
  never runs a builder, so a slow or failing source cannot slow the request.
- Sections refresh independently on their own cadence and fail independently:
  a failed refresh retains the last good ``data`` and stamps
  ``status``/``error_code``. One failed section cannot fail the snapshot.
- Section shape: ``{status: fresh|stale|error|unavailable, as_of,
  last_attempt_at, error_code, data}``. ``unavailable`` means the section has
  never produced data (source missing or failing since boot).
- Truth rule: unknown numeric values are ``null``, never ``0``. Fields the
  legacy overview endpoint hardcodes to zero (``signals_today``,
  ``data_coverage``, ``blocked_count``) are reported as ``null`` with a reason
  in ``unknown_fields`` until a real source exists.
- Freshness thresholds live here (``SECTION_POLICIES``) and are returned in
  the payload so the UI never invents its own staleness rules.
- Builders must be read-only. They call the same read paths the existing
  dashboard panels already poll; anything that would write (e.g.
  ``normalize_daemon_state(write_back=True)`` in the legacy overview stub) is
  recomputed here without the write.

The "needs attention" inbox is derived state (no persistence, no ack/dismiss):
items carry deterministic ids so the UI can key them, and first/last-observed
timestamps tracked in process memory. Items resolve by the source condition
clearing, never by dismissal.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("forven.dashboard_snapshot")

CONTRACT_VERSION = 1

# Age (seconds) beyond which market-data ingestion is called out in the inbox.
_DATA_INGESTION_STALE_SECONDS = 6 * 3600
# Drawdown fraction that raises an inbox warning — mirrors the websocket
# drawdown_warning threshold in api_domains/live_ws.py.
_DRAWDOWN_WARNING_FRACTION = 0.08
# Grace before an enabled scheduler job with next_run_at in the past counts
# as overdue (matches the loop cadence tolerances used by /api/health).
_SCHEDULER_OVERDUE_GRACE_SECONDS = 120


@dataclass(frozen=True)
class SectionPolicy:
    refresh_seconds: float
    stale_after_seconds: float


SECTION_POLICIES: dict[str, SectionPolicy] = {
    "system": SectionPolicy(10, 45),
    "trading": SectionPolicy(15, 60),
    "paper": SectionPolicy(60, 180),
    "data": SectionPolicy(60, 300),
    "scheduler": SectionPolicy(30, 120),
    "agents": SectionPolicy(30, 120),
    "pipeline": SectionPolicy(60, 300),
    "approvals": SectionPolicy(20, 90),
    "equity": SectionPolicy(60, 300),
    "leaderboard": SectionPolicy(300, 900),
    "kpis": SectionPolicy(60, 300),
}


@dataclass
class _SectionRuntime:
    data: Any = None
    as_of: str | None = None
    last_attempt_at: str | None = None
    error_code: str | None = None
    next_refresh_monotonic: float = 0.0


_STATE_LOCK = threading.Lock()
_SECTIONS: dict[str, _SectionRuntime] = {name: _SectionRuntime() for name in SECTION_POLICIES}
_GENERATED_AT: str | None = None
# Inbox item id -> first/last observed ISO timestamps (process-local; derived
# state resets on restart by design — see module docstring).
_INBOX_OBSERVED: dict[str, dict[str, str]] = {}
_INBOX_STATE = _SectionRuntime()

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(value: Any) -> float | None:
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    return max(0.0, (_utc_now() - parsed).total_seconds())


# ---------------------------------------------------------------------------
# Section builders — read-only by contract (see module docstring).
# Lazy imports keep this module import-light and avoid startup cycles.
# ---------------------------------------------------------------------------


def _build_system() -> dict[str, Any]:
    from forven.control_plane.status import health_check

    payload = health_check()
    keep = (
        "status",
        "issues",
        "scheduler_age_seconds",
        "schedulerAgeSeconds",
        "worker_loops",
        "workerLoops",
        "queues",
        "overdue_jobs",
        "overdueJobs",
        "overdue_job_ids",
        "overdueJobIds",
        "long_running_jobs",
        "longRunningJobs",
    )
    return {key: payload.get(key) for key in keep if key in payload}


def _build_trading() -> dict[str, Any]:
    from forven.control_plane.status import get_dashboard

    payload = get_dashboard(require_account_connection=False)
    keep = (
        "execution_mode",
        "simulation_active",
        "daily_risk",
        "account",
        "recovery",
        "risk",
        "prices",
    )
    return {key: payload.get(key) for key in keep if key in payload}


def _build_paper() -> dict[str, Any]:
    from forven.api_domains.paper import get_paper_summary

    payload = get_paper_summary(include_deployed=False)
    sessions = payload.get("sessions")
    if isinstance(sessions, list) and len(sessions) > 8:
        payload = dict(payload)
        payload["sessions"] = sessions[:8]
        payload["sessions_truncated_from"] = len(sessions)
    return payload


def _build_data() -> dict[str, Any]:
    from forven.api_domains.data import get_data_health

    payload = get_data_health()
    return payload if isinstance(payload, dict) else {"raw": payload}


def _build_scheduler() -> dict[str, Any]:
    from forven.control_plane.ops import get_scheduler

    now = _utc_now()
    jobs: list[dict[str, Any]] = []
    overdue_ids: list[str] = []
    error_ids: list[str] = []
    for row in get_scheduler():
        if not isinstance(row, dict):
            continue
        job = {
            key: row.get(key)
            for key in (
                "id",
                "name",
                "enabled",
                "last_run_at",
                "next_run_at",
                "running_since",
                "last_status",
                "last_error",
            )
        }
        next_run = _parse_ts(job.get("next_run_at"))
        overdue = bool(
            job.get("enabled")
            and next_run is not None
            and (now - next_run).total_seconds() > _SCHEDULER_OVERDUE_GRACE_SECONDS
        )
        job["overdue"] = overdue
        if overdue and job.get("id"):
            overdue_ids.append(str(job["id"]))
        if str(job.get("last_status") or "").lower() == "error" and job.get("id"):
            error_ids.append(str(job["id"]))
        jobs.append(job)
    return {"jobs": jobs, "overdue_job_ids": overdue_ids, "error_job_ids": error_ids}


def _build_agents() -> dict[str, Any]:
    from forven.api_core import read_agents
    from forven.api_domains.tasks import get_agent_tasks

    roster = []
    for row in read_agents() or []:
        if not isinstance(row, dict):
            continue
        roster.append(
            {
                key: row.get(key)
                for key in ("id", "name", "model", "model_id", "enabled")
            }
        )

    active: dict[str, int] = {}
    pending: dict[str, int] = {}
    for task in get_agent_tasks() or []:
        if not isinstance(task, dict):
            continue
        agent_id = str(task.get("agent_id") or "")
        status = str(task.get("status") or "").lower()
        if status == "running":
            active[agent_id] = active.get(agent_id, 0) + 1
        elif status == "pending":
            pending[agent_id] = pending.get(agent_id, 0) + 1

    # Heartbeat ages from the activity log: the `agent_stalled` websocket
    # event is never emitted anywhere in the backend, so staleness must be
    # computed from data, not events.
    heartbeats: dict[str, str] = {}
    try:
        from forven.db import get_db

        with get_db() as conn:
            rows = conn.execute(
                "SELECT source, MAX(created_at) AS last_at FROM activity_log "
                "WHERE source LIKE 'agent:%' OR source = 'brain' GROUP BY source"
            ).fetchall()
        for row in rows:
            source = str(row["source"] or "")
            agent_id = source.split(":", 1)[1] if source.startswith("agent:") else source
            if row["last_at"]:
                heartbeats[agent_id] = str(row["last_at"])
    except Exception:
        log.debug("agent heartbeat query failed", exc_info=True)

    for agent in roster:
        agent_id = str(agent.get("id") or "")
        agent["active_tasks"] = active.get(agent_id, 0)
        agent["pending_tasks"] = pending.get(agent_id, 0)
        last_seen = heartbeats.get(agent_id)
        agent["last_activity_at"] = last_seen
        agent["last_activity_age_seconds"] = _age_seconds(last_seen)

    return {"roster": roster}


def _build_pipeline() -> dict[str, Any]:
    from forven.api_domains.analytics import dashboard_funnel_stub
    from forven.strategy_lifecycle import read_lifecycle_events

    payload: dict[str, Any] = {
        "stages": dashboard_funnel_stub(),
        "recent_events": read_lifecycle_events(limit=20),
    }

    # PR #105 (feat/pipeline-explainability) adds the per-strategy explain
    # surface this section prefers. Import-guarded so the snapshot works both
    # before and after that branch merges, with no rebase needed.
    try:
        from forven.pipeline_explain import explain_pipeline
    except ImportError:
        payload["needs_attention"] = None
        payload["needs_attention_unavailable_reason"] = "pipeline_explain_not_merged"
        return payload

    needs: list[dict[str, Any]] = []
    fleet = explain_pipeline()
    strategies = fleet.get("strategies") if isinstance(fleet, dict) else None
    for entry in strategies or []:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or entry.get("badge") or "").upper()
        if "NEEDS" not in status:
            continue
        needs.append(
            {
                "strategy_id": entry.get("strategy_id") or entry.get("id"),
                "name": entry.get("name") or entry.get("display_name"),
                "stage": entry.get("stage"),
                "status": status,
                "top_blocker": entry.get("top_blocker") or entry.get("blocker"),
                "unblock_action": entry.get("unblock_action"),
            }
        )
    payload["needs_attention"] = needs
    return payload


def _build_approvals() -> dict[str, Any]:
    from forven.control_plane.approvals import get_approvals_list

    items = []
    for row in get_approvals_list(status="pending_approval", limit=50) or []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                key: row.get(key)
                for key in (
                    "id",
                    "approval_type",
                    "target_type",
                    "target_id",
                    "title",
                    "summary",
                    "created_at",
                )
            }
        )
    return {"pending_count": len(items), "items": items}


def _build_equity() -> dict[str, Any]:
    from forven.control_plane.status import get_equity_history

    payload = get_equity_history()
    if isinstance(payload, dict):
        curve = payload.get("curve")
        if isinstance(curve, list) and len(curve) > 288:
            payload = dict(payload)
            payload["curve"] = curve[-288:]
            payload["curve_truncated_from"] = len(curve)
    return payload if isinstance(payload, dict) else {"raw": payload}


def _build_leaderboard() -> dict[str, Any]:
    from forven.api_domains.analytics import (
        get_dashboard_leaderboard_stub,
        get_dashboard_winners_stub,
    )

    return {
        "entries": get_dashboard_leaderboard_stub(limit=15),
        "winners": get_dashboard_winners_stub(limit=10),
    }


def _build_kpis() -> dict[str, Any]:
    # Read-only recompute of the legacy overview stub
    # (api_domains/analytics.py:get_dashboard_overview_stub), which both
    # writes (normalize_daemon_state(write_back=True)) and serializes
    # reassuring zeros for fields it does not measure. Here: no writes, and
    # unmeasured fields are null with an explicit reason.
    from forven.api_domains import analytics as _analytics
    from forven.runtime_health import normalize_daemon_state

    daemon = normalize_daemon_state(write_back=False)
    trading_allowed, trading_reason = _analytics.is_trading_allowed()
    strategy_rows = _analytics.get_strategies()

    lifecycle_counts: dict[str, int] = {}
    best_sharpe = float("-inf")
    terminal_stages = {"archived", "rejected", "backtest_failed"}
    pipeline_count = 0
    for row in strategy_rows:
        if not isinstance(row, dict):
            continue
        stage = _analytics.core._to_lifecycle_state(row.get("stage") or row.get("status"))
        lifecycle_counts[stage] = lifecycle_counts.get(stage, 0) + 1
        if _analytics.normalize_stage(row.get("stage") or row.get("status")) not in terminal_stages:
            pipeline_count += 1
        metrics = _analytics._strategy_metrics(row)
        sharpe = _analytics._metric_float(metrics, "sharpe_ratio", "sharpe", default=float("-inf"))
        if sharpe > best_sharpe:
            best_sharpe = sharpe

    queue_counts = _analytics._get_task_queue_counts()
    ap_settings = _analytics._get_autopilot_settings()
    worker_concurrency = int(ap_settings.get("autopilot_worker_concurrency") or 4)
    autopilot_enabled = bool(ap_settings.get("autopilot_enabled", True))
    daemon_running = bool(daemon.get("running"))
    dead_letter_jobs = queue_counts["failed"]

    last_tick_error = daemon.get("last_tick_error") or None
    if daemon.get("stale_process_detected"):
        last_tick_error = last_tick_error or "Stale daemon process detected — automatic recovery applied."

    return _analytics.sanitize_json_floats(
        {
            "kpis": {
                "total_tested": len(strategy_rows),
                "best_sharpe": best_sharpe if best_sharpe != float("-inf") else None,
                "active_scans": int(daemon.get("scan_count") or 0),
                "signals_today": None,
                "pipeline_count": pipeline_count,
                "data_coverage": None,
            },
            "unknown_fields": {
                "signals_today": "no_signal_counter_source",
                "data_coverage": "no_coverage_metric_source",
                "blocked_count": "no_blocked_metric_source",
            },
            "lifecycle_counts": lifecycle_counts,
            "blocked_count": None,
            "last_ingestion_at": daemon.get("last_scan"),
            "autopilot": {
                "initialized": True,
                "running": daemon_running and autopilot_enabled,
                "paused": not bool(trading_allowed) or not autopilot_enabled,
                "run_id": str(daemon.get("run_id") or "") or None,
                "worker_concurrency": worker_concurrency,
                "active_workers": queue_counts["running"],
                "queued_jobs": queue_counts["queued"],
                "dead_letter_jobs": dead_letter_jobs,
                "last_tick_error": str(last_tick_error) if last_tick_error else None,
                "health_ok": daemon_running and dead_letter_jobs == 0,
                "disabled_reason": (
                    "Autopilot disabled in settings"
                    if not autopilot_enabled
                    else str(trading_reason)
                    if (not trading_allowed and trading_reason)
                    else None
                ),
            },
        }
    )


SECTION_BUILDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "system": _build_system,
    "trading": _build_trading,
    "paper": _build_paper,
    "data": _build_data,
    "scheduler": _build_scheduler,
    "agents": _build_agents,
    "pipeline": _build_pipeline,
    "approvals": _build_approvals,
    "equity": _build_equity,
    "leaderboard": _build_leaderboard,
    "kpis": _build_kpis,
}


# ---------------------------------------------------------------------------
# Inbox derivation — pure function over section data plus process-local
# first/last-observed tracking.
# ---------------------------------------------------------------------------


def _item(
    item_id: str,
    severity: str,
    source: str,
    message: str,
    action_label: str | None = None,
    action_href: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "severity": severity,
        "source": source,
        "message": message,
        "action_label": action_label,
        "action_href": action_href,
        "entity_id": entity_id,
    }


def _derive_inbox_items(sections: dict[str, _SectionRuntime]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def data_of(name: str) -> dict[str, Any]:
        runtime = sections.get(name)
        return runtime.data if runtime and isinstance(runtime.data, dict) else {}

    trading = data_of("trading")
    risk = trading.get("risk") if isinstance(trading.get("risk"), dict) else {}
    if risk.get("kill_switch_active"):
        items.append(
            _item(
                "halt:kill_switch",
                "critical",
                "trading",
                "Kill switch is active — live entries are blocked.",
                "Open Risk",
                "/risk",
            )
        )
    if risk.get("daily_loss_halt"):
        items.append(
            _item(
                "halt:daily_loss",
                "critical",
                "trading",
                "Daily loss halt is engaged.",
                "Open Risk",
                "/risk",
            )
        )
    drawdown = risk.get("drawdown_pct")
    if isinstance(drawdown, (int, float)) and drawdown >= _DRAWDOWN_WARNING_FRACTION:
        items.append(
            _item(
                "risk:drawdown",
                "warning",
                "trading",
                f"Drawdown at {drawdown * 100:.1f}% of high-water mark.",
                "Open Risk",
                "/risk",
            )
        )
    recovery = trading.get("recovery")
    if isinstance(recovery, dict) and recovery.get("active"):
        items.append(
            _item(
                "trading:recovery_gate",
                "warning",
                "trading",
                "Startup recovery gate is active — live entries held until reconcile completes.",
                "Open Diagnostics",
                "/diagnostics",
            )
        )

    approvals = data_of("approvals")
    pending = approvals.get("pending_count")
    if isinstance(pending, int) and pending > 0:
        items.append(
            _item(
                "approvals:pending",
                "warning",
                "approvals",
                f"{pending} approval{'s' if pending != 1 else ''} waiting on you.",
                "Review approvals",
                "/approval",
            )
        )

    system = data_of("system")
    loops = system.get("worker_loops") or system.get("workerLoops")
    if isinstance(loops, list):
        for loop in loops:
            if not isinstance(loop, dict):
                continue
            name = str(loop.get("name") or "loop")
            if loop.get("fresh") is False:
                age = loop.get("age_seconds") or loop.get("ageSeconds")
                age_text = f" (last beat {int(age)}s ago)" if isinstance(age, (int, float)) else ""
                items.append(
                    _item(
                        f"system:loop:{name}",
                        "critical",
                        "system",
                        f"Worker loop '{name}' looks dead{age_text}.",
                        "Open Diagnostics",
                        "/diagnostics",
                        entity_id=name,
                    )
                )
    queues = system.get("queues")
    if isinstance(queues, dict):
        stale_running = 0
        for key, value in queues.items():
            if "stale" in str(key) and isinstance(value, (int, float)):
                stale_running += int(value)
        if stale_running > 0:
            items.append(
                _item(
                    "agents:stale_running",
                    "warning",
                    "agents",
                    f"{stale_running} agent task{'s' if stale_running != 1 else ''} look stalled.",
                    "Open Agents",
                    "/agents",
                )
            )

    kpis = data_of("kpis")
    autopilot = kpis.get("autopilot") if isinstance(kpis.get("autopilot"), dict) else {}
    if autopilot:
        if not autopilot.get("running"):
            reason = autopilot.get("disabled_reason")
            severity = "warning" if reason == "Autopilot disabled in settings" else "critical"
            message = "Autopilot/daemon is not running."
            if reason:
                message = f"Autopilot is not running: {reason}"
            items.append(
                _item(
                    "system:autopilot_down",
                    severity,
                    "system",
                    message,
                    "Open Diagnostics",
                    "/diagnostics",
                )
            )
        dead_letters = autopilot.get("dead_letter_jobs")
        if isinstance(dead_letters, int) and dead_letters > 0:
            items.append(
                _item(
                    "tasks:dead_letters",
                    "warning",
                    "tasks",
                    f"{dead_letters} task{'s' if dead_letters != 1 else ''} permanently failed (dead letter).",
                    "Open Tasks",
                    "/tasks",
                )
            )
        tick_error = autopilot.get("last_tick_error")
        if tick_error:
            items.append(
                _item(
                    "system:last_tick_error",
                    "warning",
                    "system",
                    f"Daemon tick error: {tick_error}",
                    "Open Diagnostics",
                    "/diagnostics",
                )
            )

    data_section = data_of("data")
    ingestion_age = _age_seconds(data_section.get("last_ingestion_at") or kpis.get("last_ingestion_at"))
    if ingestion_age is not None and ingestion_age > _DATA_INGESTION_STALE_SECONDS:
        hours = ingestion_age / 3600
        items.append(
            _item(
                "data:ingestion_stale",
                "warning",
                "data",
                f"Market data ingestion is stale — last run {hours:.1f}h ago.",
                "Open Data",
                "/data",
            )
        )

    scheduler = data_of("scheduler")
    for job_id in (scheduler.get("error_job_ids") or [])[:3]:
        items.append(
            _item(
                f"scheduler:job_error:{job_id}",
                "warning",
                "scheduler",
                f"Scheduled job '{job_id}' failed its last run.",
                "Open Agents",
                "/agents",
                entity_id=str(job_id),
            )
        )
    overdue_ids = scheduler.get("overdue_job_ids") or []
    if overdue_ids:
        items.append(
            _item(
                "scheduler:overdue",
                "warning",
                "scheduler",
                f"{len(overdue_ids)} scheduled job{'s' if len(overdue_ids) != 1 else ''} overdue.",
                "Open Agents",
                "/agents",
            )
        )

    pipeline = data_of("pipeline")
    for entry in (pipeline.get("needs_attention") or [])[:10]:
        if not isinstance(entry, dict):
            continue
        strategy_id = entry.get("strategy_id")
        name = entry.get("name") or strategy_id or "strategy"
        action = entry.get("unblock_action")
        message = f"Strategy '{name}' needs your decision."
        if action:
            message = f"Strategy '{name}' needs you: {action}"
        items.append(
            _item(
                f"pipeline:needs_you:{strategy_id}",
                "warning",
                "pipeline",
                message,
                "Open Pipeline",
                "/pipeline",
                entity_id=str(strategy_id) if strategy_id else None,
            )
        )

    # Stamp first/last observed and drop tracking for resolved conditions.
    now_iso = _utc_now_iso()
    current_ids = set()
    for item in items:
        current_ids.add(item["id"])
        observed = _INBOX_OBSERVED.get(item["id"])
        if observed is None:
            observed = {"first_observed_at": now_iso}
        observed["last_observed_at"] = now_iso
        _INBOX_OBSERVED[item["id"]] = observed
        item.update(observed)
    for stale_id in [key for key in _INBOX_OBSERVED if key not in current_ids]:
        del _INBOX_OBSERVED[stale_id]

    items.sort(key=lambda entry: (_SEVERITY_ORDER.get(entry["severity"], 9), entry["id"]))
    return items


# ---------------------------------------------------------------------------
# Producer + assembly
# ---------------------------------------------------------------------------


def refresh_section(name: str) -> bool:
    """Refresh one section synchronously. Returns True on success.

    Failures retain last-good data and stamp error_code — by contract a
    builder exception can never clear a section or escape to the caller.
    """
    builder = SECTION_BUILDERS[name]
    attempt_at = _utc_now_iso()
    try:
        data = builder()
    except Exception as exc:  # noqa: BLE001 — section isolation is the contract
        log.warning("snapshot section %s failed: %s", name, exc.__class__.__name__, exc_info=True)
        with _STATE_LOCK:
            runtime = _SECTIONS[name]
            runtime.last_attempt_at = attempt_at
            runtime.error_code = exc.__class__.__name__
        return False
    with _STATE_LOCK:
        runtime = _SECTIONS[name]
        runtime.data = data
        runtime.as_of = attempt_at
        runtime.last_attempt_at = attempt_at
        runtime.error_code = None
    return True


def _rederive_inbox() -> None:
    attempt_at = _utc_now_iso()
    global _GENERATED_AT
    try:
        with _STATE_LOCK:
            sections_copy = dict(_SECTIONS)
        items = _derive_inbox_items(sections_copy)
    except Exception as exc:  # noqa: BLE001 — same isolation contract as sections
        log.warning("inbox derivation failed: %s", exc.__class__.__name__, exc_info=True)
        with _STATE_LOCK:
            _INBOX_STATE.last_attempt_at = attempt_at
            _INBOX_STATE.error_code = exc.__class__.__name__
            _GENERATED_AT = attempt_at
        return
    with _STATE_LOCK:
        _INBOX_STATE.data = {"items": items}
        _INBOX_STATE.as_of = attempt_at
        _INBOX_STATE.last_attempt_at = attempt_at
        _INBOX_STATE.error_code = None
        _GENERATED_AT = attempt_at


def refresh_all_sections_once() -> None:
    """Synchronous full refresh — used by tests and startup warm-up."""
    for name in SECTION_BUILDERS:
        refresh_section(name)
    _rederive_inbox()


def _section_status(runtime: _SectionRuntime, policy: SectionPolicy) -> str:
    if runtime.data is None:
        return "unavailable"
    if runtime.error_code is not None:
        return "error"
    age = _age_seconds(runtime.as_of)
    if age is not None and age > policy.stale_after_seconds:
        return "stale"
    return "fresh"


def _serialize_section(runtime: _SectionRuntime, policy: SectionPolicy) -> dict[str, Any]:
    return {
        "status": _section_status(runtime, policy),
        "as_of": runtime.as_of,
        "last_attempt_at": runtime.last_attempt_at,
        "error_code": runtime.error_code,
        "data": runtime.data,
    }


_INBOX_POLICY = SectionPolicy(refresh_seconds=10, stale_after_seconds=60)


def get_snapshot() -> dict[str, Any]:
    """Assemble the snapshot from cached section state. Never runs a builder."""
    with _STATE_LOCK:
        sections = {
            name: _serialize_section(runtime, SECTION_POLICIES[name])
            for name, runtime in _SECTIONS.items()
        }
        inbox = _serialize_section(_INBOX_STATE, _INBOX_POLICY)
        generated_at = _GENERATED_AT
    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": generated_at,
        "served_at": _utc_now_iso(),
        "policies": {
            name: {
                "refresh_seconds": policy.refresh_seconds,
                "stale_after_seconds": policy.stale_after_seconds,
            }
            for name, policy in SECTION_POLICIES.items()
        },
        "sections": sections,
        "inbox": inbox,
    }


async def run_snapshot_producer(tick_seconds: float = 2.0) -> None:
    """Background producer: refresh due sections, then re-derive the inbox.

    Designed for _spawn_supervised_runtime_thread — runs on its own event
    loop, off the API request loop. Builders execute in worker threads so a
    slow SQLite read cannot block this loop's tick either.
    """
    log.info("dashboard snapshot producer starting (%d sections)", len(SECTION_BUILDERS))
    while True:
        now = time.monotonic()
        due: list[str] = []
        with _STATE_LOCK:
            for name, runtime in _SECTIONS.items():
                if now >= runtime.next_refresh_monotonic:
                    runtime.next_refresh_monotonic = now + SECTION_POLICIES[name].refresh_seconds
                    due.append(name)
        if due:
            await asyncio.gather(
                *(asyncio.to_thread(refresh_section, name) for name in due),
                return_exceptions=True,
            )
            _rederive_inbox()
        await asyncio.sleep(tick_seconds)
