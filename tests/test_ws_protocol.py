"""Tests for ws_protocol.py — JSON round-trip and error cases.

All tests use only public API: encode, decode_inbound, state_to_status_msg,
state_to_station_update, RegisterMsg, StatusMsg, StationUpdateMsg.
"""
from __future__ import annotations
import json
import pytest

from ws_protocol import (
    RegisterMsg,
    StatusMsg,
    StationUpdateMsg,
    decode_inbound,
    encode,
    state_to_status_msg,
    state_to_station_update,
)
from station import StationState


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------

def test_register_round_trip():
    msg = RegisterMsg(station_id="station-1")
    raw = encode(msg)
    decoded = decode_inbound(raw)
    assert isinstance(decoded, RegisterMsg)
    assert decoded.station_id == "station-1"
    assert decoded.type == "register"


def test_status_round_trip_full_fields():
    msg = StatusMsg(
        station_id="station-2",
        status="processing",
        last_class="A",
        last_destination=3,
        last_cycle_ms=420,
        cycle_count=7,
    )
    raw = encode(msg)
    decoded = decode_inbound(raw)
    assert isinstance(decoded, StatusMsg)
    assert decoded.station_id == "station-2"
    assert decoded.status == "processing"
    assert decoded.last_class == "A"
    assert decoded.last_destination == 3
    assert decoded.last_cycle_ms == 420
    assert decoded.cycle_count == 7


def test_status_round_trip_optional_fields_none():
    msg = StatusMsg(
        station_id="station-3",
        status="free",
        last_class=None,
        last_destination=None,
        last_cycle_ms=None,
        cycle_count=0,
    )
    raw = encode(msg)
    decoded = decode_inbound(raw)
    assert isinstance(decoded, StatusMsg)
    assert decoded.last_class is None
    assert decoded.last_destination is None
    assert decoded.last_cycle_ms is None


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_decode_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown message type"):
        decode_inbound('{"type":"foo"}')


def test_decode_malformed_json_raises():
    with pytest.raises(ValueError, match="malformed JSON"):
        decode_inbound("not json")


def test_decode_missing_type_raises():
    with pytest.raises(ValueError, match="missing 'type' field"):
        decode_inbound('{"station_id": "x"}')


# ---------------------------------------------------------------------------
# Helper: state_to_status_msg
# ---------------------------------------------------------------------------

def test_state_to_status_msg_from_dataclass():
    state = StationState(
        station_id="station-4",
        status="unknown_package",
        last_class="X",
        last_destination=2,
        last_cycle_ms=300,
        cycle_count=5,
    )
    msg = state_to_status_msg(state)
    assert isinstance(msg, StatusMsg)
    assert msg.station_id == "station-4"
    assert msg.status == "unknown_package"
    assert msg.last_class == "X"
    assert msg.last_destination == 2
    assert msg.last_cycle_ms == 300
    assert msg.cycle_count == 5


# ---------------------------------------------------------------------------
# StationUpdateMsg
# ---------------------------------------------------------------------------

def test_station_update_carries_online_flag():
    msg = StationUpdateMsg(station_id="station-5", status="free")
    raw = encode(msg)
    data = json.loads(raw)
    assert "online" in data
    assert data["online"] is True
    assert data["type"] == "station_update"


def test_station_update_online_false():
    msg = StationUpdateMsg(station_id="station-5", status="error", online=False)
    raw = encode(msg)
    data = json.loads(raw)
    assert data["online"] is False


def test_state_to_station_update_helper():
    state = StationState(station_id="station-6", status="free", cycle_count=2)
    msg = state_to_station_update(state, online=True)
    assert isinstance(msg, StationUpdateMsg)
    assert msg.station_id == "station-6"
    assert msg.online is True
