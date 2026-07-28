"""SLICE-1 (Propr): each roster member sizes against its share of the challenge.

The mirror had the same defect the Hyperliquid live path did — every member sized
against the FULL challenge balance. On the live $5,000 phase with a 6-strategy
roster that is challenge-ending rather than merely untidy:

    per-trade risk = 5000 * 1%  = $50
    six stop-outs               = $300  = 2x the $150 daily-loss limit AND the
                                          ENTIRE $300 drawdown allowance

One bad session, entirely within the strategies' own rules, ends the attempt.
"""

from __future__ import annotations

import pytest

import forven.propr_mirror as pm

MID, STOP = 100.0, 97.0  # 3% stop distance


def test_slice_divides_by_roster_size(forven_db, monkeypatch):
    monkeypatch.setattr(pm, "mirror_roster", lambda: {f"S{i}": "t" for i in range(6)})
    got, meta = pm.mirror_equity_slice(5000.0)
    assert got == pytest.approx(833.333, abs=0.01)
    assert meta["roster_size"] == 6


def test_six_stopouts_no_longer_breach_the_challenge_rules(forven_db, monkeypatch):
    """The number that matters: worst-case day against the venue's kill rules."""
    monkeypatch.setattr(pm, "mirror_roster", lambda: {f"S{i}": "t" for i in range(6)})
    equity, daily_limit, drawdown_allowance = 5000.0, 150.0, 300.0

    sl, _ = pm.mirror_equity_slice(equity)
    size, _ = pm._size_mirror_order("BTC", MID, STOP, 2.0, sl)
    worst_day = size * MID * 0.03 * 6

    assert worst_day < daily_limit, f"${worst_day:.2f} breaches the ${daily_limit} daily limit"
    assert worst_day < drawdown_allowance
    # And the pre-fix behaviour did breach, so this test is not vacuous.
    old, _ = pm._size_mirror_order("BTC", MID, STOP, 2.0, equity)
    assert old * MID * 0.03 * 6 > daily_limit


def test_worst_case_risk_is_capped_regardless_of_roster_size(forven_db, monkeypatch):
    """N members each risking r of equity/N sum to r of equity — for ANY N."""
    equity = 5000.0
    for n in (1, 3, 6, 25):
        monkeypatch.setattr(pm, "mirror_roster", lambda n=n: {f"S{i}": "t" for i in range(n)})
        sl, _ = pm.mirror_equity_slice(equity)
        size, _ = pm._size_mirror_order("BTC", MID, STOP, 2.0, sl)
        total = size * MID * 0.03 * n
        assert total == pytest.approx(equity * pm.MIRROR_RISK_PCT, rel=1e-6), f"broke at roster={n}"


def test_slice_only_ever_reduces_position_size(forven_db, monkeypatch):
    monkeypatch.setattr(pm, "mirror_roster", lambda: {f"S{i}": "t" for i in range(6)})
    sl, _ = pm.mirror_equity_slice(5000.0)
    sliced, _ = pm._size_mirror_order("BTC", MID, STOP, 2.0, sl)
    full, _ = pm._size_mirror_order("BTC", MID, STOP, 2.0, 5000.0)
    assert sliced < full


def test_unreadable_roster_preserves_previous_behaviour(forven_db, monkeypatch):
    """A transient KV error must not silently resize live positions.

    Deliberately NOT fail-closed to zero here: the downstream PROPR-3 daily-budget
    check still bounds the open, and halving every position on a blip would be its
    own surprise.
    """
    def _boom():
        raise RuntimeError("kv down")

    monkeypatch.setattr(pm, "mirror_roster", _boom)
    got, meta = pm.mirror_equity_slice(5000.0)
    assert got == 5000.0
    assert "roster unreadable" in meta["reason"]


