"""Close-out cover for the last two deprecated seams of the parallel partition.

Two independent invariants, both of which have already cost real money or real
debugging time once:

1. ARCH-03 — ``forven.gauntlet.status`` must not import ``forven.routers``.
   The gauntlet runs in background workers and in the MCP process; reaching up
   through the web layer to fetch engine code drags FastAPI, the operator-auth
   dependency and every sibling endpoint module along with it. The scorer lives
   in ``forven.robustness.engine``; the router only re-exports it for pre-split
   importers. An AST walk (not a text grep) is used so a function-level import
   -- the exact shape the shim hop had -- cannot sneak the dependency back in.

2. HL-CLOSE-1 — a close that did not fill is not a close. The Bot Factory's
   live lane closes REAL positions, so it gets the same proof the scanner and
   the manual-close paths have: a rejected reduce-only IOC must leave the trade
   OPEN and report failure, never book an exit at the aggressive limit price
   that never traded.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# ARCH-03: the gauntlet does not reach up through the web layer
# ---------------------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    """Every module name imported anywhere in the file, including inside
    functions, ``try`` blocks and conditionals (an AST walk, not a top-level
    scan -- the shim this test retires was a function-level import)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import; forven.gauntlet.* has none, but a
            # relative import can never name forven.routers anyway.
            if node.level == 0 and node.module:
                names.add(node.module)
    return names


def _module_path(dotted: str) -> Path:
    import importlib

    module = importlib.import_module(dotted)
    return Path(module.__file__)


def test_gauntlet_status_does_not_import_the_router_layer():
    imported = _imported_modules(_module_path("forven.gauntlet.status"))
    offenders = sorted(
        name for name in imported
        if name == "forven.routers" or name.startswith("forven.routers.")
    )
    assert not offenders, (
        "forven/gauntlet/status.py imports the web layer: "
        f"{offenders}. The gauntlet runs in background workers -- import engine "
        "code from where it lives (e.g. forven.robustness.engine), never through "
        "a forven.routers re-export."
    )


def test_gauntlet_status_imports_the_composite_scorer_from_the_engine():
    """The positive half: the scorer is still wired, just not via the router."""
    imported = _imported_modules(_module_path("forven.gauntlet.status"))
    assert "forven.robustness.engine" in imported, (
        "forven/gauntlet/status.py must import compute_composite_robustness_score "
        "from forven.robustness.engine (COMPOSITE-LIVE-1: paper/live strategies "
        "have frozen stored metrics, so the status panel has to score the CURRENT "
        "artifacts or ~40 prod strategies render '0.0 / 100' beside 5/5 PASS chips)."
    )


def test_no_gauntlet_module_imports_the_router_layer():
    """Widen the guard to the whole package -- status.py was the last one."""
    package_dir = _module_path("forven.gauntlet.status").parent
    offenders: dict[str, list[str]] = {}
    for path in sorted(package_dir.glob("*.py")):
        bad = sorted(
            name for name in _imported_modules(path)
            if name == "forven.routers" or name.startswith("forven.routers.")
        )
        if bad:
            offenders[path.name] = bad
    assert not offenders, (
        f"forven/gauntlet reaches up into the web layer: {offenders}"
    )


# ---------------------------------------------------------------------------
# HL-CLOSE-1: the Bot Factory live lane must not book an unfilled close
# ---------------------------------------------------------------------------


_HL_REJECTION = {
    "mid": 50000.0,
    "close_price": 48500.0,  # the 3%-through-mid IOC limit that never traded
    "exit_price": None,
    "filled_size": None,
    "error": "close order returned no fill (rejected by exchange)",
}


