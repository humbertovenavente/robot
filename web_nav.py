"""Mission-control web interface for QR-guided robot delivery.

Setup:   register QR codes for base + 3 drop stations (persisted to stations.json)
Mission: detect package QR → navigate to it → grab → return to base → deliver

Endpoints
---------
GET  /                        HTML dashboard (setup + mission tabs)
GET  /stream                  MJPEG camera feed with QR overlays
WS   /ws/status               push JSON on every meaningful state change
GET  /api/status              full JSON snapshot
GET  /api/nxt-status          NXT brick connectivity
POST /api/start               start navigator (manual / testing)
POST /api/stop                stop navigator
GET  /api/setup/stations      current station registry
POST /api/setup/scan          return QR codes visible right now
POST /api/setup/assign        {station, qr} — save a QR to a station slot
POST /api/setup/clear         {station}     — remove a station QR
POST /api/mission/start       {package_qr}  — begin delivery mission
POST /api/mission/destination {station}     — choose drop-off while at base
POST /api/mission/abort       emergency stop
POST /api/claw/open           manual claw open
POST /api/claw/close          manual claw close
POST /api/test/goto           {station} — navigate directly to a station (testing)
POST /api/test/stop           stop test navigation
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import threading
import time
from typing import Optional, Set

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse

from config import load_config
from navigator import QRNavigator, NavigatorState, build_navigator, _detect_overhead, _qr_heading

QR_CODES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_codes")

log = logging.getLogger(__name__)

STATIONS_FILE = os.path.join(os.path.dirname(__file__), "stations.json")
STATION_KEYS  = ["bot", "base", "station_1", "station_2", "station_3"]
STATION_LABELS = {
    "bot":       "Bot (top marker)",
    "base":      "Base",
    "station_1": "Station 1",
    "station_2": "Station 2",
    "station_3": "Station 3",
}


# ── station registry ─────────────────────────────────────────────────────────

class StationsRegistry:
    """Persists the mapping of station name → QR code content."""

    def __init__(self, path: str = STATIONS_FILE):
        self._path = path
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path) as f:
                self._data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}

    def save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def set(self, key: str, qr: str) -> None:
        if key not in STATION_KEYS:
            raise ValueError(f"Unknown station: {key}")
        self._data[key] = qr
        self.save()

    def clear(self, key: str) -> None:
        self._data.pop(key, None)
        self.save()

    def get(self, key: str) -> Optional[str]:
        return self._data.get(key)

    def all(self) -> dict:
        return {k: self._data.get(k) for k in STATION_KEYS}

    def is_complete(self) -> bool:
        required = [k for k in STATION_KEYS if k != "bot"]
        return all(self._data.get(k) for k in required)

    def qr_set(self) -> set:
        return {v for v in self._data.values() if v}

    def is_package_qr(self, qr: str) -> bool:
        return bool(qr) and qr not in self.qr_set()

    def set_heading_offset(self, offset: float) -> None:
        self._data["bot_heading_offset"] = offset
        self.save()

    def get_heading_offset(self) -> float:
        return float(self._data.get("bot_heading_offset", 0.0))

    def set_claw_offset_px(self, px: int) -> None:
        self._data["claw_offset_px"] = px
        self.save()

    def get_claw_offset_px(self) -> int:
        return int(self._data.get("claw_offset_px", 0))


# ── mission controller ────────────────────────────────────────────────────────

class MissionController:
    """State machine that orchestrates a full pick-and-deliver cycle.

    States
    ------
    idle → going_to_package → picking_up → returning_to_base →
    awaiting_destination → going_to_station → dropping_off → idle
    """

    def __init__(self, navigator: QRNavigator, registry: StationsRegistry):
        self._nav      = navigator
        self._reg      = registry
        self.state     = "idle"
        self.package_qr: Optional[str] = None
        self.destination: Optional[str] = None   # station key
        self._arrived  = threading.Event()
        self._dest_set = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._on_change = None

    def set_on_change(self, cb) -> None:
        self._on_change = cb

    def _emit(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    def _set_state(self, state: str) -> None:
        self.state = state
        log.info("Mission: → %s", state)
        self._emit()

    def on_navigator_arrived(self, qr_payload: str) -> None:
        """Called by the navigator state-change hook when status == 'arrived'."""
        self._arrived.set()

    def _navigate_to(self, qr_code: str, label: str, timeout: float = 120.0,
                     claw_mode: bool = False) -> bool:
        self._arrived.clear()
        self._nav.set_target(qr_code)
        if claw_mode:
            self._nav.set_claw_arrived(True)
        self._nav.set_navigating(True)
        log.info("Mission: navigating to %s (QR=%s, claw_mode=%s)", label, qr_code, claw_mode)
        ok = self._arrived.wait(timeout=timeout)
        self._nav.set_navigating(False)
        if claw_mode:
            self._nav.set_claw_arrived(False)
        if not ok:
            log.warning("Mission: timeout waiting to arrive at %s", label)
        return ok

    def _claw_grab(self) -> None:
        drive = self._nav._drive
        drive.open_claw()
        time.sleep(0.4)
        drive.close_claw()

    def _claw_drop(self) -> None:
        drive = self._nav._drive
        drive.open_claw()
        time.sleep(1.5)
        drive.close_claw()

    def _run(self) -> None:
        try:
            # 1. Go to package
            self._set_state("going_to_package")
            if not self._navigate_to(self.package_qr, "package"):
                self._set_state("idle")
                return

            # 2. Pick up
            self._set_state("picking_up")
            t = threading.Thread(target=self._claw_grab, daemon=True)
            t.start(); t.join()

            # 3. Return to base
            self._set_state("returning_to_base")
            base_qr = self._reg.get("base")
            if not base_qr or not self._navigate_to(base_qr, "base"):
                self._set_state("idle")
                return

            # 4. Wait for operator to select destination
            self._set_state("awaiting_destination")
            self._dest_set.clear()
            if not self._dest_set.wait(timeout=300.0) or not self.destination:
                self._set_state("idle")
                return

            # 5. Go to station — stop when claw tip reaches station QR, not bot QR
            self._set_state("going_to_station")
            dest_qr = self._reg.get(self.destination)
            if not dest_qr or not self._navigate_to(dest_qr, self.destination, claw_mode=True):
                self._set_state("idle")
                return

            # 6. Drop off
            self._set_state("dropping_off")
            t = threading.Thread(target=self._claw_drop, daemon=True)
            t.start(); t.join()

            self._set_state("idle")

        except Exception as exc:
            log.error("Mission error: %s", exc, exc_info=True)
            self._set_state("idle")

    def start(self, package_qr: str) -> bool:
        if self.state != "idle":
            return False
        self.package_qr  = package_qr
        self.destination = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="mission")
        self._thread.start()
        return True

    def set_destination(self, station_key: str) -> bool:
        if self.state != "awaiting_destination":
            return False
        if station_key not in ["station_1", "station_2", "station_3"]:
            return False
        self.destination = station_key
        self._dest_set.set()
        return True

    def abort(self) -> None:
        self._nav.set_navigating(False)
        self._arrived.set()
        self._dest_set.set()
        self.destination = None
        self.state = "idle"
        self._emit()


# ── FastAPI app + shared state ────────────────────────────────────────────────

app = FastAPI(title="Bug Navigator")

_navigator: Optional[QRNavigator] = None
_nav_thread: Optional[threading.Thread] = None
_mission: Optional[MissionController] = None
_registry: Optional[StationsRegistry] = None
_ws_clients: Set[WebSocket] = set()
_ws_lock     = asyncio.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None

# Rate-limit WS broadcasts: always send on nav-status change, cap rest at 10 fps
_last_broadcast_t      = 0.0
_last_broadcast_status = ""
_test_target: Optional[str] = None   # station key currently being tested


def _nxt_connected() -> bool:
    if _navigator is None:
        return False
    drive = getattr(_navigator, "_drive", None)
    return getattr(drive, "_brick", None) is not None


def _make_payload() -> str:
    s = _navigator.state if _navigator else None
    return json.dumps({
        "nav_status":     s.status if s else "idle",
        "mission_state":  _mission.state if _mission else "idle",
        "qr_payload":     s.qr_payload if s else None,
        "qr_area_pct":    round(s.qr_area_pct * 100, 1) if s and s.qr_area_pct else None,
        "frame_w":        s.frame_w if s else None,
        "all_qr_payloads": s.all_qr_payloads if s else [],
        "nxt_connected":  _nxt_connected(),
        "package_qr":     _mission.package_qr if _mission else None,
        "destination":    _mission.destination if _mission else None,
        "stations":       _registry.all() if _registry else {},
        "test_target":    _test_target,
    })


def _on_state_change(state: NavigatorState) -> None:
    global _loop, _last_broadcast_t, _last_broadcast_status
    if _loop is None:
        return
    now = time.monotonic()
    status_changed = (state.status != _last_broadcast_status)
    if not status_changed and now - _last_broadcast_t < 0.1:
        return
    _last_broadcast_t      = now
    _last_broadcast_status = state.status
    if _mission and state.status == "arrived":
        _mission.on_navigator_arrived(state.qr_payload or "")
    if _test_target and state.status == "arrived":
        globals()['_test_target'] = None
        _navigator.set_navigating(False)
    asyncio.run_coroutine_threadsafe(_broadcast(_make_payload()), _loop)


async def _broadcast(message: str) -> None:
    async with _ws_lock:
        dead = set()
        for ws in _ws_clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        _ws_clients.difference_update(dead)


async def _broadcast_state() -> None:
    await _broadcast(_make_payload())


# ── HTML ──────────────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bug Navigator</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f0f;color:#e8e8e8;font-family:'Segoe UI',sans-serif;font-size:14px}
header{padding:12px 20px;background:#111827;display:flex;align-items:center;gap:12px;
  border-bottom:2px solid #7c3aed;flex-wrap:wrap}
header h1{font-size:1.2rem;color:#a78bfa;margin-right:4px}
.badge{padding:3px 11px;border-radius:20px;font-size:.75rem;font-weight:700;
  letter-spacing:.05em;text-transform:uppercase}
.badge.idle{background:#2d2d2d;color:#888}
.badge.going_to_package{background:#0e3a4a;color:#38bdf8}
.badge.picking_up{background:#3a2600;color:#fb923c}
.badge.returning_to_base{background:#1e1040;color:#a78bfa}
.badge.awaiting_destination{background:#2d2800;color:#fbbf24}
.badge.going_to_station{background:#0e3a4a;color:#38bdf8}
.badge.dropping_off{background:#3a2600;color:#fb923c}
.badge.searching{background:#0e3a4a;color:#38bdf8}
.badge.centering{background:#3a2600;color:#fb923c}
.badge.approaching{background:#0a2e0a;color:#4ade80}
.badge.arrived{background:#2e0a2e;color:#e879f9}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:5px}
.dot.on{background:#4ade80;box-shadow:0 0 5px #4ade80}
.dot.off{background:#ef4444;box-shadow:0 0 5px #ef4444}
/* tabs */
.tabs{display:flex;gap:0;border-bottom:1px solid #2a2a2a}
.tab-btn{padding:10px 24px;background:none;border:none;color:#666;cursor:pointer;
  font-size:.85rem;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
  border-bottom:2px solid transparent;transition:color .15s}
.tab-btn.active{color:#a78bfa;border-bottom-color:#7c3aed}
.tab-btn:hover{color:#e8e8e8}
.tab-pane{display:none;padding:16px;max-width:1200px;margin:0 auto}
.tab-pane.active{display:block}
/* setup */
.setup-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.camera-mini{background:#111;border-radius:8px;overflow:hidden;aspect-ratio:4/3}
.camera-mini img{width:100%;height:100%;object-fit:contain}
.station-list{display:flex;flex-direction:column;gap:10px}
.station-row{background:#1a1a2e;border-radius:8px;padding:12px 16px;
  display:flex;align-items:center;gap:10px}
.station-row .label{font-weight:700;color:#a78bfa;min-width:90px}
.station-row .qr-val{flex:1;font-family:monospace;font-size:.8rem;
  color:#4ade80;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.station-row .qr-val.empty{color:#555;font-style:italic}
.setup-status{margin-top:12px;padding:10px 14px;border-radius:8px;
  font-size:.85rem;background:#1a1a2e;color:#888}
.setup-status.ok{background:#0a2e0a;color:#4ade80}
/* mission */
.mission-grid{display:grid;grid-template-columns:1fr 340px;gap:16px}
.camera-main{background:#111;border-radius:8px;overflow:hidden;aspect-ratio:4/3}
.camera-main img{width:100%;height:100%;object-fit:contain}
.ctrl-panel{display:flex;flex-direction:column;gap:12px}
/* state flow */
.flow{display:flex;flex-wrap:wrap;gap:4px;background:#111827;border-radius:8px;padding:10px}
.flow-step{padding:4px 10px;border-radius:6px;font-size:.7rem;font-weight:700;
  text-transform:uppercase;background:#1f1f1f;color:#444;transition:all .2s}
.flow-step.active{background:#7c3aed;color:#fff;box-shadow:0 0 8px #7c3aed88}
.flow-step.done{background:#1a3a1a;color:#4ade80}
/* sections */
.section{background:#1a1a2e;border-radius:8px;padding:12px}
.section h3{font-size:.75rem;color:#7c3aed;text-transform:uppercase;
  letter-spacing:.07em;margin-bottom:8px}
.pkg-item{display:flex;align-items:center;justify-content:space-between;
  padding:6px 8px;border-radius:6px;background:#0e1525;margin-bottom:4px;
  font-size:.8rem;font-family:monospace}
.pkg-item .pkg-qr{color:#fb923c;flex:1;overflow:hidden;text-overflow:ellipsis}
.dest-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.claw-row{display:flex;gap:8px}
/* buttons */
button{padding:8px 14px;border:none;border-radius:7px;font-size:.8rem;
  font-weight:700;cursor:pointer;transition:opacity .15s}
button:hover{opacity:.85}
button:disabled{opacity:.35;cursor:default}
.btn-primary{background:#7c3aed;color:#fff}
.btn-success{background:#16a34a;color:#fff}
.btn-danger{background:#dc2626;color:#fff}
.btn-warn{background:#d97706;color:#fff}
.btn-neutral{background:#374151;color:#e8e8e8}
.btn-sm{padding:5px 10px;font-size:.72rem}
/* log */
#log-box{font-size:.7rem;color:#555;height:120px;overflow-y:auto;
  font-family:monospace;background:#111;border-radius:6px;padding:6px}
#log-box div{padding:1px 0}
/* misc */
.stat-row{display:flex;justify-content:space-between;font-size:.8rem;margin-bottom:3px}
.stat-label{color:#666}
.stat-val{font-family:monospace;color:#e8e8e8}
footer{text-align:center;padding:10px;color:#333;font-size:.72rem}
@media(max-width:760px){
  .setup-grid,.mission-grid{grid-template-columns:1fr}
  .mission-grid .ctrl-panel{order:-1}
}
</style>
</head>
<body>
<header>
  <h1>Bug Navigator</h1>
  <span id="nav-badge" class="badge idle">nav: idle</span>
  <span id="mission-badge" class="badge idle">mission: idle</span>
  <span id="nxt-badge" style="display:flex;align-items:center;padding:3px 10px;
    border-radius:20px;font-size:.75rem;font-weight:700;border:1px solid #333;
    background:#2d0a0a;color:#ef4444">
    <span id="nxt-dot" class="dot off"></span>NXT
  </span>
  <span style="margin-left:auto;display:flex;align-items:center;gap:8px">
    <select id="cam-select"
      style="padding:4px 8px;border-radius:6px;border:1px solid #374151;
             background:#1f2937;color:#e8e8e8;font-size:.75rem;cursor:pointer"
      onchange="switchCamera()">
      <option value="">Camera…</option>
    </select>
    <span style="font-size:.75rem;color:#444;display:flex;align-items:center">
      <span id="ws-dot" class="dot off"></span>live
    </span>
  </span>
</header>

<div class="tabs">
  <button class="tab-btn active" onclick="showTab('setup')">Setup</button>
  <button class="tab-btn" onclick="showTab('mission')">Mission Control</button>
  <button class="tab-btn" onclick="showTab('test')">Testing</button>
</div>

<!-- ─── SETUP TAB ─────────────────────────────────────────────────────────── -->
<div id="tab-setup" class="tab-pane active">
  <div class="setup-grid">
    <div>
      <div class="camera-mini"><img src="/stream" alt="camera"></div>
      <p style="margin-top:8px;font-size:.78rem;color:#555">
        Hold a QR code in front of the camera, then click <b>Scan</b> next to the station.
      </p>
    </div>
    <div class="station-list" id="station-list">
      <!-- rows injected by JS -->
    </div>
  </div>
  <div id="setup-status" class="setup-status">Loading station registry…</div>

  <!-- Scale calibration -->
  <div class="section" style="margin-top:14px;max-width:520px">
    <h3>Scale calibration (claw offset)</h3>
    <p style="font-size:.76rem;color:#666;margin-bottom:10px">
      Place any QR code flat on the floor. Enter its printed size and the claw reach, then hit
      <b>Measure</b>. The system reads the QR pixel size to compute px/cm automatically.
    </p>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end">
      <label style="font-size:.78rem;color:#888;display:flex;flex-direction:column;gap:3px">
        QR physical size (cm)
        <input id="cal-qr-size" type="number" value="8" min="1" max="50" step="0.5"
          style="width:100px;padding:5px 8px;border-radius:6px;border:1px solid #333;
                 background:#111;color:#e8e8e8;font-size:.85rem">
      </label>
      <label style="font-size:.78rem;color:#888;display:flex;flex-direction:column;gap:3px">
        Claw reach (cm)
        <input id="cal-claw-reach" type="number" value="11" min="1" max="50" step="0.5"
          style="width:100px;padding:5px 8px;border-radius:6px;border:1px solid #333;
                 background:#111;color:#e8e8e8;font-size:.85rem">
      </label>
      <button class="btn-primary" onclick="calibrateScale()">Measure</button>
    </div>
    <div id="cal-result" style="margin-top:8px;font-size:.78rem;color:#555;min-height:16px"></div>
  </div>

  <!-- Bot forward direction -->
  <div class="section" style="margin-top:14px;max-width:520px">
    <h3>Bot forward direction</h3>
    <p style="font-size:.76rem;color:#666;margin-bottom:10px">
      Place the bot so its <b>Bot QR</b> is visible in the camera, then draw an arrow
      pointing toward the <b>front of the robot</b>. Required for overhead navigation.
    </p>
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <button class="btn-warn" onclick="openDirModal()">Draw forward arrow</button>
      <span style="font-size:.8rem;color:#666">
        Current offset: <span id="setup-heading-offset" style="color:#e8e8e8;font-family:monospace">—</span>
      </span>
    </div>
  </div>
</div>

<!-- ─── MISSION TAB ───────────────────────────────────────────────────────── -->
<div id="tab-mission" class="tab-pane">
  <div class="mission-grid">
    <div>
      <div class="camera-main"><img src="/stream" alt="camera feed"></div>
      <div style="margin-top:8px">
        <div class="stat-row">
          <span class="stat-label">Nav status</span>
          <span class="stat-val" id="m-nav-status">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">QR detected</span>
          <span class="stat-val" id="m-qr">—</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Proximity</span>
          <span class="stat-val" id="m-area">—</span>
        </div>
      </div>
    </div>

    <div class="ctrl-panel">
      <!-- State flow -->
      <div class="flow" id="state-flow">
        <div class="flow-step" id="fs-idle">Idle</div>
        <div class="flow-step" id="fs-going_to_package">→ Package</div>
        <div class="flow-step" id="fs-picking_up">Grab</div>
        <div class="flow-step" id="fs-returning_to_base">→ Base</div>
        <div class="flow-step" id="fs-awaiting_destination">Dest?</div>
        <div class="flow-step" id="fs-going_to_station">→ Station</div>
        <div class="flow-step" id="fs-dropping_off">Drop</div>
      </div>

      <!-- Detected packages -->
      <div class="section">
        <h3>Detected Packages</h3>
        <div id="pkg-list"><span style="color:#555;font-size:.8rem">Scanning…</span></div>
      </div>

      <!-- Destination selector (shown only when awaiting) -->
      <div class="section" id="dest-section" style="display:none">
        <h3>Select Destination</h3>
        <div class="dest-grid" id="dest-grid">
          <button class="btn-success" onclick="selectDest('station_1')">Station 1</button>
          <button class="btn-success" onclick="selectDest('station_2')">Station 2</button>
          <button class="btn-success" onclick="selectDest('station_3')">Station 3</button>
        </div>
      </div>

      <!-- Claw controls -->
      <div class="section">
        <h3>Claw (Motor B)</h3>
        <div class="claw-row">
          <button class="btn-neutral" onclick="claw('open')">Open</button>
          <button class="btn-neutral" onclick="claw('close')">Close</button>
        </div>
      </div>

      <!-- Abort -->
      <button class="btn-danger" onclick="abortMission()" id="btn-abort" disabled>
        Abort Mission
      </button>

      <!-- Log -->
      <div class="section">
        <h3>Log</h3>
        <div id="log-box"></div>
      </div>
    </div>
  </div>
</div>

<!-- ─── TESTING TAB ──────────────────────────────────────────────────────── -->
<div id="tab-test" class="tab-pane">
  <div style="display:grid;grid-template-columns:1fr 340px;gap:16px">
    <!-- Left: camera feed -->
    <div>
      <div class="camera-main"><img src="/stream" alt="camera feed"></div>
    </div>

    <!-- Right: controls -->
    <div>
      <p style="color:#666;font-size:.82rem;margin-bottom:12px">
        Direct navigation — bypasses mission flow. Mission must be <b>idle</b>.
      </p>

      <div class="section" style="margin-bottom:12px">
        <h3>Go to station</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px" id="test-goto-grid">
          <!-- injected by JS -->
        </div>
      </div>

      <div class="section" style="margin-bottom:12px">
        <h3>Go to visible QR</h3>
        <p style="font-size:.75rem;color:#555;margin-bottom:8px">
          Navigate directly to any QR code currently in the camera frame.
        </p>
        <div id="test-qr-grid" style="display:flex;flex-wrap:wrap;gap:8px">
          <!-- injected by JS -->
        </div>
      </div>

      <div class="section" style="margin-bottom:12px">
        <h3>Nav status</h3>
        <div class="stat-row"><span class="stat-label">Status</span><span class="stat-val" id="t-nav">—</span></div>
        <div class="stat-row"><span class="stat-label">Target QR</span><span class="stat-val" id="t-target">—</span></div>
        <div class="stat-row"><span class="stat-label">Proximity</span><span class="stat-val" id="t-area">—</span></div>
      </div>

      <button class="btn-danger" onclick="testStop()">Stop motors</button>
    </div>
  </div>
</div>

<!-- ─── DIRECTION MODAL ─────────────────────────────────────────────────── -->
<div id="dir-modal" style="display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.88);z-index:1000;justify-content:center;align-items:center">
  <div style="background:#1a1a2e;border-radius:12px;padding:20px;
    max-width:95vw;max-height:95vh;display:flex;flex-direction:column;gap:12px;
    border:2px solid #7c3aed">
    <h2 style="color:#a78bfa;font-size:1rem;margin:0">Set Bot Forward Direction</h2>
    <p style="font-size:.78rem;color:#888;margin:0">
      Click anywhere on the image to draw an arrow pointing toward the
      <b style="color:#fb923c">front of the bot</b>. The arrow starts from the bot QR centre.
    </p>
    <div style="overflow:auto;border-radius:6px;background:#111">
      <canvas id="dir-canvas" style="max-width:80vw;cursor:crosshair;display:block"></canvas>
    </div>
    <div id="dir-status" style="font-size:.78rem;color:#fb923c;min-height:14px"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn-neutral btn-sm" onclick="refreshDirFrame()">Refresh frame</button>
      <button class="btn-success btn-sm" id="dir-confirm" onclick="confirmDirection()" disabled>
        Save direction
      </button>
      <button class="btn-neutral btn-sm" onclick="closeDirModal()">Cancel</button>
    </div>
  </div>
</div>

<footer>Bug Navigator — QR floor navigation</footer>

<script>
const STATION_KEYS   = ['bot','base','station_1','station_2','station_3'];
const STATION_LABELS = {bot:'Bot',base:'Base',station_1:'Station 1',station_2:'Station 2',station_3:'Station 3'};
const FLOW_STATES    = ['idle','going_to_package','picking_up','returning_to_base',
                        'awaiting_destination','going_to_station','dropping_off'];

let stationsData = {};
let allQrPayloads = [];
let missionState  = 'idle';

// ── tabs ──────────────────────────────────────────────────────────────────────
function showTab(id) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  event.target.classList.add('active');
}

// ── log ───────────────────────────────────────────────────────────────────────
function addLog(msg) {
  const box  = document.getElementById('log-box');
  const line = document.createElement('div');
  line.textContent = new Date().toLocaleTimeString() + '  ' + msg;
  box.prepend(line);
  if (box.children.length > 80) box.lastChild.remove();
}

// ── NXT badge ─────────────────────────────────────────────────────────────────
function applyNxt(ok) {
  const b = document.getElementById('nxt-badge');
  const d = document.getElementById('nxt-dot');
  d.className = ok ? 'dot on' : 'dot off';
  b.style.background    = ok ? '#0a2e0a' : '#2d0a0a';
  b.style.color         = ok ? '#4ade80' : '#ef4444';
  b.style.borderColor   = ok ? '#166534' : '#7f1d1d';
  b.innerHTML = `<span class="${ok?'dot on':'dot off'}" id="nxt-dot"></span>NXT`;
}

// ── state flow display ────────────────────────────────────────────────────────
function applyFlow(ms) {
  const idx = FLOW_STATES.indexOf(ms);
  FLOW_STATES.forEach((s, i) => {
    const el = document.getElementById('fs-' + s);
    if (!el) return;
    el.className = 'flow-step' + (i === idx ? ' active' : i < idx ? ' done' : '');
  });
}

// ── package list ──────────────────────────────────────────────────────────────
function renderPackages(qrs) {
  const box = document.getElementById('pkg-list');
  const stationSet = new Set(Object.values(stationsData).filter(Boolean));
  const pkgs = qrs.filter(q => q && !stationSet.has(q));

  if (!pkgs.length) {
    box.innerHTML = '<span style="color:#555;font-size:.8rem">No packages in frame</span>';
    return;
  }
  box.innerHTML = '';
  pkgs.forEach(qr => {
    const row = document.createElement('div');
    row.className = 'pkg-item';
    row.innerHTML = `<span class="pkg-qr">${esc(qr)}</span>
      <button class="btn-primary btn-sm" onclick="startMission('${esc(qr)}')"
        ${missionState !== 'idle' ? 'disabled' : ''}>Pick up</button>`;
    box.appendChild(row);
  });
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                  .replace(/"/g,'&quot;');
}

// ── full state update ─────────────────────────────────────────────────────────
function applyState(s) {
  stationsData  = s.stations || {};
  allQrPayloads = s.all_qr_payloads || [];
  missionState  = s.mission_state || 'idle';

  // Header badges
  const nb = document.getElementById('nav-badge');
  nb.textContent = 'nav: ' + s.nav_status;
  nb.className   = 'badge ' + s.nav_status;

  const mb = document.getElementById('mission-badge');
  mb.textContent = 'mission: ' + missionState;
  mb.className   = 'badge ' + missionState;

  // Mission panel stats
  document.getElementById('m-nav-status').textContent = s.nav_status || '—';
  document.getElementById('m-qr').textContent         = s.qr_payload || '—';
  document.getElementById('m-area').textContent       = s.qr_area_pct != null
    ? s.qr_area_pct + ' %' : '—';

  applyFlow(missionState);
  renderPackages(allQrPayloads);

  // Show/hide destination panel
  document.getElementById('dest-section').style.display =
    missionState === 'awaiting_destination' ? 'block' : 'none';

  // Abort button
  document.getElementById('btn-abort').disabled = (missionState === 'idle');

  if (s.nxt_connected !== undefined) applyNxt(s.nxt_connected);

  // Testing tab stats
  document.getElementById('t-nav').textContent    = s.nav_status || '—';
  document.getElementById('t-target').textContent = s.test_target || '—';
  document.getElementById('t-area').textContent   = s.qr_area_pct != null ? s.qr_area_pct + ' %' : '—';

  renderStations();
  renderTestButtons();
  renderTestQRButtons();
}

// ── setup: station list ───────────────────────────────────────────────────────
function renderStations() {
  const list = document.getElementById('station-list');
  list.innerHTML = '';
  STATION_KEYS.forEach(key => {
    const qr  = stationsData[key];
    const row = document.createElement('div');
    row.className = 'station-row';

    let extra = '';
    if (key === 'bot' && qr) {
      extra = '';
    }
    if (key === 'base') {
      extra = `<button class="btn-neutral btn-sm" onclick="generateBaseQR()" title="Generate and print base QR">⚙ Generate</button>`;
    }

    row.innerHTML = `
      <span class="label">${STATION_LABELS[key]}</span>
      <span class="qr-val ${qr ? '' : 'empty'}">${qr ? esc(qr) : 'not set'}</span>
      ${extra}
      <button class="btn-primary btn-sm" onclick="scanStation('${key}')">Scan</button>
      <button class="btn-neutral btn-sm" onclick="clearStation('${key}')"${qr?'':' disabled'}>✕</button>
    `;
    list.appendChild(row);
  });

  const required = STATION_KEYS.filter(k => k !== 'bot');
  const allSet   = required.every(k => stationsData[k]);
  const botOk    = !!stationsData['bot'];
  const status   = document.getElementById('setup-status');
  const modeNote = botOk ? ' [overhead mode]' : ' [onboard mode — add Bot QR for overhead]';
  status.textContent = allSet
    ? '✓ Stations registered — ready for missions.' + modeNote
    : 'Scan QR codes for each station above.' + modeNote;
  status.className = 'setup-status' + (allSet ? ' ok' : '');
}

async function generateBaseQR() {
  const r = await fetch('/api/setup/generate-base', {method:'POST'});
  const d = await r.json();
  if (d.ok) {
    stationsData['base'] = d.content;
    addLog(`Base QR generated → "${d.content}" saved to qr_codes/base.png`);
    renderStations();
  } else {
    addLog('Generate failed: ' + d.reason);
  }
}

async function scanStation(key) {
  const r = await fetch('/api/setup/scan', {method:'POST'});
  const d = await r.json();
  if (!d.qr_codes || !d.qr_codes.length) {
    addLog('No QR codes visible in frame');
    return;
  }
  if (d.qr_codes.length === 1) {
    await assignStation(key, d.qr_codes[0]);
    return;
  }
  // Multiple QRs — let user pick
  const choice = window.prompt(
    `Multiple QR codes visible:\n${d.qr_codes.map((q,i)=>`${i+1}. ${q}`).join('\n')}\n\nEnter number:`
  );
  const idx = parseInt(choice, 10) - 1;
  if (idx >= 0 && idx < d.qr_codes.length) {
    await assignStation(key, d.qr_codes[idx]);
  }
}

async function assignStation(key, qr) {
  const r = await fetch('/api/setup/assign', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({station: key, qr})
  });
  const d = await r.json();
  if (d.ok) {
    stationsData[key] = qr;
    addLog(`${STATION_LABELS[key]} → "${qr}"`);
    renderStations();
  } else {
    addLog('Assign failed: ' + d.reason);
  }
}

async function clearStation(key) {
  const r = await fetch('/api/setup/clear', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({station: key})
  });
  if ((await r.json()).ok) {
    stationsData[key] = null;
    addLog(`${STATION_LABELS[key]} cleared`);
    renderStations();
  }
}

// ── camera selector ───────────────────────────────────────────────────────────
async function loadCameras() {
  const sel = document.getElementById('cam-select');
  try {
    const d = await (await fetch('/api/camera/list')).json();
    sel.innerHTML = '';
    if (!d.devices || !d.devices.length) {
      sel.innerHTML = '<option value="">No devices found</option>';
      return;
    }
    d.devices.forEach(dev => {
      const opt = document.createElement('option');
      opt.value = dev.index;
      opt.textContent = `[${dev.index}] ${dev.label}`;
      if (dev.index === d.current) opt.selected = true;
      sel.appendChild(opt);
    });
  } catch(e) {
    sel.innerHTML = '<option value="">Error loading</option>';
  }
}

async function switchCamera() {
  const sel = document.getElementById('cam-select');
  const idx = parseInt(sel.value, 10);
  if (isNaN(idx)) return;
  sel.disabled = true;
  try {
    const r = await fetch('/api/camera/switch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({index: idx})
    });
    const d = await r.json();
    addLog(d.ok ? `Camera switched to index ${idx}` : 'Camera switch failed: ' + d.reason);
    if (!d.ok) await loadCameras();
  } catch(e) {
    addLog('Camera switch request failed');
  }
  sel.disabled = false;
}

// ── testing actions ───────────────────────────────────────────────────────────
function renderTestButtons() {
  const grid = document.getElementById('test-goto-grid');
  if (!grid) return;
  grid.innerHTML = '';
  STATION_KEYS.forEach(key => {
    const qr = stationsData[key];
    const btn = document.createElement('button');
    btn.className = 'btn-primary';
    btn.textContent = 'Go to ' + STATION_LABELS[key];
    btn.disabled = !qr || missionState !== 'idle';
    if (qr) btn.onclick = () => testGoto(key);
    grid.appendChild(btn);
  });
}

function renderTestQRButtons() {
  const grid = document.getElementById('test-qr-grid');
  if (!grid) return;
  grid.innerHTML = '';
  const visible = allQrPayloads.filter(Boolean);
  if (!visible.length) {
    grid.innerHTML = '<span style="color:#555;font-size:.8rem">No QR codes visible in frame</span>';
    return;
  }
  visible.forEach(qr => {
    const btn = document.createElement('button');
    btn.className = 'btn-warn';
    btn.textContent = qr.length > 22 ? qr.slice(0, 20) + '…' : qr;
    btn.title = qr;
    btn.disabled = missionState !== 'idle';
    btn.onclick = () => testGotoQR(qr);
    grid.appendChild(btn);
  });
}

async function testGotoQR(qr) {
  const r = await fetch('/api/test/goto-qr', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({qr})
  });
  const d = await r.json();
  addLog(d.ok ? `Testing: navigating to QR "${qr}"` : 'Error: ' + d.reason);
}

async function testGoto(station) {
  const r = await fetch('/api/test/goto', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({station})
  });
  const d = await r.json();
  addLog(d.ok ? `Testing: navigating to ${STATION_LABELS[station]}` : 'Error: ' + d.reason);
}

async function testStop() {
  const r = await fetch('/api/test/stop', {method:'POST'});
  addLog((await r.json()).ok ? 'Testing: motors stopped' : 'Stop failed');
}

// ── mission actions ───────────────────────────────────────────────────────────
async function startMission(packageQr) {
  const r = await fetch('/api/mission/start', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({package_qr: packageQr})
  });
  const d = await r.json();
  addLog(d.ok ? `Mission started → "${packageQr}"` : 'Start failed: ' + d.reason);
}

async function selectDest(station) {
  const r = await fetch('/api/mission/destination', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({station})
  });
  const d = await r.json();
  addLog(d.ok ? `Destination set: ${STATION_LABELS[station]}` : 'Error: ' + d.reason);
}

async function abortMission() {
  if (!confirm('Abort mission and stop all motors?')) return;
  const r = await fetch('/api/mission/abort', {method:'POST'});
  addLog((await r.json()).ok ? 'Mission aborted' : 'Abort failed');
}

async function claw(action) {
  const r = await fetch('/api/claw/' + action, {method:'POST'});
  const d = await r.json();
  addLog(d.ok ? `Claw ${action}` : `Claw ${action} failed: ` + d.reason);
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connectWS() {
  const ws = new WebSocket('ws://' + location.host + '/ws/status');
  const dot = document.getElementById('ws-dot');
  ws.onopen  = () => { dot.className = 'dot on'; addLog('WebSocket connected'); };
  ws.onclose = () => { dot.className = 'dot off'; setTimeout(connectWS, 2000); };
  ws.onerror = () => ws.close();
  ws.onmessage = e => {
    const s = JSON.parse(e.data);
    applyState(s);
    addLog(`[${s.mission_state}] nav=${s.nav_status}` +
           (s.qr_payload ? ` QR=${s.qr_payload}` : '') +
           (s.qr_area_pct != null ? ` ${s.qr_area_pct}%` : ''));
  };
}

// NXT poll (in case it reconnects between state changes)
setInterval(async () => {
  try {
    const d = await (await fetch('/api/nxt-status')).json();
    applyNxt(d.connected);
  } catch(_){}
}, 3000);

// ── direction modal ───────────────────────────────────────────────────────────
let _dirQrInfo   = null;
let _dirArrow    = null;   // {ax, ay} endpoint of arrow (image coords)
let _dirImgEl    = null;

function _drawDirCanvas() {
  const canvas = document.getElementById('dir-canvas');
  if (!_dirImgEl) return;
  const ctx = canvas.getContext('2d');
  canvas.width  = _dirImgEl.naturalWidth;
  canvas.height = _dirImgEl.naturalHeight;
  ctx.drawImage(_dirImgEl, 0, 0);

  const info = _dirQrInfo;
  if (!info || !info.found) {
    ctx.fillStyle = '#ff4444';
    ctx.font = 'bold 18px sans-serif';
    ctx.fillText('Bot QR not detected — refresh frame or check camera.', 16, 36);
    return;
  }

  const {cx, cy, x, y, w, h} = info;

  // QR bounding box
  ctx.strokeStyle = '#00ff88'; ctx.lineWidth = 3;
  ctx.strokeRect(x, y, w, h);

  // Centre dot
  ctx.fillStyle = '#00ff88';
  ctx.beginPath(); ctx.arc(cx, cy, 7, 0, Math.PI*2); ctx.fill();

  // Arrow (if the user has clicked)
  if (_dirArrow) {
    const {ax, ay} = _dirArrow;
    _arrowLine(ctx, cx, cy, ax, ay, '#ff6600', 4, 22);
  }
}

function _arrowLine(ctx, x1, y1, x2, y2, color, lw, hl) {
  const ang = Math.atan2(y2-y1, x2-x1);
  ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = lw;
  ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x2, y2);
  ctx.lineTo(x2 - hl*Math.cos(ang - Math.PI/6), y2 - hl*Math.sin(ang - Math.PI/6));
  ctx.lineTo(x2 - hl*Math.cos(ang + Math.PI/6), y2 - hl*Math.sin(ang + Math.PI/6));
  ctx.closePath(); ctx.fill();
}

async function openDirModal() {
  document.getElementById('dir-modal').style.display = 'flex';
  document.getElementById('dir-status').textContent  = 'Loading…';
  _dirArrow = null;
  document.getElementById('dir-confirm').disabled = true;
  await refreshDirFrame();
}

async function refreshDirFrame() {
  // Fetch QR info and snapshot in parallel
  const [infoRes, imgUrl] = await Promise.all([
    fetch('/api/setup/bot-qr-info').then(r => r.json()),
    Promise.resolve('/api/setup/snapshot?' + Date.now()),
  ]);
  _dirQrInfo = infoRes;

  const status = document.getElementById('dir-status');
  if (!infoRes.found) {
    status.textContent = '⚠ ' + (infoRes.reason || 'Bot QR not found');
  } else {
    status.textContent = 'Current offset: ' +
      (infoRes.current_offset ? (infoRes.current_offset * 180/Math.PI).toFixed(1) + '°' : '0°');
  }

  const img = new Image();
  img.onload = () => { _dirImgEl = img; _drawDirCanvas(); };
  img.src = imgUrl;
}

document.getElementById('dir-canvas').addEventListener('click', e => {
  if (!_dirQrInfo || !_dirQrInfo.found) return;
  const canvas = document.getElementById('dir-canvas');
  const rect   = canvas.getBoundingClientRect();
  const scaleX = canvas.width  / rect.width;
  const scaleY = canvas.height / rect.height;
  _dirArrow = {
    ax: (e.clientX - rect.left) * scaleX,
    ay: (e.clientY - rect.top)  * scaleY,
  };
  _drawDirCanvas();
  document.getElementById('dir-confirm').disabled = false;
  document.getElementById('dir-status').textContent =
    'Arrow set — click Save direction to confirm.';
});

async function confirmDirection() {
  if (!_dirArrow || !_dirQrInfo || !_dirQrInfo.found) return;
  const angle = Math.atan2(
    _dirArrow.ay - _dirQrInfo.cy,
    _dirArrow.ax - _dirQrInfo.cx,
  );
  const r = await fetch('/api/setup/bot-direction', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({arrow_angle: angle}),
  });
  const d = await r.json();
  if (d.ok) {
    addLog(`Bot forward direction saved (offset ${d.offset_deg.toFixed(1)}°)`);
    _applyHeadingOffset(d.offset_deg);
    closeDirModal();
  } else {
    document.getElementById('dir-status').textContent = 'Error: ' + d.reason;
  }
}

function _applyHeadingOffset(deg) {
  const el = document.getElementById('setup-heading-offset');
  if (el) el.textContent = deg != null ? deg.toFixed(1) + '°' : '—';
}

function closeDirModal() {
  document.getElementById('dir-modal').style.display = 'none';
  _dirArrow = null; _dirQrInfo = null; _dirImgEl = null;
}

// ── scale calibration ─────────────────────────────────────────────────────────
async function calibrateScale() {
  const qrSize   = parseFloat(document.getElementById('cal-qr-size').value)   || 8;
  const clawReach = parseFloat(document.getElementById('cal-claw-reach').value) || 11;
  const res = document.getElementById('cal-result');
  res.style.color = '#fb923c';
  res.textContent = 'Measuring…';
  const r = await fetch('/api/setup/calibrate-scale', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({qr_size_cm: qrSize, claw_reach_cm: clawReach}),
  });
  const d = await r.json();
  if (d.ok) {
    res.style.color = '#4ade80';
    res.textContent = `✓ QR "${d.qr_payload}" = ${d.qr_size_px}px → `
      + `${d.px_per_cm} px/cm → claw offset = ${d.claw_offset_px}px`;
    addLog(`Scale calibrated: ${d.px_per_cm}px/cm  claw offset=${d.claw_offset_px}px`);
  } else {
    res.style.color = '#ef4444';
    res.textContent = '✗ ' + d.reason;
    addLog('Calibration failed: ' + d.reason);
  }
}

// Bootstrap
(async () => {
  try {
    const s = await (await fetch('/api/status')).json();
    applyState(s);
  } catch(_) {}
  loadCameras();
  try {
    const info = await (await fetch('/api/setup/bot-qr-info')).json();
    if (info.current_offset != null)
      _applyHeadingOffset(parseFloat((info.current_offset * 180 / Math.PI).toFixed(1)));
  } catch(_) {}
  connectWS();
})();
</script>
</body>
</html>
"""


