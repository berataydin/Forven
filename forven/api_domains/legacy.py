import logging

from fastapi import WebSocket

from forven.routers import websockets as websockets_routes

log = logging.getLogger("forven.legacy_api")
LEGACY_API_SUNSET_DATE = "2026-06-30"
LEGACY_API_SUNSET_HTTP = "Tue, 30 Jun 2026 00:00:00 GMT"

# API-05: the `/api/forven/*` HTTP compatibility handlers that used to live here
# (legacy_forven_get and the eight typed delegates) were unreachable dead code —
# ForvenV1CompatMiddleware rewrites the path to `/api/*` before routing, so the
# routes that called them never matched. They were deleted along with those
# routes. Only the WS delegate survives: BaseHTTPMiddleware never sees a
# `websocket` scope, so the legacy WS paths are genuinely still live.
#
# LEGACY_API_SUNSET_* stay here because the middleware's response headers and the
# WS deprecation warning both quote them.


async def legacy_websocket_endpoint(ws: WebSocket):
    await websockets_routes.websocket_endpoint(ws)


__all__ = [
    "LEGACY_API_SUNSET_DATE",
    "LEGACY_API_SUNSET_HTTP",
    "legacy_websocket_endpoint",
    "log",
]
