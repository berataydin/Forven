"""Propr.xyz prop-firm execution adapter (PROPR-1).

Propr (https://propr.xyz) is an on-chain prop firm built ON Hyperliquid: the
operator buys a challenge, gets a funded account (``accountId``), and trades
real Hyperliquid markets through Propr's REST API (``X-API-Key`` auth). This
module mirrors the function surface of ``forven.exchange.hyperliquid`` so the
scanner's execution choke point (``_execute_direct``) can dispatch to either
venue — same kwargs, same return-payload contract (``entry_price``,
``order_ids``, ``fill_price_unknown``, ``filled_size``, ...).

Safety model — Propr has NO testnet, every order spends real challenge money:

* ``forven.config.propr_enabled()`` — the hidden integration flag. It is
  deliberately absent from the settings manifest/UI; an operator must know to
  set ``FORVEN_PROPR_ENABLED=1`` (or hand-edit config.json). Gates the nav
  page, the /api/propr routes, and venue selection.
* ``_assert_propr_execution_allowed()`` — every order-placing/cancelling call
  additionally requires ``FORVEN_ALLOW_PROPR_LIVE=1``, the direct analog of
  the FORVEN_ALLOW_MAINNET guard. Read-only calls (positions/attempts/status)
  deliberately do NOT require it.
* Sim redirect — an active sim clock routes to the shared mock exchange
  exactly like the Hyperliquid adapter, so paper/sim can never reach Propr.

Venue quirks (from github.com/XBorgLabs/propr-docs):
* Client order ids are ULIDs (``intentId``); batches need an ``orderGroupId``
  ULID; conditional (stop/TP) orders need an existing ``positionId`` OR must
  ride in the entry's order group.
* ``reduceOnly: true`` is MANDATORY on closes — omitting it opens a reverse
  position instead of closing.
* No market-data endpoints: mids come from Hyperliquid MAINNET (read-only —
  Propr fills happen on real HL markets, so HL mainnet marks ARE the truth).
* Quantities/prices travel as decimal strings; crypto assets are bare tickers
  (BTC), HIP-3 assets use an ``xyz:`` prefix (unsupported here for now — HL
  meta can't quantize them, so they fail closed).
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time

import requests

from forven.circuit_breaker import propr_account_breaker, propr_trade_breaker
from forven.config import load_config, propr_enabled
from forven.db import kv_get

log = logging.getLogger("forven.exchange.propr")

DEFAULT_API_BASE = "https://api.propr.xyz/v1"

# 4xx = the request was bad but the service is healthy; only 5xx/transport
# failures trip the breakers. These statuses get a bounded no-trip retry,
# mirroring the HL transient-retry stance (a gateway blip must not open the
# breaker and halt trading).
_TRANSIENT_STATUSES = {429, 502, 503, 504}
_REQUEST_TIMEOUT_S = 15.0
_RETRY_BASE_DELAY_S = 0.5

# Post-create fill confirmation: a Propr market order fills on HL within
# moments, but the create response may still say "pending". Bounded poll so
# the scanner gets a real averageFillPrice instead of fill_price_unknown.
_FILL_POLL_ATTEMPTS = 6
_FILL_POLL_DELAY_S = 1.0

# Crockford base32 (ULID alphabet): no I, L, O, U.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_ACTIVE_ATTEMPT_STATUSES = {"active", "in_progress", "ongoing", "funded", "passed", "open"}

_account_lock = threading.Lock()
_account_cache: dict = {"account_id": None, "attempt_id": None, "at": 0.0}
_ACCOUNT_CACHE_TTL_S = 300.0


class ProprApiError(RuntimeError):
    """A Propr API call failed. ``status_code`` 0 = transport/local failure."""

    def __init__(self, status_code: int, message: str, payload=None):
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.payload = payload


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def allow_live() -> bool:
    """The explicit real-money opt-in (FORVEN_ALLOW_MAINNET analog)."""
    return _is_truthy(os.environ.get("FORVEN_ALLOW_PROPR_LIVE"))


def _assert_propr_execution_allowed() -> None:
    """Single chokepoint guarding every Propr order-placing/cancelling call.

    Two ways through (read-only functions deliberately do NOT call this):

    * FORVEN_ALLOW_PROPR_LIVE=1 — the explicit real-money opt-in, or
    * the account is VERIFIABLY a paper/trial account right now: Propr reports
      ``account.type`` on the challenge attempt ("paper" during a free-trial
      evaluation), re-verified at most _ACCOUNT_TYPE_CACHE_TTL_S old. The
      moment Propr flips the account to a funded type — the evaluation ending
      is exactly when it "becomes real" — this bypass dies and every order
      fails closed until the operator sets the env opt-in. A failed or
      ambiguous type read also fails closed.
    """
    if not propr_enabled():
        raise RuntimeError(
            "Refusing Propr order: the Propr integration is not enabled "
            "(FORVEN_PROPR_ENABLED is unset). This hidden flag is intentional — "
            "see forven/exchange/propr.py."
        )
    if allow_live():
        return
    account_type = get_account_type()
    if account_type == "paper":
        return
    raise RuntimeError(
        "Refusing to place a Propr order: the account is not verifiably a "
        f"paper/trial account (exchange-reported type={account_type!r}) and "
        "FORVEN_ALLOW_PROPR_LIVE is not set. Once a challenge account is real, "
        "orders need that explicit opt-in on top of FORVEN_PROPR_ENABLED."
    )


_account_type_cache: dict = {"type": None, "at": 0.0}
_ACCOUNT_TYPE_CACHE_TTL_S = 300.0


def get_account_type(force_refresh: bool = False) -> str | None:
    """The exchange-reported Propr account type ('paper' during a trial).

    Cached briefly; a stale cache is NEVER trusted for the paper bypass — an
    expired entry re-reads, and a failed re-read returns None (fail closed).
    """
    now = time.time()
    if (
        not force_refresh
        and _account_type_cache["type"] is not None
        and (now - _account_type_cache["at"]) < _ACCOUNT_TYPE_CACHE_TTL_S
    ):
        return _account_type_cache["type"]
    try:
        _, attempt_id = resolve_account()
        attempt = get_challenge_attempt(attempt_id) if attempt_id else {}
        account = attempt.get("account")
        raw = str(account.get("type") or "").strip().lower() if isinstance(account, dict) else ""
        if raw:
            _account_type_cache.update({"type": raw, "at": now})
            return raw
        return None
    except ProprApiError as exc:
        log.warning("Could not verify Propr account type (fail closed): %s", exc)
        return None


# ---------------------------------------------------------------------------
# ULIDs
# ---------------------------------------------------------------------------

def _encode_crockford(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid() -> str:
    """A fresh random ULID (48-bit ms timestamp + 80 random bits)."""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode_crockford(ts, 10) + _encode_crockford(rand, 16)


def deterministic_ulid(key: str) -> str:
    """A stable ULID-shaped id derived from an idempotency key.

    Propr's ``intentId`` must be a ULID, but its idempotency only helps if a
    retry sends the SAME id — so the scanner's ``{trade_id}:open`` style keys
    are hashed into all 26 characters (timestamp bits included; Propr
    validates the format, not the embedded time). Same key => same intentId
    => a network-retry can never double-fill an order.
    """
    digest = hashlib.sha256(str(key).encode("utf-8")).digest()
    ts = int.from_bytes(digest[:6], "big")
    rand = int.from_bytes(digest[6:16], "big")
    return _encode_crockford(ts, 10) + _encode_crockford(rand, 16)


# ---------------------------------------------------------------------------
# Credentials / HTTP
# ---------------------------------------------------------------------------

def _settings() -> dict:
    try:
        raw = kv_get("forven:settings", {})
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def get_api_key() -> str:
    """Resolve the Propr API key: env first, then the encrypted secrets store."""
    env = str(os.environ.get("FORVEN_PROPR_API_KEY", "") or "").strip()
    if env:
        return env
    try:
        secrets_blob = kv_get("forven:settings:secrets", {}) or {}
    except Exception:
        return ""
    if not isinstance(secrets_blob, dict):
        return ""
    raw = str(secrets_blob.get("propr_api_key", "") or "").strip()
    if not raw:
        return ""
    try:
        from forven.secret_storage import decrypt_secret
        return decrypt_secret(raw).strip()
    except Exception as exc:
        log.warning("Could not decrypt stored Propr API key: %s", exc)
        return ""


def get_base_url() -> str:
    env = str(os.environ.get("FORVEN_PROPR_API_BASE", "") or "").strip()
    if env:
        return env.rstrip("/")
    try:
        cfg_value = str(load_config().get("propr_api_base", "") or "").strip()
    except Exception:
        cfg_value = ""
    return (cfg_value or DEFAULT_API_BASE).rstrip("/")


def _request(method: str, path: str, *, breaker, body=None, params=None,
             timeout: float = _REQUEST_TIMEOUT_S, retries: int = 2):
    """Breaker-guarded Propr REST call. Raises ProprApiError on any failure."""
    key = get_api_key()
    if not key:
        raise ProprApiError(
            0,
            "Propr API key is not configured — add it on the Propr page or set "
            "FORVEN_PROPR_API_KEY.",
        )
    url = f"{get_base_url()}{path}"
    for attempt in range(retries + 1):
        if not breaker.can_execute():
            raise ProprApiError(0, f"circuit breaker '{breaker.name}' is open")
        try:
            resp = requests.request(
                method,
                url,
                headers={"X-API-Key": key, "Content-Type": "application/json"},
                json=body,
                params=params,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            breaker.record_failure()
            raise ProprApiError(0, f"Propr API unreachable ({method} {path}): {exc}") from exc
        if resp.status_code in _TRANSIENT_STATUSES and attempt < retries:
            time.sleep(_RETRY_BASE_DELAY_S * (attempt + 1))
            continue
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if resp.status_code >= 400:
            message = None
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("error")
            message = message or (resp.text or "")[:300] or f"HTTP {resp.status_code}"
            if resp.status_code >= 500:
                breaker.record_failure()
            else:
                # 4xx: the service answered — a bad request must not open the
                # breaker and take down healthy order flow.
                breaker.record_success()
            raise ProprApiError(resp.status_code, f"{method} {path}: {message}", payload)
        breaker.record_success()
        return payload if payload is not None else {}
    raise ProprApiError(0, f"{method} {path}: retries exhausted")


def _rows(payload) -> list[dict]:
    """Unwrap a list response that may arrive bare or under data/orders/... keys."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "orders", "positions", "trades",
                    "attempts", "challenges", "challengeAttempts"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        # single-object response
        return [payload]
    return []