# ── MJPEG stream ──────────────────────────────────────────────────────────────

def _placeholder_frame():
    import numpy as np
    img = np.zeros((240, 320, 3), dtype=np.uint8)
    cv2.putText(img, "waiting for camera...", (20, 120),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 60, 60), 1)
    return img


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _HTML


@app.get("/stream")
async def stream():
    def generate():
        while True:
            sq = _registry.qr_set() if _registry else set()
            frame = _navigator.get_annotated_frame(station_qrs=sq) if _navigator else None
            if frame is None:
                frame = _placeholder_frame()
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok:
                time.sleep(0.05)
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                   + buf.tobytes() + b"\r\n")
            time.sleep(0.04)
    return StreamingResponse(generate(),
                             media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/nxt-status")
async def api_nxt_status():
    return {"connected": _nxt_connected()}


@app.get("/api/status")
async def api_status():
    s = _navigator.state if _navigator else None
    return {
        "nav_status":      s.status if s else "idle",
        "mission_state":   _mission.state if _mission else "idle",
        "qr_payload":      s.qr_payload if s else None,
        "qr_area_pct":     round(s.qr_area_pct * 100, 1) if s and s.qr_area_pct else None,
        "frame_w":         s.frame_w if s else None,
        "all_qr_payloads": s.all_qr_payloads if s else [],
        "nxt_connected":   _nxt_connected(),
        "package_qr":      _mission.package_qr if _mission else None,
        "destination":     _mission.destination if _mission else None,
        "stations":        _registry.all() if _registry else {},
    }


@app.post("/api/start")
async def api_start():
    """Enable navigation toward any visible QR (manual/testing mode)."""
    if _navigator:
        _navigator.set_target(None)
        _navigator.set_navigating(True)
        return {"ok": True}
    return {"ok": False, "reason": "navigator not running"}


@app.post("/api/stop")
async def api_stop():
    if _navigator:
        _navigator.set_navigating(False)
        return {"ok": True}
    return {"ok": False, "reason": "navigator not running"}


# ── setup routes ──────────────────────────────────────────────────────────────

@app.get("/api/setup/stations")
async def api_setup_stations():
    return _registry.all() if _registry else {}


@app.post("/api/setup/scan")
async def api_setup_scan():
    """Return QR codes currently visible in the camera frame."""
    if _navigator is None:
        return {"qr_codes": []}
    return {"qr_codes": list(_navigator.state.all_qr_payloads)}


@app.post("/api/setup/assign")
async def api_setup_assign(body: dict):
    station = body.get("station", "")
    qr      = body.get("qr", "").strip()
    if not qr:
        return {"ok": False, "reason": "empty QR"}
    try:
        _registry.set(station, qr)
        log.info("Setup: %s → %s", station, qr)
        if station == "bot":
            _apply_bot_qr()
        asyncio.create_task(_broadcast_state())
        return {"ok": True}
    except ValueError as e:
        return {"ok": False, "reason": str(e)}


@app.post("/api/setup/clear")
async def api_setup_clear(body: dict):
    station = body.get("station", "")
    if station not in STATION_KEYS:
        return {"ok": False, "reason": "unknown station"}
    _registry.clear(station)
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


# ── mission routes ────────────────────────────────────────────────────────────

@app.post("/api/mission/start")
async def api_mission_start(body: dict):
    package_qr = body.get("package_qr", "").strip()
    if not package_qr:
        return {"ok": False, "reason": "package_qr required"}
    if not _registry.is_complete():
        return {"ok": False, "reason": "not all stations registered"}
    ok = _mission.start(package_qr)
    if ok:
        asyncio.create_task(_broadcast_state())
    return {"ok": ok, "reason": "" if ok else "mission already running"}


@app.post("/api/mission/destination")
async def api_mission_destination(body: dict):
    station = body.get("station", "")
    ok = _mission.set_destination(station)
    if ok:
        asyncio.create_task(_broadcast_state())
    return {"ok": ok, "reason": "" if ok else "not awaiting destination"}


@app.post("/api/mission/abort")
async def api_mission_abort():
    _mission.abort()
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


# ── claw routes ───────────────────────────────────────────────────────────────

@app.post("/api/claw/open")
async def api_claw_open():
    drive = getattr(_navigator, "_drive", None) if _navigator else None
    if drive and hasattr(drive, "open_claw"):
        threading.Thread(target=drive.open_claw, daemon=True).start()
        return {"ok": True}
    return {"ok": False, "reason": "no claw available"}


@app.post("/api/claw/close")
async def api_claw_close():
    drive = getattr(_navigator, "_drive", None) if _navigator else None
    if drive and hasattr(drive, "close_claw"):
        threading.Thread(target=drive.close_claw, daemon=True).start()
        return {"ok": True}
    return {"ok": False, "reason": "no claw available"}


# ── base QR generation ───────────────────────────────────────────────────────

BASE_QR_CONTENT = "BASE_STATION"

def _generate_base_qr() -> str:
    """Generate base station QR PNG and return its file path."""
    try:
        import qrcode as _qr
    except ImportError:
        raise RuntimeError("qrcode library not installed — run: pip install 'qrcode[pil]'")
    os.makedirs(QR_CODES_DIR, exist_ok=True)
    q = _qr.QRCode(version=1, error_correction=_qr.constants.ERROR_CORRECT_L,
                   box_size=12, border=4)
    q.add_data(BASE_QR_CONTENT)
    q.make(fit=True)
    img  = q.make_image(fill_color="black", back_color="white")
    path = os.path.join(QR_CODES_DIR, "base.png")
    img.save(path)
    return path


# ── setup extras: snapshot + bot-direction ────────────────────────────────────

@app.get("/api/setup/snapshot")
async def api_snapshot():
    """Current camera frame as JPEG (for direction-setting UI)."""
    from fastapi.responses import Response as _Resp
    sq    = _registry.qr_set() if _registry else set()
    frame = _navigator.get_annotated_frame(station_qrs=sq) if _navigator else None
    if frame is None:
        frame = _placeholder_frame()
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return _Resp(content=buf.tobytes(), media_type="image/jpeg")


@app.get("/api/setup/bot-qr-info")
async def api_bot_qr_info():
    """Position + corner data of the bot QR in the current camera frame."""
    bot_qr = _registry.get("bot") if _registry else None
    if not bot_qr:
        return {"found": False, "reason": "bot QR not registered yet"}
    if _navigator is None:
        return {"found": False, "reason": "navigator not running"}
    with _navigator._frame_lock:
        frame = _navigator._raw_frame.copy() if _navigator._raw_frame is not None else None
    if frame is None:
        return {"found": False, "reason": "no camera frame yet"}
    items = _detect_overhead(frame)
    bot   = next((i for i in items if i["payload"] == bot_qr), None)
    if not bot:
        return {"found": False, "reason": "bot QR not visible in frame"}
    c  = bot["corners"]
    return {
        "found":     True,
        "cx":        bot["cx"],
        "cy":        bot["cy"],
        "x":         int(c[:, 0].min()),
        "y":         int(c[:, 1].min()),
        "w":         int(c[:, 0].max() - c[:, 0].min()),
        "h":         int(c[:, 1].max() - c[:, 1].min()),
        "qr_angle":  _qr_heading(c, 0.0),
        "current_offset": _registry.get_heading_offset() if _registry else 0.0,
    }


@app.post("/api/setup/bot-direction")
async def api_bot_direction(body: dict):
    """Save heading offset derived from user-drawn arrow.

    body: {arrow_angle: float}  — angle in image coords (atan2(dy,dx), y-down)
    The server reads the bot QR corners from the live frame and computes the
    offset = arrow_angle − qr_corner_angle, then stores it.
    """
    arrow_angle = body.get("arrow_angle")
    if arrow_angle is None:
        return {"ok": False, "reason": "arrow_angle required"}
    bot_qr = _registry.get("bot") if _registry else None
    if not bot_qr:
        return {"ok": False, "reason": "bot QR not registered"}
    if _navigator is None:
        return {"ok": False, "reason": "navigator not running"}
    with _navigator._frame_lock:
        frame = _navigator._raw_frame.copy() if _navigator._raw_frame is not None else None
    if frame is None:
        return {"ok": False, "reason": "no camera frame"}
    items = _detect_overhead(frame)
    bot   = next((i for i in items if i["payload"] == bot_qr), None)
    if not bot:
        return {"ok": False, "reason": "bot QR not visible — hold it in frame and retry"}
    qr_angle = _qr_heading(bot["corners"], 0.0)
    offset   = (arrow_angle - qr_angle + math.pi) % (2 * math.pi) - math.pi
    _registry.set_heading_offset(offset)
    _apply_bot_qr()
    return {"ok": True, "offset_deg": round(math.degrees(offset), 1), "offset_rad": offset}


@app.post("/api/setup/calibrate-scale")
async def api_calibrate_scale(body: dict):
    """Measure pixel size of any visible QR to derive px/cm scale.

    body: {qr_size_cm: float, claw_reach_cm: float}
    Detects the largest QR in frame, measures its pixel size (average of width+height),
    computes px_per_cm, then sets claw_offset_px = round(claw_reach_cm * px_per_cm).
    """
    import numpy as np
    qr_size_cm   = float(body.get("qr_size_cm",   8.0))
    claw_reach_cm = float(body.get("claw_reach_cm", 11.0))
    if qr_size_cm <= 0:
        return {"ok": False, "reason": "qr_size_cm must be > 0"}
    if _navigator is None:
        return {"ok": False, "reason": "navigator not running"}
    with _navigator._frame_lock:
        frame = _navigator._raw_frame.copy() if _navigator._raw_frame is not None else None
    if frame is None:
        return {"ok": False, "reason": "no camera frame yet"}

    items = _detect_overhead(frame)
    if not items:
        return {"ok": False, "reason": "no QR codes visible in frame — place one and retry"}

    # Pick the largest QR by bounding-box area
    def _qr_size_px(item):
        c = item["corners"]
        w = (np.linalg.norm(c[1] - c[0]) + np.linalg.norm(c[2] - c[3])) / 2
        h = (np.linalg.norm(c[3] - c[0]) + np.linalg.norm(c[2] - c[1])) / 2
        return (w + h) / 2   # average side length in pixels

    best      = max(items, key=_qr_size_px)
    size_px   = _qr_size_px(best)
    px_per_cm = size_px / qr_size_cm
    claw_px   = round(claw_reach_cm * px_per_cm)

    _registry.set_claw_offset_px(claw_px)
    _navigator._claw_offset_px = claw_px
    log.info("Scale calibration: QR=%.1fpx / %.1fcm → %.2fpx/cm  claw=%dpx",
             size_px, qr_size_cm, px_per_cm, claw_px)
    return {
        "ok":          True,
        "qr_payload":  best["payload"],
        "qr_size_px":  round(size_px, 1),
        "px_per_cm":   round(px_per_cm, 2),
        "claw_offset_px": claw_px,
    }


@app.post("/api/setup/generate-base")
async def api_generate_base():
    """Generate base station QR PNG in qr_codes/ and auto-register it."""
    try:
        path = _generate_base_qr()
        _registry.set("base", BASE_QR_CONTENT)
        asyncio.create_task(_broadcast_state())
        return {"ok": True, "content": BASE_QR_CONTENT, "path": path}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ── test routes ──────────────────────────────────────────────────────────────

@app.post("/api/test/goto")
async def api_test_goto(body: dict):
    global _test_target
    if _mission and _mission.state != "idle":
        return {"ok": False, "reason": "mission running — abort it first"}
    station = body.get("station", "")
    qr = _registry.get(station) if _registry else None
    if not qr:
        return {"ok": False, "reason": f"station '{station}' not registered"}
    _test_target = station
    _navigator.set_target(qr)
    _navigator.set_navigating(True)
    log.info("Test: navigating to %s (QR=%s)", station, qr)
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


@app.post("/api/test/goto-qr")
async def api_test_goto_qr(body: dict):
    global _test_target
    if _mission and _mission.state != "idle":
        return {"ok": False, "reason": "mission running — abort it first"}
    qr = body.get("qr", "").strip()
    if not qr:
        return {"ok": False, "reason": "qr required"}
    if _navigator is None:
        return {"ok": False, "reason": "navigator not running"}
    _test_target = qr
    _navigator.set_target(qr)
    _navigator.set_navigating(True)
    log.info("Test: navigating to QR=%s", qr)
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


@app.post("/api/test/stop")
async def api_test_stop():
    global _test_target
    _test_target = None
    if _navigator:
        _navigator.set_navigating(False)
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


@app.get("/api/camera/list")
async def api_camera_list():
    """List available /dev/video* capture devices with labels from v4l2-ctl."""
    import glob as _glob, subprocess
    devices = []
    for path in sorted(_glob.glob("/dev/video*")):
        try:
            idx = int(path.replace("/dev/video", ""))
        except ValueError:
            continue
        label = path
        try:
            result = subprocess.run(
                ["v4l2-ctl", f"--device={path}", "--info"],
                capture_output=True, text=True, timeout=2,
            )
            for line in result.stdout.splitlines():
                if "Card type" in line:
                    label = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass
        devices.append({"index": idx, "path": path, "label": label})
    current = getattr(_app_config, "camera_index", 0) if _app_config else 0
    return {"devices": devices, "current": current}


@app.post("/api/camera/switch")
async def api_camera_switch(body: dict):
    """Switch active camera by restarting the navigator with a new index."""
    global _navigator, _nav_thread
    if _mission and _mission.state != "idle":
        return {"ok": False, "reason": "mission running — abort it first"}
    idx = body.get("index")
    if idx is None:
        return {"ok": False, "reason": "index required"}
    idx = int(idx)
    if _navigator:
        _navigator.stop()
    if _nav_thread:
        _nav_thread.join(timeout=3.0)
    _app_config.camera_index = idx
    _navigator = build_navigator(_app_config, on_state_change=_on_state_change)
    _apply_bot_qr()
    if _mission:
        _mission._nav = _navigator
    _nav_thread = threading.Thread(target=_navigator.run, daemon=True, name="navigator")
    _nav_thread.start()
    log.info("Camera switched to index %d", idx)
    asyncio.create_task(_broadcast_state())
    return {"ok": True, "index": idx}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket):
    await websocket.accept()
    async with _ws_lock:
        _ws_clients.add(websocket)
    await websocket.send_text(_make_payload())
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with _ws_lock:
            _ws_clients.discard(websocket)


