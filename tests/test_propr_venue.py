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
    from forven.config import get_live_venue, propr_enabled

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    monkeypatch.delenv("FORVEN_LIVE_VENUE", raising=False)
    assert propr_enabled() is False
    assert get_live_venue() == "hyperliquid"


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


def test_live_venue_propr_requires_hidden_flag(monkeypatch):
    from forven.config import get_live_venue

    monkeypatch.setenv("FORVEN_LIVE_VENUE", "propr")
    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    # A stale/hand-set venue must never route to a disabled integration.
    assert get_live_venue() == "hyperliquid"
    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    assert get_live_venue() == "propr"


def test_set_live_venue_refuses_disabled_propr(monkeypatch):
    from forven.config import set_live_venue

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    with pytest.raises(ValueError):
        set_live_venue("propr")
    with pytest.raises(ValueError):
        set_live_venue("binance")


def test_set_live_venue_persists_when_enabled(monkeypatch):
    from forven.config import get_live_venue, load_config, set_live_venue

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.delenv("FORVEN_LIVE_VENUE", raising=False)
    set_live_venue("propr")
    assert load_config().get("live_venue") == "propr"
    assert get_live_venue() == "propr"
    set_live_venue("hyperliquid")
    assert get_live_venue() == "hyperliquid"


# ---------------------------------------------------------------------------
# Order-placement guards
# ---------------------------------------------------------------------------

def test_market_order_refuses_without_enable_flag(monkeypatch):
    from forven.exchange import propr

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="not enabled"):
        propr.market_order("BTC", "buy", 0.001)


def test_market_order_refuses_without_live_opt_in(monkeypatch):
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="FORVEN_ALLOW_PROPR_LIVE"):
        propr.market_order("BTC", "buy", 0.001)


def test_close_and_leverage_share_the_guard(monkeypatch):
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="FORVEN_ALLOW_PROPR_LIVE"):
        propr.close_position("BTC", 0.001, "sell")
    with pytest.raises(RuntimeError, match="FORVEN_ALLOW_PROPR_LIVE"):
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


# ---------------------------------------------------------------------------
# Scanner venue routing
# ---------------------------------------------------------------------------

def test_resolve_trade_venue_reads_open_stamp(forven_db):
    from forven.db import get_db
    from forven.scanner import _resolve_trade_venue

    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, asset, direction, signal_data) VALUES (?, ?, ?, ?, ?)",
            ("T-propr", "S1", "BTC", "long", json.dumps({"venue": "propr"})),
        )
        conn.execute(
            "INSERT INTO trades (id, strategy, asset, direction, signal_data) VALUES (?, ?, ?, ?, ?)",
            ("T-old", "S1", "ETH", "long", json.dumps({})),
        )
    assert _resolve_trade_venue("T-propr") == "propr"
    assert _resolve_trade_venue("T-old") is None
    assert _resolve_trade_venue("T-missing") is None
