"""SLICE-1: each live strategy sizes against an equal share of the account.

Live sizing computed every order against the FULL balance, and no strategy knew
five others were sizing against the same dollars. S06153 (3% risk, ~2.95% stop)
therefore asked for `999 * 0.03 / 0.0295` = $1,016 on a $999 account, was refused
48 times by the hard notional cap, and did not trade for 13 days. S05665 the same
against its $100 go-live ceiling, 24 times. The caps were correct; the sizing was
asking a question no cap can answer, because "how much may I use?" is a question
about the OTHER strategies.

Dividing first makes it a division instead of a search. Two properties these tests
pin, because they are the reason this is simpler than bounding it with caps:

  * worst-case portfolio risk is `risk_per_trade` of the ACCOUNT regardless of how
    many strategies run, and
  * a position can never exceed `slice * leverage` — which needs no new code,
    because `size_fraction` was already clamped to [0, 1].
"""

from __future__ import annotations

import pytest

from forven.exchange import risk as risk_mod
from forven.exchange.risk import apply_go_live_ceiling, live_equity_slice
from forven.strategies.sizing import position_units, size_fraction

ATR = {"sizing_mode": "atr", "risk_per_trade": 0.03}


def _notional(equity: float, *, stop: float = 0.0295, lev: float = 2.0, price: float = 75.0,
              ec: dict | None = None) -> float:
    ec = ec or ATR
    frac = size_fraction(ec, stop, leverage=lev, initial_capital=equity, current_equity=equity)
    return position_units(equity=equity, size_fraction=frac, leverage=lev, entry_price=price) * price


# --------------------------------------------------------------------------- #
# The slice itself
# --------------------------------------------------------------------------- #

def test_slice_is_an_equal_share(forven_db, monkeypatch):
    monkeypatch.setattr(risk_mod, "live_cohort_ids", lambda: ["A", "B", "C", "D", "E", "F"])
    got, meta = live_equity_slice(999.0)
    assert got == pytest.approx(166.5)
    assert meta["cohort_size"] == 6


def test_slice_is_scale_invariant(forven_db, monkeypatch):
    """The operator's framing: 10x the account should mean 10x the slice, nothing else."""
    monkeypatch.setattr(risk_mod, "live_cohort_ids", lambda: ["A", "B", "C", "D", "E"])
    small, _ = live_equity_slice(500.0)
    large, _ = live_equity_slice(5_000.0)
    assert small == pytest.approx(100.0)
    assert large == pytest.approx(1_000.0)
    assert large / small == pytest.approx(10.0)


def test_unreadable_cohort_fails_closed(forven_db, monkeypatch):
    """Never guess N. Guessing LOW hands one strategy the entire account."""
    monkeypatch.setattr(risk_mod, "live_cohort_ids", lambda: None)
    got, meta = live_equity_slice(999.0)
    assert got is None
    assert "cohort" in meta["reason"]


def test_missing_equity_fails_closed(forven_db):
    for bad in (None, 0, -5):
        got, _ = live_equity_slice(bad)
        assert got is None, f"{bad!r} must not yield a slice"


def test_empty_cohort_floors_at_one(forven_db, monkeypatch):
    """A strategy placing a live order IS in the cohort; a zero count means the read
    disagrees with reality, and dividing by zero is not how to discover that."""
    monkeypatch.setattr(risk_mod, "live_cohort_ids", lambda: [])
    got, meta = live_equity_slice(999.0)
    assert got == pytest.approx(999.0)
    assert meta["cohort_size"] == 1


# --------------------------------------------------------------------------- #
# SLICE-BASE-1: the sizing base when direction books are on
# --------------------------------------------------------------------------- #

def _patch_books(monkeypatch, addrs: dict, eqs: dict):
    import forven.scanner as scanner
    from forven.exchange import books

    monkeypatch.setattr(books, "book_address", lambda b, settings=None: addrs.get(b))
    monkeypatch.setattr(scanner, "_book_account_equity", lambda a: eqs.get(a))
    return scanner


def test_book_sizing_base_combines_both_books(forven_db, monkeypatch):
    """Strategies deploy ONE direction at a time, so the base is the combined
    long+short pool — per-book division halved every slice."""
    scanner = _patch_books(
        monkeypatch,
        {"long": "0xlong", "short": "0xshort"},
        {"0xlong": 508.7, "0xshort": 490.5},
    )
    assert scanner._book_sizing_equity("short") == pytest.approx(999.2)
    assert scanner._book_sizing_equity("long") == pytest.approx(999.2)


def test_book_sizing_base_degrades_to_routed_book_when_counterpart_unreadable(forven_db, monkeypatch):
    """The counterpart is additive only — losing its read under-sizes, never
    over-sizes, and never blocks the open."""
    scanner = _patch_books(
        monkeypatch,
        {"long": "0xlong", "short": "0xshort"},
        {"0xshort": 490.5},  # long book read fails
    )
    assert scanner._book_sizing_equity("short") == pytest.approx(490.5)


def test_book_sizing_base_fails_closed_when_routed_book_unreadable(forven_db, monkeypatch):
    """The routed book fronts the margin — its read stays mandatory."""
    scanner = _patch_books(
        monkeypatch,
        {"long": "0xlong", "short": "0xshort"},
        {"0xlong": 508.7},  # short book read fails
    )
    assert scanner._book_sizing_equity("short") is None


def test_book_sizing_base_does_not_double_count_a_shared_address(forven_db, monkeypatch):
    """A misconfig pointing both books at one wallet must not double the base."""
    scanner = _patch_books(
        monkeypatch,
        {"long": "0xSAME", "short": "0xsame"},
        {"0xSAME": 490.5, "0xsame": 490.5},
    )
    assert scanner._book_sizing_equity("long") == pytest.approx(490.5)