def _fmt_decimal(value: float) -> str:
    """Propr takes quantities/prices as decimal strings; never scientific."""
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return text or "0"


# ---------------------------------------------------------------------------
# Account / challenge-attempt resolution
# ---------------------------------------------------------------------------

def list_challenges() -> list[dict]:
    return _rows(_request("GET", "/challenges", breaker=propr_account_breaker))


def list_challenge_attempts() -> list[dict]:
    return _rows(_request("GET", "/challenge-attempts", breaker=propr_account_breaker))


def get_challenge_attempt(attempt_id: str) -> dict:
    payload = _request(
        "GET", f"/challenge-attempts/{attempt_id}", breaker=propr_account_breaker
    )
    return payload if isinstance(payload, dict) else {}


def _attempt_field(attempt: dict, *names) -> str | None:
    for name in names:
        value = attempt.get(name)
        if value:
            return str(value)
    return None


def _attempt_account_id(attempt: dict) -> str | None:
    direct = _attempt_field(attempt, "accountId", "account_id")
    if direct:
        return direct
    account = attempt.get("account")
    if isinstance(account, dict):
        return _attempt_field(account, "id", "accountId")
    return None


def _attempt_status(attempt: dict) -> str:
    return str(attempt.get("status") or "").strip().lower()


def resolve_account(force_refresh: bool = False) -> tuple[str, str | None]:
    """Resolve (account_id, attempt_id) for trading.

    Order: FORVEN_PROPR_ACCOUNT_ID env / settings override, then the ACTIVE
    challenge attempt from the API (cached 5 min). Raises ProprApiError when
    no tradable account exists — the caller must fail closed.
    """
    override = str(os.environ.get("FORVEN_PROPR_ACCOUNT_ID", "") or "").strip() or \
        str(_settings().get("propr_account_id", "") or "").strip()
    if override:
        return override, None

    with _account_lock:
        fresh = (time.time() - _account_cache["at"]) < _ACCOUNT_CACHE_TTL_S
        if not force_refresh and fresh and _account_cache["account_id"]:
            return _account_cache["account_id"], _account_cache["attempt_id"]

    attempts = list_challenge_attempts()

    def _created_at(a: dict) -> str:
        return _attempt_field(a, "createdAt", "created_at", "startedAt") or ""

    attempts = sorted(attempts, key=_created_at, reverse=True)
    active = [
        a for a in attempts
        if _attempt_status(a) in _ACTIVE_ATTEMPT_STATUSES and _attempt_account_id(a)
    ]
    chosen = active[0] if active else next(
        (a for a in attempts if _attempt_account_id(a)), None
    )
    if chosen is None:
        raise ProprApiError(
            0,
            "No Propr challenge attempt with an accountId — purchase a challenge "
            "at app.propr.xyz first.",
        )
    account_id = _attempt_account_id(chosen)
    attempt_id = _attempt_field(chosen, "id", "attemptId", "attempt_id")
    if not active:
        log.warning(
            "Propr: no ACTIVE challenge attempt; using most recent attempt %s "
            "(status=%s) — orders will likely be rejected if it has ended.",
            attempt_id, _attempt_status(chosen),
        )
    with _account_lock:
        _account_cache.update(
            {"account_id": account_id, "attempt_id": attempt_id, "at": time.time()}
        )
    return account_id, attempt_id


