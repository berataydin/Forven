"""The live-graduation recommender must not recommend on stale-engine evidence.

LIVE-LOOP-1's eligibility ladder does NOT route through
policy.evaluate_promotion / _strict_robustness_reject, so the live gate's
stale-engine refusal never reached it. Without the check the recommender can tell
the operator a strategy is READY on evidence the actual promotion gate will then
refuse -- and a recommendation that cannot be acted on is worse than none,
because it spends the operator's trust in the signal.

Live at the time of writing: bumping BACKTEST_ENGINE_VERSION to 6 (per-print
funding intervals -- every non-8h perp was mispriced before it) invalidated the
whole paper cohort's evidence at once. Spot-checked strategies carry artifacts
stamped v1, v2 and v5.
"""

from __future__ import annotations

import forven.live_graduation as lg


def _strategy(sid: str = "S_TEST") -> dict:
    return {"id": sid, "stage_changed_at": None}


def test_stale_engine_artifacts_block_the_recommendation(forven_db, monkeypatch):
    monkeypatch.setattr(
        "forven.policy._check_engine_artifact_freshness",
        lambda sid, required_types=None: (
            False,
            "Validation artifacts predate the current engine version (v6) ...: walk_forward (engine v2)",
        ),
    )
    result = lg.evaluate_graduation_candidate(_strategy())
    assert result["eligible"] is False
    assert any("engine freshness" in r for r in result["reasons"]), result["reasons"]
    assert result["evidence"]["engine_artifacts_fresh"] is False


def test_fresh_artifacts_add_no_engine_reason(forven_db, monkeypatch):
    """The check must not become a permanent blocker once evidence is re-run."""
    monkeypatch.setattr(
        "forven.policy._check_engine_artifact_freshness",
        lambda sid, required_types=None: (True, "All validation artifacts match the current engine version"),
    )
    result = lg.evaluate_graduation_candidate(_strategy())
    assert result["evidence"]["engine_artifacts_fresh"] is True
    assert not [r for r in result["reasons"] if "engine freshness" in r]


def test_unverifiable_provenance_fails_closed(forven_db, monkeypatch):
    """An exception must BLOCK, never wave the candidate through.

    The whole ladder is documented as fail-closed; a provenance check that
    degrades to "assume fresh" on error would be the fail-open this audit exists
    to remove.
    """
    def _boom(sid, required_types=None):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("forven.policy._check_engine_artifact_freshness", _boom)
    result = lg.evaluate_graduation_candidate(_strategy())
    assert result["eligible"] is False
    assert any("unverifiable" in r for r in result["reasons"]), result["reasons"]
    assert result["evidence"]["engine_artifacts_fresh"] is False
