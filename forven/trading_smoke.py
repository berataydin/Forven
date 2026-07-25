from __future__ import annotations

import json
from typing import Any

from forven.config import get_execution_mode
from forven.db import get_db, log_activity
from forven.exchange.risk import register as register_risk
from forven.exchange.risk import release as release_risk
from forven.scanner import _close_trade_db, _open_trade_db, _update_trade_fill
from forven.sim.clock import get_now

_STATUS_ORDER = {"ok": 0, "warn": 1, "fail": 2}
_DEFAULT_ASSET_CANDIDATES = ("SOL", "ETH", "BTC")


def resolve_effective_testnet(requested_testnet: bool) -> bool:
    """The network the smoke will ACTUALLY hit, resolved from the credentials.

    MAINNET-GUARD-1: the mainnet block below reasoned about the REQUESTED flag,
    but ``get_exchange`` picks the endpoint from the credential-resolved
    ``USE_TESTNET`` — so ``--mainnet`` omitted plus mainnet-resolving credentials
    ran an "it's only testnet" smoke that opened and closed a REAL position.
    Resolve once, up front, and gate on that.
    """
    from forven.exchange.hyperliquid import resolve_configured_testnet

    return resolve_configured_testnet(bool(requested_testnet))


def get_account_value(*, testnet: bool = True, require_connection: bool = True) -> dict[str, Any]:
    from forven.exchange.hyperliquid import get_account_value as _get_account_value

    return _get_account_value(testnet=testnet, require_connection=require_connection)


def get_positions(*, testnet: bool = True) -> dict[str, Any]:
    from forven.exchange.hyperliquid import get_positions as _get_positions

    return _get_positions(testnet=testnet)


def get_open_orders(*, testnet: bool = True) -> list[Any]:
    from forven.exchange.hyperliquid import get_open_orders as _get_open_orders

    return _get_open_orders(testnet=testnet)


def get_all_mids(*, testnet: bool = True) -> dict[str, Any]:
    from forven.exchange.hyperliquid import get_all_mids as _get_all_mids

    return _get_all_mids(testnet=testnet)


def market_order(
    asset: str,
    side: str,
    size: float,
    stop_loss_price: float | None = None,
    *,
    testnet: bool = True,
) -> dict[str, Any]:
    from forven.exchange.hyperliquid import market_order as _market_order
    import uuid

    idempotency_key = f"smoke-{uuid.uuid4().hex[:16]}"
    return _market_order(
        asset=asset,
        side=side,
        size=size,
        stop_loss_price=stop_loss_price,
        idempotency_key=idempotency_key,
        testnet=testnet,
    )


def close_position(
    asset: str,
    size: float,
    side: str = "sell",
    *,
    testnet: bool = True,
) -> dict[str, Any]:
    from forven.exchange.hyperliquid import close_position as _close_position

    return _close_position(asset=asset, size=size, side=side, testnet=testnet)


def cancel_all_orders(*, asset: str | None = None, testnet: bool = True) -> list[dict[str, Any]]:
    from forven.exchange.hyperliquid import cancel_all_orders as _cancel_all_orders

    return _cancel_all_orders(asset=asset, testnet=testnet)


