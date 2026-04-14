"""Unit tests for ws_client.py (no live orchestrator required)."""
import json
import pytest

from station import StationState
from ws_client import build_status_listener, StationWsClient
from ws_protocol import decode_inbound


class _StubConfig:
    def __init__(self, url=None, station_id="station-1"):
        self.orchestrator_url = url
        self.station_id = station_id

    @property
    def orchestrator_enabled(self) -> bool:
        return bool(self.orchestrator_url and self.orchestrator_url.strip())


def test_standalone_listener_is_noop():
    cfg = _StubConfig(url=None)
    listener = build_status_listener(cfg)
    # Must not raise and must not start a thread
    state = StationState(station_id="station-1", status="free")
    listener(state)
    assert not hasattr(listener, "_client")  # no client attached in standalone mode


def test_empty_string_url_is_standalone():
    cfg = _StubConfig(url="")
    listener = build_status_listener(cfg)
    state = StationState(station_id="station-1", status="processing")
    listener(state)  # no crash
    assert not hasattr(listener, "_client")


def test_enabled_listener_creates_client_and_coalesces(monkeypatch):
    cfg = _StubConfig(url="ws://127.0.0.1:1/ws", station_id="alpha")
    listener = build_status_listener(cfg)
    client: StationWsClient = listener._client
    # Prevent the thread from actually starting (we only check enqueue logic)
    client._started = True
    # Manually init what start_in_thread would have set up
    import asyncio
    client._loop = asyncio.new_event_loop()
    client._state_event = asyncio.Event()
    try:
        listener(StationState(station_id="alpha", status="processing", cycle_count=1))
        # Drain pending tasks
        client._loop.call_soon(client._loop.stop)
        client._loop.run_forever()
        assert client._pending_msg is not None
        parsed = decode_inbound(client._pending_msg)
        assert parsed.status == "processing"
        assert parsed.cycle_count == 1
        # Send a second state — pending msg must be REPLACED (coalesce)
        listener(StationState(station_id="alpha", status="free", cycle_count=2))
        client._loop.call_soon(client._loop.stop)
        client._loop.run_forever()
        parsed2 = decode_inbound(client._pending_msg)
        assert parsed2.status == "free"
        assert parsed2.cycle_count == 2
    finally:
        client._loop.close()


def test_url_gets_role_query_appended():
    cfg = _StubConfig(url="ws://1.2.3.4:8000/ws", station_id="x")
    listener = build_status_listener(cfg)
    assert "role=station" in listener._client.url


def test_backoff_schedule():
    from ws_client import _BACKOFF_SCHEDULE
    assert _BACKOFF_SCHEDULE == [1.0, 2.0, 4.0, 8.0]