# ── app lifecycle ─────────────────────────────────────────────────────────────

_app_config = None


def _apply_bot_qr() -> None:
    """Push current bot QR, heading offset, and claw offset from registry into the navigator."""
    if _navigator and _registry:
        bot_qr        = _registry.get("bot")
        offset        = _registry.get_heading_offset()
        claw_offset   = _registry.get_claw_offset_px()
        _navigator.set_bot_qr(bot_qr)
        _navigator._heading_offset = offset
        if claw_offset > 0:
            _navigator._claw_offset_px = claw_offset
        log.info("Navigator bot_qr=%s heading_offset=%.1f° claw_offset=%dpx",
                 bot_qr or "None", math.degrees(offset), _navigator._claw_offset_px)


@app.on_event("startup")
async def _startup():
    global _loop, _navigator, _nav_thread, _mission, _registry
    _loop     = asyncio.get_event_loop()
    _registry = StationsRegistry()

    _navigator = build_navigator(_app_config, on_state_change=_on_state_change)
    _apply_bot_qr()

    _mission   = MissionController(_navigator, _registry)
    _mission.set_on_change(
        lambda: asyncio.run_coroutine_threadsafe(_broadcast_state(), _loop)
    )

    _nav_thread = threading.Thread(target=_navigator.run, daemon=True, name="navigator")
    _nav_thread.start()
    mode = "overhead" if _registry.get("bot") else "onboard"
    log.info("Navigator started [%s mode]", mode)


@app.on_event("shutdown")
async def _shutdown():
    if _navigator:
        _navigator.stop()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Bug Navigator mission control")
    parser.add_argument("--config", default=None)
    parser.add_argument("--host",   default="0.0.0.0")
    parser.add_argument("--port",   type=int, default=8080)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    global _app_config
    _app_config = load_config(args.config) if args.config else load_config()

    log.info("Bug Navigator starting at http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
