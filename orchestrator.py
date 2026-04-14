"""FastAPI orchestrator (Phase 2).

Aggregator only: receives station status, broadcasts to dashboards.
Does NOT send commands to stations (D-11, ARCHITECTURE Pattern 2).

Run: uvicorn orchestrator:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Set

from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ws_protocol import (
    RegisterMsg, StatusMsg, StationUpdateMsg,
    decode_inbound, encode,
)

log = logging.getLogger("orchestrator")

app = FastAPI(title="LEGO Package Sorter Orchestrator")

REPO_ROOT = Path(__file__).resolve().parent
_templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(REPO_ROOT / "static")), name="static")


@app.get("/")
async def dashboard_index(request: Request):
    return _templates.TemplateResponse(request, "dashboard.html")


@dataclass
class StationRecord:
    station_id: str
    status: str = "free"
    last_class: Optional[str] = None
    last_destination: Optional[int] = None
    last_cycle_ms: Optional[int] = None
    cycle_count: int = 0
    online: bool = True
    path_blocked: bool = False          # OBS-01
    blocking_object: Optional[str] = None


class Registry:
    def __init__(self) -> None:
        self._stations: Dict[str, StationRecord] = {}
        self._station_ws: Dict[str, WebSocket] = {}
        self._dashboards: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    def reset(self) -> None:
        """Clear all state. Use only in tests."""
        self._stations.clear()
        self._station_ws.clear()
        self._dashboards.clear()
        self._lock = asyncio.Lock()

    async def claim_station(self, station_id: str, ws: WebSocket) -> Optional[WebSocket]:
        """Register a station WS. Returns the previous WS (if any) so caller can close it (D-12)."""
        async with self._lock:
            prev_ws = self._station_ws.get(station_id)
            self._station_ws[station_id] = ws
            if station_id not in self._stations:
                self._stations[station_id] = StationRecord(station_id=station_id)
            self._stations[station_id].online = True
            return prev_ws

    async def release_station(self, station_id: str, ws: WebSocket) -> bool:
        """Mark station offline if ws is still the current one. Returns True if it was current."""
        async with self._lock:
            if self._station_ws.get(station_id) is ws:
                del self._station_ws[station_id]
                if station_id in self._stations:
                    self._stations[station_id].online = False
                return True
            return False

    async def apply_status(self, msg: StatusMsg) -> StationRecord:
        async with self._lock:
            rec = self._stations.setdefault(msg.station_id, StationRecord(station_id=msg.station_id))
            rec.status = msg.status
            rec.last_class = msg.last_class
            rec.last_destination = msg.last_destination
            rec.last_cycle_ms = msg.last_cycle_ms
            rec.cycle_count = msg.cycle_count
            rec.online = True
            rec.path_blocked = msg.path_blocked
            rec.blocking_object = msg.blocking_object
            return rec

    async def add_dashboard(self, ws: WebSocket) -> list:
        async with self._lock:
            self._dashboards.add(ws)
            return list(self._stations.values())

    async def remove_dashboard(self, ws: WebSocket) -> None:
        async with self._lock:
            self._dashboards.discard(ws)

    async def dashboards_snapshot(self) -> Set[WebSocket]:
        async with self._lock:
            return set(self._dashboards)

    async def all_stations(self) -> list:
        async with self._lock:
            return list(self._stations.values())


registry = Registry()


def _record_to_update(rec: StationRecord) -> StationUpdateMsg:
    return StationUpdateMsg(
        station_id=rec.station_id,
        status=rec.status,
        last_class=rec.last_class,
        last_destination=rec.last_destination,
        last_cycle_ms=rec.last_cycle_ms,
        cycle_count=rec.cycle_count,
        online=rec.online,
        path_blocked=rec.path_blocked,
        blocking_object=rec.blocking_object,
    )


async def _broadcast_to_dashboards(update: StationUpdateMsg) -> None:
    payload = encode(update)
    dead: list = []
    for ws in await registry.dashboards_snapshot():
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        await registry.remove_dashboard(ws)


@app.get("/api/stations")
async def get_stations():
    recs = await registry.all_stations()
    return JSONResponse({"stations": [asdict(r) for r in recs]})


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, role: str = Query(default="")):
    if role not in ("station", "dashboard"):
        await websocket.close(code=4003)
        return
    await websocket.accept()
    if role == "dashboard":
        await _handle_dashboard(websocket)
    else:
        await _handle_station(websocket)


async def _handle_dashboard(ws: WebSocket) -> None:
    snapshot = await registry.add_dashboard(ws)
    try:
        for rec in snapshot:
            await ws.send_text(encode(_record_to_update(rec)))
        # Dashboard is receive-only (D-04); we just hold the socket open
        while True:
            await ws.receive_text()  # ignore any client chatter
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("dashboard ws error: %s", e)
    finally:
        await registry.remove_dashboard(ws)


async def _handle_station(ws: WebSocket) -> None:
    station_id: Optional[str] = None
    try:
        first = await ws.receive_text()
        try:
            msg = decode_inbound(first)
        except ValueError as e:
            log.warning("station sent malformed first message: %s", e)
            await ws.close(code=4002)
            return
        if not isinstance(msg, RegisterMsg):
            log.warning("station first message was not register: %s", type(msg).__name__)
            await ws.close(code=4002)
            return
        station_id = msg.station_id
        prev = await registry.claim_station(station_id, ws)
        if prev is not None:
            try:
                await prev.close(code=4000)
            except Exception:
                pass
        # Broadcast that station is (re)online with its last known state
        recs = await registry.all_stations()
        for rec in recs:
            if rec.station_id == station_id:
                await _broadcast_to_dashboards(_record_to_update(rec))
                break
        while True:
            raw = await ws.receive_text()
            try:
                m = decode_inbound(raw)
            except ValueError as e:
                log.warning("station %s sent bad message: %s", station_id, e)
                await ws.close(code=4002)
                return
            if isinstance(m, StatusMsg):
                rec = await registry.apply_status(m)
                await _broadcast_to_dashboards(_record_to_update(rec))
            # RegisterMsg mid-session is ignored (idempotent)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("station ws error (id=%s): %s", station_id, e)
    finally:
        if station_id is not None:
            was_current = await registry.release_station(station_id, ws)
            if was_current:
                recs = await registry.all_stations()
                for rec in recs:
                    if rec.station_id == station_id:
                        await _broadcast_to_dashboards(_record_to_update(rec))
                        break


def main() -> int:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("orchestrator:app", host="0.0.0.0", port=8000, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
