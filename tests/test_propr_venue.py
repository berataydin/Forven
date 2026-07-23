"""PROPR-1: Propr.xyz prop-firm venue — hidden flag, guards, adapter contract.

Propr has NO testnet, so the safety model is layered: a hidden visibility flag
(FORVEN_PROPR_ENABLED — deliberately absent from the settings manifest/UI)
gates the nav page, API routes, and venue selection; a separate explicit
opt-in (FORVEN_ALLOW_PROPR_LIVE) gates every order-placing call. These tests
pin the guard behavior, the deterministic-ULID idempotency scheme, the order
payload contract (reduceOnly legs, order groups), and the scanner's
stamped-venue close routing.
"""

from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# ULIDs
# ---------------------------------------------------------------------------

_CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_new_ulid_shape_and_uniqueness():
    from forven.exchange.propr import new_ulid

    seen = {new_ulid() for _ in range(50)}
    assert len(seen) == 50
    for value in seen:
        assert len(value) == 26
        assert set(value) <= _CROCKFORD


def test_deterministic_ulid_is_stable_and_key_sensitive():
    from forven.exchange.propr import deterministic_ulid

    a1 = deterministic_ulid("T123:open:entry")
    a2 = deterministic_ulid("T123:open:entry")
    b = deterministic_ulid("T123:open:stop")
    assert a1 == a2  # same key => same intentId => retry-safe idempotency
    assert a1 != b
    assert len(a1) == 26 and set(a1) <= _CROCKFORD


# ---------------------------------------------------------------------------
# Hidden flag + venue resolution
# ---------------------------------------------------------------------------

def test_propr_disabled_by_default(monkeypatch):
    from forven.config import propr_enabled

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    assert propr_enabled() is False


def test_propr_enabled_via_env(monkeypatch):
    from forven.config import propr_enabled

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    assert propr_enabled() is True
    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "0")
    assert propr_enabled() is False


def test_beta_build_forces_propr_off(monkeypatch):
    from forven.config import propr_enabled

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.setenv("FORVEN_ENV", "beta")
    assert propr_enabled() is False


# ---------------------------------------------------------------------------
# Order-placement guards
# ---------------------------------------------------------------------------

def test_market_order_refuses_without_enable_flag(monkeypatch):
    from forven.exchange import propr

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="not enabled"):
        propr.market_order("BTC", "buy", 0.001)


def test_market_order_refuses_without_opt_in_or_paper_account(monkeypatch):
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    # Account type unverifiable (no API key in tests) => fail closed.
    monkeypatch.setattr(propr, "get_account_type", lambda force_refresh=False: None)
    with pytest.raises(RuntimeError, match="not verifiably a paper"):
        propr.market_order("BTC", "buy", 0.001)


def test_real_account_type_fails_closed(monkeypatch):
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    # The evaluation ended: Propr now reports a funded/real account type.
    monkeypatch.setattr(propr, "get_account_type", lambda force_refresh=False: "live")
    with pytest.raises(RuntimeError, match="not verifiably a paper"):
        propr.close_position("BTC", 0.001, "sell")
    with pytest.raises(RuntimeError, match="not verifiably a paper"):
        propr.set_leverage("BTC", 2.0)


# ---------------------------------------------------------------------------
# Order payload contract (HTTP fully mocked)
# ---------------------------------------------------------------------------