def test_empty_roster_floors_at_one(forven_db, monkeypatch):
    monkeypatch.setattr(pm, "mirror_roster", lambda: {})
    got, meta = pm.mirror_equity_slice(5000.0)
    assert got == 5000.0 and meta["roster_size"] == 1


# ---------------------------------------------------------------------------
# MIRROR-RISK-1: the operator-tunable mirror risk (Settings page knob)
# ---------------------------------------------------------------------------

def test_mirror_risk_defaults_to_the_constant(forven_db):
    assert pm.mirror_risk_fraction({}) == pytest.approx(pm.MIRROR_RISK_PCT)


def test_mirror_risk_reads_the_whole_percent_setting(forven_db):
    assert pm.mirror_risk_fraction({pm.MIRROR_RISK_SETTING_KEY: 4}) == pytest.approx(0.04)
    assert pm.mirror_risk_fraction({pm.MIRROR_RISK_SETTING_KEY: "1.5"}) == pytest.approx(0.015)


def test_mirror_risk_rejects_garbage_and_caps_at_ten_percent(forven_db):
    for bad in (None, "", "nan", float("nan"), 0, -3):
        assert pm.mirror_risk_fraction({pm.MIRROR_RISK_SETTING_KEY: bad}) == pytest.approx(
            pm.MIRROR_RISK_PCT
        ), f"{bad!r} must fall back to the default"
    assert pm.mirror_risk_fraction({pm.MIRROR_RISK_SETTING_KEY: 50}) == pytest.approx(0.10)


def test_sizing_honors_the_tuned_mirror_risk(forven_db, monkeypatch):
    """The knob must reach the order, not just the reader."""
    monkeypatch.setattr(pm, "mirror_risk_fraction", lambda settings=None: 0.04)
    size, _ = pm._size_mirror_order("BTC", MID, STOP, 2.0, 1000.0)
    # 4% of $1,000 over a $3 stop distance.
    assert size == pytest.approx(1000.0 * 0.04 / 3.0)


# ---------------------------------------------------------------------------
# PROPR-ORDER-SHAPE — the request body, pinned against the LIVE API's behaviour
# ---------------------------------------------------------------------------

def test_orders_are_submitted_as_one_batch_with_a_top_level_group_id(forven_db, monkeypatch):
    """POST /accounts/{id}/orders takes {"orders": [...]}, group id TOP-LEVEL.

    Verified against the live API on 2026-07-27 by actually placing orders on the
    paper challenge account: a full bracket (entry + stop_market + take_profit_market)
    was ACCEPTED in one batch — entry filled at 65342, both conditionals resting.

    An earlier revision of this test asserted the exact opposite ("each order must
    be POSTed separately"), citing a live measurement. That measurement was real
    but the conclusion drawn from it was wrong: the batch wrapper was failing
    schema validation because 5 of the 11 required per-order fields were missing,
    and a bare object happened to get one error further. Fixing the shape instead
    of the fields kept it broken. The lesson worth keeping is that a probe tells
    you which body was rejected, not why — the spec did, in one read.
    """
    import forven.exchange.propr as pr

    sent: list[dict] = []

    def _capture(method, path, **kw):
        sent.append({"method": method, "path": path, "body": kw.get("body")})
        return {"data": [{"orderId": f"o{i}", "intentId": o["intentId"], "status": "open"}
                         for i, o in enumerate(kw.get("body", {}).get("orders", []))]}

    monkeypatch.setattr(pr, "_request", _capture)
    orders = [
        {"intentId": "A", "asset": "BTC", "type": "limit", "side": "buy"},
        {"intentId": "B", "asset": "BTC", "type": "stop_market", "side": "sell"},
    ]
    pr._create_orders("urn:prp-account:X", orders, "GROUPULID")

    assert len(sent) == 1, "a bracket must go up as ONE batch, not order-by-order"
    body = sent[0]["body"]
    assert [o["intentId"] for o in body["orders"]] == ["A", "B"]
    # 13059 ORDER_VALIDATION_GROUP_ID_REQUIRED when orders.length > 1, and it is
    # an envelope field — as a per-order key it is silently ignored and the batch
    # is rejected for the missing group.
    assert body["orderGroupId"] == "GROUPULID"
    assert all("orderGroupId" not in o for o in body["orders"])


