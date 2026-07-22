"""Propr strategy mirror (PROPR-2).

The operator picks a roster of strategies on the /propr page; this service
copies their trades onto the Propr challenge account. It deliberately does NOT
touch any execution path: live trading dispatches to Hyperliquid exactly as it
always has, paper trading stays local — the mirror is a read-only OBSERVER of
the trades table that places its own independently-sized orders on Propr.

Runs as the `forven-propr-mirror` interval job (60s, seeded only while the
hidden PROPR-1 flag is on). Every placement rides the adapter's execution
guard, so mirroring works unattended on a verifiable paper/trial account and
fails closed the moment Propr flips the account to real money (then
FORVEN_ALLOW_PROPR_LIVE is required).

Semantics:
* OPEN mirror — a roster strategy's paper/live trade opens => place a Propr
  market entry with the trade's stop/TP as a bracket. Sizing is independent of
  the source: risk_pct (capped) of the CHALLENGE account's equity at the
  trade's stop distance, notional-capped inside the venue's leverage room.
  Entries are skipped (recorded, not retried) when they pre-date the strategy
  joining the roster, are older than the freshness window (a stale entry is a
  different trade than the strategy took), or price already crossed the stop.
* CLOSE mirror — the source trade leaves OPEN => reduce-only close of the
  mirrored quantity + best-effort cancel of the resting bracket legs.
* Idempotency — entry intentIds derive deterministically from the source
  trade id, so a re-tick after a lost state write cannot double-open; closes
  are reduceOnly, so a duplicate close is harmless by construction.
* Propr merges same-asset/same-side positions into one; two roster strategies
  long the same coin share a merged position, and each mirrored close reduces
  it by that trade's own quantity.

State: kv `forven:propr-mirror:state` {trade_id: {...}} — display + retry
bookkeeping only; correctness never depends on it surviving.
Roster: kv `forven:settings` key `propr_mirror_strategies` {sid: added_iso},
toggle `propr_mirror_enabled`. Managed ONLY via /api/propr/mirror (the generic
settings PUT preserves unknown keys; nothing here is in the settings manifest).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from forven.config import propr_enabled
from forven.db import get_db, kv_get, kv_set

log = logging.getLogger("forven.propr_mirror")

MIRROR_ENABLED_KEY = "propr_mirror_enabled"
MIRROR_STRATEGIES_KEY = "propr_mirror_strategies"
STATE_KEY = "forven:propr-mirror:state"
HALT_STATE_KEY = "forven:propr-mirror:halt"

# Account-level halt (PROPR-3): stop OPENING mirrored positions well before the
# venue's challenge rules fail the attempt. 0.8 = halt at 80% of the allowance
# (e.g. a $150/day cap halts new opens at $120 of daily loss). Closes always
# run — reducing risk is never halted.
DAILY_LOSS_HALT_FRACTION = 0.8
DRAWDOWN_HALT_FRACTION = 0.8
# Used only when the challenge payload doesn't carry readable rules — the
# free-trial terms (3%/day, 6% drawdown), which are also Propr's strictest
# published tier, so the fallback can only ever be MORE conservative.
_FALLBACK_DAILY_LOSS_PCT = 3.0
_FALLBACK_DRAWDOWN_PCT = 6.0

# Risk defaults — independent of the source account's sizing.
DEFAULT_RISK_PCT = 0.01
MAX_RISK_PCT = 0.02
# Notional headroom inside the venue's leverage caps (5x BTC/ETH, 2x rest).
_NOTIONAL_LEVERAGE_HEADROOM = {"BTC": 4.5, "ETH": 4.5}
_DEFAULT_NOTIONAL_HEADROOM = 1.9

# An entry the mirror missed by more than this is a DIFFERENT trade than the
# strategy took — skip it rather than chase.
OPEN_FRESHNESS_MINUTES = 30
MAX_OPENS_PER_TICK = 3
MAX_OPEN_ATTEMPTS = 3
MAX_CLOSE_ATTEMPTS = 10
_STATE_RETENTION_DAYS = 7


def _settings() -> dict:
    try:
        raw = kv_get("forven:settings", {})
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def mirror_enabled(settings: dict | None = None) -> bool:
    s = settings if isinstance(settings, dict) else _settings()
    return bool(s.get(MIRROR_ENABLED_KEY, False))


def mirror_roster(settings: dict | None = None) -> dict[str, str]:
    """{strategy_id: added_at_iso}. Malformed entries are dropped, not fatal."""
    s = settings if isinstance(settings, dict) else _settings()
    raw = s.get(MIRROR_STRATEGIES_KEY)
    if not isinstance(raw, dict):
        return {}
    roster: dict[str, str] = {}
    for sid, added in raw.items():
        cleaned = str(sid or "").strip()
        if cleaned:
            roster[cleaned] = str(added or "")
    return roster


def set_mirror_config(enabled: bool | None = None, strategy_ids: list[str] | None = None) -> dict:
    """Persist the toggle and/or roster. New roster entries are stamped with
    the current time so pre-existing open trades are never mirrored; entries
    already on the roster keep their original timestamp."""
    settings = _settings()
    if enabled is not None:
        settings[MIRROR_ENABLED_KEY] = bool(enabled)
    if strategy_ids is not None:
        existing = mirror_roster(settings)
        now_iso = datetime.now(timezone.utc).isoformat()
        settings[MIRROR_STRATEGIES_KEY] = {
            str(sid).strip(): existing.get(str(sid).strip(), now_iso)
            for sid in strategy_ids
            if str(sid or "").strip()
        }
    kv_set("forven:settings", settings)
    return {"enabled": mirror_enabled(settings), "strategies": mirror_roster(settings)}


def get_state() -> dict:
    raw = kv_get(STATE_KEY, {}) or {}
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict) -> None:
    kv_set(STATE_KEY, state)


def get_halt_state() -> dict:
    raw = kv_get(HALT_STATE_KEY, {}) or {}
    return raw if isinstance(raw, dict) else {}


def _num(value) -> float | None:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _challenge_rules(attempt: dict, equity: float, high_water_mark: float | None) -> dict:
    """The CURRENT phase's risk rules, from the venue's own challenge payload.

    Propr expresses limits as percentages of the phase starting balance
    (maxDailyLossPercent / maxDrawdownPercent) with drawdownType static
    (measured from the starting balance) or trailing (from the high-water
    mark). Unreadable fields fall back to the strictest published tier, so a
    parse failure can only make the halt MORE conservative.
    """
    challenge = attempt.get("challenge") if isinstance(attempt.get("challenge"), dict) else {}
    attempt_phases = [p for p in (attempt.get("phases") or []) if isinstance(p, dict)]
    current_id = str(attempt.get("currentPhaseId") or "")
    att_phase = next(
        (p for p in attempt_phases if str(p.get("attemptPhaseId") or "") == current_id), None
    )
    if att_phase is None and attempt_phases:
        att_phase = next(
            (p for p in attempt_phases if str(p.get("status") or "").lower() == "active"),
            attempt_phases[0],
        )
    starting_balance = _num((att_phase or {}).get("startingBalance")) or _num(
        challenge.get("initialBalance")
    )

    ch_phase = None
    if att_phase is not None:
        phase_id = str(att_phase.get("phaseId") or "")
        ch_phase = next(
            (p for p in (challenge.get("phases") or [])
             if isinstance(p, dict) and str(p.get("phaseId") or "") == phase_id),
            None,
        )
    if ch_phase is None:
        phases = [p for p in (challenge.get("phases") or []) if isinstance(p, dict)]
        ch_phase = phases[0] if phases else {}

    daily_pct = _num(ch_phase.get("maxDailyLossPercent"))
    dd_pct = _num(ch_phase.get("maxDrawdownPercent"))
    dd_type = str(ch_phase.get("drawdownType") or "").strip().lower()
    source = "challenge"
    if daily_pct is None or dd_pct is None or starting_balance is None:
        source = "defaults"
        daily_pct = daily_pct or _FALLBACK_DAILY_LOSS_PCT
        dd_pct = dd_pct or _FALLBACK_DRAWDOWN_PCT
        starting_balance = starting_balance or equity

    # Drawdown reference/floor pair by type; unknown type takes whichever pair
    # yields the HIGHER floor (the conservative one).
    static_ref = starting_balance
    static_floor = starting_balance * (1 - dd_pct / 100.0)
    trailing_ref = high_water_mark or equity
    trailing_floor = trailing_ref * (1 - dd_pct / 100.0)
    if dd_type == "trailing":
        dd_ref, dd_floor = trailing_ref, trailing_floor
    elif dd_type == "static":
        dd_ref, dd_floor = static_ref, static_floor
    else:
        dd_ref, dd_floor = (
            (trailing_ref, trailing_floor)
            if trailing_floor >= static_floor
            else (static_ref, static_floor)
        )

    return {
        "source": source,
        "starting_balance": starting_balance,
        "daily_loss_limit_usd": starting_balance * daily_pct / 100.0,
        "drawdown_type": dd_type or "unknown",
        "drawdown_ref": dd_ref,
        "drawdown_floor": dd_floor,
    }


def _evaluate_halt(attempt: dict, equity: float, now: datetime) -> dict:
    """Account-level halt check, persisted for the page. Day-start equity is
    OUR first observation of the UTC day — a strictly tighter proxy than the
    venue's day anchor is guaranteed to be, since we can only observe late."""
    prev = get_halt_state()
    day = now.strftime("%Y-%m-%d")
    if str(prev.get("day") or "") == day and _num(prev.get("day_start_equity")):
        day_start = float(prev["day_start_equity"])
    else:
        day_start = equity

    account = attempt.get("account") if isinstance(attempt.get("account"), dict) else {}
    high_water_mark = _num(account.get("highWaterMark"))
    rules = _challenge_rules(attempt, equity, high_water_mark)

    reasons: list[str] = []
    daily_loss = max(0.0, day_start - equity)
    daily_budget = rules["daily_loss_limit_usd"] * DAILY_LOSS_HALT_FRACTION
    if daily_loss >= daily_budget:
        reasons.append(
            f"daily loss ${daily_loss:.2f} reached {DAILY_LOSS_HALT_FRACTION:.0%} of the "
            f"${rules['daily_loss_limit_usd']:.2f} venue cap"
        )
    dd_used = max(0.0, rules["drawdown_ref"] - equity)
    dd_allowance = max(0.0, rules["drawdown_ref"] - rules["drawdown_floor"])
    if dd_allowance > 0 and dd_used >= DRAWDOWN_HALT_FRACTION * dd_allowance:
        reasons.append(
            f"drawdown ${dd_used:.2f} reached {DRAWDOWN_HALT_FRACTION:.0%} of the "
            f"${dd_allowance:.2f} allowance ({rules['drawdown_type']})"
        )

    halt = {
        "day": day,
        "day_start_equity": day_start,
        "equity": equity,
        "daily_loss": daily_loss,
        "daily_loss_limit_usd": rules["daily_loss_limit_usd"],
        "daily_halt_at_usd": daily_budget,
        "drawdown_used": dd_used,
        "drawdown_allowance_usd": dd_allowance,
        "drawdown_type": rules["drawdown_type"],
        "rules_source": rules["source"],
        "halted": bool(reasons),
        "reasons": reasons,
        "checked_at": now.isoformat(),
    }
    kv_set(HALT_STATE_KEY, halt)

    if reasons and not prev.get("halted"):
        log.warning("Propr mirror HALTED (opens blocked): %s", "; ".join(reasons))
        try:
            from forven.notifications import emit_notification
            emit_notification(
                "propr_mirror_halt",
                severity="warning",
                source="propr_mirror",
                title="Propr mirror halted — challenge limit proximity",
                summary="; ".join(reasons),
                body=(
                    "New mirrored opens are blocked to protect the challenge account. "
                    "Closes still execute. The daily-loss halt clears at the next UTC "
                    "day; the drawdown halt clears if equity recovers."
                ),
                dedupe_key=f"propr_mirror_halt:{day}",
            )
        except Exception as exc:
            log.debug("Could not emit propr mirror halt notification: %s", exc)
    return halt


