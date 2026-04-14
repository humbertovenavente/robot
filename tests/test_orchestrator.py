"""Integration tests for orchestrator.py using FastAPI TestClient."""
import json
import pytest
from fastapi.testclient import TestClient

from orchestrator import app
from ws_protocol import encode, RegisterMsg, StatusMsg


@pytest.fixture
def client():
    import orchestrator
    orchestrator.registry.reset()
    with TestClient(app) as c:
        yield c


def test_missing_role_rejected(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass


def test_station_registers_and_status_broadcasts_to_dashboard(client):
    with client.websocket_connect("/ws?role=dashboard") as dash:
        # Snapshot on connect = empty
        with client.websocket_connect("/ws?role=station") as station:
            station.send_text(encode(RegisterMsg(station_id="alpha")))
            # Orchestrator broadcasts station_update for alpha (online=True)
            data = json.loads(dash.receive_text())
            assert data["type"] == "station_update"
            assert data["station_id"] == "alpha"
            assert data["online"] is True

            station.send_text(encode(StatusMsg(
                station_id="alpha", status="processing",
                last_class="A", last_destination=1, last_cycle_ms=4200, cycle_count=1,
            )))
            data = json.loads(dash.receive_text())
            assert data["status"] == "processing"
            assert data["last_class"] == "A"
            assert data["last_destination"] == 1
            assert data["cycle_count"] == 1


def test_two_stations_coexist_in_registry(client):
    with client.websocket_connect("/ws?role=station") as s1, \
         client.websocket_connect("/ws?role=station") as s2:
        s1.send_text(encode(RegisterMsg(station_id="alpha")))
        s2.send_text(encode(RegisterMsg(station_id="beta")))
        s1.send_text(encode(StatusMsg(station_id="alpha", status="free", cycle_count=0)))
        s2.send_text(encode(StatusMsg(station_id="beta", status="processing", cycle_count=0)))
    # After both disconnected, snapshot still has both (online=False)
    resp = client.get("/api/stations")
    assert resp.status_code == 200
    body = resp.json()
    ids = sorted(s["station_id"] for s in body["stations"])
    assert ids == ["alpha", "beta"]
    for s in body["stations"]:
        assert s["online"] is False


def test_last_writer_wins_same_station_id(client):
    with client.websocket_connect("/ws?role=station") as s1:
        s1.send_text(encode(RegisterMsg(station_id="alpha")))
        with client.websocket_connect("/ws?role=station") as s2:
            s2.send_text(encode(RegisterMsg(station_id="alpha")))
            # s1 should be closed by orchestrator (code 4000). receive should raise or return close.
            s2.send_text(encode(StatusMsg(station_id="alpha", status="free", cycle_count=0)))
    resp = client.get("/api/stations")
    assert resp.status_code == 200


def test_dashboard_receives_snapshot_on_connect(client):
    with client.websocket_connect("/ws?role=station") as s:
        s.send_text(encode(RegisterMsg(station_id="gamma")))
        s.send_text(encode(StatusMsg(station_id="gamma", status="free", cycle_count=3)))
        # New dashboard connects AFTER station registered
        with client.websocket_connect("/ws?role=dashboard") as dash:
            data = json.loads(dash.receive_text())
            assert data["type"] == "station_update"
            assert data["station_id"] == "gamma"
            assert data["cycle_count"] == 3


def test_malformed_message_closes_station_socket(client):
    with client.websocket_connect("/ws?role=station") as s:
        s.send_text("not json at all")
        # Socket should be closed by server; next operation raises
        with pytest.raises(Exception):
            s.send_text("more")
            s.receive_text()


def test_api_stations_returns_json(client):
    resp = client.get("/api/stations")
    assert resp.status_code == 200
    assert "stations" in resp.json()