def test_conditional_legs_use_the_market_trigger_type_names(forven_db, monkeypatch):
    """`stop_market` / `take_profit_market` — NOT `stop` / `take_profit`.

    The short names are rejected with a bare "Bad Request Exception" naming no
    field, which is indistinguishable from the missing-required-field rejection
    that preceded it. Measured live 2026-07-27: with `stop` the whole bracket was
    refused; with `stop_market` the identical body was accepted and the trigger
    rested at 63381.
    """
    import forven.exchange.propr as pr

    captured: list[dict] = []
    monkeypatch.setattr(pr, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(pr, "_quantize_size", lambda asset, size: float(size))
    monkeypatch.setattr(pr, "_round_price", lambda price, asset: float(price))
    monkeypatch.setattr(pr, "resolve_account", lambda force_refresh=False: ("acct-1", "att-1"))
    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.setenv("FORVEN_ALLOW_PROPR_LIVE", "1")
    import forven.exchange.liquidity as liquidity
    monkeypatch.setattr(liquidity, "check_order_liquidity", lambda *a, **k: (True, None))

    def _fake(account_id, orders, group_id=None):
        captured.extend(orders)
        return [{"orderId": f"o{i}", "intentId": o["intentId"],
                 "status": "filled" if o["type"] == "limit" else "open",
                 "averageFillPrice": "50010" if o["type"] == "limit" else None,
                 "cumulativeQuantity": o["quantity"]} for i, o in enumerate(orders)]

    monkeypatch.setattr(pr, "_create_orders", _fake)
    pr.market_order("BTC", "buy", 0.01, stop_loss_price=48_000, take_profit_price=55_000)

    by_type = {o["type"]: o for o in captured}
    assert set(by_type) == {"limit", "stop_market", "take_profit_market"}
    for name in ("stop_market", "take_profit_market"):
        leg = by_type[name]
        assert leg["reduceOnly"] is True
        assert leg["triggerPrice"], f"{name} needs a triggerPrice"
        assert leg["timeInForce"] == "GTC", "conditionals rest; IOC would kill them instantly"
        # positionSide aligns with the ORDER side (buy->long, sell->short), even
        # though the leg protects a long. The published docs show `long` here;
        # the venue answers 13096. See the module docstring.
        assert (leg["side"], leg["positionSide"]) == ("sell", "short")


def test_entry_failure_raises_but_a_leg_failure_keeps_the_entry(forven_db, monkeypatch):
    """Submitting singly means a bracket is no longer atomic.

    A failed ENTRY must surface (nothing was opened). A failed protective LEG
    must NOT discard the filled entry — the caller reports it through
    `protective_leg_failed` and the mirror re-arms or closes. Losing a real
    filled position because its stop was refused would be the worse trade.
    """
    import forven.exchange.propr as pr

    calls = {"n": 0}

    def _entry_ok_leg_fails(method, path, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"orderId": "entry-1", "status": "open"}
        raise pr.ProprApiError(400, "order_create_failed", {"code": 13051})

    monkeypatch.setattr(pr, "_request", _entry_ok_leg_fails)
    created = pr._create_orders("acct", [
        {"intentId": "A", "type": "market"},
        {"intentId": "B", "type": "stop_market"},
    ])
    assert len(created) == 1, "the filled entry must be returned, not discarded"

    def _entry_fails(method, path, **kw):
        raise pr.ProprApiError(400, "order_create_failed", {"code": 13051})

    monkeypatch.setattr(pr, "_request", _entry_fails)
    with pytest.raises(pr.ProprApiError):
        pr._create_orders("acct", [{"intentId": "A", "type": "market"}])
