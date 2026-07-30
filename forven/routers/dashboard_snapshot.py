"""Dashboard snapshot API — serves the cached system-truth payload.

The handler is intentionally trivial: it returns the immutable snapshot
assembled by the background producer (forven/dashboard_snapshot.py) and never
runs a section builder, so it stays cheap even when data sources are slow.
"""

from fastapi import APIRouter

from forven import dashboard_snapshot

router = APIRouter(tags=["dashboard-snapshot"])


@router.get("/api/dashboard/snapshot")
def get_dashboard_snapshot():
    return dashboard_snapshot.get_snapshot()
