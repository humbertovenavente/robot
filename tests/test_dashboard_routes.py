"""Dashboard route + static file tests."""
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


def test_dashboard_html_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert "LEGO Package Sorter" in r.text


def test_dashboard_js_served(client):
    r = client.get("/static/dashboard.js")
    assert r.status_code == 200
    assert "WebSocket" in r.text
    assert "role=dashboard" in r.text


def test_dashboard_css_served(client):
    r = client.get("/static/dashboard.css")
    assert r.status_code == 200
    assert ".badge" in r.text


def test_dashboard_receives_initial_snapshot_and_live_update(client):
    with client.websocket_connect("/ws?role=station") as station:
        station.send_text(encode(RegisterMsg(station_id="alpha")))
        station.send_text(encode(StatusMsg(
            station_id="alpha", status="free",
            last_class="A", last_destination=1, last_cycle_ms=3200, cycle_count=1,
        )))
        with client.websocket_connect("/ws?role=dashboard") as dash:
            snap = json.loads(dash.receive_text())
            assert snap["type"] == "station_update"
            assert snap["station_id"] == "alpha"
            # Live update
            station.send_text(encode(StatusMsg(
                station_id="alpha", status="processing", cycle_count=1,
            )))
            live = json.loads(dash.receive_text())
            assert live["status"] == "processing"
