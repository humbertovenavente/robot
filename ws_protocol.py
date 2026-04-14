"""Shared WebSocket protocol models (Phase 2 D-02, D-03).

Single source of truth for orchestrator <-> station JSON wire format.
Both orchestrator.py (Plan 02-02) and ws_client.py (Plan 02-03) import from here.
No imports from fastapi, uvicorn, or websockets — pure Pydantic + stdlib so the
station-side client doesn't pull in FastAPI.
"""
from __future__ import annotations
import json
from typing import Literal, Optional, Union
from pydantic import BaseModel


class RegisterMsg(BaseModel):
    """Station -> Orchestrator: sent once per WS (re)connect (D-02, D-09)."""
    type: Literal["register"] = "register"
    station_id: str


class StatusMsg(BaseModel):
    """Station -> Orchestrator: one per StationState transition (D-02)."""
    type: Literal["status"] = "status"
    station_id: str
    status: str           # free | processing | unknown_package | error
    last_class: Optional[str] = None
    last_destination: Optional[int] = None
    last_cycle_ms: Optional[int] = None
    cycle_count: int = 0


class StationUpdateMsg(BaseModel):
    """Orchestrator -> Dashboard: broadcast on every incoming StatusMsg (D-03, D-04)."""
    type: Literal["station_update"] = "station_update"
    station_id: str
    status: str
    last_class: Optional[str] = None
    last_destination: Optional[int] = None
    last_cycle_ms: Optional[int] = None
    cycle_count: int = 0
    online: bool = True   # False when orchestrator detects station WS drop


Inbound = Union[RegisterMsg, StatusMsg]


def encode(msg: BaseModel) -> str:
    """Serialize a protocol model to a compact JSON string."""
    return msg.model_dump_json()


def decode_inbound(raw: str) -> Inbound:
    """Parse an inbound JSON string from a station. Raises ValueError on bad input."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed JSON: {e}") from e
    if not isinstance(data, dict) or "type" not in data:
        raise ValueError("missing 'type' field")
    t = data["type"]
    if t == "register":
        return RegisterMsg(**data)
    if t == "status":
        return StatusMsg(**data)
    raise ValueError(f"unknown message type: {t!r}")


def state_to_status_msg(state) -> StatusMsg:
    """Build a StatusMsg from a StationState dataclass (Plan 02-03 helper)."""
    return StatusMsg(
        station_id=state.station_id,
        status=state.status,
        last_class=state.last_class,
        last_destination=state.last_destination,
        last_cycle_ms=state.last_cycle_ms,
        cycle_count=state.cycle_count,
    )


def state_to_station_update(state, online: bool = True) -> StationUpdateMsg:
    """Build a StationUpdateMsg the orchestrator broadcasts to dashboards."""
    return StationUpdateMsg(
        station_id=state.station_id,
        status=state.status,
        last_class=state.last_class,
        last_destination=state.last_destination,
        last_cycle_ms=state.last_cycle_ms,
        cycle_count=state.cycle_count,
        online=online,
    )
