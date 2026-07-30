"""Regression tests for the api-ui hardening batch.

Covers API-01 through API-10, ws-no-origin-check and OPS-4's health surface.
Every test here fails on the pre-fix code.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from forven import api_core as core
from forven.db import get_db


# --------------------------------------------------------------------------- #
# API-03 / ARCH-02: `_coerce_float` was defined twice; the lenient one won
# --------------------------------------------------------------------------- #


def test_module_level_coerce_float_is_the_strict_one():
    """A fat-fingered risk limit must keep the previous value, not be salvaged.

    Before the fix a SECOND `_coerce_float` (the legacy-metadata parser) shadowed
    this one, so "1,5" became 15.0 and "20 to 40" became 30.0 in every settings
    coercion.
    """
    assert core._coerce_float("1,5", 0.0) == 0.0
    assert core._coerce_float("20 to 40", 0.0) == 0.0
    assert core._coerce_float("52.1%", 0.0) == 0.0
    # Genuine numbers still parse.
    assert core._coerce_float("2.5", 0.0) == 2.5
    assert core._coerce_float(None, 7.0) == 7.0


def test_legacy_metadata_parser_keeps_its_salvage_heuristics():
    """The lenient parser is still there — just under a name that says where."""
    assert core._coerce_legacy_metadata_float("1,5") == 15.0
    assert core._coerce_legacy_metadata_float("-0.536 to -1.463") == pytest.approx(-0.9995)
    assert core._coerce_legacy_metadata_float("52.1%") == pytest.approx(52.1)
    assert core._coerce_legacy_metadata_float(None, None) is None


# --------------------------------------------------------------------------- #
# API-02: PUT /api/settings/{section} wrote unbounded real-money limits
# --------------------------------------------------------------------------- #


def _stored_settings() -> dict:
    from forven.db import kv_get

    return kv_get("forven:settings", {}) or {}


def test_out_of_range_risk_limit_is_refused_and_nothing_persists(forven_db):
    core._save_settings_payload(core._default_settings_payload())
    before = _stored_settings().get("max_risk_per_trade_pct")

    with pytest.raises(HTTPException) as excinfo:
        core.put_settings_section("risk", {"max_risk_per_trade_pct": 500})

    assert excinfo.value.status_code == 422
    assert "max_risk_per_trade_pct" in str(excinfo.value.detail)
    assert _stored_settings().get("max_risk_per_trade_pct") == before


def test_live_hard_ceilings_are_bounded(forven_db):
    core._save_settings_payload(core._default_settings_payload())

    with pytest.raises(HTTPException) as excinfo:
        core.put_settings_section("risk", {"live_hard_max_per_trade_risk_pct": -1})
    assert excinfo.value.status_code == 422

    with pytest.raises(HTTPException) as excinfo:
        core.put_settings_section("risk", {"max_daily_loss_pct": 101})
    assert excinfo.value.status_code == 422


def test_unparseable_number_is_refused_rather_than_salvaged(forven_db):
    core._save_settings_payload(core._default_settings_payload())

    with pytest.raises(HTTPException) as excinfo:
        core.put_settings_section("risk", {"max_daily_loss_pct": "1,5"})

    assert excinfo.value.status_code == 422
    assert "must be a number" in str(excinfo.value.detail)


def test_unknown_settings_key_is_refused_loudly(forven_db):
    """The "the setting does not stick" class: dropped silently, now a 422."""
    core._save_settings_payload(core._default_settings_payload())

    with pytest.raises(HTTPException) as excinfo:
        core.put_settings_section("risk", {"max_risk_per_trade_ptc": 1.5})

    assert excinfo.value.status_code == 422
    assert "max_risk_per_trade_ptc" in str(excinfo.value.detail)


def test_in_range_risk_write_still_lands(forven_db):
    """Full editability is deliberate — the guard must not narrow the range."""
    core._save_settings_payload(core._default_settings_payload())

    core.put_settings_section("risk", {"max_risk_per_trade_pct": 7.5})

    assert _stored_settings().get("max_risk_per_trade_pct") == 7.5


# --------------------------------------------------------------------------- #
# API-04: the duplicate-route guard inspected zero routes
# --------------------------------------------------------------------------- #


def _two_router_app(register) -> FastAPI:
    first = APIRouter()
    second = APIRouter()
    register(first)
    register(second)
    app = FastAPI()
    app.include_router(first)
    app.include_router(second)
    return app


def test_duplicate_route_guard_detects_a_duplicate_across_routers():
    from forven.api import _assert_no_duplicate_routes

    def _register(router: APIRouter) -> None:
        @router.get("/api/backtesting/run")
        def _handler():  # pragma: no cover - never called
            return {}

    app = _two_router_app(_register)

    with pytest.raises(RuntimeError) as excinfo:
        _assert_no_duplicate_routes(app)
    assert "GET /api/backtesting/run" in str(excinfo.value)


def test_duplicate_websocket_route_is_caught_too():
    """WS routes carry no `.methods`, so even a flattening FastAPI skipped them."""
    from forven.api import _assert_no_duplicate_routes

    def _register(router: APIRouter) -> None:
        @router.websocket("/api/ws/live")
        async def _handler(ws):  # pragma: no cover - never called
            return None

    app = _two_router_app(_register)

    with pytest.raises(RuntimeError) as excinfo:
        _assert_no_duplicate_routes(app)
    assert "WEBSOCKET /api/ws/live" in str(excinfo.value)


def test_duplicate_route_guard_walks_prefixed_includes():
    from forven.api import iter_effective_routes

    child = APIRouter()

    @child.get("/thing")
    def _c():  # pragma: no cover - never called
        return {}

    nested_include = SimpleNamespace(
        original_router=SimpleNamespace(routes=child.routes),
        include_context=SimpleNamespace(prefix="/nested"),
    )
    outer_include = SimpleNamespace(
        original_router=SimpleNamespace(routes=[nested_include]),
        include_context=SimpleNamespace(prefix="/api"),
    )

    assert ("GET", "/api/nested/thing") in set(iter_effective_routes([outer_include]))


def test_real_app_has_no_duplicate_routes():
    from forven.api import _assert_no_duplicate_routes, app, iter_effective_routes

    # The guard is a no-op unless it can actually see the routers' routes.
    effective = list(iter_effective_routes(app.routes))
    assert len(effective) > 100
    _assert_no_duplicate_routes(app)

    # WS routes are inside the walked set — they were invisible to the old loop
    # regardless of FastAPI version, because they have no `.methods`.
    assert ("WEBSOCKET", "/api/ws/live") in set(effective)


# --------------------------------------------------------------------------- #
# API-09 / API-10: bounded `limit`, typed factory-reset confirmation
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(forven_db):
    from forven.api import app

    return TestClient(app, raise_server_exceptions=False)


def test_negative_log_limit_is_rejected_before_the_handler(client):
    """SQLite reads a negative LIMIT as "no limit" — `?limit=-1` dumped the log."""
    assert client.get("/api/logs?limit=-1").status_code == 422
    assert client.get("/api/logs?limit=0").status_code == 422
    assert client.get("/api/logs?limit=100000").status_code == 422
    assert client.get("/api/logs?limit=50").status_code == 200


def test_recent_trades_limit_is_bounded_too(client):
    """Same class as /api/logs: db.get_recent_trades passes `limit` to "LIMIT ?",
    so `?limit=-1` streamed the WHOLE trade ledger (sizes, fills, PnL)."""
    assert client.get("/api/trades/recent?limit=-1").status_code == 422
    assert client.get("/api/trades/recent?limit=0").status_code == 422
    assert client.get("/api/trades/recent?limit=100000").status_code == 422
    # The real frontend caller (getForvenRecentTrades) uses 20.
    assert client.get("/api/trades/recent?limit=20").status_code == 200


def test_factory_reset_requires_the_typed_confirmation(client, monkeypatch):
    import forven.db as db_mod

    calls: list[dict] = []

    def _fake_factory_reset(keep_categories=None, *, allow_credentials_wipe=False):
        calls.append({"keep": keep_categories, "creds": allow_credentials_wipe})
        return {"status": "ok", "wiped": [], "kept": []}

    monkeypatch.setattr(db_mod, "factory_reset", _fake_factory_reset)

    assert client.post("/api/system/factory-reset", json={}).status_code == 422
    assert client.post("/api/system/factory-reset", json={"keep": []}).status_code == 422
    assert (
        client.post("/api/system/factory-reset", json={"confirm_phrase": "yes", "keep": []}).status_code
        == 422
    )
    assert calls == [], "a destructive wipe ran without the typed confirmation"

    response = client.post(
        "/api/system/factory-reset",
        json={"confirm_phrase": "FACTORY RESET", "keep": []},
    )
    assert response.status_code == 200
    # `[]` (wipe everything) must stay distinguishable from an absent key.
    assert calls == [{"keep": [], "creds": False}]


def test_factory_reset_absent_keep_still_means_defaults(client, monkeypatch):
    import forven.db as db_mod

    calls: list[dict] = []
    monkeypatch.setattr(
        db_mod,
        "factory_reset",
        lambda keep_categories=None, *, allow_credentials_wipe=False: (
            calls.append({"keep": keep_categories}) or {"status": "ok", "wiped": [], "kept": []}
        ),
    )

    response = client.post("/api/system/factory-reset", json={"confirm_phrase": "FACTORY RESET"})

    assert response.status_code == 200
    assert calls == [{"keep": None}]


# --------------------------------------------------------------------------- #
# API-01: lifecycle reads loaded the whole strategies table
# --------------------------------------------------------------------------- #


def _insert_strategy(strategy_id: str, **overrides) -> None:
    row = {
        "id": strategy_id,
        "name": overrides.get("name", strategy_id),
        "type": "rsi_momentum",
        "symbol": overrides.get("symbol", "ETH"),
        "timeframe": "1h",
        "params": "{}",
        "metrics": json.dumps({"sharpe_ratio": 1.0}),
        "status": overrides.get("stage", "quick_screen"),
        "owner": "brain",
        "stage": overrides.get("stage", "quick_screen"),
        "source": overrides.get("source"),
        "source_ref": overrides.get("source_ref"),
        "display_id": overrides.get("display_id"),
        "updated_at": overrides.get("updated_at", "2026-07-01T00:00:00+00:00"),
    }
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage,
             source, source_ref, display_id, stage_changed_at, created_at, updated_at)
            VALUES (:id, :name, :type, :symbol, :timeframe, :params, :metrics, :status,
                    :owner, :stage, :source, :source_ref, :display_id,
                    :updated_at, :updated_at, :updated_at)
            """,
            row,
        )


