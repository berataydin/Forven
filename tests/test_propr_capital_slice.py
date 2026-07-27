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
    size, _ = pm._size_mirror_order("BTC", MID, STOP, 0.01, 2.0, sl)
    worst_day = size * MID * 0.03 * 6

    assert worst_day < daily_limit, f"${worst_day:.2f} breaches the ${daily_limit} daily limit"
    assert worst_day < drawdown_allowance
    # And the pre-fix behaviour did breach, so this test is not vacuous.
    old, _ = pm._size_mirror_order("BTC", MID, STOP, 0.01, 2.0, equity)
    assert old * MID * 0.03 * 6 > daily_limit


def test_worst_case_risk_is_capped_regardless_of_roster_size(forven_db, monkeypatch):
    """N members each risking r of equity/N sum to r of equity — for ANY N."""
    equity = 5000.0
    for n in (1, 3, 6, 25):
        monkeypatch.setattr(pm, "mirror_roster", lambda n=n: {f"S{i}": "t" for i in range(n)})
        sl, _ = pm.mirror_equity_slice(equity)
        size, _ = pm._size_mirror_order("BTC", MID, STOP, 0.01, 2.0, sl)
        total = size * MID * 0.03 * n
        assert total == pytest.approx(equity * 0.01, rel=1e-6), f"broke at roster={n}"


def test_slice_only_ever_reduces_position_size(forven_db, monkeypatch):
    monkeypatch.setattr(pm, "mirror_roster", lambda: {f"S{i}": "t" for i in range(6)})
    sl, _ = pm.mirror_equity_slice(5000.0)
    sliced, _ = pm._size_mirror_order("BTC", MID, STOP, 0.01, 2.0, sl)
    full, _ = pm._size_mirror_order("BTC", MID, STOP, 0.01, 2.0, 5000.0)
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
# PROPR-ORDER-SHAPE — the request body, pinned against the LIVE API's behaviour
# ---------------------------------------------------------------------------

def test_orders_are_submitted_one_bare_object_at_a_time(forven_db, monkeypatch):
    """POST /accounts/{id}/orders takes ONE order, not {"orders": [...]}.

    Measured against the live API on 2026-07-27:

        {"orders": [...]}  -> 400 "Bad Request Exception"    (schema rejected)
        bare order object  -> 400 order_create_failed 13051  (schema ACCEPTED)

    The wrapper never reaches order creation, which is why every mirror attempt
    since PROPR-1 was rejected. This test exists because the rest of this suite
    stubs `_request` and therefore asserted our own assumption about the shape
    rather than the venue's — the TEST-5 gap the 2026-07-25 audit named, which
    then cost exactly what it predicted.
    """
    import forven.exchange.propr as pr

    sent: list[dict] = []

    def _capture(method, path, **kw):
        sent.append({"method": method, "path": path, "body": kw.get("body")})
        return {"orderId": f"o{len(sent)}", "status": "open"}

    monkeypatch.setattr(pr, "_request", _capture)
    orders = [
        {"intentId": "A", "asset": "BTC", "type": "market", "side": "buy"},
        {"intentId": "B", "asset": "BTC", "type": "stop_market", "side": "sell"},
    ]
    pr._create_orders("urn:prp-account:X", orders)

    assert len(sent) == 2, "each order must be POSTed separately, not batched"
    for call in sent:
        body = call["body"]
        assert "orders" not in body, (
            "the batch wrapper is back — the venue rejects it at schema validation"
        )
        assert body.get("intentId"), "the bare order object must be the body itself"


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