def roster_candidates() -> list[dict]:
    """Strategies the picker offers: paper / live_graduated / gauntlet stages."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, COALESCE(display_name, name) AS name, stage, timeframe "
            "FROM strategies WHERE stage IN ('paper', 'live_graduated', 'gauntlet') "
            "ORDER BY stage, id"
        ).fetchall()
    return [dict(r) for r in rows]


def _parse_when(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _signal_data(row: dict) -> dict:
    raw = row.get("signal_data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _stop_and_tp(row: dict) -> tuple[float | None, float | None]:
    data = _signal_data(row)

    def _num(*keys):
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    return _num("stop_loss_price", "stop_loss"), _num("take_profit_price", "take_profit")


def _roster_trades(roster_ids: set[str]) -> list[dict]:
    """OPEN paper/live trades belonging to roster strategies."""
    if not roster_ids:
        return []
    placeholders = ",".join("?" for _ in roster_ids)
    params = list(roster_ids)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, strategy, strategy_id, asset, direction, entry_price, risk_pct, "
            f"leverage, status, execution_type, opened_at, signal_data FROM trades "
            f"WHERE status = 'OPEN' AND execution_type IN ('paper', 'live') "
            f"AND (strategy_id IN ({placeholders}) OR strategy IN ({placeholders}))",
            params + params,
        ).fetchall()
    return [dict(r) for r in rows]


def _trade_status(trade_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return str(dict(row)["status"]) if row else None


def _trade_roster_key(row: dict, roster_ids: set[str]) -> str | None:
    for key in ("strategy_id", "strategy"):
        value = str(row.get(key) or "").strip()
        if value and value in roster_ids:
            return value
    return None


def _size_mirror_order(
    asset: str, mid: float, stop_price: float, risk_pct: float | None,
    leverage: float | None, equity: float,
) -> tuple[float, str | None]:
    """Independent Propr sizing: risk fraction of CHALLENGE equity at the stop
    distance, notional-capped inside the venue's leverage room. Returns
    (size, skip_reason)."""
    risk = float(risk_pct) if risk_pct else DEFAULT_RISK_PCT
    risk = min(max(risk, 0.0), MAX_RISK_PCT) or DEFAULT_RISK_PCT
    stop_dist = abs(mid - stop_price)
    if stop_dist <= 0:
        return 0.0, "zero stop distance"
    size = (equity * risk) / stop_dist
    headroom = _NOTIONAL_LEVERAGE_HEADROOM.get(asset, _DEFAULT_NOTIONAL_HEADROOM)
    if leverage:
        headroom = min(headroom, max(1.0, float(leverage)))
    max_notional = equity * headroom
    if size * mid > max_notional:
        size = max_notional / mid
    return size, None


def _mirror_open(
    propr, row: dict, state: dict, equity: float, now: datetime,
    risk_budget: dict | None = None,
) -> None:
    trade_id = str(row["id"])
    entry = state.setdefault(trade_id, {"status": "pending", "attempts": 0})
    entry.update({
        "strategy": str(row.get("strategy_id") or row.get("strategy") or ""),
        "asset": str(row.get("asset") or ""),
        "direction": str(row.get("direction") or ""),
        "source_execution_type": str(row.get("execution_type") or ""),
    })

    asset = propr.normalize_asset(str(row.get("asset") or ""))
    direction = str(row.get("direction") or "").strip().lower()
    stop_price, tp_price = _stop_and_tp(row)
    if stop_price is None:
        entry.update({"status": "skipped", "reason": "source trade has no stop — never mirror unprotected"})
        return

    mid = float(propr.get_all_mids().get(asset, 0) or 0)
    if mid <= 0:
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry.update({"status": "error" if entry["attempts"] < MAX_OPEN_ATTEMPTS else "failed",
                      "reason": f"no mid price for {asset}"})
        return

    # Price already through the stop => the trade is over before we arrived.
    if (direction == "long" and mid <= stop_price) or (direction == "short" and mid >= stop_price):
        entry.update({"status": "skipped", "reason": "price already beyond the stop at mirror time"})
        return

    size, skip_reason = _size_mirror_order(
        asset, mid, stop_price, row.get("risk_pct"), row.get("leverage"), equity
    )
    if skip_reason or size <= 0:
        entry.update({"status": "skipped", "reason": skip_reason or "size resolved to zero"})
        return

    # PROPR-3: loss-at-stop of everything already open counts against the
    # remaining daily budget, so N concurrent stop-outs can't stack past the
    # venue's daily cap. Deferred (status stays pending) — it retries next
    # tick and mirrors normally if room frees up inside the freshness window.
    risk_usd = size * abs(mid - stop_price)
    if risk_budget is not None:
        if risk_usd > risk_budget.get("remaining", 0.0):
            entry.update({
                "status": "pending",
                "reason": (
                    f"deferred: risk-at-stop ${risk_usd:.2f} exceeds the remaining "
                    f"daily budget ${risk_budget.get('remaining', 0.0):.2f}"
                ),
            })
            return

    lev = propr.set_leverage(asset, float(row.get("leverage") or 1.0))
    if isinstance(lev, dict) and lev.get("error"):
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry.update({"status": "error" if entry["attempts"] < MAX_OPEN_ATTEMPTS else "failed",
                      "reason": f"set_leverage: {lev['error']}"})
        return

    result = propr.market_order(
        asset,
        "buy" if direction == "long" else "sell",
        size,
        stop_loss_price=stop_price,
        take_profit_price=tp_price,
        idempotency_key=f"propr-mirror:{trade_id}",
    )
    if isinstance(result, dict) and not result.get("error"):
        entry.update({
            "status": "open",
            "reason": None,
            "quantity": result.get("filled_size") or size,
            "entry_price": result.get("entry_price"),
            "entry_order_id": result.get("entry_order_id"),
            "stop_order_id": result.get("stop_order_id"),
            "take_profit_order_id": result.get("take_profit_order_id"),
            "risk_usd": risk_usd,
            "opened_at": now.isoformat(),
        })
        if risk_budget is not None:
            risk_budget["remaining"] = max(0.0, risk_budget.get("remaining", 0.0) - risk_usd)
        log.info("Propr mirror OPEN %s %s %s (size %.6g) for trade %s",
                 asset, direction, entry["strategy"], size, trade_id)
    else:
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry.update({"status": "error" if entry["attempts"] < MAX_OPEN_ATTEMPTS else "failed",
                      "reason": str((result or {}).get("error") or "order rejected")})


def _mirror_close(propr, trade_id: str, entry: dict, now: datetime) -> None:
    asset = propr.normalize_asset(str(entry.get("asset") or ""))
    direction = str(entry.get("direction") or "").strip().lower()
    quantity = float(entry.get("quantity") or 0)
    if quantity <= 0:
        entry.update({"status": "closed", "reason": "nothing to close (zero mirrored quantity)"})
        return
    result = propr.close_position(asset, quantity, "sell" if direction == "long" else "buy")
    if isinstance(result, dict) and not result.get("error"):
        entry.update({
            "status": "closed",
            "reason": None,
            "exit_price": result.get("exit_price"),
            "closed_at": now.isoformat(),
        })
        # The bracket legs are reduce-only so they can never re-open a
        # position, but cancel them anyway to keep the order book tidy.
        for leg_key in ("stop_order_id", "take_profit_order_id"):
            leg_id = entry.get(leg_key)
            if leg_id:
                try:
                    propr.cancel_order(asset, leg_id)
                except Exception as exc:
                    log.debug("Propr mirror: bracket cancel %s failed: %s", leg_id, exc)
        log.info("Propr mirror CLOSE %s %s for trade %s", asset, direction, trade_id)
    else:
        attempts = int(entry.get("close_attempts") or 0) + 1
        entry["close_attempts"] = attempts
        entry["reason"] = str((result or {}).get("error") or "close rejected")
        if attempts >= MAX_CLOSE_ATTEMPTS:
            entry["status"] = "close_failed"
            log.error("Propr mirror: close for trade %s FAILED after %d attempts: %s",
                      trade_id, attempts, entry["reason"])


def mirror_tick() -> dict:
    """One observer pass. Cheap no-op unless the flag, the toggle, and a
    non-empty roster all hold. Never raises — the job wrapper logs the summary."""
    if not propr_enabled():
        return {"skipped": "propr disabled"}
    settings = _settings()
    if not mirror_enabled(settings):
        return {"skipped": "mirror disabled"}
    roster = mirror_roster(settings)
    if not roster:
        return {"skipped": "empty roster"}
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        return {"skipped": "sim active"}

    from forven.exchange import propr

    now = datetime.now(timezone.utc)
    state = get_state()
    summary = {"opened": 0, "closed": 0, "errors": 0, "skipped": 0}
    roster_ids = set(roster)

    # --- close pass first: reducing risk always outranks adding it ----------
    for trade_id, entry in list(state.items()):
        if entry.get("status") not in ("open",):
            continue
        source_status = _trade_status(trade_id)
        if source_status == "OPEN":
            continue
        try:
            _mirror_close(propr, trade_id, entry, now)
            summary["closed" if entry.get("status") == "closed" else "errors"] += 1
        except Exception as exc:
            summary["errors"] += 1
            entry["reason"] = str(exc)
            log.warning("Propr mirror close for %s raised: %s", trade_id, exc)

    # --- account read + PROPR-3 halt evaluation ------------------------------
    # Every active tick (not just when there's something to open) so the
    # day-start equity anchor is captured early and the page's halt readout
    # stays current.
    equity = None
    attempt_payload: dict = {}
    try:
        account = propr.get_account_value() or {}
        equity = _num(account.get("accountValue"))
        attempt_payload = account.get("attempt") if isinstance(account.get("attempt"), dict) else {}
    except Exception as exc:
        log.debug("Propr mirror: account read failed: %s", exc)
    halt = _evaluate_halt(attempt_payload, equity, now) if equity else None

    # --- open pass -----------------------------------------------------------
    try:
        open_rows = _roster_trades(roster_ids)
    except Exception as exc:
        _save_state(state)
        return {**summary, "error": f"trades query failed: {exc}"}

    to_open = []
    for row in open_rows:
        trade_id = str(row["id"])
        existing = state.get(trade_id)
        if existing and existing.get("status") not in ("pending", "error"):
            continue
        sid = _trade_roster_key(row, roster_ids)
        added_at = _parse_when(roster.get(sid or ""))
        opened_at = _parse_when(row.get("opened_at"))
        if added_at and opened_at and opened_at < added_at:
            state[trade_id] = {"status": "skipped", "reason": "opened before strategy joined the roster",
                               "asset": row.get("asset"), "strategy": sid}
            summary["skipped"] += 1
            continue
        if opened_at and (now - opened_at) > timedelta(minutes=OPEN_FRESHNESS_MINUTES):
            state[trade_id] = {"status": "skipped", "reason": "entry older than the freshness window",
                               "asset": row.get("asset"), "strategy": sid}
            summary["skipped"] += 1
            continue
        to_open.append(row)

    if to_open and halt and halt.get("halted"):
        # PROPR-3: opens blocked near the challenge limits. Trades are NOT
        # marked in state — if the halt clears inside their freshness window
        # they mirror normally; past it they expire to a stale-skip, which is
        # correct (a delayed entry is a different trade than the strategy took).
        summary["halted"] = halt.get("reasons")
    elif to_open:
        if not equity:
            for row in to_open:
                entry = state.setdefault(str(row["id"]), {"attempts": 0})
                entry.update({"status": "error", "reason": "challenge equity unavailable — fail closed"})
            summary["errors"] += len(to_open)
        else:
            # Remaining daily risk room = the halt line, minus realized daily
            # loss, minus loss-at-stop of every mirrored position still open —
            # so concurrent stop-outs can't stack past the venue's daily cap.
            risk_budget = None
            if halt is not None:
                open_risk = sum(
                    _num(e.get("risk_usd")) or 0.0
                    for e in state.values()
                    if e.get("status") == "open"
                )
                risk_budget = {
                    "remaining": max(
                        0.0,
                        float(halt["daily_halt_at_usd"]) - float(halt["daily_loss"]) - open_risk,
                    )
                }
            for row in to_open[:MAX_OPENS_PER_TICK]:
                try:
                    _mirror_open(propr, row, state, equity, now, risk_budget=risk_budget)
                    status = state.get(str(row["id"]), {}).get("status")
                    if status == "open":
                        summary["opened"] += 1
                    elif status == "skipped":
                        summary["skipped"] += 1
                    elif status == "pending":
                        summary["deferred"] = summary.get("deferred", 0) + 1
                    else:
                        summary["errors"] += 1
                except Exception as exc:
                    summary["errors"] += 1
                    state.setdefault(str(row["id"]), {})["reason"] = str(exc)
                    log.warning("Propr mirror open for %s raised: %s", row["id"], exc)

    # --- prune aged terminal entries ----------------------------------------
    cutoff = now - timedelta(days=_STATE_RETENTION_DAYS)
    for trade_id, entry in list(state.items()):
        if entry.get("status") in ("closed", "skipped", "failed", "close_failed"):
            # Terminal records without their own timestamp (skips/failures) are
            # stamped now so they show on the page for the retention window
            # instead of being pruned in the same tick that wrote them.
            entry.setdefault("recorded_at", now.isoformat())
            stamp = _parse_when(
                entry.get("closed_at") or entry.get("recorded_at") or entry.get("opened_at")
            )
            if stamp is None or stamp < cutoff:
                state.pop(trade_id, None)

    _save_state(state)
    return summary