def test_single_lifecycle_read_never_loads_the_whole_table(forven_db, monkeypatch):
    import forven.strategy_lifecycle as lifecycle

    for n in range(5):
        _insert_strategy(f"s-api01-{n}", display_id=f"S9000{n}")

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on regression
        raise AssertionError("read_lifecycle_strategy re-loaded the whole strategies table")

    monkeypatch.setattr(lifecycle, "get_strategies", _boom)

    payload = lifecycle.read_lifecycle_strategy("s-api01-3")
    assert payload["strategy"]["id"] == "s-api01-3"

    by_display = lifecycle.read_lifecycle_strategy("S90002")
    assert by_display["strategy"]["id"] == "s-api01-2"

    with pytest.raises(HTTPException) as excinfo:
        lifecycle.read_lifecycle_strategy("s-api01-nope")
    assert excinfo.value.status_code == 404


def test_lifecycle_list_pushes_filters_and_window_into_sql(forven_db, monkeypatch):
    import forven.strategy_lifecycle as lifecycle

    _insert_strategy("s-btc-1", symbol="BTC", name="btc breakout", updated_at="2026-07-05T00:00:00+00:00")
    _insert_strategy("s-eth-1", symbol="ETH", name="eth carry", updated_at="2026-07-04T00:00:00+00:00")
    _insert_strategy("s-eth-2", symbol="ETH", name="eth thrust", updated_at="2026-07-03T00:00:00+00:00")
    _insert_strategy(
        "s-drop-1", symbol="SOL", name="dropzone idea", source="ai_dropzone",
        updated_at="2026-07-02T00:00:00+00:00",
    )

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on regression
        raise AssertionError("read_lifecycle_strategies re-loaded the whole strategies table")

    monkeypatch.setattr(lifecycle, "get_strategies", _boom)

    by_symbol = lifecycle.read_lifecycle_strategies(symbol="eth")
    assert [s["id"] for s in by_symbol] == ["s-eth-1", "s-eth-2"]

    by_name = lifecycle.read_lifecycle_strategies(name="thrust")
    assert [s["id"] for s in by_name] == ["s-eth-2"]

    by_source = lifecycle.read_lifecycle_strategies(source="ai_dropzone")
    assert [s["id"] for s in by_source] == ["s-drop-1"]

    # A NULL source still reads as "core" — the pre-fix Python filter did too.
    by_core = lifecycle.read_lifecycle_strategies(source="core")
    assert [s["id"] for s in by_core] == ["s-btc-1", "s-eth-1", "s-eth-2"]

    # source_ref falls back to the id when the column is empty.
    assert [s["id"] for s in lifecycle.read_lifecycle_strategies(source_ref="eth-2")] == ["s-eth-2"]

    # Window semantics are unchanged: newest first, offset clamps at 0.
    assert [s["id"] for s in lifecycle.read_lifecycle_strategies(limit=2)] == ["s-btc-1", "s-eth-1"]
    assert [s["id"] for s in lifecycle.read_lifecycle_strategies(limit=2, offset=2)] == [
        "s-eth-2",
        "s-drop-1",
    ]
    assert lifecycle.read_lifecycle_strategies(limit=0) == []
    assert len(lifecycle.read_lifecycle_strategies(limit=-1)) == 4