# --------------------------------------------------------------------------- #
# What the slice does to sizing — the actual defect
# --------------------------------------------------------------------------- #

def test_s06153_order_no_longer_exceeds_the_account(forven_db, monkeypatch):
    """The reported case, with its real numbers."""
    monkeypatch.setattr(risk_mod, "live_cohort_ids", lambda: ["A", "B", "C", "D", "E", "F"])
    equity = 999.17

    before = _notional(equity)
    assert before > equity, "precondition: full-equity sizing asked for more than the account"

    sl, _ = live_equity_slice(equity)
    after = _notional(sl)
    assert after < equity
    assert after == pytest.approx(169.35, abs=0.5)


def test_total_risk_is_independent_of_cohort_size(forven_db, monkeypatch):
    """N strategies each risking r of equity/N sum to r of equity — for ANY N.

    This is the property that makes the model self-limiting: promoting another
    strategy divides the risk rather than adding to it.
    """
    equity, stop = 1_000.0, 0.02
    ec = {"sizing_mode": "atr", "risk_per_trade": 0.01}
    for n in (1, 3, 6, 20):
        monkeypatch.setattr(risk_mod, "live_cohort_ids", lambda n=n: [f"S{i}" for i in range(n)])
        sl, _ = live_equity_slice(equity)
        total_risk = _notional(sl, stop=stop, ec=ec) * stop * n
        assert total_risk == pytest.approx(equity * 0.01, rel=1e-6), f"broke at N={n}"


def test_position_never_exceeds_slice_times_leverage(forven_db, monkeypatch):
    """'$100 slice at 2x is at most a $200 position' — already true via clamp01."""
    monkeypatch.setattr(risk_mod, "live_cohort_ids", lambda: ["A", "B", "C", "D", "E"])
    sl, _ = live_equity_slice(500.0)
    assert sl == pytest.approx(100.0)
    for lev in (1.0, 2.0, 3.0, 10.0):
        # A stop far tighter than the risk budget is what drives size_fraction to its cap.
        n = _notional(sl, stop=0.0005, lev=lev)
        assert n <= sl * lev + 1e-6, f"lev={lev}: ${n:.2f} exceeds slice*lev ${sl * lev:.2f}"


def test_leverage_does_not_change_risk(forven_db):
    """The operator's point: margin is not risk. Loss at the stop is leverage-invariant."""
    losses = {lev: _notional(166.5, lev=lev) * 0.0295 for lev in (1.3, 2.0, 3.0, 10.0)}
    # 1x is the exception: size_fraction saturates at 1.0, so it cannot reach the budget.
    assert len(set(round(v, 6) for v in losses.values())) == 1, losses


# --------------------------------------------------------------------------- #
# The go-live ceiling — an optional TIGHTER cap that clamps, never refuses
# --------------------------------------------------------------------------- #

def test_ceiling_clamps_rather_than_refusing(forven_db, monkeypatch):
    """S05665 sized $475 against a $100 ceiling and was REFUSED 24 times over three
    days instead of trading at $100. A ceiling that refuses is a strategy-disabler."""
    monkeypatch.setattr(
        risk_mod, "get_live_notional_ceilings", lambda: {"S1": {"ceiling_usd": 100.0}}
    )
    units, clamp = apply_go_live_ceiling("S1", 6.3333, 75.0)  # $475 requested
    assert clamp is not None
    assert units * 75.0 == pytest.approx(100.0, abs=0.01)
    assert units * 75.0 <= 100.0, "clamped notional must never round back above the cap"


def test_ceiling_only_ever_reduces(forven_db, monkeypatch):
    monkeypatch.setattr(
        risk_mod, "get_live_notional_ceilings", lambda: {"S1": {"ceiling_usd": 100.0}}
    )
    units, clamp = apply_go_live_ceiling("S1", 1.0, 75.0)  # $75, inside the cap
    assert clamp is None and units == 1.0, "an order inside the ceiling must be untouched"


def test_no_ceiling_is_a_no_op(forven_db, monkeypatch):
    monkeypatch.setattr(risk_mod, "get_live_notional_ceilings", lambda: {})
    units, clamp = apply_go_live_ceiling("S1", 6.3333, 75.0)
    assert clamp is None and units == 6.3333


def test_unreadable_ceiling_leaves_size_unchanged(forven_db, monkeypatch):
    """Fail SOFT here, deliberately: the slice and the downstream caps still bound
    the order, so a KV hiccup must not resize it."""
    def _boom():
        raise RuntimeError("kv down")

    monkeypatch.setattr(risk_mod, "get_live_notional_ceilings", _boom)
    units, clamp = apply_go_live_ceiling("S1", 6.3333, 75.0)
    assert clamp is None and units == 6.3333


def test_slice_then_ceiling_is_the_smaller_of_the_two(forven_db, monkeypatch):
    """End to end on the reported case: $166 slice, $169 risk-sized, $100 ceiling wins."""
    monkeypatch.setattr(risk_mod, "live_cohort_ids", lambda: ["A", "B", "C", "D", "E", "F"])
    monkeypatch.setattr(
        risk_mod, "get_live_notional_ceilings", lambda: {"S06153": {"ceiling_usd": 100.0}}
    )
    sl, _ = live_equity_slice(999.17)
    frac = size_fraction(ATR, 0.0295, leverage=2.0, initial_capital=sl, current_equity=sl)
    units = round(position_units(equity=sl, size_fraction=frac, leverage=2.0, entry_price=75.0), 6)
    assert units * 75.0 == pytest.approx(169.35, abs=0.5)

    final, clamp = apply_go_live_ceiling("S06153", units, 75.0)
    assert clamp is not None
    assert final * 75.0 == pytest.approx(100.0, abs=0.01)
