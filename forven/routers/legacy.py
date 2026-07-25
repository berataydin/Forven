from fastapi import APIRouter, Depends, WebSocket

from forven.api_domains import legacy as legacy_domain
from forven.api_security import require_operator_access

# API-05: this router used to also carry nine `/api/forven/*` HTTP routes. Every
# one of them was DEAD: ForvenV1CompatMiddleware (api_core) rewrites
# ``/api/forven/<x>`` to ``/api/<x>`` in the request scope before routing, so a
# handler registered under ``/api/forven/*`` can never match. Their tests passed
# only because they mounted a bare FastAPI() with no middleware. They are gone;
# the canonical `/api/*` routes serve those callers, and the middleware still
# stamps the Deprecation/Sunset/X-Forven-Legacy-Route headers on the way out.
#
# The two WEBSOCKET routes below stay, and are the reason this module still
# exists: BaseHTTPMiddleware only sees ``http`` scopes, so the rewrite never
# touches a WS handshake and these paths ARE reachable.
router = APIRouter(tags=["legacy"], dependencies=[Depends(require_operator_access)])


@router.websocket("/api/forven/ws/live")
@router.websocket("/forven/ws/live")
async def legacy_websocket_endpoint(ws: WebSocket):
    legacy_domain.log.warning(
        "Legacy websocket route used: /api/forven/ws/live (scheduled sunset %s)",
        legacy_domain.LEGACY_API_SUNSET_DATE,
    )
    await legacy_domain.legacy_websocket_endpoint(ws)