# --------------------------------------------------------------------------- #
# ws-no-origin-check: the WS handshake enforced the key but never the Origin
# --------------------------------------------------------------------------- #


class _FakeWs:
    """Minimal HTTPConnection stand-in for the handshake guard."""

    def __init__(self, headers: dict[str, str], scheme: str = "ws"):
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.url = type("U", (), {"scheme": scheme})()
        self.query_params: dict[str, str] = {}
        self.closed_with: int | None = None

    async def close(self, code: int = 1000):
        self.closed_with = code


@pytest.fixture(autouse=True)
def _clean_origin_env(monkeypatch):
    monkeypatch.delenv("FORVEN_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("FORVEN_CSRF_PROTECT", raising=False)


def test_cross_site_ws_origin_is_flagged(monkeypatch):
    from forven.api_security import is_cross_site_ws_handshake

    hostile = _FakeWs({"host": "127.0.0.1:8003", "origin": "https://evil.example"})
    assert is_cross_site_ws_handshake(hostile) is True


def test_same_origin_and_allowlisted_ws_origins_pass(monkeypatch):
    from forven.api_security import is_cross_site_ws_handshake

    # Same origin: the browser stamps http:// while the socket scheme is ws://.
    same = _FakeWs({"host": "127.0.0.1:8003", "origin": "http://127.0.0.1:8003"})
    assert is_cross_site_ws_handshake(same) is False

    monkeypatch.setenv("FORVEN_CORS_ORIGINS", "http://localhost:5173")
    dev = _FakeWs({"host": "127.0.0.1:8003", "origin": "http://localhost:5173"})
    assert is_cross_site_ws_handshake(dev) is False


def test_absent_ws_origin_is_allowed_for_non_browser_clients():
    from forven.api_security import is_cross_site_ws_handshake

    cli = _FakeWs({"host": "127.0.0.1:8003"})
    assert is_cross_site_ws_handshake(cli) is False


def test_ws_handshake_closes_on_a_hostile_origin_even_without_an_api_key(monkeypatch):
    from forven.api_security import require_api_access_ws

    monkeypatch.delenv("FORVEN_API_KEY", raising=False)
    hostile = _FakeWs({"host": "127.0.0.1:8003", "origin": "https://evil.example"})

    allowed = asyncio.run(require_api_access_ws(hostile))

    assert allowed is False
    assert hostile.closed_with == 1008


def test_ws_handshake_still_allows_the_local_ui(monkeypatch):
    from forven.api_security import require_api_access_ws

    monkeypatch.delenv("FORVEN_API_KEY", raising=False)
    local = _FakeWs({"host": "127.0.0.1:8003", "origin": "http://127.0.0.1:8003"})

    assert asyncio.run(require_api_access_ws(local)) is True
    assert local.closed_with is None


# --------------------------------------------------------------------------- #
# API-08: threadpool handlers must not drive the API loop with asyncio.run
# --------------------------------------------------------------------------- #


def test_ws_broadcast_from_a_worker_thread_hops_to_the_api_loop(monkeypatch):
    delivered: list[dict] = []

    class _Manager:
        async def broadcast(self, message):
            delivered.append(message)

    monkeypatch.setattr("forven.api_domains.live_ws.ws_manager", _Manager())

    async def _drive():
        monkeypatch.setattr(core, "_API_EVENT_LOOP", asyncio.get_running_loop())
        scheduled: list[bool] = []

        def _worker():
            scheduled.append(core.dispatch_ws_broadcast({"type": "certification_change"}))

        # A real OS thread — the whole point is that the handler is NOT on the loop.
        worker = threading.Thread(target=_worker)
        worker.start()
        await asyncio.to_thread(worker.join, 5.0)
        # Let the queued callback and the task it creates run.
        for _ in range(5):
            await asyncio.sleep(0.01)
        return scheduled

    scheduled = asyncio.run(_drive())

    assert scheduled == [True]
    assert delivered == [{"type": "certification_change"}]


def test_ws_broadcast_without_an_api_loop_reports_failure_instead_of_raising(monkeypatch):
    class _Manager:
        async def broadcast(self, message):  # pragma: no cover - must never run
            raise AssertionError("broadcast attempted with no API loop")

    monkeypatch.setattr("forven.api_domains.live_ws.ws_manager", _Manager())
    monkeypatch.setattr(core, "_API_EVENT_LOOP", None)

    assert core.dispatch_ws_broadcast({"type": "certification_change"}) is False


# --------------------------------------------------------------------------- #
# OPS-4: the real-money arming flag was invisible
# --------------------------------------------------------------------------- #


def test_mainnet_arming_is_surfaced(forven_db, monkeypatch):
    monkeypatch.setenv("FORVEN_ALLOW_MAINNET", "1")
    assert core.mainnet_arming_snapshot()["armed"] is True
    assert core.get_settings()["mainnet_armed"] is True

    monkeypatch.delenv("FORVEN_ALLOW_MAINNET", raising=False)
    assert core.mainnet_arming_snapshot()["armed"] is False
    assert core.get_settings()["mainnet_armed"] is False


def test_health_check_reports_mainnet_arming(forven_db, monkeypatch):
    """The health surface is where an operator looks to answer "is this instance
    able to spend real money?" — it could not answer that at all."""
    from forven.control_plane import status as cp_status

    monkeypatch.setenv("FORVEN_ALLOW_MAINNET", "1")
    armed = cp_status.health_check()
    assert armed["mainnet_armed"] is True
    assert armed["mainnet_arming"]["flag"] == "FORVEN_ALLOW_MAINNET"
    # The runtime summary keys still come through untouched.
    assert "issues" in armed and "status" in armed

    monkeypatch.delenv("FORVEN_ALLOW_MAINNET", raising=False)
    assert cp_status.health_check()["mainnet_armed"] is False


def test_diagnostics_snapshot_reports_mainnet_arming(monkeypatch):
    from forven import diagnostics

    monkeypatch.setattr(diagnostics, "run_all_checks", lambda: [])
    monkeypatch.setattr(diagnostics, "_mcp_servers_section", lambda: [])

    monkeypatch.setenv("FORVEN_ALLOW_MAINNET", "true")
    assert diagnostics.snapshot()["mainnet_armed"] is True

    monkeypatch.delenv("FORVEN_ALLOW_MAINNET", raising=False)
    payload = diagnostics.snapshot()
    assert payload["mainnet_armed"] is False
    assert payload["mainnet_arming"]["armed"] is False


def test_diagnostics_mainnet_section_never_takes_the_snapshot_down(monkeypatch):
    """Fail-soft: a broken exchange import must not break `forven doctor`."""
    from forven import api_core, diagnostics

    def _boom():
        raise RuntimeError("exchange module exploded")

    monkeypatch.setattr(api_core, "mainnet_arming_snapshot", _boom)
    monkeypatch.setenv("FORVEN_ALLOW_MAINNET", "1")

    section = diagnostics._mainnet_arming_section()
    assert section["armed"] is True
    assert section["source"] == "env_fallback"