# ---------------------------------------------------------------------------
# Market data / quantization (delegated to Hyperliquid MAINNET — same markets)
# ---------------------------------------------------------------------------

def get_all_mids(testnet: bool = True) -> dict[str, float]:
    """Mids from Hyperliquid MAINNET. ``testnet`` is accepted for signature
    parity with the HL adapter and ignored — Propr fills happen on real HL
    markets, so mainnet marks are the only correct reference (read-only
    mainnet reads are explicitly allowed by the HL guard)."""
    from forven.exchange.hyperliquid import get_all_mids as hl_get_all_mids
    return hl_get_all_mids(testnet=False)


def _mainnet_url() -> str:
    from hyperliquid.utils import constants
    return constants.MAINNET_API_URL


def _quantize_size(asset: str, size: float) -> float:
    from forven.exchange.hyperliquid import quantize_size
    return quantize_size(asset, size, _mainnet_url())


def _round_price(price: float, asset: str) -> float:
    from forven.exchange.hyperliquid import round_to_tick
    return round_to_tick(price, asset, _mainnet_url())


def normalize_asset(asset: str) -> str:
    """Bare crypto tickers uppercase; xyz:-prefixed HIP-3 names pass through."""
    cleaned = str(asset or "").strip()
    if cleaned.lower().startswith("xyz:"):
        return "xyz:" + cleaned[4:].upper()
    return cleaned.upper()


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _order_id(order: dict) -> str | None:
    value = order.get("orderId") or order.get("order_id") or order.get("id")
    return str(value) if value is not None else None


def _order_status(order: dict) -> str:
    return str(order.get("status") or "").strip().lower()


