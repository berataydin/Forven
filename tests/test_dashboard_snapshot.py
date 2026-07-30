"""Contract tests for the dashboard snapshot (forven/dashboard_snapshot.py).

Locked contract v1 (2026-07-30): cached-only handler, independently failing
sections, stale-while-error with last-good retention, unknown-is-null-never-
zero, thresholds served in the payload, derived inbox with deterministic ids.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import forven.dashboard_snapshot as snap


@pytest.fixture(autouse=True)
def _reset_snapshot_state():
    """Snapshot state is module-level by design; isolate it per test."""
    def _clear():
        with snap._STATE_LOCK:
            for runtime in snap._SECTIONS.values():
                runtime.data = None
                runtime.as_of = None
                runtime.last_attempt_at = None
                runtime.error_code = None
                runtime.next_refresh_monotonic = 0.0
            snap._INBOX_STATE.data = None
            snap._INBOX_STATE.as_of = None
            snap._INBOX_STATE.last_attempt_at = None
            snap._INBOX_STATE.error_code = None
            snap._INBOX_OBSERVED.clear()
            snap._GENERATED_AT = None

    _clear()
    yield
    _clear()


def _stub_all_builders(monkeypatch, value=None):
    for name in snap.SECTION_BUILDERS:
        monkeypatch.setitem(snap.SECTION_BUILDERS, name, lambda name=name: value or {"section": name})


def _seed_section(name: str, data: dict) -> None:
    with snap._STATE_LOCK:
        runtime = snap._SECTIONS[name]
        runtime.data = data
        runtime.as_of = snap._utc_now_iso()
        runtime.last_attempt_at = runtime.as_of
        runtime.error_code = None


class TestSnapshotContract:
    def test_payload_shape_and_policies(self, monkeypatch):
        _stub_all_builders(monkeypatch)
        snap.refresh_all_sections_once()
        payload = snap.get_snapshot()

        assert payload["contract_version"] == 1
        assert payload["generated_at"] is not None
        assert payload["served_at"] is not None
        assert set(payload["sections"]) == set(snap.SECTION_POLICIES)
        # The UI must never invent freshness rules — thresholds ship in-band.
        assert set(payload["policies"]) == set(snap.SECTION_POLICIES)
        for policy in payload["policies"].values():
            assert policy["refresh_seconds"] > 0
            assert policy["stale_after_seconds"] > 0
        for section in payload["sections"].values():
            assert section["status"] == "fresh"
            assert section["as_of"] is not None
            assert section["error_code"] is None
        assert payload["inbox"]["status"] == "fresh"

    def test_one_failed_section_cannot_fail_the_snapshot(self, monkeypatch):
        _stub_all_builders(monkeypatch)
        monkeypatch.setitem(
            snap.SECTION_BUILDERS, "trading", lambda: (_ for _ in ()).throw(ValueError("boom"))
        )
        snap.refresh_all_sections_once()
        payload = snap.get_snapshot()

        trading = payload["sections"]["trading"]
        # Never produced data → unavailable, with the safe error code.
        assert trading["status"] == "unavailable"
        assert trading["error_code"] == "ValueError"
        assert trading["data"] is None
        assert payload["sections"]["system"]["status"] == "fresh"

    def test_failed_refresh_retains_last_good_data(self, monkeypatch):
        _stub_all_builders(monkeypatch)
        snap.refresh_all_sections_once()

        monkeypatch.setitem(
            snap.SECTION_BUILDERS, "trading", lambda: (_ for _ in ()).throw(TimeoutError("slow"))
        )
        assert snap.refresh_section("trading") is False
        section = snap.get_snapshot()["sections"]["trading"]

        assert section["status"] == "error"
        assert section["error_code"] == "TimeoutError"
        # Stale-while-error: the last good payload survives the failure.
        assert section["data"] == {"section": "trading"}
        assert section["as_of"] is not None
        assert section["last_attempt_at"] >= section["as_of"]

    def test_old_data_reports_stale(self, monkeypatch):
        _stub_all_builders(monkeypatch)
        snap.refresh_all_sections_once()
        from datetime import timedelta, timezone, datetime

        with snap._STATE_LOCK:
            snap._SECTIONS["system"].as_of = (
                datetime.now(timezone.utc)
                - timedelta(seconds=snap.SECTION_POLICIES["system"].stale_after_seconds + 5)
            ).isoformat()
        assert snap.get_snapshot()["sections"]["system"]["status"] == "stale"


class TestKpisTruthRules:
    def test_unknown_numerics_are_null_with_reasons(self, forven_db):
        assert snap.refresh_section("kpis") is True
        data = snap.get_snapshot()["sections"]["kpis"]["data"]

        # The legacy overview endpoint serializes these as reassuring zeros;
        # the snapshot must say "unknown", not "0".
        assert data["kpis"]["signals_today"] is None
        assert data["kpis"]["data_coverage"] is None
        assert data["blocked_count"] is None
        assert set(data["unknown_fields"]) == {
            "signals_today",
            "data_coverage",
            "blocked_count",
        }

    def test_kpis_builder_never_writes_daemon_state(self, forven_db, monkeypatch):
        import forven.runtime_health as runtime_health

        calls: list[dict] = []
        original = runtime_health.normalize_daemon_state

        def _spy(*args, **kwargs):
            calls.append(kwargs)
            return original(*args, **kwargs)

        monkeypatch.setattr(runtime_health, "normalize_daemon_state", _spy)
        assert snap.refresh_section("kpis") is True
        assert calls, "kpis builder should read daemon state"
        for kwargs in calls:
            assert kwargs.get("write_back") is False


class TestInbox:
    def test_halt_items_are_critical_and_actionable(self):
        _seed_section(
            "trading",
            {"risk": {"kill_switch_active": True, "daily_loss_halt": True, "drawdown_pct": 0.02}},
        )
        snap._rederive_inbox()
        items = snap.get_snapshot()["inbox"]["data"]["items"]

        ids = [item["id"] for item in items]
        assert ids == ["halt:daily_loss", "halt:kill_switch"]
        for item in items:
            assert item["severity"] == "critical"
            # Acceptance gate: critical inbox items always carry an action.
            assert item["action_label"]
            assert item["action_href"]
            assert item["first_observed_at"]
            assert item["last_observed_at"]

    def test_severity_ordering_and_deterministic_ids(self):
        _seed_section("trading", {"risk": {"kill_switch_active": True}})
        _seed_section("approvals", {"pending_count": 2, "items": []})
        snap._rederive_inbox()
        items = snap.get_snapshot()["inbox"]["data"]["items"]

        assert [item["id"] for item in items] == ["halt:kill_switch", "approvals:pending"]
        assert items[0]["severity"] == "critical"
        assert items[1]["severity"] == "warning"
        assert "2 approvals" in items[1]["message"]

    def test_first_observed_survives_rederive_and_resolution_drops_item(self):
        _seed_section("trading", {"risk": {"kill_switch_active": True}})
        snap._rederive_inbox()
        first = snap.get_snapshot()["inbox"]["data"]["items"][0]["first_observed_at"]

        snap._rederive_inbox()
        again = snap.get_snapshot()["inbox"]["data"]["items"][0]
        assert again["first_observed_at"] == first
        assert again["last_observed_at"] >= first

        # Condition clears → item resolves by source state, not dismissal.
        _seed_section("trading", {"risk": {"kill_switch_active": False}})
        snap._rederive_inbox()
        assert snap.get_snapshot()["inbox"]["data"]["items"] == []
        assert "halt:kill_switch" not in snap._INBOX_OBSERVED

    def test_stalled_agents_and_dead_letters_surface(self):
        _seed_section("system", {"queues": {"agent_stale_running": 2, "agent_pending": 5}})
        _seed_section(
            "kpis",
            {"autopilot": {"running": True, "dead_letter_jobs": 3, "last_tick_error": None}},
        )
        snap._rederive_inbox()
        ids = {item["id"] for item in snap.get_snapshot()["inbox"]["data"]["items"]}
        assert "agents:stale_running" in ids
        assert "tasks:dead_letters" in ids

    def test_pipeline_needs_you_items(self):
        _seed_section(
            "pipeline",
            {
                "needs_attention": [
                    {"strategy_id": "s-1", "name": "Alpha", "unblock_action": "Approve promotion"}
                ]
            },
        )
        snap._rederive_inbox()
        items = snap.get_snapshot()["inbox"]["data"]["items"]
        assert items[0]["id"] == "pipeline:needs_you:s-1"
        assert "Approve promotion" in items[0]["message"]
        assert items[0]["action_href"] == "/pipeline"


class TestEndpoint:
    @pytest.fixture
    def client(self, forven_db):
        from forven.api import app

        return TestClient(app, raise_server_exceptions=False)

    def test_endpoint_serves_cache_and_never_runs_builders(self, client, monkeypatch):
        _stub_all_builders(monkeypatch)
        snap.refresh_all_sections_once()

        # If the handler ran a builder, this would raise and 500.
        for name in snap.SECTION_BUILDERS:
            monkeypatch.setitem(
                snap.SECTION_BUILDERS,
                name,
                lambda: (_ for _ in ()).throw(AssertionError("handler must not run builders")),
            )

        response = client.get("/api/dashboard/snapshot")
        assert response.status_code == 200
        payload = response.json()
        assert payload["contract_version"] == 1
        assert payload["sections"]["system"]["data"] == {"section": "system"}

        # Cached payload is identical across reads (modulo served_at).
        second = client.get("/api/dashboard/snapshot").json()
        first_sections = payload["sections"]
        assert second["sections"] == first_sections
        assert second["generated_at"] == payload["generated_at"]

    def test_endpoint_before_first_refresh_reports_unavailable(self, client):
        response = client.get("/api/dashboard/snapshot")
        assert response.status_code == 200
        payload = response.json()
        for section in payload["sections"].values():
            assert section["status"] == "unavailable"
            assert section["data"] is None
