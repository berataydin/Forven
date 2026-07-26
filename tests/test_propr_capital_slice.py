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