def _order_fill_price(order: dict) -> float | None:
    for key in ("averageFillPrice", "average_fill_price", "avgFillPrice", "fillPrice"):
        value = order.get(key)
        if value not in (None, "", "0"):
            try:
                fill = float(value)
            except (TypeError, ValueError):
                continue
            if fill > 0:
                return fill
    return None


def _order_filled_size(order: dict) -> float | None:
    for key in ("cumulativeQuantity", "cumulative_quantity", "filledQuantity", "executedQuantity"):
        value = order.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def list_orders(limit: int | None = None) -> list[dict]:
    account_id, _ = resolve_account()
    params = {"limit": limit} if limit else None
    return _rows(_request(
        "GET", f"/accounts/{account_id}/orders",
        breaker=propr_account_breaker, params=params,
    ))


def list_trades(limit: int | None = None) -> list[dict]:
    account_id, _ = resolve_account()
    params = {"limit": limit} if limit else None
    return _rows(_request(
        "GET", f"/accounts/{account_id}/trades",
        breaker=propr_account_breaker, params=params,
    ))


def _poll_order_fill(account_id: str, order_id: str) -> dict | None:
    """Bounded poll for a just-created order's fill fields."""
    for _ in range(_FILL_POLL_ATTEMPTS):
        time.sleep(_FILL_POLL_DELAY_S)
        try:
            orders = _rows(_request(
                "GET", f"/accounts/{account_id}/orders",
                breaker=propr_account_breaker,
            ))
        except ProprApiError as exc:
            log.debug("Propr fill poll failed for %s: %s", order_id, exc)
            continue
        match = next((o for o in orders if _order_id(o) == str(order_id)), None)
        if match is None:
            continue
        status = _order_status(match)
        if status in ("filled", "partially_filled") and _order_fill_price(match):
            return match
        if status in ("rejected", "cancelled", "canceled"):
            return match
    return None


def _create_orders(account_id: str, orders: list[dict]) -> list[dict]:
    body: dict = {"orders": orders}
    payload = _request(
        "POST", f"/accounts/{account_id}/orders",
        breaker=propr_trade_breaker, body=body,
    )
    return _rows(payload)


def _match_created(created: list[dict], intent_ids: dict[str, str],
                   labels: list[str]) -> dict[str, dict]:
    """Map created-order rows back to entry/stop/take_profit labels.

    Prefer intentId echo; fall back to submission order (the API creates
    orders in request order)."""
    by_label: dict[str, dict] = {}
    for label in labels:
        intent = intent_ids.get(label)
        match = next(
            (o for o in created
             if intent and str(o.get("intentId") or o.get("intent_id") or "") == intent),
            None,
        )
        if match is not None:
            by_label[label] = match
    if not by_label and len(created) == len(labels):
        by_label = dict(zip(labels, created))
    return by_label


