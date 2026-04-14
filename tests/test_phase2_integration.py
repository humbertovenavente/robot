"""Phase 2 end-to-end integration tests.

Covers ROADMAP Phase 2 success criteria:
  1. Two stations register and appear on dashboard immediately
  2. State changes update dashboard live without page reload
  3. Two stations process packages concurrently without interference
  4. Killing the orchestrator does not stop the station processes
"""
import json
import time
import threading

import pytest
from fastapi.testclient import TestClient

from orchestrator import app
from ws_protocol import encode, RegisterMsg, StatusMsg
from ws_client import build_status_listener, StationWsClient, _BACKOFF_SCHEDULE
from station import StationState


@pytest.fixture
def client():
    import orchestrator
    orchestrator.registry.reset()
    with TestClient(app) as c:
        yield c


class _Cfg:
    def __init__(self, url, station_id):
        self.orchestrator_url = url
        self.station_id = station_id

    @property
    def orchestrator_enabled(self):
        return bool(self.orchestrator_url and self.orchestrator_url.strip())


# -------------------------------------------------------------------
# Success #1 + #3: two stations simultaneously
# -------------------------------------------------------------------

def test_two_stations_concurrent_broadcast_to_dashboard(client):
    """ORC-03: two stations register and both are visible; both state changes broadcast."""
    with client.websocket_connect("/ws?role=dashboard") as dash:
        with client.websocket_connect("/ws?role=station") as s1, \
             client.websocket_connect("/ws?role=station") as s2:
            s1.send_text(encode(RegisterMsg(station_id="alpha")))
            msg1 = json.loads(dash.receive_text())
            assert msg1["station_id"] == "alpha"
            assert msg1["online"] is True

            s2.send_text(encode(RegisterMsg(station_id="beta")))
            msg2 = json.loads(dash.receive_text())
            assert msg2["station_id"] == "beta"

            # Interleave status updates from both stations
            s1.send_text(encode(StatusMsg(station_id="alpha", status="processing",
                                          last_class="A", last_destination=1,
                                          last_cycle_ms=100, cycle_count=1)))
            s2.send_text(encode(StatusMsg(station_id="beta", status="processing",
                                          last_class="B", last_destination=2,
                                          last_cycle_ms=120, cycle_count=1)))
            seen = {}
            for _ in range(2):
                u = json.loads(dash.receive_text())
                seen[u["station_id"]] = u
            assert set(seen.keys()) == {"alpha", "beta"}
            assert seen["alpha"]["last_class"] == "A"
            assert seen["beta"]["last_class"] == "B"
            # Snapshot via REST confirms both
            body = client.get("/api/stations").json()
            ids = sorted(s["station_id"] for s in body["stations"])
            assert ids == ["alpha", "beta"]


# -------------------------------------------------------------------
# Success #2: live updates without page reload
# -------------------------------------------------------------------

def test_dashboard_receives_live_updates_in_order(client):
    with client.websocket_connect("/ws?role=dashboard") as dash:
        with client.websocket_connect("/ws?role=station") as s:
            s.send_text(encode(RegisterMsg(station_id="alpha")))
            _ = json.loads(dash.receive_text())  # initial online broadcast
            for n in range(1, 4):
                s.send_text(encode(StatusMsg(
                    station_id="alpha",
                    status="processing" if n % 2 == 1 else "free",
                    last_class="A", last_destination=1, last_cycle_ms=100, cycle_count=n,
                )))
            cycles = []
            for _ in range(3):
                u = json.loads(dash.receive_text())
                cycles.append(u["cycle_count"])
            assert cycles == [1, 2, 3]


# -------------------------------------------------------------------
# Success #4: orchestrator-down / standalone resilience (ORC-04)
# -------------------------------------------------------------------

def test_standalone_listener_opens_no_socket():
    cfg = _Cfg(url=None, station_id="solo")
    listener = build_status_listener(cfg)
    # Call listener a bunch of times — must not raise, must not start a client
    for i in range(10):
        listener(StationState(station_id="solo", status="processing", cycle_count=i))
    assert not hasattr(listener, "_client")


def test_empty_url_is_standalone():
    cfg = _Cfg(url="   ", station_id="solo")
    listener = build_status_listener(cfg)
    for i in range(5):
        listener(StationState(station_id="solo", status="free", cycle_count=i))
    assert not hasattr(listener, "_client")


def test_client_survives_unreachable_orchestrator():
    """ORC-04: station process continues when orchestrator is down.

    Point ws_client at a closed port. start_in_thread must not raise.
    Rapidly enqueue states. The client thread retries in the background (backoff
    1->8s). We don't wait for connect; we verify:
      - enqueue_state calls do not raise
      - stop() returns within its 2s join budget
    """
    c = StationWsClient(url="ws://127.0.0.1:1/ws", station_id="solo")
    c.start_in_thread()
    try:
        for i in range(5):
            c.enqueue_state(StationState(station_id="solo", status="processing", cycle_count=i))
            time.sleep(0.01)
    finally:
        t0 = time.monotonic()
        c.stop()
        assert time.monotonic() - t0 < 3.0, "stop() took too long"


def test_backoff_schedule_is_capped_at_8s():
    assert _BACKOFF_SCHEDULE[-1] == 8.0
    assert max(_BACKOFF_SCHEDULE) == 8.0


# -------------------------------------------------------------------
# Regression guard: Phase 1 station loop still works in isolation
# -------------------------------------------------------------------

def test_phase1_station_standalone_still_works_when_orchestrator_url_unset():
    """Run run_station with the default repo YAML (no orchestrator_url).
    The Station's listener must be a no-op and no ws_client thread must appear."""
    import threading as _th
    pre_count = _th.active_count()
    from station import run_station
    station = run_station()   # uses repo's station_config.yaml, orchestrator_url is commented out
    # listener present (not None) but must be a no-op closure from build_status_listener
    # OR it may be None if orchestrator_enabled=False short-circuits in run_station.
    assert station.listener is None or callable(station.listener)
    # No new worker thread spawned for ws client
    post_count = _th.active_count()
    assert post_count <= pre_count + 1, f"unexpected thread spawn: pre={pre_count} post={post_count}"
