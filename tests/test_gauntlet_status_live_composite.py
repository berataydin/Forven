"""COMPOSITE-LIVE-1: the gauntlet-status composite is scored from the CURRENT
artifacts, not the frozen stored stamp.

Paper/live strategies have frozen stored metrics (the recalc's metric-sync
deliberately skips operator-owned stages), so the stored composite pinned at its
pre-promotion value forever — ~40 prod strategies rendered "0.0 / 100" beside
5/5 PASS test chips. The status endpoint now calls the same scorer the recalc
uses (compute_composite_robustness_score) and only falls back to the stored
value when the scorer has nothing to say.

The patch target is ``forven.robustness.engine`` — where the scorer LIVES.
forven.routers.robustness re-exports it for pre-split importers, but
gauntlet.status imports the engine directly (ARCH-03), so patching the router
re-export would leave these tests green while exercising nothing. If you move
the scorer again, move this patch target with it and re-run the seam check in
tests/test_finish_shims.py.
"""

from __future__ import annotations

import json


def _insert_strategy(strategy_id: str, *, stage: str, metrics: dict) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, metrics, created_at, updated_at)
               VALUES (?, ?, 'rule_engine', 'ETH', '4h', '{}', ?, ?, ?,
                       '2026-07-21T00:00:00+00:00', '2026-07-21T00:00:00+00:00')""",
            (strategy_id, strategy_id, stage, stage, json.dumps(metrics)),
        )


def test_status_composite_prefers_live_scorer(forven_db, monkeypatch):
    from forven.gauntlet.status import get_strategy_gauntlet_status
    import forven.robustness.engine as robustness_engine

    _insert_strategy(
        "S99101", stage="live_graduated",
        metrics={"composite_robustness_score": 0.0, "robustness_tests_passed": 0},
    )
    monkeypatch.setattr(
        robustness_engine, "compute_composite_robustness_score",
        lambda sid: {"score": 100.0, "passed": 5, "canonical_total": 5,
                     "avg_margin": 0.5, "measured_total": 5, "tests": []},
    )
    status = get_strategy_gauntlet_status("S99101")
    assert status["composite_robustness_score"] == 100.0  # not the frozen 0.0


def test_status_composite_falls_back_to_stored_when_no_artifacts(forven_db, monkeypatch):
    from forven.gauntlet.status import get_strategy_gauntlet_status
    import forven.robustness.engine as robustness_engine

    _insert_strategy(
        "S99102", stage="gauntlet",
        metrics={"composite_robustness_score": 72.6},
    )
    monkeypatch.setattr(robustness_engine, "compute_composite_robustness_score", lambda sid: None)
    status = get_strategy_gauntlet_status("S99102")
    assert status["composite_robustness_score"] == 72.6


def test_status_composite_fallback_applies_legacy_scale_guard(forven_db, monkeypatch):
    from forven.gauntlet.status import get_strategy_gauntlet_status
    import forven.robustness.engine as robustness_engine

    # Legacy 0-1 fraction in the stored blob must still surface as 0-100.
    _insert_strategy("S99103", stage="gauntlet", metrics={"robustness": 0.726})
    monkeypatch.setattr(robustness_engine, "compute_composite_robustness_score", lambda sid: None)
    status = get_strategy_gauntlet_status("S99103")
    assert status["composite_robustness_score"] == 72.6


def test_status_composite_survives_scorer_error(forven_db, monkeypatch):
    from forven.gauntlet.status import get_strategy_gauntlet_status
    import forven.robustness.engine as robustness_engine

    _insert_strategy("S99104", stage="paper", metrics={"composite_robustness_score": 40.0})

    def _boom(sid):
        raise RuntimeError("scorer exploded")

    monkeypatch.setattr(robustness_engine, "compute_composite_robustness_score", _boom)
    status = get_strategy_gauntlet_status("S99104")
    assert status["composite_robustness_score"] == 40.0  # fail-soft to stored


def test_status_composite_patch_target_is_the_module_status_imports_from(forven_db, monkeypatch):
    """The seam check: patching the ENGINE must actually reach the status reader.

    Without this, a future move of the scorer (or of the import in status.py)
    silently detaches every test above — they would keep passing while asserting
    nothing about production. Here the patched scorer returns a sentinel score no
    fallback path can produce, so the assertion can only hold if status.py really
    called the patched function.
    """
    import forven.robustness.engine as robustness_engine
    from forven.gauntlet.status import get_strategy_gauntlet_status

    calls: list[str] = []

    def _sentinel(sid):
        calls.append(sid)
        return {"score": 13.37, "passed": 1, "canonical_total": 5,
                "avg_margin": 0.1, "measured_total": 1, "tests": []}

    _insert_strategy("S99105", stage="paper", metrics={"composite_robustness_score": 40.0})
    monkeypatch.setattr(robustness_engine, "compute_composite_robustness_score", _sentinel)
    status = get_strategy_gauntlet_status("S99105")
    assert calls == ["S99105"], "gauntlet.status did not call the patched engine scorer"
    assert status["composite_robustness_score"] == 13.37