def market_order(
    asset: str, side: str, size: float,
    stop_loss_price: float | None = None,
    take_profit_price: float | None = None,
    idempotency_key: str | None = None,
    testnet: bool = True,
    vault_address: str | None = None,
) -> dict:
    """Place a Propr market order with optional stop/TP legs (one order group).

    Mirrors the HL adapter's return contract so the scanner's fill/order-id
    extraction works unchanged. ``testnet`` is ignored (Propr has none — the
    _assert guard is the real gate); ``vault_address`` is unsupported (a Propr
    challenge is a single account, no sub-account routing).
    """
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_market_order
        return sim_market_order(asset, side, size, stop_loss_price, take_profit_price)

    _assert_propr_execution_allowed()
    if vault_address:
        return {"error": "Propr does not support sub-account routing (vault_address)"}

    asset_n = normalize_asset(asset)
    is_buy = str(side).upper() in ("B", "BUY", "LONG")
    position_side = "long" if is_buy else "short"

    size = _quantize_size(asset_n, size)
    if size <= 0:
        return {"error": f"order size for {asset_n} rounds below the lot size (szDecimals)"}

    mid = float(get_all_mids().get(asset_n, 0) or 0)
    if mid == 0:
        return {"error": f"Could not get mid price for {asset_n}"}

    # LIQ-1 mirror: Propr fills execute on the real Hyperliquid mainnet book,
    # so the same pre-trade liquidity guard applies (volume floor + spread +
    # depth participation + walk-the-book impact; fails closed on missing data).
    from forven.exchange.liquidity import check_order_liquidity
    liq_ok, liq_reason = check_order_liquidity(asset_n, is_buy, size, mid)
    if not liq_ok:
        log.warning("Propr %s %s open blocked by liquidity guard: %s", asset_n, side, liq_reason)
        return {"error": liq_reason, "liquidity_blocked": True}

    # SIZE-1 mirror: refuse a wrong-side protective stop (see HL adapter).
    if stop_loss_price and (
        (is_buy and stop_loss_price >= mid) or ((not is_buy) and stop_loss_price <= mid)
    ):
        return {
            "error": (
                f"refusing inverted stop-loss for {asset_n}: sl={stop_loss_price} is not on "
                f"the loss side of entry ~{mid} (is_buy={is_buy})"
            )
        }

    account_id, _ = resolve_account()
    key_root = idempotency_key or new_ulid()
    intent_ids = {"entry": deterministic_ulid(f"{key_root}:entry")}
    quantity = _fmt_decimal(size)

    entry = {
        "intentId": intent_ids["entry"],
        "asset": asset_n,
        "type": "market",
        "side": "buy" if is_buy else "sell",
        "positionSide": position_side,
        "quantity": quantity,
        "reduceOnly": False,
    }
    orders = [entry]
    order_labels = ["entry"]

    if stop_loss_price:
        intent_ids["stop"] = deterministic_ulid(f"{key_root}:stop")
        orders.append({
            "intentId": intent_ids["stop"],
            "asset": asset_n,
            "type": "stop_market",
            "side": "sell" if is_buy else "buy",
            "positionSide": position_side,
            "quantity": quantity,
            "triggerPrice": _fmt_decimal(_round_price(float(stop_loss_price), asset_n)),
            "reduceOnly": True,
        })
        order_labels.append("stop")

    if take_profit_price:
        intent_ids["take_profit"] = deterministic_ulid(f"{key_root}:tp")
        orders.append({
            "intentId": intent_ids["take_profit"],
            "asset": asset_n,
            "type": "take_profit_market",
            "side": "sell" if is_buy else "buy",
            "positionSide": position_side,
            "quantity": quantity,
            "triggerPrice": _fmt_decimal(_round_price(float(take_profit_price), asset_n)),
            "reduceOnly": True,
        })
        order_labels.append("take_profit")

    if len(orders) > 1:
        group_id = deterministic_ulid(f"{key_root}:group")
        for order in orders:
            order["orderGroupId"] = group_id

    try:
        created = _create_orders(account_id, orders)
    except ProprApiError as exc:
        return {"error": f"Propr order rejected: {exc}"}

    by_label = _match_created(created, intent_ids, order_labels)
    entry_row = by_label.get("entry")
    if entry_row is None or _order_id(entry_row) is None:
        return {
            "error": "Propr order create returned no entry order id",
            "raw_response": created,
        }
    if _order_status(entry_row) in ("rejected", "cancelled", "canceled"):
        return {
            "error": f"Propr entry order {_order_status(entry_row)}",
            "raw_response": created,
        }

    entry_order_id = _order_id(entry_row)
    fill = _order_fill_price(entry_row)
    filled_size = _order_filled_size(entry_row)
    if fill is None:
        polled = _poll_order_fill(account_id, entry_order_id)
        if polled is not None:
            if _order_status(polled) in ("rejected", "cancelled", "canceled"):
                return {"error": f"Propr entry order {_order_status(polled)} after submit"}
            fill = _order_fill_price(polled)
            filled_size = _order_filled_size(polled) or filled_size

    order_ids = {
        label: _order_id(row) for label, row in by_label.items() if _order_id(row)
    }
    protective_leg_failed = [
        label for label in order_labels
        if label in ("stop", "take_profit") and label not in order_ids
    ]

    payload = {
        "venue": "propr",
        "account_id": account_id,
        "mid": mid,
        "entry_price": fill if fill is not None else mid,
        "requested_size": size,
        "filled_size": filled_size if filled_size is not None else size,
        "stop_loss": stop_loss_price,
        "take_profit": take_profit_price,
        "order_ids": order_ids,
        "client_order_ids": {k: v for k, v in intent_ids.items()},
        "entry_order_id": entry_order_id,
        "order_id": entry_order_id,
    }
    if "stop" in order_ids:
        payload["stop_order_id"] = order_ids["stop"]
    if "take_profit" in order_ids:
        payload["take_profit_order_id"] = order_ids["take_profit"]
    if protective_leg_failed:
        payload["protective_leg_failed"] = protective_leg_failed
        log.error(
            "Propr %s %s entry accepted but leg(s) %s missing from response — "
            "caller must arm them", asset_n, side, protective_leg_failed,
        )
    if fill is None:
        payload["fill_price_unknown"] = True
    return payload