def _insert_live_bot_trade(trade_id: str, *, bot_id: str) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, signal_entry_price,
             fill_entry_price, size, risk_pct, leverage, status, execution_type, source,
             signal_data, opened_at)
            VALUES (?, ?, ?, 'BTC', 'long', 50000, 50000, 50000, 0.1, 0.01, 1,
                    'OPEN', 'live', 'bot', '{}', datetime('now'))
            """,
            (trade_id, f"bot:{bot_id}", f"bot:{bot_id}"),
        )


@pytest.fixture()
def _quiet_execution_failures(monkeypatch):
    """Record _report_execution_failure calls instead of paging the operator
    and handing off to the brain developer loop."""
    import forven.scanner as scanner

    reported: list[tuple] = []
    monkeypatch.setattr(
        scanner, "_report_execution_failure",
        lambda *a, **k: reported.append((a, k)),
    )
    return reported


def test_bot_live_close_rejection_leaves_the_trade_open(
    forven_db, monkeypatch, _quiet_execution_failures
):
    """A rejected reduce-only IOC must not be booked as a close for a bot.

    close_live routes through scanner._execute_direct, which raises on the
    HL-CLOSE-1 ``error`` key before any fill is recorded. This asserts the whole
    chain end to end (real _execute_direct, real close_live) rather than the
    contract of either half in isolation -- the failure this guards against is
    precisely the two halves drifting apart.
    """
    import forven.exchange.hyperliquid as hl
    from forven.bot_factory.live_exec import close_live
    from forven.db import get_db

    _insert_live_bot_trade("t-bot-reject", bot_id="B1")

    calls: list[tuple] = []

    def _rejecting_close(asset, size, side, **kwargs):
        calls.append((asset, size, side))
        return dict(_HL_REJECTION)

    monkeypatch.setattr(hl, "close_position", _rejecting_close)
    # The pre-close funding read is best-effort; keep it off the network.
    monkeypatch.setattr(hl, "get_positions", lambda *a, **k: {"positions": []})

    out = close_live(
        {"id": "B1"},
        position={"trade_id": "t-bot-reject", "current_price": 50000.0},
        reason="test_rejection",
    )

    assert calls, "close_live never reached the exchange close"
    assert out["state"] == "failed", f"unfilled close reported as {out['state']!r}"
    assert "no fill" in str(out.get("message", "")).lower()
    assert _quiet_execution_failures, "a failed live close must be reported"

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, exit_price, pnl_usd FROM trades WHERE id = 't-bot-reject'"
        ).fetchone()
    assert row["status"] == "OPEN", "a close that did not fill was booked as closed"
    assert row["exit_price"] is None, "booked an exit price for a close that never filled"


def test_bot_live_close_without_a_fill_stays_pending_not_closed(
    forven_db, monkeypatch, _quiet_execution_failures
):
    """No error, but no fill either: hold the trade for the reconcile sweep.

    _execute_direct marks the row pending_close_reconcile in this case; booking
    it closed would strand a real position on the venue with its stop retired.
    """
    import forven.exchange.hyperliquid as hl
    from forven.bot_factory.live_exec import close_live
    from forven.db import get_db

    _insert_live_bot_trade("t-bot-pending", bot_id="B2")

    monkeypatch.setattr(
        hl, "close_position",
        lambda asset, size, side, **kwargs: {
            "mid": 50000.0, "close_price": 48500.0,
            "exit_price": None, "fill_price": None, "filled_size": None,
        },
    )
    monkeypatch.setattr(hl, "get_positions", lambda *a, **k: {"positions": []})

    out = close_live(
        {"id": "B2"},
        position={"trade_id": "t-bot-pending", "current_price": 50000.0},
        reason="test_pending",
    )

    assert out["state"] == "pending", f"unconfirmed close reported as {out['state']!r}"
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, exit_price FROM trades WHERE id = 't-bot-pending'"
        ).fetchone()
    assert row["status"] == "OPEN"
    assert row["exit_price"] is None


def test_bot_live_close_books_a_confirmed_fill(
    forven_db, monkeypatch, _quiet_execution_failures
):
    """The other side of the guard: a genuine full fill still closes.

    Without this, "never book an unfilled close" could be satisfied by never
    booking anything.
    """
    import forven.exchange.hyperliquid as hl
    from forven.bot_factory.live_exec import close_live
    from forven.db import get_db

    _insert_live_bot_trade("t-bot-filled", bot_id="B3")

    monkeypatch.setattr(
        hl, "close_position",
        lambda asset, size, side, **kwargs: {
            "mid": 51000.0, "close_price": 51000.0,
            "exit_price": 51000.0, "filled_size": 0.1,
        },
    )
    monkeypatch.setattr(hl, "get_positions", lambda *a, **k: {"positions": []})

    out = close_live(
        {"id": "B3"},
        position={"trade_id": "t-bot-filled", "current_price": 51000.0},
        reason="test_filled",
    )

    assert out["state"] == "closed", f"a confirmed full fill reported as {out['state']!r}"
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM trades WHERE id = 't-bot-filled'"
        ).fetchone()
    assert row["status"] != "OPEN"