def _make_check(name: str, status: str, summary: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_status = status if status in _STATUS_ORDER else "fail"
    return {
        "name": name,
        "status": normalized_status,
        "summary": summary,
        "details": details or {},
    }


def _merge_status(current: str, candidate: str) -> str:
    return current if _STATUS_ORDER[current] >= _STATUS_ORDER[candidate] else candidate


def _safe_float(value: object) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _load_trade_signal_data(trade_id: str) -> dict[str, Any]:
    with get_db() as conn:
        row = conn.execute("SELECT signal_data FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not row:
        return {}
    raw = row["signal_data"]
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _update_trade_metadata(
    trade_id: str,
    *,
    extra_signal_data: dict[str, Any] | None = None,
    status: str | None = None,
    closed: bool = False,
) -> None:
    signal_data = _load_trade_signal_data(trade_id)
    if extra_signal_data:
        signal_data.update(_json_safe(extra_signal_data))

    updates = ["signal_data = ?"]
    values: list[Any] = [json.dumps(signal_data)]
    if status:
        updates.append("status = ?")
        values.append(str(status))
    if closed:
        updates.append("closed_at = ?")
        values.append(get_now().isoformat())
    values.append(trade_id)

    with get_db() as conn:
        conn.execute(
            f"UPDATE trades SET {', '.join(updates)} WHERE id = ?",
            tuple(values),
        )


def _mark_trade_failed(trade_id: str, reason: str, payload: dict[str, Any] | None = None) -> None:
    extra_signal_data = {
        "smoke_error": str(reason),
        "smoke_failed_at": get_now().isoformat(),
    }
    if payload:
        extra_signal_data["smoke_failure_payload"] = _json_safe(payload)
    _update_trade_metadata(
        trade_id,
        extra_signal_data=extra_signal_data,
        status="FAILED",
        closed=True,
    )


def _get_trade_status(trade_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not row:
        return None
    return str(row["status"] or "").strip() or None


def _get_portfolio_position_count(trade_id: str) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM portfolio_positions WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
    return int(row["count"] if row else 0)


def _extract_position_assets(payload: dict[str, Any] | None) -> set[str]:
    assets: set[str] = set()
    positions = payload.get("positions", []) if isinstance(payload, dict) else []
    if not isinstance(positions, list):
        return assets
    for row in positions:
        if not isinstance(row, dict):
            continue
        position = row.get("position")
        candidate = position if isinstance(position, dict) else row
        coin = str(
            candidate.get("coin")
            or candidate.get("asset")
            or row.get("coin")
            or row.get("asset")
            or ""
        ).strip().upper()
        if not coin:
            continue
        size = (
            candidate.get("szi")
            or candidate.get("size")
            or candidate.get("sz")
            or row.get("szi")
            or row.get("size")
            or row.get("sz")
        )
        parsed_size = _safe_float(size)
        if parsed_size is not None and abs(parsed_size) <= 0:
            continue
        assets.add(coin)
    return assets


def _extract_order_assets(payload: list[Any] | None) -> set[str]:
    assets: set[str] = set()
    if not isinstance(payload, list):
        return assets
    for row in payload:
        if not isinstance(row, dict):
            continue
        coin = str(row.get("coin") or row.get("asset") or "").strip().upper()
        if coin:
            assets.add(coin)
    return assets


def _count_orders_for_asset(payload: list[Any] | None, asset: str) -> int:
    if not isinstance(payload, list):
        return 0
    normalized_asset = str(asset or "").strip().upper()
    return sum(
        1
        for row in payload
        if isinstance(row, dict) and str(row.get("coin") or row.get("asset") or "").strip().upper() == normalized_asset
    )


def _extract_fill_details(payload: dict[str, Any]) -> tuple[float | None, str | None]:
    fill = (
        payload.get("entry_price")
        or payload.get("exit_price")
        or payload.get("close_price")
        or payload.get("fill_price")
        or payload.get("mid")
    )
    exchange_order_id = payload.get("order_id") or payload.get("orderId") or payload.get("oid")
    fill_value = _safe_float(fill)
    exchange_order_text = str(exchange_order_id) if exchange_order_id is not None else None
    return fill_value, exchange_order_text


def _calculate_pnl(
    *,
    direction: str,
    entry_price: float,
    exit_price: float,
    size: float,
    leverage: float,
) -> tuple[float, float]:
    signed = 1.0 if direction == "long" else -1.0
    pnl_pct = 0.0
    if entry_price > 0:
        pnl_pct = ((exit_price - entry_price) / entry_price) * signed * leverage
    pnl_usd = (exit_price - entry_price) * size * signed
    return float(pnl_pct), float(pnl_usd)


def _select_smoke_asset(
    requested_asset: str | None,
    mids: dict[str, Any],
    blocked_assets: set[str],
) -> tuple[str, float]:
    normalized_requested = str(requested_asset or "").strip().upper()
    candidates: list[str] = []
    if normalized_requested:
        candidates.append(normalized_requested)
    candidates.extend(asset for asset in _DEFAULT_ASSET_CANDIDATES if asset not in candidates)
    candidates.extend(
        str(asset).strip().upper()
        for asset, mid in mids.items()
        if str(asset).strip() and _safe_float(mid) and str(asset).strip().upper() not in candidates
    )

    for candidate in candidates:
        if candidate in blocked_assets:
            if normalized_requested == candidate:
                raise RuntimeError(f"Requested smoke asset {candidate} already has an open position or order")
            continue
        mid = _safe_float(mids.get(candidate))
        if mid is None or mid <= 0:
            continue
        return candidate, mid

    raise RuntimeError("No flat, priceable test asset is available for the active smoke order")


def _check_account_connectivity(*, testnet: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    account = get_account_value(testnet=testnet, require_connection=True)
    account_value = _safe_float(account.get("accountValue") if isinstance(account, dict) else None)
    margin_used = _safe_float(account.get("totalMarginUsed") if isinstance(account, dict) else None)
    return account, _make_check(
        "account",
        "ok",
        "HyperLiquid private account connectivity check passed",
        {
            "testnet": bool(testnet),
            "account_value": account_value,
            "total_margin_used": margin_used,
        },
    )


def _check_positions(*, testnet: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = get_positions(testnet=testnet)
    positions = payload.get("positions", []) if isinstance(payload, dict) else []
    return payload, _make_check(
        "positions",
        "ok",
        "Position snapshot loaded",
        {
            "testnet": bool(testnet),
            "position_count": len(positions) if isinstance(positions, list) else 0,
            "active_assets": sorted(_extract_position_assets(payload)),
        },
    )


def _check_open_orders(*, testnet: bool) -> tuple[list[Any], dict[str, Any]]:
    payload = get_open_orders(testnet=testnet)
    return payload, _make_check(
        "open_orders",
        "ok",
        "Open-order snapshot loaded",
        {
            "testnet": bool(testnet),
            "open_order_count": len(payload) if isinstance(payload, list) else 0,
            "active_assets": sorted(_extract_order_assets(payload)),
        },
    )


def _check_market_mids(*, testnet: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = get_all_mids(testnet=testnet)
    mid_count = len(payload) if isinstance(payload, dict) else 0
    if not isinstance(payload, dict) or not payload:
        return {}, _make_check(
            "market_mids",
            "fail",
            "Could not load HyperLiquid mid prices",
            {"testnet": bool(testnet)},
        )
    return payload, _make_check(
        "market_mids",
        "ok",
        "Market mids loaded",
        {
            "testnet": bool(testnet),
            "mid_count": mid_count,
            "sample_assets": sorted(str(asset).strip().upper() for asset in list(payload.keys())[:10]),
        },
    )


def _run_active_order_smoke(
    *,
    testnet: bool,
    allow_mainnet: bool,
    asset: str | None,
    usd_notional: float,
    direction: str,
    strategy_id: str,
    positions_payload: dict[str, Any],
    open_orders_payload: list[Any],
    mids_payload: dict[str, Any],
) -> dict[str, Any]:
    # MAINNET-GUARD-1: gate on the CREDENTIAL-RESOLVED network, never the caller's
    # requested flag — a caller asking for testnet against mainnet-resolving
    # credentials was placing a real order here while reporting testnet=True. The
    # resolved value is what every downstream order call uses too, so the report
    # and the endpoint can never disagree.
    requested_testnet = bool(testnet)
    testnet = resolve_effective_testnet(requested_testnet)
    if not testnet and not allow_mainnet:
        return _make_check(
            "execution",
            "fail",
            "Active trading smoke is blocked on mainnet",
            {
                "testnet": bool(testnet),
                "requested_testnet": requested_testnet,
                "allow_mainnet": bool(allow_mainnet),
            },
        )

    normalized_direction = str(direction or "long").strip().lower()
    if normalized_direction not in {"long", "short"}:
        return _make_check(
            "execution",
            "fail",
            f"Unsupported smoke direction: {direction}",
            {"direction": direction},
        )

    requested_notional = float(usd_notional)
    if requested_notional <= 0:
        return _make_check(
            "execution",
            "fail",
            "Smoke notional must be greater than zero",
            {"usd_notional": requested_notional},
        )

    blocked_assets = _extract_position_assets(positions_payload).union(_extract_order_assets(open_orders_payload))
    try:
        selected_asset, mid_price = _select_smoke_asset(asset, mids_payload, blocked_assets)
    except Exception as exc:
        return _make_check(
            "execution",
            "fail",
            "No safe asset was available for the active smoke order",
            {
                "requested_asset": str(asset or "").strip().upper() or None,
                "blocked_assets": sorted(blocked_assets),
                "error": str(exc),
            },
        )

    size = round(requested_notional / mid_price, 6)
    if size <= 0:
        return _make_check(
            "execution",
            "fail",
            "Calculated smoke order size was invalid",
            {
                "asset": selected_asset,
                "mid_price": mid_price,
                "usd_notional": requested_notional,
                "size": size,
            },
        )

    trade_id = _open_trade_db(
        strategy_id,
        selected_asset,
        normalized_direction,
        mid_price,
        size,
        0.0001,
        1.0,
        {
            "smoke_test": True,
            "smoke_source": "trading_plane_smoke",
            "requested_mid": mid_price,
            "requested_notional_usd": requested_notional,
        },
        execution_type="soak_smoke",
    )
    register_risk(trade_id, selected_asset, normalized_direction, strategy_id, 0.0001, entry_price=mid_price)

    open_payload: dict[str, Any] | None = None
    close_payload: dict[str, Any] | None = None
    entry_price = mid_price
    exit_price: float | None = None
    exchange_open_succeeded = False
    exchange_close_succeeded = False
    cleanup_errors: list[str] = []
    cancel_results: list[dict[str, Any]] = []

    log_activity(
        "info",
        "trading_smoke",
        f"Starting active trading smoke {trade_id} {selected_asset} {normalized_direction} size={size}",
        {
            "trade_id": trade_id,
            "asset": selected_asset,
            "usd_notional": requested_notional,
            # The RESOLVED network (MAINNET-GUARD-1) — this is real money when False.
            "testnet": testnet,
            "requested_testnet": requested_testnet,
        },
    )

    try:
        open_payload = market_order(
            asset=selected_asset,
            side=normalized_direction,
            size=size,
            stop_loss_price=None,
            testnet=testnet,
        )
        if not isinstance(open_payload, dict):
            raise RuntimeError("open order did not return a JSON object")
        if open_payload.get("error"):
            raise RuntimeError(str(open_payload.get("error")))
        exchange_open_succeeded = True

        entry_fill, open_order_id = _extract_fill_details(open_payload)
        if entry_fill is None or entry_fill <= 0:
            raise RuntimeError("open order response did not include a usable fill price")
        entry_price = entry_fill
        _update_trade_fill(
            trade_id,
            entry_fill,
            "entry",
            signal_price=mid_price,
            exchange_order_id=open_order_id,
        )

        close_side = "sell" if normalized_direction == "long" else "buy"
        close_payload = close_position(
            selected_asset,
            size,
            close_side,
            testnet=testnet,
        )
        if not isinstance(close_payload, dict):
            raise RuntimeError("close order did not return a JSON object")
        if close_payload.get("error"):
            raise RuntimeError(str(close_payload.get("error")))
        exchange_close_succeeded = True

        close_fill, close_order_id = _extract_fill_details(close_payload)
        if close_fill is None or close_fill <= 0:
            raise RuntimeError("close order response did not include a usable fill price")
        exit_price = close_fill
        _update_trade_fill(
            trade_id,
            close_fill,
            "exit",
            signal_price=entry_price,
            exchange_order_id=close_order_id,
        )

        pnl_pct, pnl_usd = _calculate_pnl(
            direction=normalized_direction,
            entry_price=entry_price,
            exit_price=exit_price,
            size=size,
            leverage=1.0,
        )
        _close_trade_db(trade_id, exit_price, pnl_pct, pnl_usd)
        release_risk(trade_id)

        open_orders_after = get_open_orders(testnet=testnet)
        residual_order_count = _count_orders_for_asset(open_orders_after, selected_asset)
        if residual_order_count:
            return _make_check(
                "execution",
                "fail",
                "Active smoke left residual open orders",
                {
                    "trade_id": trade_id,
                    "testnet": bool(testnet),
                    "requested_testnet": requested_testnet,
                    "asset": selected_asset,
                    "direction": normalized_direction,
                    "usd_notional": requested_notional,
                    "size": size,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_pct": pnl_pct,
                    "pnl_usd": pnl_usd,
                    "residual_open_orders": residual_order_count,
                    "trade_status": _get_trade_status(trade_id),
                    "portfolio_position_count": _get_portfolio_position_count(trade_id),
                },
            )

        log_activity(
            "info",
            "trading_smoke",
            f"Active trading smoke passed for {trade_id}",
            {"trade_id": trade_id, "asset": selected_asset, "entry_price": entry_price, "exit_price": exit_price},
        )
        return _make_check(
            "execution",
            "ok",
            "Active order smoke passed" if testnet else "Active MAINNET order smoke passed",
            {
                "trade_id": trade_id,
                "testnet": bool(testnet),
                "requested_testnet": requested_testnet,
                "asset": selected_asset,
                "direction": normalized_direction,
                "usd_notional": requested_notional,
                "size": size,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
                "trade_status": _get_trade_status(trade_id),
                "portfolio_position_count": _get_portfolio_position_count(trade_id),
            },
        )
    except Exception as exc:
        error_text = str(exc)
        try:
            cancel_results = cancel_all_orders(asset=selected_asset, testnet=testnet)
        except Exception as cancel_exc:
            cleanup_errors.append(f"cancel_all_orders failed: {cancel_exc}")

        if not exchange_open_succeeded:
            try:
                _mark_trade_failed(trade_id, error_text, open_payload)
            except Exception as update_exc:
                cleanup_errors.append(f"mark_trade_failed failed: {update_exc}")
            try:
                release_risk(trade_id)
            except Exception as release_exc:
                cleanup_errors.append(f"release_risk failed: {release_exc}")
            return _make_check(
                "execution",
                "fail",
                "Open-order phase failed during the active smoke",
                {
                    "trade_id": trade_id,
                    "asset": selected_asset,
                    "direction": normalized_direction,
                    "usd_notional": requested_notional,
                    "size": size,
                    "error": error_text,
                    "trade_status": _get_trade_status(trade_id),
                    "portfolio_position_count": _get_portfolio_position_count(trade_id),
                    "cancel_results": cancel_results,
                    "cleanup_errors": cleanup_errors,
                },
            )

        if not exchange_close_succeeded:
            try:
                _update_trade_metadata(
                    trade_id,
                    extra_signal_data={
                        "smoke_close_error": error_text,
                        "smoke_cleanup_at": get_now().isoformat(),
                        "smoke_cancel_results": cancel_results,
                    },
                )
            except Exception as update_exc:
                cleanup_errors.append(f"update_trade_metadata failed: {update_exc}")
            return _make_check(
                "execution",
                "fail",
                "Close-order phase failed after the smoke trade opened",
                {
                    "trade_id": trade_id,
                    "asset": selected_asset,
                    "direction": normalized_direction,
                    "usd_notional": requested_notional,
                    "size": size,
                    "entry_price": entry_price,
                    "error": error_text,
                    "trade_status": _get_trade_status(trade_id),
                    "portfolio_position_count": _get_portfolio_position_count(trade_id),
                    "cancel_results": cancel_results,
                    "cleanup_errors": cleanup_errors,
                    "cleanup_required": True,
                },
            )

        try:
            _update_trade_metadata(
                trade_id,
                extra_signal_data={
                    "smoke_post_close_error": error_text,
                    "smoke_cleanup_at": get_now().isoformat(),
                    "smoke_cancel_results": cancel_results,
                    "smoke_close_payload": close_payload,
                },
                status="CLOSED",
                closed=True,
            )
        except Exception as update_exc:
            cleanup_errors.append(f"post-close metadata update failed: {update_exc}")
        try:
            release_risk(trade_id)
        except Exception as release_exc:
            cleanup_errors.append(f"release_risk failed: {release_exc}")
        return _make_check(
            "execution",
            "fail",
            "Exchange close succeeded but local reconciliation failed",
            {
                "trade_id": trade_id,
                "asset": selected_asset,
                "direction": normalized_direction,
                "usd_notional": requested_notional,
                "size": size,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "error": error_text,
                "trade_status": _get_trade_status(trade_id),
                "portfolio_position_count": _get_portfolio_position_count(trade_id),
                "cancel_results": cancel_results,
                "cleanup_errors": cleanup_errors,
                "local_reconciliation_required": True,
            },
        )


def collect_trading_plane_smoke(
    *,
    testnet: bool = True,
    place_test_order: bool = False,
    allow_mainnet: bool = False,
    asset: str | None = None,
    usd_notional: float = 15.0,
    direction: str = "long",
    strategy_id: str = "SOAK_HL_SMOKE",
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    overall_status = "ok"

    # MAINNET-GUARD-1: resolve the network ONCE, up front, so every check, every
    # order and the reported summary describe the endpoint the SDK will actually
    # use — not the flag the caller asked for.
    requested_testnet = bool(testnet)
    testnet = resolve_effective_testnet(requested_testnet)

    account_payload: dict[str, Any] | None = None
    positions_payload: dict[str, Any] | None = None
    open_orders_payload: list[Any] | None = None
    mids_payload: dict[str, Any] | None = None

    base_checks: list[tuple[str, Any]] = [
        ("account", lambda: _check_account_connectivity(testnet=testnet)),
        ("positions", lambda: _check_positions(testnet=testnet)),
        ("open_orders", lambda: _check_open_orders(testnet=testnet)),
        ("market_mids", lambda: _check_market_mids(testnet=testnet)),
    ]

    for name, builder in base_checks:
        try:
            payload, check = builder()
        except Exception as exc:
            payload = None
            check = _make_check(
                name,
                "fail",
                f"{name} raised an exception",
                {"error": str(exc), "testnet": bool(testnet)},
            )
        if name == "account" and isinstance(payload, dict):
            account_payload = payload
        elif name == "positions" and isinstance(payload, dict):
            positions_payload = payload
        elif name == "open_orders" and isinstance(payload, list):
            open_orders_payload = payload
        elif name == "market_mids" and isinstance(payload, dict):
            mids_payload = payload

        checks.append(check)
        overall_status = _merge_status(overall_status, str(check.get("status") or "fail"))

    if place_test_order:
        preflight_ok = all(
            str(check.get("status") or "fail") == "ok"
            for check in checks
            if str(check.get("name") or "") in {"account", "positions", "open_orders", "market_mids"}
        )
        if not preflight_ok or positions_payload is None or open_orders_payload is None or mids_payload is None:
            active_check = _make_check(
                "execution",
                "fail",
                "Active smoke was not attempted because preflight checks failed",
                {"testnet": bool(testnet), "requested_asset": str(asset or "").strip().upper() or None},
            )
        else:
            active_check = _run_active_order_smoke(
                testnet=testnet,
                allow_mainnet=allow_mainnet,
                asset=asset,
                usd_notional=float(usd_notional),
                direction=direction,
                strategy_id=str(strategy_id or "SOAK_HL_SMOKE").strip() or "SOAK_HL_SMOKE",
                positions_payload=positions_payload,
                open_orders_payload=open_orders_payload,
                mids_payload=mids_payload,
            )
    else:
        active_check = _make_check(
            "execution",
            "ok",
            "Active order smoke skipped",
            {"requested": False, "testnet": bool(testnet)},
        )

    checks.append(active_check)
    overall_status = _merge_status(overall_status, str(active_check.get("status") or "fail"))

    execution_details = active_check.get("details", {})
    account_details = next((check.get("details", {}) for check in checks if check.get("name") == "account"), {})
    positions_details = next((check.get("details", {}) for check in checks if check.get("name") == "positions"), {})
    orders_details = next((check.get("details", {}) for check in checks if check.get("name") == "open_orders"), {})
    mids_details = next((check.get("details", {}) for check in checks if check.get("name") == "market_mids"), {})

    return {
        "generated_at": get_now().isoformat(),
        "status": overall_status,
        "summary": {
            "execution_mode": get_execution_mode(),
            "testnet": bool(testnet),
            "requested_testnet": requested_testnet,
            "mode": "active" if place_test_order else "passive",
            "requested_asset": str(asset or "").strip().upper() or None,
            "selected_asset": execution_details.get("asset"),
            "usd_notional": float(usd_notional),
            "direction": str(direction or "long").strip().lower(),
            "trade_id": execution_details.get("trade_id"),
            "account_value": account_details.get("account_value"),
            "position_count": positions_details.get("position_count"),
            "open_order_count": orders_details.get("open_order_count"),
            "mid_count": mids_details.get("mid_count"),
        },
        "checks": checks,
        "account": account_payload,
    }