def limit_order(
    asset: str, side: str, size: float, price: float,
    stop_loss_price: float | None = None,
    take_profit_price: float | None = None,
    tif: str = "Gtc",
    idempotency_key: str | None = None,
    testnet: bool = True,
    vault_address: str | None = None,
) -> dict:
    """Propr limit order (GTC/IOC/FOK/GTX map from the HL tif values)."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_market_order
        return sim_market_order(asset, side, size, stop_loss_price, take_profit_price)

    _assert_propr_execution_allowed()
    if vault_address:
        return {"error": "Propr does not support sub-account routing (vault_address)"}

    asset_n = normalize_asset(asset)
    is_buy = str(side).upper() in ("B", "BUY", "LONG")
    size = _quantize_size(asset_n, size)
    if size <= 0:
        return {"error": f"order size for {asset_n} rounds below the lot size (szDecimals)"}

    account_id, _ = resolve_account()
    key_root = idempotency_key or new_ulid()
    tif_map = {"gtc": "GTC", "ioc": "IOC", "fok": "FOK", "alo": "GTX", "gtx": "GTX"}
    order = {
        "intentId": deterministic_ulid(f"{key_root}:entry"),
        "asset": asset_n,
        "type": "limit",
        "side": "buy" if is_buy else "sell",
        "positionSide": "long" if is_buy else "short",
        "quantity": _fmt_decimal(size),
        "price": _fmt_decimal(_round_price(float(price), asset_n)),
        "timeInForce": tif_map.get(str(tif).strip().lower(), "GTC"),
        "reduceOnly": False,
    }
    try:
        created = _create_orders(account_id, [order])
    except ProprApiError as exc:
        return {"error": f"Propr limit order rejected: {exc}"}
    row = created[0] if created else {}
    order_id = _order_id(row)
    if order_id is None:
        return {"error": "Propr limit order create returned no order id", "raw_response": created}
    return {
        "venue": "propr",
        "account_id": account_id,
        "order_id": order_id,
        "entry_order_id": order_id,
        "order_ids": {"entry": order_id},
        "requested_size": size,
        "price": price,
        "status": _order_status(row),
    }


def cancel_order(asset: str, oid, testnet: bool = True, vault_address: str | None = None) -> dict:
    """Cancel by orderId. A 400 means already filled/cancelled — surfaced, not raised."""
    _assert_propr_execution_allowed()
    account_id, _ = resolve_account()
    try:
        _request(
            "POST", f"/accounts/{account_id}/orders/{oid}/cancel",
            breaker=propr_trade_breaker,
        )
        return {"cancelled": True, "order_id": str(oid)}
    except ProprApiError as exc:
        if exc.status_code == 400:
            return {"cancelled": False, "already_filled_or_cancelled": True, "order_id": str(oid)}
        return {"error": str(exc), "order_id": str(oid)}


# ---------------------------------------------------------------------------
# Positions / protective legs
# ---------------------------------------------------------------------------

def _position_quantity(position: dict) -> float:
    for key in ("quantity", "size", "szi"):
        value = position.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def _position_side(position: dict) -> str:
    side = str(position.get("positionSide") or position.get("side") or "").strip().lower()
    if side in ("long", "short"):
        return side
    return "long" if _position_quantity(position) >= 0 else "short"


def _position_id(position: dict) -> str | None:
    value = position.get("positionId") or position.get("position_id") or position.get("id")
    return str(value) if value is not None else None


def raw_positions() -> list[dict]:
    """Open Propr positions, zero-quantity rows filtered (docs: closed
    positions may linger with quantity "0" and status "open")."""
    account_id, _ = resolve_account()
    rows = _rows(_request(
        "GET", f"/accounts/{account_id}/positions",
        breaker=propr_account_breaker,
    ))
    return [p for p in rows if abs(_position_quantity(p)) > 0]


def get_positions(testnet: bool = True, *, account_address: str | None = None) -> dict:
    """HL-shaped positions payload ({"positions": [...], "marginSummary": {...}}).

    Each row keeps the raw Propr fields and adds a "coin" alias so venue-
    agnostic consumers (e.g. the scanner's best-effort funding read) can match
    by asset without knowing the venue."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_get_positions
        return sim_get_positions()
    positions = []
    for p in raw_positions():
        row = dict(p)
        row.setdefault("coin", normalize_asset(str(p.get("asset") or "")))
        positions.append(row)
    return {"positions": positions, "marginSummary": {}}


def get_account_value(
    testnet: bool = True, require_connection: bool = False, *, account_address: str | None = None
) -> dict:
    """Challenge-account equity from the attempt details (HL-shaped payload).

    Field names are matched permissively — the docs don't pin the balance key.
    Returns {"accountValue": float|None, ...}; raises only when
    require_connection=True and the read failed (mirrors the HL contract the
    book-equity reader relies on)."""
    try:
        _, attempt_id = resolve_account()
        attempt = get_challenge_attempt(attempt_id) if attempt_id else {}
        value = None
        for key in ("currentBalance", "current_balance", "currentEquity", "equity",
                    "accountValue", "balance"):
            raw = attempt.get(key)
            if raw not in (None, ""):
                try:
                    value = float(raw)
                    break
                except (TypeError, ValueError):
                    continue
        if value is None:
            account = attempt.get("account")
            if isinstance(account, dict):
                # marginBalance = wallet balance + unrealized PnL — the true
                # current equity (verified against a live attempt payload);
                # bare "balance" is the realized wallet only.
                for key in ("marginBalance", "margin_balance", "equity", "balance", "currentBalance"):
                    raw = account.get(key)
                    if raw not in (None, ""):
                        try:
                            value = float(raw)
                            break
                        except (TypeError, ValueError):
                            continue
        if value is None and require_connection:
            raise ProprApiError(0, "Propr attempt details carry no recognizable balance field")
        return {"accountValue": value, "attempt": attempt, "venue": "propr"}
    except ProprApiError:
        if require_connection:
            raise
        return {"accountValue": None, "venue": "propr"}


def _find_position(asset: str, position_direction: str) -> dict | None:
    asset_n = normalize_asset(asset)
    want = str(position_direction or "").strip().lower()
    want = "long" if want in ("long", "buy", "b") else "short"
    for p in raw_positions():
        if normalize_asset(str(p.get("asset") or p.get("coin") or "")) != asset_n:
            continue
        if _position_side(p) == want:
            return p
    return None


def _place_conditional(
    asset: str, position_direction: str, size: float, trigger_price: float,
    order_type: str, label: str,
) -> dict:
    """Shared stop_market / take_profit_market placement against an existing
    position (Propr requires the positionId for standalone conditionals)."""
    _assert_propr_execution_allowed()
    asset_n = normalize_asset(asset)
    is_long = str(position_direction).strip().lower() in ("long", "buy", "b")
    # The caller typically arms a leg moments after the entry filled; the
    # position row can lag the fill, so retry briefly before failing.
    position = None
    for attempt in range(3):
        position = _find_position(asset_n, "long" if is_long else "short")
        if position is not None:
            break
        if attempt < 2:
            time.sleep(1.0)
    if position is None or _position_id(position) is None:
        return {"error": f"no open Propr {asset_n} {'long' if is_long else 'short'} position to protect"}
    size = _quantize_size(asset_n, size)
    if size <= 0:
        return {"error": f"protective size for {asset_n} rounds below the lot size"}
    account_id, _ = resolve_account()
    order = {
        "intentId": new_ulid(),
        "asset": asset_n,
        "type": order_type,
        "side": "sell" if is_long else "buy",
        "positionSide": "long" if is_long else "short",
        "positionId": _position_id(position),
        "quantity": _fmt_decimal(size),
        "triggerPrice": _fmt_decimal(_round_price(float(trigger_price), asset_n)),
        "reduceOnly": True,
    }
    try:
        created = _create_orders(account_id, [order])
    except ProprApiError as exc:
        return {"error": f"Propr {label} rejected: {exc}"}
    row = created[0] if created else {}
    order_id = _order_id(row)
    if order_id is None:
        return {"error": f"Propr {label} create returned no order id", "raw_response": created}
    return {f"{label}_order_id": order_id, "order_id": order_id, "venue": "propr"}


def place_protective_stop(
    asset: str, position_direction: str, size: float, stop_loss_price: float,
    testnet: bool = True, vault_address: str | None = None,
) -> dict:
    result = _place_conditional(
        asset, position_direction, size, stop_loss_price, "stop_market", "stop"
    )
    return result


def place_take_profit(
    asset: str, position_direction: str, size: float, take_profit_price: float,
    testnet: bool = True, vault_address: str | None = None,
) -> dict:
    return _place_conditional(
        asset, position_direction, size, take_profit_price,
        "take_profit_market", "take_profit",
    )


def close_position(
    asset: str, size: float, side: str = "sell", testnet: bool = True,
    vault_address: str | None = None, *, slippage_bps: float | None = None,
) -> dict:
    """Reduce-only market close. ``reduceOnly: true`` is load-bearing — without
    it Propr opens a REVERSE position instead of closing (docs' #1 footgun).
    ``slippage_bps`` is accepted for signature parity; a Propr market order has
    no client-side price cap to widen."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_close_position
        return sim_close_position(asset, size, side)

    _assert_propr_execution_allowed()
    asset_n = normalize_asset(asset)
    is_buy = str(side).strip().lower() in ("b", "buy")
    # Closing with a BUY reduces a SHORT; closing with a SELL reduces a LONG.
    position_side = "short" if is_buy else "long"

    raw_size = float(size)
    size = _quantize_size(asset_n, raw_size)
    if size <= 0:
        # Reduce-only: attempting beats refusing (a refusal strands a live
        # position) — same stance as the HL adapter.
        if raw_size > 0:
            log.warning("Propr close %s: szDecimals unknown; attempting raw size %s", asset_n, raw_size)
            size = raw_size
        else:
            return {"error": f"close size for {asset_n} is non-positive"}

    mid = float(get_all_mids().get(asset_n, 0) or 0)
    account_id, _ = resolve_account()
    order = {
        "intentId": new_ulid(),
        "asset": asset_n,
        "type": "market",
        "side": "buy" if is_buy else "sell",
        "positionSide": position_side,
        "quantity": _fmt_decimal(size),
        "reduceOnly": True,
    }
    try:
        created = _create_orders(account_id, [order])
    except ProprApiError as exc:
        return {"error": f"Propr close rejected: {exc}"}
    row = created[0] if created else {}
    order_id = _order_id(row)
    if order_id is None:
        return {"error": "Propr close create returned no order id", "raw_response": created}

    fill = _order_fill_price(row)
    filled_size = _order_filled_size(row)
    if fill is None:
        polled = _poll_order_fill(account_id, order_id)
        if polled is not None:
            fill = _order_fill_price(polled)
            filled_size = _order_filled_size(polled) or filled_size

    return {
        "venue": "propr",
        "account_id": account_id,
        "mid": mid,
        "close_price": fill if fill is not None else (mid or None),
        "exit_price": fill,
        "requested_size": float(size),
        "filled_size": filled_size,
        "order_id": order_id,
        "exit_order_id": order_id,
        "order_ids": {"exit": order_id},
    }


# ---------------------------------------------------------------------------
# Leverage / margin config
# ---------------------------------------------------------------------------

_leverage_limits_cache: dict = {"limits": None, "at": 0.0}
_LEVERAGE_CACHE_TTL_S = 3600.0


def _effective_leverage_limit(asset: str) -> float | None:
    """Max leverage for an asset from /leverage-limits/effective (cached 1h).
    None = endpoint unavailable or asset not listed (caller falls back to the
    documented defaults: 5x BTC/ETH, 2x everything else)."""
    now = time.time()
    limits = _leverage_limits_cache["limits"]
    if limits is None or (now - _leverage_limits_cache["at"]) > _LEVERAGE_CACHE_TTL_S:
        try:
            payload = _request(
                "GET", "/leverage-limits/effective", breaker=propr_account_breaker
            )
            table: dict[str, float] = {}
            for row in _rows(payload):
                name = normalize_asset(str(row.get("asset") or row.get("symbol") or ""))
                raw = row.get("maxLeverage") or row.get("max_leverage") or row.get("leverage")
                if name and raw not in (None, ""):
                    try:
                        table[name] = float(raw)
                    except (TypeError, ValueError):
                        continue
            limits = table
            _leverage_limits_cache.update({"limits": limits, "at": now})
        except ProprApiError as exc:
            log.debug("Propr leverage-limits read failed: %s", exc)
            limits = _leverage_limits_cache["limits"] or {}
    return (limits or {}).get(normalize_asset(asset))


_DOCUMENTED_LEVERAGE_CAPS = {"BTC": 5.0, "ETH": 5.0}
_DEFAULT_LEVERAGE_CAP = 2.0


def set_leverage(
    asset: str, leverage: float, testnet: bool = True,
    vault_address: str | None = None, is_cross: bool | None = None,
) -> dict:
    """Set leverage via the margin-config endpoints, clamped to the venue cap.

    Returns {"leverage": applied, "clamped": bool} or {"error": ...} — the
    scanner fails the open on error (opening at unknown leverage invalidates
    the stop math, same stance as the HL B2 guard)."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        return {"leverage": leverage, "sim": True}

    _assert_propr_execution_allowed()
    asset_n = normalize_asset(asset)
    requested = max(1.0, float(leverage))
    cap = _effective_leverage_limit(asset_n)
    if cap is None:
        cap = _DOCUMENTED_LEVERAGE_CAPS.get(asset_n, _DEFAULT_LEVERAGE_CAP)
    applied = min(requested, float(cap))

    account_id, _ = resolve_account()
    try:
        config = _request(
            "GET", f"/accounts/{account_id}/margin-config/{asset_n}",
            breaker=propr_account_breaker,
        )
    except ProprApiError as exc:
        return {"error": f"Propr margin-config read failed for {asset_n}: {exc}"}
    config = config if isinstance(config, dict) else {}
    config_id = config.get("configId") or config.get("config_id") or config.get("id")
    if config_id is None:
        return {"error": f"Propr margin-config for {asset_n} carries no configId"}

    current_leverage = None
    try:
        current_leverage = float(config.get("leverage"))
    except (TypeError, ValueError):
        pass
    margin_mode = str(config.get("marginMode") or config.get("margin_mode") or "").lower()
    desired_mode = margin_mode
    if is_cross is not None:
        desired_mode = "cross" if is_cross else "isolated"

    if current_leverage == applied and (not desired_mode or desired_mode == margin_mode):
        return {"leverage": applied, "clamped": applied < requested, "unchanged": True}

    body: dict = {"leverage": applied}
    if desired_mode:
        body["marginMode"] = desired_mode
    try:
        _request(
            "PUT", f"/accounts/{account_id}/margin-config/{config_id}",
            breaker=propr_trade_breaker, body=body,
        )
    except ProprApiError as exc:
        return {"error": f"Propr set_leverage failed for {asset_n}: {exc}"}
    if applied < requested:
        log.info(
            "Propr leverage clamped for %s: requested %sx, venue cap %sx",
            asset_n, requested, applied,
        )
    return {"leverage": applied, "clamped": applied < requested}


# ---------------------------------------------------------------------------
# Status (page / nav support)
# ---------------------------------------------------------------------------

def get_user() -> dict:
    payload = _request("GET", "/users/me", breaker=propr_account_breaker)
    return payload if isinstance(payload, dict) else {}


def get_status(include_remote: bool = True) -> dict:
    """Integration status for the API/UI. Safe to call in every state — with
    the hidden flag off it reports only {"enabled": False} so the endpoint
    leaks nothing about the integration to a casual caller."""
    enabled = propr_enabled()
    if not enabled:
        return {"enabled": False}
    status: dict = {
        "enabled": True,
        "allow_live": allow_live(),
        "api_key_configured": bool(get_api_key()),
        "base_url": get_base_url(),
    }
    if not (include_remote and status["api_key_configured"]):
        status["connected"] = False
        return status
    try:
        user = get_user()
        status["user_id"] = user.get("userId") or user.get("id")
        try:
            account_id, attempt_id = resolve_account()
            status["account_id"] = account_id
            status["attempt_id"] = attempt_id
            account = get_account_value()
            status["account_value"] = account.get("accountValue")
            attempt = account.get("attempt") or {}
            if isinstance(attempt, dict) and attempt:
                status["attempt_status"] = attempt.get("status")
            status["account_type"] = get_account_type()
            # Orders place when the operator opted in OR the account is a
            # verifiable paper/trial account — the page renders this truth.
            status["orders_allowed"] = bool(
                status["allow_live"] or status["account_type"] == "paper"
            )
        except ProprApiError as exc:
            status["account_error"] = str(exc)
        status["connected"] = True
    except ProprApiError as exc:
        status["connected"] = False
        status["connection_error"] = str(exc)
    return status