@pytest.fixture
def armed_propr(monkeypatch):
    """Propr adapter with guards satisfied and all I/O stubbed."""
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.setenv("FORVEN_ALLOW_PROPR_LIVE", "1")
    monkeypatch.setattr(propr, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(propr, "_quantize_size", lambda asset, size: float(size))
    monkeypatch.setattr(propr, "_round_price", lambda price, asset: float(price))
    monkeypatch.setattr(propr, "resolve_account", lambda force_refresh=False: ("acct-1", "att-1"))

    import forven.exchange.liquidity as liquidity
    monkeypatch.setattr(liquidity, "check_order_liquidity", lambda *a, **k: (True, None))

    sent: list[dict] = []

    def fake_create(account_id, orders):
        sent.append({"account_id": account_id, "orders": orders})
        created = []
        for i, order in enumerate(orders):
            created.append({
                "orderId": f"oid-{i}",
                "intentId": order["intentId"],
                "status": "filled" if order["type"] == "market" else "open",
                "averageFillPrice": "50010" if order["type"] == "market" else None,
                "cumulativeQuantity": order["quantity"],
            })
        return created

    monkeypatch.setattr(propr, "_create_orders", fake_create)
    return propr, sent


def test_market_order_builds_grouped_bracket(armed_propr):
    propr, sent = armed_propr
    result = propr.market_order(
        "BTC", "buy", 0.002,
        stop_loss_price=48_000.0,
        take_profit_price=55_000.0,
        idempotency_key="T1:open",
    )
    assert not result.get("error")
    orders = sent[0]["orders"]
    assert [o["type"] for o in orders] == ["market", "stop_market", "take_profit_market"]
    entry, stop, tp = orders
    assert entry["reduceOnly"] is False
    assert stop["reduceOnly"] is True and tp["reduceOnly"] is True
    # Protective legs sit on the exit side of a long.
    assert entry["side"] == "buy" and stop["side"] == "sell" and tp["side"] == "sell"
    assert entry["positionSide"] == stop["positionSide"] == tp["positionSide"] == "long"
    # Batched orders share one ULID order group.
    groups = {o["orderGroupId"] for o in orders}
    assert len(groups) == 1 and len(groups.pop()) == 26
    # Return contract the scanner's _extract_order_meta reads.
    assert result["entry_order_id"] == "oid-0"
    assert result["stop_order_id"] == "oid-1"
    assert result["take_profit_order_id"] == "oid-2"
    assert result["order_ids"] == {"entry": "oid-0", "stop": "oid-1", "take_profit": "oid-2"}
    assert result["entry_price"] == pytest.approx(50_010.0)
    assert not result.get("fill_price_unknown")


def test_market_order_idempotency_keys_are_stable(armed_propr):
    propr, sent = armed_propr
    propr.market_order("BTC", "buy", 0.002, stop_loss_price=48_000.0, idempotency_key="T2:open")
    propr.market_order("BTC", "buy", 0.002, stop_loss_price=48_000.0, idempotency_key="T2:open")
    first, second = sent[0]["orders"], sent[1]["orders"]
    assert [o["intentId"] for o in first] == [o["intentId"] for o in second]


def test_market_order_refuses_inverted_stop(armed_propr):
    propr, _ = armed_propr
    result = propr.market_order("BTC", "buy", 0.002, stop_loss_price=51_000.0)
    assert "inverted stop-loss" in result["error"]


def test_close_position_is_reduce_only(armed_propr):
    propr, sent = armed_propr
    result = propr.close_position("BTC", 0.002, "sell")
    assert not result.get("error")
    (order,) = sent[0]["orders"]
    assert order["reduceOnly"] is True
    assert order["type"] == "market"
    # Selling to close reduces the LONG side.
    assert order["side"] == "sell" and order["positionSide"] == "long"
    assert result["exit_price"] == pytest.approx(50_010.0)
    assert result["order_id"] == "oid-0"


def test_market_order_rejects_vault_routing(armed_propr):
    propr, _ = armed_propr
    result = propr.market_order("BTC", "buy", 0.002, vault_address="0x" + "1" * 40)
    assert "sub-account" in result["error"]


def test_set_leverage_sends_the_sdk_exact_body(monkeypatch):
    """The margin-config PUT requires ALL FOUR fields (exchange, asset,
    marginMode, leverage) with leverage as an INTEGER — per Propr's own SDK
    (update_margin_config). Missing exchange/asset 400'd the first mirrored
    trades on 2026-07-23."""
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.setenv("FORVEN_ALLOW_PROPR_LIVE", "1")
    monkeypatch.setattr(propr, "resolve_account", lambda force_refresh=False: ("acct-1", "att-1"))
    propr._leverage_limits_cache.update({"limits": {"ETH": 5.0}, "at": 1e18})

    calls = []

    def fake_request(method, path, *, breaker, body=None, params=None, **kw):
        calls.append({"method": method, "path": path, "body": body})
        if method == "GET" and "margin-config" in path:
            # Real live response shape.
            return {"configId": "urn:prp-margin-config:X", "asset": "ETH",
                    "marginMode": "cross", "leverage": "1"}
        return {}

    monkeypatch.setattr(propr, "_request", fake_request)
    result = propr.set_leverage("ETH", 2.0)
    assert result == {"leverage": 2, "clamped": False}
    put = next(c for c in calls if c["method"] == "PUT")
    assert put["body"] == {
        "exchange": "hyperliquid",
        "asset": "ETH",
        "marginMode": "cross",
        "leverage": 2,
    }
    assert isinstance(put["body"]["leverage"], int)


# ---------------------------------------------------------------------------
# Router hiding
# ---------------------------------------------------------------------------

def test_status_reports_only_enabled_false_when_hidden(monkeypatch):
    from forven.exchange import propr

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    assert propr.get_status() == {"enabled": False}


def test_router_routes_404_when_hidden(monkeypatch):
    from fastapi import HTTPException

    from forven.routers import propr as propr_router

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    with pytest.raises(HTTPException) as exc:
        propr_router.propr_overview()
    assert exc.value.status_code == 404


def test_paper_account_type_bypasses_env_opt_in(armed_propr, monkeypatch):
    """The free-trial path: no env opt-in, but Propr verifies the account as
    paper — orders place. This bypass dies with the trial (previous test)."""
    propr, sent = armed_propr
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    monkeypatch.setattr(propr, "get_account_type", lambda force_refresh=False: "paper")
    result = propr.market_order("BTC", "buy", 0.002, stop_loss_price=48_000.0)
    assert not result.get("error")
    assert sent, "order should have been submitted"


# ---------------------------------------------------------------------------
# Strategy mirror (PROPR-2)
# ---------------------------------------------------------------------------

def _insert_trade(trade_id: str, strategy_id: str, *, asset: str = "BTC",
                  direction: str = "long", status: str = "OPEN",
                  opened_at: str | None = None, stop: float | None = 48_000.0,
                  execution_type: str = "paper") -> None:
    from datetime import datetime, timezone

    from forven.db import get_db

    signal = {}
    if stop is not None:
        signal["stop_loss_price"] = stop
        signal["take_profit_price"] = 55_000.0
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, "
            "risk_pct, leverage, status, execution_type, opened_at, signal_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_id, strategy_id, strategy_id, asset, direction, 50_000.0,
                0.01, 1.0, status, execution_type,
                opened_at or datetime.now(timezone.utc).isoformat(), json.dumps(signal),
            ),
        )


