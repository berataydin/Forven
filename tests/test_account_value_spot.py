"""Regression: get_account_value must include FREE SPOT USDC in total equity.

Two incidents pin the formula from both sides:

1. Testnet rehearsal — sub-account collateral sat in the spot wallet; once an
   isolated perp position opened, the perp marginSummary.accountValue dropped
   to ~the isolated margin ($13) and the ~$316 spot balance was excluded, so
   equity looked like it crashed $329 -> $13 and the kill-switch flattened the
   position on a fake 48% drawdown. Free spot must COUNT.

2. Bot Factory named wallet — the exchange reported the open isolated
   position's margin BOTH as perp accountValue AND as spot USDC on "hold"
   (hold tracked marginUsed tick-for-tick), so summing spot TOTAL showed a
   $19.41 wallet as $38.82. Held spot must NOT count — it's already in perp.

Hence: accountValue = perp accountValue + free spot (total - hold), consistent
whether collateral is in spot or perp, flat or in a position.

3. EQ-ATOMIC-1 (2026-07-28 false daily halt) — the spot leg silently read as
   zero during a 429 storm ("best-effort degrade to perp-only"), so a $490
   book reported $8.13, poisoned the book equity cache, and latched a false
   −48% daily-loss halt. A failed spot read must now fail the WHOLE read.
"""

import pytest

import forven.exchange.hyperliquid as hl


class _FakeInfo:
    def user_state(self, address, dex=""):
        return {}

    def spot_user_state(self, address):
        return {}


def _patch(monkeypatch, perp_account_value, spot_total, spot_free, margin_used="0.0"):
    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "_get_account_info_client", lambda testnet: (_FakeInfo(), "0xacct"))
    state = {
        "marginSummary": {
            "accountValue": str(perp_account_value),
            "totalMarginUsed": str(margin_used),
            "totalNtlPos": "0.0",
            "totalRawUsd": "0.0",
        },
        "withdrawable": "0.0",
    }
    monkeypatch.setattr(hl, "_with_breaker", lambda name, br, fn, *a, **k: state)
    monkeypatch.setattr(hl, "_extract_spot_usdc_balance", lambda info, addr: (spot_total, spot_free))


def test_open_position_includes_spot_not_just_isolated_margin(monkeypatch):
    # Position open: perp accountValue = isolated margin ($13.14), spot = $316.
    _patch(monkeypatch, perp_account_value="13.14", spot_total=316.0, spot_free=316.0, margin_used="13.14")
    acc = hl.get_account_value(testnet=True, account_address="0xLONG")
    assert acc["accountValue"] == pytest.approx(329.14)  # NOT 13.14
    assert acc["withdrawable"] == pytest.approx(316.0)


def test_flat_spot_funded_reads_full_balance(monkeypatch):
    # Flat, all collateral in spot: perp accountValue 0, spot $329.
    _patch(monkeypatch, perp_account_value="0.0", spot_total=329.0, spot_free=329.0)
    acc = hl.get_account_value(testnet=True, account_address="0xLONG")
    assert acc["accountValue"] == pytest.approx(329.0)


def test_perp_funded_with_no_spot_unchanged(monkeypatch):
    # Collateral already in perp, no spot: equity = perp value (no double-count).
    _patch(monkeypatch, perp_account_value="329.0", spot_total=0.0, spot_free=0.0, margin_used="13.14")
    acc = hl.get_account_value(testnet=True, account_address="0xLONG")
    assert acc["accountValue"] == pytest.approx(329.0)


def test_spot_hold_backing_perp_margin_not_double_counted(monkeypatch):
    # Open isolated position whose margin the exchange reports BOTH as the perp
    # accountValue AND as spot USDC on hold: $19.26 perp, spot total $19.41 of
    # which only $0.15 is free. Equity is $19.41, not $38.67.
    _patch(
        monkeypatch,
        perp_account_value="19.26",
        spot_total=19.41,
        spot_free=0.15,
        margin_used="19.26",
    )
    acc = hl.get_account_value(testnet=True, account_address="0xBOT")
    assert acc["accountValue"] == pytest.approx(19.41)
    assert acc["withdrawable"] == pytest.approx(0.15)


def test_spot_read_failure_fails_the_whole_read(monkeypatch):
    """EQ-ATOMIC-1: a perp-only partial is indistinguishable from a real crash
    downstream — the read RAISES so callers substitute last-known-good or skip
    the tick, instead of serving $13.14 for a $329 wallet."""
    _patch(monkeypatch, perp_account_value="13.14", spot_total=316.0, spot_free=316.0)
    def _boom(info, addr):
        raise RuntimeError("spot read failed")
    monkeypatch.setattr(hl, "_extract_spot_usdc_balance", _boom)
    with pytest.raises(RuntimeError, match="spot read failed"):
        hl.get_account_value(testnet=True, account_address="0xLONG")


def test_extract_spot_balance_raises_on_transport_and_shape_failures():
    """EQ-ATOMIC-1: transport errors propagate; a 'null'/shapeless payload (what
    a rate-limited CloudFront edge actually serves) is a FAILED read, not $0."""
    class _RaisingInfo:
        def spot_user_state(self, wallet):
            raise ConnectionError("429 Too Many Requests")

    with pytest.raises(ConnectionError):
        hl._extract_spot_usdc_balance(_RaisingInfo(), "0x1")

    class _NullInfo:
        def spot_user_state(self, wallet):
            return None  # json 'null' body from the rate limiter

    with pytest.raises(ValueError):
        hl._extract_spot_usdc_balance(_NullInfo(), "0x1")

    class _NoBalances:
        def spot_user_state(self, wallet):
            return {"unexpected": 1}

    with pytest.raises(ValueError):
        hl._extract_spot_usdc_balance(_NoBalances(), "0x1")


def test_extract_spot_balance_tolerates_feature_absence_and_empty_wallets():
    """No spot endpoint on the client = feature absence, and an empty balances
    list = a genuinely empty wallet — both still read as zero, no raise."""
    class _NoSpotEndpoint:
        pass

    assert hl._extract_spot_usdc_balance(_NoSpotEndpoint(), "0x1") == (0.0, 0.0)

    class _EmptyWallet:
        def spot_user_state(self, wallet):
            return {"balances": []}

    assert hl._extract_spot_usdc_balance(_EmptyWallet(), "0x1") == (0.0, 0.0)
