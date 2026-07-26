"""Contract test between the connector and the REAL hyperliquid-python-sdk.

TEST-4 (audit 2026-07-25). Every order-path test in this suite duck-types the
exchange object, and `hyperliquid-python-sdk` carries no upper bound in
requirements-ci.txt, so `pip install` moves it on every fresh CI run. That
combination means an SDK method rename or signature change passes 100% of CI and
first surfaces as an AttributeError inside `_submit` on a REAL mainnet order —
after every risk gate has already approved it and the capital is committed.

These tests import the actual SDK classes and assert the surface the connector
depends on still exists and still accepts the argument shapes it passes. They do
NOT hit the network: only `inspect` and class attributes are used.

Deliberately NOT asserted here: the RESPONSE envelope shape
(`response.data.statuses[]`). That is already fail-closed at runtime by
`_require_bulk_order_ids` (hyperliquid.py) and by
`execution_results.parse_close_receipt`, which refuse to treat an unconfirmed
response as a fill. Pinning the envelope statically would duplicate that guard
and would break on benign additive changes.
"""

from __future__ import annotations

import inspect

import pytest

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid


# Methods forven/exchange/hyperliquid.py calls on the SDK's Exchange.
# `order` and `bulk_orders` place real orders; `cancel` and `update_leverage`
# are the other two mutation paths.
EXCHANGE_METHODS = ("order", "bulk_orders", "cancel", "update_leverage")

# The Info surface the connector uses. This list is ALSO what
# HyperliquidInfoClient (the Protocol) declares and what
# _HyperliquidDirectInfoClient (the /info fallback) must implement — the
# three are pinned to each other by test_protocol_matches_real_info below.
# `user_fills` / `user_fills_by_time` were added by the audit: without them the
# fallback client silently returned [] and ghost-recovered closes were stamped
# at the reconcile-time mid instead of the true fill.
INFO_METHODS = (
    "all_mids",
    "extra_agents",
    "open_orders",
    "spot_meta",
    "spot_user_state",
    "user_state",
    "user_fills",
    "user_fills_by_time",
)


@pytest.mark.parametrize("name", EXCHANGE_METHODS)
def test_exchange_method_exists(name: str) -> None:
    assert callable(getattr(Exchange, name, None)), (
        f"hyperliquid SDK Exchange.{name}() is gone or is no longer callable. "
        "The connector calls it on the live order path; a rename here reaches "
        "production as an AttributeError on a real order."
    )


@pytest.mark.parametrize("name", INFO_METHODS)
def test_info_method_exists(name: str) -> None:
    assert callable(getattr(Info, name, None)), (
        f"hyperliquid SDK Info.{name}() is gone or is no longer callable. "
        "The connector and the direct-/info fallback client both rely on it."
    )


def _accepts(func, *args, **kwargs) -> bool:
    """True when func's signature would bind these arguments (no call made)."""
    sig = inspect.signature(func)
    try:
        sig.bind(None, *args, **kwargs)  # None = self
        return True
    except TypeError:
        return False


def test_bulk_orders_accepts_a_list_of_order_dicts() -> None:
    """`_submit("place_order", ..., exchange.bulk_orders, orders)` passes one list."""
    assert _accepts(Exchange.bulk_orders, [{"coin": "BTC"}]), (
        f"Exchange.bulk_orders no longer accepts a single list argument; "
        f"signature is now {inspect.signature(Exchange.bulk_orders)}"
    )


def test_cancel_accepts_name_and_oid() -> None:
    """The connector calls `exchange.cancel(asset.upper(), oid)`."""
    assert _accepts(Exchange.cancel, "BTC", 123), (
        f"Exchange.cancel no longer accepts (name, oid); "
        f"signature is now {inspect.signature(Exchange.cancel)}"
    )


def test_update_leverage_accepts_leverage_name_and_cross_flag() -> None:
    """Leverage is set before every live entry; a signature change fails the open.

    The audit found the connector must send an INTEGER leverage — a decimal-string
    was rejected by the venue — so the parameter must still be positional here.
    """
    sig = inspect.signature(Exchange.update_leverage)
    params = [p for p in sig.parameters if p != "self"]
    assert params[:2] == ["leverage", "name"], (
        f"Exchange.update_leverage parameter order changed: {params} "
        "(connector passes leverage then coin positionally)"
    )
    assert _accepts(Exchange.update_leverage, 5, "BTC", False), (
        f"Exchange.update_leverage no longer accepts (leverage, name, is_cross); "
        f"signature is now {sig}"
    )


def test_info_all_mids_still_takes_an_optional_dex() -> None:
    """get_all_mids() calls info.all_mids() with no arguments."""
    assert _accepts(Info.all_mids), (
        f"Info.all_mids can no longer be called with no arguments; "
        f"signature is now {inspect.signature(Info.all_mids)}"
    )


def test_info_user_fills_by_time_accepts_a_start_time() -> None:
    """Exit-price recovery pages the fill ledger from a start timestamp."""
    assert _accepts(Info.user_fills_by_time, "0xabc", 1700000000000), (
        f"Info.user_fills_by_time no longer accepts (address, start_time); "
        f"signature is now {inspect.signature(Info.user_fills_by_time)}"
    )


def test_protocol_matches_real_info() -> None:
    """The Protocol, the direct fallback and the real SDK must not drift apart.

    HL-INFO-1: the direct-/info fallback client is used on the documented-normal
    testnet path. When it lacked `user_fills`, exit-price recovery silently
    returned [] and every ghost-recovered close was stamped at the reconcile-time
    mid. Anything the connector may call through the Protocol must therefore
    exist on BOTH the real SDK Info and the fallback client.
    """
    from forven.exchange.hyperliquid import _HyperliquidDirectInfoClient

    missing_on_sdk = [m for m in INFO_METHODS if not callable(getattr(Info, m, None))]
    missing_on_fallback = [
        m for m in INFO_METHODS if not callable(getattr(_HyperliquidDirectInfoClient, m, None))
    ]
    assert not missing_on_sdk, f"real SDK Info is missing: {missing_on_sdk}"
    assert not missing_on_fallback, (
        f"the direct-/info fallback client is missing: {missing_on_fallback} — "
        "callers degrade SILENTLY when the fallback is active"
    )


def test_cloid_still_constructs_from_a_hex_string() -> None:
    """Order idempotency depends on deterministic client order ids.

    `_build_order_cloids` derives a Cloid per leg so a retry cannot double-submit.
    If Cloid's construction API changes, retries stop being idempotent.
    """
    assert hasattr(Cloid, "from_str"), "Cloid.from_str is gone — order cloids cannot be built"
    cloid = Cloid.from_str("0x" + "ab" * 16)
    assert cloid.to_raw().startswith("0x")


def test_mainnet_and_testnet_urls_still_exist_and_differ() -> None:
    """The whole testnet/mainnet guard rests on these two constants."""
    assert constants.MAINNET_API_URL and constants.TESTNET_API_URL
    assert constants.MAINNET_API_URL != constants.TESTNET_API_URL
