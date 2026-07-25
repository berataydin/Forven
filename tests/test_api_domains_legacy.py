from __future__ import annotations

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient

from forven.api import app


# API-05: these tests used to mount a bare ``FastAPI()`` with only the legacy
# router included. That app has NO middleware, so the `/api/forven/*` HTTP routes
# matched there and the suite certified nine handlers that production could never
# reach — ForvenV1CompatMiddleware rewrites the path to `/api/*` before routing.
# Everything below now drives the real ``forven.api:app`` so the middleware is in
# the loop, which is the only way these assertions mean anything.
@pytest.fixture
def client(forven_db):
    return TestClient(app, raise_server_exceptions=False)


def test_legacy_prefix_is_rewritten_to_the_canonical_route(client, monkeypatch):
    """`/api/forven/<x>` reaches the CANONICAL handler, not a legacy shim."""
    monkeypatch.setattr(
        "forven.control_plane.status.get_dashboard",
        lambda require_account_connection=True: {"execution_mode": "paper", "daemon_running": True},
    )

    response = client.get("/api/forven/dashboard")

    assert response.status_code == 200
    assert response.json()["execution_mode"] == "paper"
    # The middleware — not a per-route helper — stamps the sunset headers.
    assert response.headers["Deprecation"] == "true"
    assert "Sunset" in response.headers
    assert response.headers["X-Forven-Legacy-Route"] == "/api/forven/dashboard"


def test_legacy_http_routes_are_gone_from_the_router():
    """The nine unreachable `/api/forven/*` HTTP routes must not come back."""
    from forven.api import iter_effective_routes

    legacy_http = sorted(
        f"{method} {path}"
        for method, path in iter_effective_routes(app.routes)
        if path.startswith("/api/forven/") and method != "WEBSOCKET"
    )
    assert legacy_http == [], f"unreachable legacy HTTP routes re-registered: {legacy_http}"


def test_legacy_websocket_route_survives_the_middleware(client, monkeypatch):
    """WS handshakes are a `websocket` scope, so the rewrite never sees them."""

    async def _fake_legacy_websocket_endpoint(ws: WebSocket):
        await ws.accept()
        await ws.send_text("legacy-ok")
        await ws.close()

    monkeypatch.setattr(
        "forven.routers.legacy.legacy_domain.legacy_websocket_endpoint",
        _fake_legacy_websocket_endpoint,
    )

    with client.websocket_connect("/api/forven/ws/live") as websocket:
        assert websocket.receive_text() == "legacy-ok"