@pytest.fixture
def mirror_env(forven_db, monkeypatch):
    """Mirror enabled with strategy S-M1 on the roster and the adapter stubbed."""
    import forven.propr_mirror as pm
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    pm.set_mirror_config(enabled=True, strategy_ids=["S-M1"])
    # Roster stamp must PRE-date the test trades.
    from forven.db import kv_get, kv_set
    settings = kv_get("forven:settings", {})
    settings[pm.MIRROR_STRATEGIES_KEY] = {"S-M1": "2020-01-01T00:00:00+00:00"}
    kv_set("forven:settings", settings)

    calls: dict = {"orders": [], "closes": [], "levs": [], "equity": {"value": 5_000.0}}
    # Shaped like the real attempt-detail payload (verified live): static
    # 3%-daily / 6%-drawdown rules on a $5,000 phase.
    attempt_payload = {
        "currentPhaseId": "ap-1",
        "phases": [{"attemptPhaseId": "ap-1", "phaseId": "p-1",
                    "status": "active", "startingBalance": "5000"}],
        "challenge": {
            "initialBalance": "5000",
            "phases": [{"phaseId": "p-1", "maxDailyLossPercent": "3",
                        "maxDrawdownPercent": "6", "drawdownType": "static",
                        "profitTargetPercent": "10"}],
        },
        "account": {"highWaterMark": "5000"},
    }
    monkeypatch.setattr(propr, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(
        propr, "get_account_value",
        lambda *a, **k: {"accountValue": calls["equity"]["value"], "attempt": attempt_payload},
    )
    monkeypatch.setattr(propr, "set_leverage",
                        lambda *a, **k: calls["levs"].append(a) or {"leverage": 1.0})

    def fake_market_order(asset, side, size, **kwargs):
        calls["orders"].append({"asset": asset, "side": side, "size": size, **kwargs})
        return {"entry_order_id": "oid-e", "stop_order_id": "oid-s",
                "entry_price": 50_005.0, "filled_size": size,
                "order_ids": {"entry": "oid-e", "stop": "oid-s"}}

    def fake_close(asset, size, side, **kwargs):
        calls["closes"].append({"asset": asset, "size": size, "side": side})
        return {"exit_price": 51_000.0, "order_id": "oid-c"}

    monkeypatch.setattr(propr, "market_order", fake_market_order)
    monkeypatch.setattr(propr, "close_position", fake_close)
    monkeypatch.setattr(propr, "cancel_order", lambda *a, **k: {"cancelled": True})
    return pm, calls


def test_mirror_tick_noops_when_disabled(forven_db, monkeypatch):
    import forven.propr_mirror as pm

    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    pm.set_mirror_config(enabled=False, strategy_ids=["S-M1"])
    assert pm.mirror_tick() == {"skipped": "mirror disabled"}
    pm.set_mirror_config(enabled=True, strategy_ids=[])
    assert pm.mirror_tick() == {"skipped": "empty roster"}


def test_mirror_opens_fresh_roster_trade(mirror_env):
    pm, calls = mirror_env
    _insert_trade("T-m1", "S-M1")
    _insert_trade("T-other", "S-OTHER", asset="ETH")  # not on the roster

    summary = pm.mirror_tick()
    assert summary["opened"] == 1
    assert len(calls["orders"]) == 1
    order = calls["orders"][0]
    assert order["asset"] == "BTC" and order["side"] == "buy"
    assert order["stop_loss_price"] == 48_000.0
    assert order["idempotency_key"] == "propr-mirror:T-m1"
    # Sizing: 1% of $5,000 equity over a $2,000 stop distance = 0.025 BTC.
    assert order["size"] == pytest.approx(0.025)
    assert pm.get_state()["T-m1"]["status"] == "open"


def test_mirror_skips_preexisting_and_unprotected_trades(mirror_env):
    pm, calls = mirror_env
    _insert_trade("T-pre", "S-M1", opened_at="2019-06-01T00:00:00+00:00")
    _insert_trade("T-naked", "S-M1", asset="ETH", stop=None)

    summary = pm.mirror_tick()
    assert summary["opened"] == 0
    assert not calls["orders"]
    state = pm.get_state()
    assert state["T-pre"]["status"] == "skipped"
    assert "roster" in state["T-pre"]["reason"]
    assert state["T-naked"]["status"] == "skipped"
    assert "no stop" in state["T-naked"]["reason"]


def test_mirror_closes_when_source_closes(mirror_env):
    pm, calls = mirror_env
    _insert_trade("T-m2", "S-M1")
    pm.mirror_tick()
    assert pm.get_state()["T-m2"]["status"] == "open"

    from forven.db import get_db
    with get_db() as conn:
        conn.execute("UPDATE trades SET status = 'CLOSED' WHERE id = ?", ("T-m2",))

    summary = pm.mirror_tick()
    assert summary["closed"] == 1
    assert len(calls["closes"]) == 1
    close = calls["closes"][0]
    # Closing a mirrored long = reduce-only SELL of the mirrored quantity.
    assert close["side"] == "sell"
    assert close["size"] == pytest.approx(0.025)
    assert pm.get_state()["T-m2"]["status"] == "closed"


def test_challenge_rules_parse_the_real_payload_shape():
    from forven.propr_mirror import _challenge_rules

    attempt = {
        "currentPhaseId": "ap-1",
        "phases": [{"attemptPhaseId": "ap-1", "phaseId": "p-1", "startingBalance": "5000"}],
        "challenge": {"phases": [{"phaseId": "p-1", "maxDailyLossPercent": "3",
                                  "maxDrawdownPercent": "6", "drawdownType": "static"}]},
    }
    rules = _challenge_rules(attempt, equity=4_900.0, high_water_mark=5_100.0)
    assert rules["source"] == "challenge"
    assert rules["daily_loss_limit_usd"] == pytest.approx(150.0)
    assert rules["drawdown_ref"] == pytest.approx(5_000.0)  # static: starting balance
    assert rules["drawdown_floor"] == pytest.approx(4_700.0)


def test_challenge_rules_fall_back_conservatively():
    from forven.propr_mirror import _challenge_rules

    rules = _challenge_rules({}, equity=5_000.0, high_water_mark=None)
    assert rules["source"] == "defaults"
    assert rules["daily_loss_limit_usd"] == pytest.approx(150.0)  # 3% of equity
    assert rules["drawdown_floor"] == pytest.approx(4_700.0)


def test_halt_trips_on_trailing_drawdown(forven_db):
    from datetime import datetime, timezone

    from forven.propr_mirror import _evaluate_halt

    attempt = {
        "currentPhaseId": "ap-1",
        "phases": [{"attemptPhaseId": "ap-1", "phaseId": "p-1", "startingBalance": "5000"}],
        "challenge": {"phases": [{"phaseId": "p-1", "maxDailyLossPercent": "3",
                                  "maxDrawdownPercent": "6", "drawdownType": "trailing"}]},
        "account": {"highWaterMark": "6000"},
    }
    # HWM 6000, 6% trailing => $360 allowance; $400 used >= 80% of it.
    halt = _evaluate_halt(attempt, equity=5_600.0, now=datetime.now(timezone.utc))
    assert halt["halted"] is True
    assert any("drawdown" in r for r in halt["reasons"])


def test_halted_tick_blocks_opens_but_still_closes(mirror_env):
    from datetime import datetime, timezone

    from forven.db import get_db, kv_set

    pm, calls = mirror_env
    # A previously mirrored trade whose source has since closed...
    _insert_trade("T-h1", "S-M1")
    pm.mirror_tick()
    assert pm.get_state()["T-h1"]["status"] == "open"
    with get_db() as conn:
        conn.execute("UPDATE trades SET status = 'CLOSED' WHERE id = ?", ("T-h1",))
    # ...plus a fresh open, arriving while daily loss sits past the halt line:
    # day-start $5,000 vs equity $4,870 = $130 loss >= 80% of the $150 cap.
    _insert_trade("T-h2", "S-M1", asset="BTC")
    kv_set(pm.HALT_STATE_KEY, {
        "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "day_start_equity": 5_000.0,
    })
    calls["equity"]["value"] = 4_870.0

    summary = pm.mirror_tick()
    assert summary["closed"] == 1 and calls["closes"]      # risk reduction ran
    assert summary["opened"] == 0                           # new risk blocked
    assert len(calls["orders"]) == 1                        # only T-h1's original open
    assert "halted" in summary and any("daily loss" in r for r in summary["halted"])
    halt = pm.get_halt_state()
    assert halt["halted"] is True
    assert halt["daily_loss"] == pytest.approx(130.0)
    # Rules-panel fields (visual on the page): target and progress from the
    # venue's own phase terms.
    assert halt["starting_balance"] == pytest.approx(5_000.0)
    assert halt["profit_target_usd"] == pytest.approx(500.0)
    assert halt["profit_progress_usd"] == pytest.approx(-130.0)
    # T-h2 was NOT stamped in state — it mirrors normally if the halt clears
    # within its freshness window.
    assert "T-h2" not in pm.get_state()


def test_open_risk_at_stops_consumes_the_daily_budget(mirror_env):
    """Three concurrent 1%-risk opens would stack $150 of stop risk — exactly
    the venue's daily cap. The budget check defers the third at the $120 halt
    line instead of letting simultaneous stop-outs fail the challenge."""
    pm, calls = mirror_env
    for i in range(3):
        _insert_trade(f"T-b{i}", "S-M1", asset=["BTC", "ETH", "SOL"][i])
    # All three price/size identically for the test.
    from forven.exchange import propr
    mids = {"BTC": 50_000.0, "ETH": 50_000.0, "SOL": 50_000.0}
    import unittest.mock as mock
    with mock.patch.object(propr, "get_all_mids", lambda testnet=True: mids):
        summary = pm.mirror_tick()
    # $50 risk each against the $120 budget: two open, the third defers.
    assert summary["opened"] == 2
    assert summary.get("deferred") == 1
    assert len(calls["orders"]) == 2
    deferred = [e for e in pm.get_state().values() if e.get("status") == "pending"]
    assert len(deferred) == 1 and "deferred" in deferred[0]["reason"]


def test_day_rollover_resets_the_daily_anchor(mirror_env):
    from datetime import datetime, timezone

    from forven.db import kv_set

    pm, calls = mirror_env
    kv_set(pm.HALT_STATE_KEY, {"day": "2020-01-01", "day_start_equity": 5_000.0})
    calls["equity"]["value"] = 4_870.0
    _insert_trade("T-d1", "S-M1")

    summary = pm.mirror_tick()
    # New UTC day: the anchor re-bases to current equity, so yesterday's loss
    # doesn't halt today ($130 drawdown is also inside the $240 halt line).
    assert "halted" not in summary
    assert summary["opened"] == 1
    halt = pm.get_halt_state()
    assert halt["day"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert halt["day_start_equity"] == pytest.approx(4_870.0)
    assert halt["daily_loss"] == pytest.approx(0.0)


def test_mirror_roster_preserves_join_timestamps(forven_db, monkeypatch):
    import forven.propr_mirror as pm

    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    pm.set_mirror_config(strategy_ids=["S-A"])
    first = pm.mirror_roster()["S-A"]
    pm.set_mirror_config(strategy_ids=["S-A", "S-B"])
    roster = pm.mirror_roster()
    assert roster["S-A"] == first  # unchanged for the existing entry
    assert set(roster) == {"S-A", "S-B"}
    pm.set_mirror_config(strategy_ids=["S-B"])
    assert set(pm.mirror_roster()) == {"S-B"}
