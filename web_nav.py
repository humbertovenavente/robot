"""Mission-control web interface for QR-guided robot delivery.

Setup:   register QR codes for base + 3 drop stations (persisted to stations.json)
Mission: open claw → navigate to package → return to base → select station
         → deliver → return to base → close claw

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
from navigator import (
    QRNavigator,
    NavigatorState,
    build_navigator,
    _detect_aruco,
    _detect_overhead,
    _qr_heading,
)

QR_CODES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_codes")

log = logging.getLogger(__name__)

STATIONS_FILE  = os.path.join(os.path.dirname(__file__), "stations.json")
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
        return {v for v in self._data.values() if isinstance(v, str) and v}

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

    def set_claw_center_offset_px(self, px: float) -> None:
        self._data["claw_center_offset_px"] = round(float(px), 1)
        self.save()

    def get_claw_center_offset_px(self) -> Optional[float]:
        px = self._data.get("claw_center_offset_px")
        return float(px) if px is not None else None

    def clear_claw_center_offset_px(self) -> None:
        self._data.pop("claw_center_offset_px", None)
        self.save()

    def set_base_parking_offset(self, dx: float, dy: float) -> None:
        self._data["base_parking_offset"] = {"dx": round(dx, 1), "dy": round(dy, 1)}
        self.save()

    def get_base_parking_offset(self) -> Optional[tuple]:
        off = self._data.get("base_parking_offset")
        if off and (off.get("dx") or off.get("dy")):
            return float(off["dx"]), float(off["dy"])
        return None

    def clear_base_parking_offset(self) -> None:
        self._data.pop("base_parking_offset", None)
        self.save()

    def set_station_drop_offset(self, station_key: str, dx: float, dy: float) -> None:
        if station_key not in ("station_1", "station_2", "station_3"):
            raise ValueError(f"Unknown station: {station_key}")
        offsets = self._data.setdefault("station_drop_offsets", {})
        offsets[station_key] = {"dx": round(dx, 1), "dy": round(dy, 1)}
        self.save()

    def get_station_drop_offset(self, station_key: str) -> Optional[tuple]:
        offsets = self._data.get("station_drop_offsets", {})
        off = offsets.get(station_key)
        if off and ("dx" in off) and ("dy" in off):
            return float(off["dx"]), float(off["dy"])
        return None

    def clear_station_drop_offset(self, station_key: str) -> None:
        offsets = self._data.get("station_drop_offsets", {})
        offsets.pop(station_key, None)
        if not offsets:
            self._data.pop("station_drop_offsets", None)
        self.save()

    def all_station_drop_offsets(self) -> dict:
        offsets = self._data.get("station_drop_offsets", {})
        return {
            k: {"dx": float(v["dx"]), "dy": float(v["dy"])}
            for k, v in offsets.items()
            if k in ("station_1", "station_2", "station_3") and ("dx" in v) and ("dy" in v)
        }

    def set_bot_aruco_id(self, marker_id: int) -> None:
        self._data["bot_aruco_id"] = int(marker_id)
        self.save()

    def get_bot_aruco_id(self) -> Optional[int]:
        marker_id = self._data.get("bot_aruco_id")
        return int(marker_id) if marker_id is not None else None

    def clear_bot_aruco_id(self) -> None:
        self._data.pop("bot_aruco_id", None)
        self.save()

    # ── package queue ──────────────────────────────────────────────────────────
    def register_package(self, qr: str, station_key: str) -> None:
        pkgs = self._data.setdefault("packages", {})
        pkgs[qr] = station_key
        self.save()

    def unregister_package(self, qr: str) -> None:
        self._data.get("packages", {}).pop(qr, None)
        self.save()

    def get_package_station(self, qr: str) -> Optional[str]:
        return self._data.get("packages", {}).get(qr)

    def all_packages(self) -> dict:
        return dict(self._data.get("packages", {}))

    def clear_all_packages(self) -> None:
        self._data.pop("packages", None)
        self.save()


# ── mission controller ────────────────────────────────────────────────────────

class MissionController:
    """State machine that orchestrates a full pick-and-deliver cycle.

    States
    ------
    idle → opening_claw → going_to_package → grabbing_package → returning_to_base →
    awaiting_destination → going_to_station → dropping_off →
    returning_to_base_after_drop → closing_claw → idle
    """

    def __init__(self, navigator: QRNavigator, registry: StationsRegistry):
        self._nav      = navigator
        self._reg      = registry
        self.state     = "idle"
        self.package_qr: Optional[str] = None
        self.destination: Optional[str] = None   # station key
        self._arrived  = threading.Event()
        self._dest_set = threading.Event()
        self._abort    = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._abort_thread: Optional[threading.Thread] = None
        self._on_change = None
        self._claw_is_open: Optional[bool] = None

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

    def _reset_context(self) -> None:
        self.package_qr = None
        self.destination = None

    def on_navigator_arrived(self, qr_payload: str) -> None:
        """Called by the navigator state-change hook when status == 'arrived'."""
        self._arrived.set()

    def _aborted(self) -> bool:
        return self._abort.is_set()

    def _navigate_to(self, qr_code: str, label: str, timeout: float = 120.0,
                     claw_mode: bool = False, claw_adjust_px: int = 0,
                     allow_reverse: bool = True, force_reverse: bool = False,
                     target_offset: Optional[tuple] = None,
                     pickup_contact: bool = False) -> bool:
        self._arrived.clear()
        self._nav.set_target(qr_code)
        self._nav.set_target_offset(target_offset)
        self._nav.set_motion_policy(allow_reverse=allow_reverse, force_reverse=force_reverse)
        if claw_mode:
            self._nav.set_claw_arrived(True, adjust_px=claw_adjust_px)
        self._nav.set_pickup_contact_arrived(pickup_contact)
        self._nav.set_navigating(True)
        log.info("Mission: navigating to %s (QR=%s, claw_mode=%s, claw_adjust_px=%d, allow_reverse=%s, force_reverse=%s, target_offset=%s, pickup_contact=%s)",
                 label, qr_code, claw_mode, claw_adjust_px, allow_reverse, force_reverse, target_offset, pickup_contact)
        deadline = time.monotonic() + timeout
        ok = False
        while time.monotonic() < deadline:
            if self._aborted():
                break
            if self._arrived.wait(timeout=0.1):
                ok = True
                break
        self._nav.set_navigating(False)
        self._nav.set_target_offset(None)
        self._nav.set_motion_policy(allow_reverse=True, force_reverse=False)
        self._nav.set_pickup_contact_arrived(False)
        if claw_mode:
            self._nav.set_claw_arrived(False)
        if self._aborted():
            log.info("Mission: aborted while navigating to %s", label)
            return False
        if not ok:
            log.warning("Mission: timeout waiting to arrive at %s", label)
        return ok

    def _claw_open(self, settle_s: float = 0.4) -> None:
        drive = self._nav._drive
        drive.open_claw()
        time.sleep(settle_s)
        self._claw_is_open = True

    def _claw_close(self, settle_s: float = 0.4) -> None:
        drive = self._nav._drive
        drive.close_claw()
        time.sleep(settle_s)
        self._claw_is_open = False

    def _near_base_parking(self) -> bool:
        st = getattr(self._nav, "state", None)
        if not st or not st.bot_pos or not st.parking_target_pos:
            return False
        dx = st.bot_pos[0] - st.parking_target_pos[0]
        dy = st.bot_pos[1] - st.parking_target_pos[1]
        return math.hypot(dx, dy) <= 90

    def _depart_base_forward(self, duration_s: float = 2.0) -> None:
        if not self._near_base_parking():
            return
        drive = self._nav._drive
        self._nav.set_navigating(False)
        self._nav.set_motor_override(True)
        try:
            # Needs to be gentle, but still above drivetrain static friction.
            nudge_speed = max(65, min(75, int(getattr(self._nav, "_drive_speed", 50))))
            drive.move_forward(nudge_speed)
            time.sleep(duration_s)
        finally:
            self._nav.set_motor_override(False)
            drive.stop_motors()

    def _clear_base_forward(self, distance_px: Optional[float] = None, timeout_s: float = 4.0) -> None:
        """Drive straight forward out of the base slot before station alignment.

        Uses overhead pose to stop after advancing a target distance along the
        current heading. Falls back to the time-based nudge if pose is missing.
        """
        if not self._near_base_parking():
            return

        st = getattr(self._nav, "state", None)
        if not st or not st.bot_pos or st.bot_heading is None:
            self._depart_base_forward()
            return

        target_px = float(distance_px) if distance_px is not None else 0.0
        if target_px <= 0:
            target_px = float(getattr(self._nav, "_claw_center_offset_px", 0.0) or 0.0)
        if target_px <= 0:
            target_px = float(getattr(self._nav, "_claw_offset_px", 0) or 0)
        if target_px <= 0:
            self._depart_base_forward()
            return

        start_x, start_y = st.bot_pos
        hdg = float(st.bot_heading)
        ux, uy = math.cos(hdg), math.sin(hdg)
        drive = self._nav._drive
        self._nav.set_navigating(False)
        self._nav.set_motor_override(True)
        try:
            clear_speed = max(65, min(75, int(getattr(self._nav, "_drive_speed", 50))))
            drive.move_forward(clear_speed)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if self._aborted():
                    break
                cur = getattr(self._nav, "state", None)
                if not cur or not cur.bot_pos:
                    time.sleep(0.03)
                    continue
                dx = cur.bot_pos[0] - start_x
                dy = cur.bot_pos[1] - start_y
                forward_progress = dx * ux + dy * uy
                if forward_progress >= target_px:
                    break
                time.sleep(0.03)
        finally:
            self._nav.set_motor_override(False)
            drive.stop_motors()

    def _station_push_forward(self, duration_s: float = 0.4) -> None:
        """After station QR occlusion, keep moving forward a bit so the package lands more centered."""
        drive = self._nav._drive
        self._nav.set_navigating(False)
        self._nav.set_motor_override(True)
        try:
            push_speed = max(30, min(60, int(getattr(self._nav, "_drive_speed", 50) * 0.8)))
            drive.move_forward(push_speed)
            time.sleep(duration_s)
        finally:
            self._nav.set_motor_override(False)
            drive.stop_motors()

    def _creep_to_package(self, duration_s: float = 1.0) -> None:
        """Slow forward push after centering on the package — closes the remaining gap."""
        drive = self._nav._drive
        self._nav.set_motor_override(True)
        try:
            creep_speed = max(62, int(getattr(self._nav, "_drive_speed", 62)))
            drive.move_forward(creep_speed)
            time.sleep(duration_s)
        finally:
            self._nav.set_motor_override(False)
            drive.stop_motors()

    def _backup_after_grab(self, timeout_s: float = 5.0) -> None:
        """Reverse after closing the claw to clear the pickup spot.

        Default distance: half the arm length.
        If the front is closer to the frame edge than the back, back up a full arm length.
        If the back is already near the frame edge, skip the reverse.
        """
        st = getattr(self._nav, "state", None)
        if not st or not st.bot_pos or st.bot_heading is None:
            return

        arm_px = float(getattr(self._nav, "_claw_center_offset_px", 0.0) or 0.0)
        if arm_px <= 0:
            arm_px = float(getattr(self._nav, "_claw_offset_px", 0) or 0)
        if arm_px <= 0:
            return

        bx, by   = st.bot_pos
        hdg      = float(st.bot_heading)
        fw       = float(st.frame_w or 640)
        fh       = float(st.frame_h or 480)
        cos_h, sin_h = math.cos(hdg), math.sin(hdg)

        def _ray_dist(dx, dy):
            """Pixels until the ray (bx+t*dx, by+t*dy) hits a frame boundary."""
            ts = []
            if abs(dx) > 1e-6:
                ts.append(((fw - bx) / dx) if dx > 0 else (bx / (-dx)))
            if abs(dy) > 1e-6:
                ts.append(((fh - by) / dy) if dy > 0 else (by / (-dy)))
            return min(ts) if ts else float("inf")

        front_dist = _ray_dist( cos_h,  sin_h)
        back_dist  = _ray_dist(-cos_h, -sin_h)

        _EDGE_MARGIN_PX = 70
        if back_dist < _EDGE_MARGIN_PX:
            return  # back already near frame edge — skip reverse

        if front_dist < back_dist:
            target_px = arm_px          # front near edge → back up full arm length
        else:
            target_px = arm_px * 0.5    # normal → back up half arm length

        target_px = min(target_px, back_dist - _EDGE_MARGIN_PX)
        if target_px <= 5:
            return

        start_x, start_y = st.bot_pos
        drive = self._nav._drive
        self._nav.set_motor_override(True)
        try:
            back_speed = max(50, min(70, int(getattr(self._nav, "_drive_speed", 60) * 0.85)))
            drive.drive(-back_speed, -back_speed)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if self._aborted():
                    break
                cur = getattr(self._nav, "state", None)
                if not cur or not cur.bot_pos:
                    time.sleep(0.03)
                    continue
                dx = cur.bot_pos[0] - start_x
                dy = cur.bot_pos[1] - start_y
                backward_progress = -(dx * cos_h + dy * sin_h)
                if backward_progress >= target_px:
                    break
                time.sleep(0.03)
        finally:
            self._nav.set_motor_override(False)
            drive.stop_motors()

    def _run(self) -> None:
        try:
            self._abort.clear()
            base_qr = self._reg.get("base")

            # 1. Open claw before package retrieval
            self._set_state("opening_claw")
            t = threading.Thread(target=self._claw_open, daemon=True)
            t.start(); t.join()
            if self._aborted():
                self._reset_context()
                self._set_state("idle")
                return

            # 2. Go to package
            self._set_state("going_to_package")
            if not self._navigate_to(
                self.package_qr, "package", claw_mode=True, allow_reverse=False, pickup_contact=True,
                claw_adjust_px=35,
            ):
                self._reset_context()
                self._set_state("idle")
                return

            # 3. Creep forward to seat the claw on the package, then close
            self._creep_to_package()
            if self._aborted():
                self._reset_context()
                self._set_state("idle")
                return
            self._set_state("grabbing_package")
            t = threading.Thread(target=self._claw_close, kwargs={"settle_s": 0.8}, daemon=True)
            t.start(); t.join()
            if self._aborted():
                self._reset_context()
                self._set_state("idle")
                return
            self._backup_after_grab()

            # 4. Go directly to station — no return to base
            self._set_state("going_to_station")
            dest_qr = self._reg.get(self.destination)
            drop_offset = self._reg.get_station_drop_offset(self.destination) if self.destination else None
            if not dest_qr or not self._navigate_to(
                dest_qr, self.destination, claw_mode=True, claw_adjust_px=0,
                allow_reverse=False, target_offset=drop_offset
            ):
                self._reset_context()
                self._set_state("idle")
                return

            # 5. Open claw to release at the station
            self._set_state("dropping_off")
            t = threading.Thread(target=self._claw_open, kwargs={"settle_s": 1.5}, daemon=True)
            t.start(); t.join()
            if self._aborted():
                self._reset_context()
                self._set_state("idle")
                return

            # 6. Return to base
            self._set_state("returning_to_base_after_drop")
            if not base_qr or not self._navigate_to(base_qr, "base", force_reverse=True):
                self._reset_context()
                self._set_state("idle")
                return

            # 7. Close claw once back at base
            self._set_state("closing_claw")
            t = threading.Thread(target=self._claw_close, daemon=True)
            t.start(); t.join()

            self._reset_context()
            self._set_state("idle")

        except Exception as exc:
            log.error("Mission error: %s", exc, exc_info=True)
            self._reset_context()
            self._set_state("idle")

    def start(self, package_qr: str, destination: str) -> bool:
        if self.state != "idle":
            return False
        if destination not in ["station_1", "station_2", "station_3"]:
            return False
        self._abort.clear()
        self.package_qr  = package_qr
        self.destination = destination
        self._thread = threading.Thread(target=self._run, daemon=True, name="mission")
        self._thread.start()
        return True

    def abort(self) -> None:
        if self._abort_thread and self._abort_thread.is_alive():
            return
        self._abort.set()
        self._nav.set_navigating(False)
        self._arrived.clear()
        self._dest_set.clear()
        self._abort_thread = threading.Thread(target=self._run_abort_recovery,
                                              daemon=True, name="mission-abort")
        self._abort_thread.start()

    def _run_abort_recovery(self) -> None:
        try:
            mission_thread = self._thread
            if mission_thread and mission_thread.is_alive():
                mission_thread.join(timeout=2.0)

            base_qr = self._reg.get("base")

            if self._claw_is_open is not True:
                self._set_state("dropping_off")
                self._claw_open(settle_s=1.0)

            self._abort.clear()
            if base_qr:
                self._set_state("returning_to_base_after_drop")
                self._navigate_to(base_qr, "base", force_reverse=True)

            self._set_state("closing_claw")
            self._claw_close(settle_s=0.8)
        finally:
            self._abort.clear()
            self._reset_context()
            self.state = "idle"
            self._emit()


# ── auto mission controller ───────────────────────────────────────────────────

class AutoMissionController:
    """Autonomous loop: watches the frame for registered packages and dispatches
    missions one at a time without human intervention.

    States: idle → waiting_for_package → dispatching → (mission runs) → waiting_for_package …
    """

    def __init__(self, mission: MissionController, navigator: QRNavigator,
                 registry: StationsRegistry):
        self._mission   = mission
        self._nav       = navigator
        self._registry  = registry
        self.state      = "idle"
        self._running   = threading.Event()
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

    def _set_state(self, s: str) -> None:
        self.state = s
        log.info("AutoMission: → %s", s)
        self._emit()

    def start(self) -> bool:
        if self.state != "idle":
            return False
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="auto-mission")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running.clear()
        # Let the current mission finish naturally; the loop will exit after it completes.

    def _find_package(self):
        """Return (package_qr, station_key) for the first registered package visible in frame."""
        nav_state = getattr(self._nav, "state", None)
        if not nav_state:
            return None, None
        reserved = self._registry.qr_set()  # station/base/bot QR values
        for qr in (nav_state.all_qr_payloads or []):
            if not qr or qr in reserved:
                continue
            station = self._registry.get_package_station(qr)
            if station:
                return qr, station
        return None, None

    def _loop(self) -> None:
        try:
            while self._running.is_set():
                self._set_state("waiting_for_package")

                # Wait until any ongoing mission finishes and mission controller is idle
                while self._running.is_set() and self._mission.state != "idle":
                    time.sleep(0.2)
                if not self._running.is_set():
                    break

                # Scan for a registered package visible in frame
                pkg_qr, station = None, None
                while self._running.is_set() and pkg_qr is None:
                    pkg_qr, station = self._find_package()
                    if pkg_qr is None:
                        time.sleep(0.3)

                if not self._running.is_set():
                    break

                log.info("AutoMission: detected package '%s' → %s", pkg_qr, station)
                self._set_state("dispatching")

                ok = self._mission.start(pkg_qr, station)
                if not ok:
                    log.warning("AutoMission: failed to start mission (mission state=%s)", self._mission.state)
                    time.sleep(1.0)
                    continue

                # Wait for mission to complete
                while self._running.is_set() and self._mission.state != "idle":
                    time.sleep(0.2)

                # Auto-unregister the delivered package so it isn't re-dispatched
                self._registry.unregister_package(pkg_qr)
                log.info("AutoMission: package '%s' delivered and unregistered", pkg_qr)
                self._emit()

        finally:
            self._set_state("idle")


# ── FastAPI app + shared state ────────────────────────────────────────────────

app = FastAPI(title="Bug Navigator")

_navigator: Optional[QRNavigator] = None
_nav_thread: Optional[threading.Thread] = None
_mission: Optional[MissionController] = None
_auto_mission: Optional["AutoMissionController"] = None
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
        "auto_state":     _auto_mission.state if _auto_mission else "idle",
        "qr_payload":     s.qr_payload if s else None,
        "qr_area_pct":    round(s.qr_area_pct * 100, 1) if s and s.qr_area_pct else None,
        "frame_w":        s.frame_w if s else None,
        "all_qr_payloads": s.all_qr_payloads if s else [],
        "nxt_connected":  _nxt_connected(),
        "package_qr":     _mission.package_qr if _mission else None,
        "destination":    _mission.destination if _mission else None,
        "bot_aruco_id":   _registry.get_bot_aruco_id() if _registry else None,
        "claw_center_offset_px": _registry.get_claw_center_offset_px() if _registry else None,
        "stations":          _registry.all() if _registry else {},
        "station_drop_offsets": _registry.all_station_drop_offsets() if _registry else {},
        "registered_packages": _registry.all_packages() if _registry else {},
        "test_target":       _test_target,
        "full_station_qrs":  list(s.full_station_qrs) if s else [],
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
.badge.opening_claw{background:#3a2600;color:#fb923c}
.badge.going_to_package{background:#0e3a4a;color:#38bdf8}
.badge.grabbing_package{background:#3a2600;color:#fb923c}
.badge.returning_to_base{background:#1e1040;color:#a78bfa}
.badge.awaiting_destination{background:#2d2800;color:#fbbf24}
.badge.going_to_station{background:#0e3a4a;color:#38bdf8}
.badge.dropping_off{background:#3a2600;color:#fb923c}
.badge.returning_to_base_after_drop{background:#1e1040;color:#a78bfa}
.badge.closing_claw{background:#3a2600;color:#fb923c}
.badge.waiting_for_package{background:#1a2a00;color:#a3e635}
.badge.dispatching{background:#0e3a4a;color:#38bdf8}
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
.btn-secondary{background:#4b5563;color:#9ca3af;cursor:not-allowed}
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
  <button class="tab-btn" onclick="showTab('auto')">Auto Mission</button>
  <button class="tab-btn" onclick="showTab('mission')">Mission Control</button>
  <button class="tab-btn" onclick="showTab('test')">Testing</button>
  <button class="tab-btn" onclick="showTab('manual')">Manual Drive</button>
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

  <!-- Base parking position -->
  <div class="section" style="margin-top:14px;max-width:520px">
    <h3>Base parking position</h3>
    <p style="font-size:.76rem;color:#666;margin-bottom:10px">
      Define where the robot should stop when returning to base. Place the base QR in view,
      then click <b>Pick position</b> and click the desired parking spot on the image.
    </p>
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <button class="btn-primary" onclick="openParkModal()">Pick position</button>
      <button class="btn-neutral btn-sm" onclick="clearParking()">Clear</button>
      <span style="font-size:.8rem;color:#666">
        Offset: <span id="setup-parking-offset" style="color:#e8e8e8;font-family:monospace">not set</span>
      </span>
    </div>
  </div>

  <!-- Bot direction marker -->
  <div class="section" style="margin-top:14px;max-width:520px">
    <h3>Bot Direction Marker (ArUco)</h3>
    <p style="font-size:.76rem;color:#666;margin-bottom:10px">
      Keep the <b>Bot QR</b> for robot identity, and mount an <b>ArUco marker</b> at the
      front of the robot for direction. Generate one here, print it, mount it at the nose,
      then save its ID.
    </p>
    <div style="display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap">
      <label style="font-size:.78rem;color:#888;display:flex;flex-direction:column;gap:3px">
        ArUco ID
        <input id="aruco-id" type="number" value="0" min="0" max="49" step="1"
          style="width:100px;padding:5px 8px;border-radius:6px;border:1px solid #333;
                 background:#111;color:#e8e8e8;font-size:.85rem">
      </label>
      <button class="btn-warn" onclick="generateAruco()">Generate PNG</button>
      <button class="btn-primary" onclick="saveBotAruco()">Save as bot front</button>
      <button class="btn-neutral btn-sm" onclick="clearBotAruco()">Clear</button>
      <button class="btn-neutral btn-sm" onclick="refreshArucoInfo()">Refresh</button>
    </div>
    <div style="margin-top:8px;display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <span style="font-size:.8rem;color:#666">
        Configured ID: <span id="setup-aruco-id" style="color:#e8e8e8;font-family:monospace">—</span>
      </span>
      <span style="font-size:.8rem;color:#666">
        Visible IDs: <span id="setup-aruco-visible" style="color:#e8e8e8;font-family:monospace">—</span>
      </span>
    </div>
    <div id="aruco-result" style="margin-top:8px;font-size:.78rem;color:#555;min-height:16px"></div>
  </div>

  <div class="section" style="margin-top:14px;max-width:520px">
    <h3>Virtual Claw Center</h3>
    <p style="font-size:.76rem;color:#666;margin-bottom:10px">
      Click the actual <b>claw centre</b> in the camera image while the <b>bot QR</b> and
      configured <b>front ArUco</b> are visible. The system stores the projected pixel
      distance from the ArUco centre along the bot's forward axis.
    </p>
    <div style="display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap">
      <button class="btn-primary" onclick="openClawCenterModal()">📍 Set Claw Center</button>
      <button class="btn-neutral btn-sm" onclick="refreshClawCenterInfo()">Refresh</button>
      <button class="btn-neutral btn-sm" onclick="clearClawCenterOffset()">Clear</button>
    </div>
    <div style="margin-top:8px;display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <span style="font-size:.8rem;color:#666">
        Configured offset: <span id="setup-claw-center-offset" style="color:#e8e8e8;font-family:monospace">not set</span>
      </span>
      <span style="font-size:.8rem;color:#666">
        Forward axis: <span id="setup-claw-center-axis" style="color:#e8e8e8;font-family:monospace">—</span>
      </span>
    </div>
    <div id="claw-center-result" style="margin-top:8px;font-size:.78rem;color:#555;min-height:16px"></div>
  </div>

</div>

<!-- ─── AUTO MISSION TAB ─────────────────────────────────────────────────── -->
<div id="tab-auto" class="tab-pane">
  <div style="display:grid;grid-template-columns:1fr 360px;gap:16px">
    <div>
      <div class="camera-main"><img src="/stream" alt="camera feed"></div>
    </div>
    <div class="ctrl-panel">

      <!-- Auto mission control -->
      <div class="section">
        <h3>Auto Mission</h3>
        <div class="stat-row" style="margin-bottom:8px">
          <span class="stat-label">Status</span>
          <span id="auto-state-badge" class="badge idle">idle</span>
        </div>
        <div style="display:flex;gap:8px">
          <button id="btn-auto-start" class="btn-primary" style="flex:1" onclick="autoStart()">▶ Start Auto</button>
          <button id="btn-auto-stop"  class="btn-danger"  style="flex:1" onclick="autoStop()" disabled>■ Stop</button>
        </div>
      </div>

      <!-- Package registration -->
      <div class="section">
        <h3>Register Packages</h3>
        <p style="font-size:.78rem;color:#666;margin-bottom:8px">Scan packages below, assign each a destination station, then start Auto Mission.</p>
        <div id="auto-detect-list"><span style="color:#555;font-size:.8rem">Scanning…</span></div>
      </div>

      <!-- Package queue -->
      <div class="section">
        <h3>Package Queue</h3>
        <div id="auto-queue-list"><span style="color:#555;font-size:.8rem">No packages registered.</span></div>
      </div>

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
        <div class="flow-step" id="fs-opening_claw">Open Claw</div>
        <div class="flow-step" id="fs-going_to_package">→ Package</div>
        <div class="flow-step" id="fs-grabbing_package">Grab</div>
        <div class="flow-step" id="fs-going_to_station">→ Station</div>
        <div class="flow-step" id="fs-dropping_off">Open Claw</div>
        <div class="flow-step" id="fs-returning_to_base_after_drop">→ Base</div>
        <div class="flow-step" id="fs-closing_claw">Close Claw</div>
      </div>

      <!-- Mission setup: package + destination -->
      <div class="section" id="mission-setup">
        <h3>Package</h3>
        <div id="pkg-list"><span style="color:#555;font-size:.8rem">Scanning…</span></div>
        <h3 style="margin-top:10px">Destination</h3>
        <div class="dest-grid" id="dest-grid"></div>
        <button class="btn-primary" id="btn-start-mission" style="margin-top:10px;width:100%" disabled onclick="startMission()">
          Start Mission
        </button>
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

<!-- ─── MANUAL DRIVE TAB ─────────────────────────────────────────────── -->
<div id="tab-manual" class="tab-pane">
  <div style="display:grid;grid-template-columns:1fr 340px;gap:16px">

    <!-- Left: camera feed -->
    <div>
      <div class="camera-main"><img src="/stream" alt="camera feed"></div>
    </div>

    <!-- Right: controls -->
    <div style="display:flex;flex-direction:column;gap:12px">

      <div class="section">
        <h3>Speed</h3>
        <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
          <input type="range" id="manual-speed" min="20" max="100" value="60" style="flex:1"
            oninput="document.getElementById('manual-spd-val').textContent=this.value">
          <span id="manual-spd-val" style="font-family:monospace;color:#a78bfa;min-width:28px">60</span>
        </div>
      </div>

      <div class="section">
        <h3>Drive (hold to move)</h3>
        <div style="display:grid;grid-template-columns:repeat(3,58px);grid-template-rows:repeat(3,54px);
                    gap:5px;margin-top:8px;user-select:none;-webkit-user-select:none">
          <div></div>
          <button class="btn-primary" id="mbtn-fwd"   style="font-size:1.3rem;touch-action:none">↑</button>
          <div></div>
          <button class="btn-neutral" id="mbtn-left"  style="font-size:1.3rem;touch-action:none">←</button>
          <button class="btn-danger"  id="mbtn-stop"  style="font-size:1rem;touch-action:none">■</button>
          <button class="btn-neutral" id="mbtn-right" style="font-size:1.3rem;touch-action:none">→</button>
          <div></div>
          <button class="btn-warn"    id="mbtn-back"  style="font-size:1.3rem;touch-action:none">↓</button>
          <div></div>
        </div>
        <p style="font-size:.72rem;color:#555;margin-top:8px">Hold = drive · release = stop</p>
      </div>

      <div class="section">
        <h3>Motor output</h3>
        <div class="stat-row" style="margin-top:6px">
          <span class="stat-label">Left</span>
          <span id="manual-L" class="stat-val" style="font-family:monospace">0</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Right</span>
          <span id="manual-R" class="stat-val" style="font-family:monospace">0</span>
        </div>
        <div class="stat-row" style="margin-top:6px">
          <span class="stat-label">Status</span>
          <span id="manual-status" class="stat-val" style="color:#555">stopped</span>
        </div>
      </div>

    </div>
  </div>
</div>

<!-- ─── PARKING MODAL ──────────────────────────────────────────────────── -->
<div id="park-modal" style="display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.88);z-index:1000;justify-content:center;align-items:center">
  <div style="background:#1a1a2e;border-radius:12px;padding:20px;
    max-width:95vw;max-height:95vh;display:flex;flex-direction:column;gap:12px;
    border:2px solid #7c3aed">
    <h2 style="color:#a78bfa;font-size:1rem;margin:0">Set Base Parking Position</h2>
    <p style="font-size:.78rem;color:#888;margin:0">
      Click the spot where the robot should park near the
      <b style="color:#4ade80">base QR</b>. Green box = base QR.
      Orange circle = current parking spot.
    </p>
    <div style="overflow:auto;border-radius:6px;background:#111">
      <canvas id="park-canvas" style="max-width:80vw;cursor:crosshair;display:block"></canvas>
    </div>
    <div id="park-status" style="font-size:.78rem;color:#fb923c;min-height:14px"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn-neutral btn-sm" onclick="refreshParkFrame()">Refresh frame</button>
      <button class="btn-success btn-sm" id="park-confirm" onclick="confirmParking()" disabled>
        Save position
      </button>
      <button class="btn-neutral btn-sm" onclick="closeParkModal()">Cancel</button>
    </div>
  </div>
</div>

<!-- ─── STATION DROP MODAL ─────────────────────────────────────────────── -->
<div id="station-drop-modal" style="display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.88);z-index:1000;justify-content:center;align-items:center">
  <div style="background:#1a1a2e;border-radius:12px;padding:20px;
    max-width:95vw;max-height:95vh;display:flex;flex-direction:column;gap:12px;
    border:2px solid #16a34a">
    <h2 id="station-drop-title" style="color:#4ade80;font-size:1rem;margin:0">Set Station Drop Target</h2>
    <p style="font-size:.78rem;color:#888;margin:0">
      Click the spot where the <b style="color:#fb923c">claw centre</b> should align when dropping.
      Green box = station QR. Orange circle = current saved drop point.
    </p>
    <div style="overflow:auto;border-radius:6px;background:#111">
      <canvas id="station-drop-canvas" style="max-width:80vw;cursor:crosshair;display:block"></canvas>
    </div>
    <div id="station-drop-status" style="font-size:.78rem;color:#fb923c;min-height:14px"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn-neutral btn-sm" onclick="refreshStationDropFrame()">Refresh frame</button>
      <button class="btn-success btn-sm" id="station-drop-confirm" onclick="confirmStationDrop()" disabled>
        Save drop target
      </button>
      <button class="btn-neutral btn-sm" onclick="clearStationDrop()">Clear</button>
      <button class="btn-neutral btn-sm" onclick="closeStationDropModal()">Cancel</button>
    </div>
  </div>
</div>

<!-- ─── CLAW CENTER MODAL ──────────────────────────────────────────────── -->
<div id="claw-center-modal" style="display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.88);z-index:1000;justify-content:center;align-items:center">
  <div style="background:#1a1a2e;border-radius:12px;padding:20px;
    max-width:95vw;max-height:95vh;display:flex;flex-direction:column;gap:12px;
    border:2px solid #f59e0b">
    <h2 style="color:#f59e0b;font-size:1rem;margin:0">Set Virtual Claw Center</h2>
    <p style="font-size:.78rem;color:#888;margin:0">
      Click the real <b style="color:#38bdf8">claw centre</b>. The orange point is the current
      saved position, measured from the <b style="color:#fde047">ArUco centre</b> along the
      <b style="color:#4ade80">forward axis</b> from the bot QR to the ArUco.
    </p>
    <div style="overflow:auto;border-radius:6px;background:#111">
      <canvas id="claw-center-canvas" style="max-width:80vw;cursor:crosshair;display:block"></canvas>
    </div>
    <div id="claw-center-status" style="font-size:.78rem;color:#fb923c;min-height:14px"></div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn-neutral btn-sm" onclick="refreshClawCenterFrame()">Refresh frame</button>
      <button class="btn-success btn-sm" id="claw-center-confirm" onclick="confirmClawCenter()" disabled>
        Save claw center
      </button>
      <button class="btn-neutral btn-sm" onclick="clearClawCenterOffset()">Clear</button>
      <button class="btn-neutral btn-sm" onclick="closeClawCenterModal()">Cancel</button>
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
const FLOW_STATES    = ['idle','opening_claw','going_to_package','grabbing_package',
                        'going_to_station','dropping_off','returning_to_base_after_drop','closing_claw'];

let stationsData   = {};
let stationDropOffsets = {};
let allQrPayloads  = [];
let missionState   = 'idle';
let autoState      = 'idle';
let fullStationKeys = new Set();  // station keys (station_1 etc.) currently full
let selectedPackage = null;
let selectedStation = null;
let registeredPackages = {};  // {qr: station_key}

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
    if (selectedPackage) { selectedPackage = null; _updateStartBtn(); }
    box.innerHTML = '<span style="color:#555;font-size:.8rem">No packages in frame</span>';
    return;
  }
  // Deselect if previously selected package is no longer visible
  if (selectedPackage && !pkgs.includes(selectedPackage)) {
    selectedPackage = null; _updateStartBtn();
  }
  box.innerHTML = '';
  pkgs.forEach(qr => {
    const row = document.createElement('div');
    row.className = 'pkg-item';
    const label = document.createElement('span');
    label.className = 'pkg-qr';
    label.textContent = qr;
    const btn = document.createElement('button');
    const isSelected = (selectedPackage === qr);
    btn.className = isSelected ? 'btn-success btn-sm' : 'btn-secondary btn-sm';
    btn.textContent = isSelected ? '✓ Selected' : 'Select';
    btn.disabled = (missionState !== 'idle');
    btn.onclick = () => { selectedPackage = isSelected ? null : qr; renderPackages(allQrPayloads); _updateStartBtn(); };
    row.appendChild(label);
    row.appendChild(btn);
    box.appendChild(row);
  });
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                  .replace(/"/g,'&quot;');
}

// ── full state update ─────────────────────────────────────────────────────────
function applyState(s) {
  stationsData       = s.stations || {};
  stationDropOffsets = s.station_drop_offsets || {};
  allQrPayloads      = s.all_qr_payloads || [];
  missionState       = s.mission_state || 'idle';
  autoState          = s.auto_state   || 'idle';
  registeredPackages = s.registered_packages || {};
  const clawCenterOffset = s.claw_center_offset_px;

  // Map full QR payloads → station keys
  const fullQRs = new Set(s.full_station_qrs || []);
  fullStationKeys = new Set();
  for (const [key, qr] of Object.entries(stationsData)) {
    if (qr && fullQRs.has(qr)) fullStationKeys.add(key);
  }

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
  renderDestButtons();

  // Show/hide destination panel
  document.getElementById('mission-setup').style.display =
    missionState === 'idle' ? 'block' : 'none';
  _updateStartBtn();

  // Abort button
  document.getElementById('btn-abort').disabled = (missionState === 'idle');

  // Auto mission tab
  const ab = document.getElementById('auto-state-badge');
  if (ab) { ab.textContent = autoState; ab.className = 'badge ' + autoState; }
  const btnAs = document.getElementById('btn-auto-start');
  const btnAx = document.getElementById('btn-auto-stop');
  if (btnAs) btnAs.disabled = (autoState !== 'idle');
  if (btnAx) btnAx.disabled = (autoState === 'idle');
  renderAutoDetect(allQrPayloads);
  renderAutoQueue();

  if (s.nxt_connected !== undefined) applyNxt(s.nxt_connected);

  // Testing tab stats
  document.getElementById('t-nav').textContent    = s.nav_status || '—';
  document.getElementById('t-target').textContent = s.test_target || '—';
  document.getElementById('t-area').textContent   = s.qr_area_pct != null ? s.qr_area_pct + ' %' : '—';

  const clawOffsetEl = document.getElementById('setup-claw-center-offset');
  if (clawOffsetEl) {
    clawOffsetEl.textContent = clawCenterOffset != null ? `${Number(clawCenterOffset).toFixed(1)} px` : 'not set';
  }

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
    if (key.startsWith('station_')) {
      const hasDrop = !!stationDropOffsets[key];
      extra += ` <button class="btn-neutral btn-sm" onclick="openStationDropModal('${key}')"${qr?'':' disabled'}>${hasDrop ? '🎯 Edit Drop' : '🎯 Set Drop'}</button>`;
    }

    const isFull = fullStationKeys.has(key);
    row.innerHTML = `
      <span class="label">${STATION_LABELS[key]}</span>
      ${isFull ? '<span class="badge" style="background:#7f1d1d;color:#fca5a5;font-size:.65rem">FULL</span>' : ''}
      ${(key.startsWith('station_') && stationDropOffsets[key]) ? '<span class="badge" style="background:#0a2e0a;color:#4ade80;font-size:.65rem">DROP SET</span>' : ''}
      <span class="qr-val ${qr ? '' : 'empty'}">${qr ? esc(qr) : 'not set'}</span>
      ${extra}
      <button class="btn-primary btn-sm" onclick="scanStation('${key}')">Scan</button>
      <button class="btn-neutral btn-sm" onclick="clearStation('${key}')"${qr?'':' disabled'}>✕</button>
    `;
    list.appendChild(row);
  });

  const baseOk   = !!stationsData['base'];
  const botOk    = !!stationsData['bot'];
  const stCount  = ['station_1','station_2','station_3'].filter(k => stationsData[k]).length;
  const status   = document.getElementById('setup-status');
  const modeNote = botOk ? ' [overhead mode]' : ' [onboard mode — add Bot QR for overhead]';
  status.textContent = baseOk
    ? `✓ Base registered — ${stCount}/3 station(s) set. Missions ready.` + modeNote
    : 'Register Base QR to enable missions. Stations are optional.' + modeNote;
  status.className = 'setup-status' + (baseOk ? ' ok' : '');
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
    if (key === 'bot') {
      refreshArucoInfo();
      refreshClawCenterInfo();
    }
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
    if (key === 'bot') {
      refreshArucoInfo();
      refreshClawCenterInfo();
    }
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
    if (d.ok) {
      document.querySelectorAll('img[src*="/stream"]').forEach(img => {
        img.src = '';
        setTimeout(() => { img.src = '/stream'; }, 300);
      });
    } else {
      await loadCameras();
    }
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
    const qr   = stationsData[key];
    const full = fullStationKeys.has(key);
    const btn  = document.createElement('button');
    btn.className   = full ? 'btn-warn' : 'btn-primary';
    btn.textContent = 'Go to ' + STATION_LABELS[key] + (full ? ' ⚠ FULL' : '');
    btn.disabled    = !qr || missionState !== 'idle';
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

// ── manual drive ──────────────────────────────────────────────────────────────
function _manualSpd() { return parseInt(document.getElementById('manual-speed').value) || 60; }

function _manualSetDisplay(l, r) {
  document.getElementById('manual-L').textContent = l;
  document.getElementById('manual-R').textContent = r;
  document.getElementById('manual-status').textContent = (l !== 0 || r !== 0) ? 'driving' : 'stopped';
  document.getElementById('manual-status').style.color = (l !== 0 || r !== 0) ? '#4ade80' : '#555';
}

async function _manualSend(left, right) {
  _manualSetDisplay(left, right);
  try {
    await fetch('/api/manual/drive', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({left, right})
    });
  } catch(e) {}
}

async function _manualStopSend() {
  _manualSetDisplay(0, 0);
  try { await fetch('/api/manual/stop', {method:'POST'}); } catch(e) {}
}

function _setupManualBtn(id, lFn, rFn) {
  const btn = document.getElementById(id);
  if (!btn) return;
  const go   = () => { const s=_manualSpd(); _manualSend(lFn(s), rFn(s)); };
  const halt = () => _manualStopSend();
  btn.addEventListener('mousedown',  go);
  btn.addEventListener('mouseup',    halt);
  btn.addEventListener('mouseleave', halt);
  btn.addEventListener('touchstart', e => { e.preventDefault(); go(); },   {passive:false});
  btn.addEventListener('touchend',   e => { e.preventDefault(); halt(); }, {passive:false});
}

document.addEventListener('DOMContentLoaded', () => {
  _setupManualBtn('mbtn-fwd',   s => s,   s => s);   // forward
  _setupManualBtn('mbtn-back',  s => -s,  s => -s);  // backward
  _setupManualBtn('mbtn-left',  s => -s,  s => s);   // spin left
  _setupManualBtn('mbtn-right', s => s,   s => -s);  // spin right
  const stopBtn = document.getElementById('mbtn-stop');
  if (stopBtn) stopBtn.addEventListener('click', _manualStopSend);
});

// ── destination buttons ───────────────────────────────────────────────────────
function renderDestButtons() {
  const grid = document.getElementById('dest-grid');
  if (!grid) return;
  grid.innerHTML = '';
  ['station_1','station_2','station_3'].forEach(key => {
    const registered = !!stationsData[key];
    const full = fullStationKeys.has(key);
    const isSelected = (selectedStation === key);
    const btn  = document.createElement('button');
    if (!registered) {
      btn.className = 'btn-secondary';
      btn.textContent = STATION_LABELS[key] + ' (not set)';
    } else if (isSelected) {
      btn.className = 'btn-success';
      btn.textContent = '✓ ' + STATION_LABELS[key];
    } else {
      btn.className = full ? 'btn-danger' : 'btn-neutral';
      btn.textContent = STATION_LABELS[key] + (full ? ' (FULL)' : '');
    }
    btn.disabled = (missionState !== 'idle') || !registered;
    btn.onclick  = () => { selectedStation = isSelected ? null : key; renderDestButtons(); _updateStartBtn(); };
    grid.appendChild(btn);
  });
}

function _updateStartBtn() {
  const btn = document.getElementById('btn-start-mission');
  if (!btn) return;
  btn.disabled = (missionState !== 'idle') || !selectedPackage || !selectedStation;
}

// ── mission actions ───────────────────────────────────────────────────────────
async function startMission() {
  if (!selectedPackage || !selectedStation) return;
  const r = await fetch('/api/mission/start', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({package_qr: selectedPackage, station: selectedStation})
  });
  const d = await r.json();
  if (d.ok) {
    addLog(`Mission started → "${selectedPackage}" → ${STATION_LABELS[selectedStation]}`);
    selectedPackage = null; selectedStation = null;
  } else {
    addLog('Start failed: ' + d.reason);
  }
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

// ── auto mission ─────────────────────────────────────────────────────────────

function renderAutoDetect(qrs) {
  const box = document.getElementById('auto-detect-list');
  if (!box) return;
  const reserved = new Set(Object.values(stationsData).filter(Boolean));
  const candidates = qrs.filter(q => q && !reserved.has(q) && !registeredPackages[q]);
  if (!candidates.length) {
    box.innerHTML = '<span style="color:#555;font-size:.8rem">No unregistered packages in frame.</span>';
    return;
  }
  box.innerHTML = '';
  candidates.forEach(qr => {
    const row = document.createElement('div');
    row.className = 'pkg-item';
    const label = document.createElement('span');
    label.className = 'pkg-qr';
    label.textContent = qr;
    const sel = document.createElement('select');
    sel.style.cssText = 'background:#111;color:#e8e8e8;border:1px solid #333;border-radius:5px;padding:3px 6px;font-size:.8rem';
    sel.innerHTML = '<option value="">Station…</option>' +
      ['station_1','station_2','station_3'].map(k => {
        const registered = !!stationsData[k];
        return `<option value="${k}"${!registered?' disabled':''}>${STATION_LABELS[k]}${!registered?' (not set)':''}</option>`;
      }).join('');
    const btn = document.createElement('button');
    btn.className = 'btn-primary btn-sm';
    btn.textContent = 'Register';
    btn.onclick = async () => {
      if (!sel.value) return;
      const r = await fetch('/api/packages/register', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({qr, station: sel.value})
      });
      const d = await r.json();
      addLog(d.ok ? `Registered "${qr}" → ${STATION_LABELS[sel.value]}` : 'Error: ' + d.reason);
    };
    row.appendChild(label);
    row.appendChild(sel);
    row.appendChild(btn);
    box.appendChild(row);
  });
}

function renderAutoQueue() {
  const box = document.getElementById('auto-queue-list');
  if (!box) return;
  const entries = Object.entries(registeredPackages);
  if (!entries.length) {
    box.innerHTML = '<span style="color:#555;font-size:.8rem">No packages registered.</span>';
    return;
  }
  box.innerHTML = '';
  entries.forEach(([qr, stationKey]) => {
    const row = document.createElement('div');
    row.className = 'pkg-item';
    const label = document.createElement('span');
    label.className = 'pkg-qr';
    label.textContent = qr;
    const dest = document.createElement('span');
    dest.style.cssText = 'font-size:.78rem;color:#a78bfa;margin-left:4px';
    dest.textContent = '→ ' + (STATION_LABELS[stationKey] || stationKey);
    const btn = document.createElement('button');
    btn.className = 'btn-danger btn-sm';
    btn.textContent = 'Remove';
    btn.onclick = async () => {
      const r = await fetch('/api/packages/unregister', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({qr})
      });
      addLog((await r.json()).ok ? `Removed "${qr}"` : 'Remove failed');
    };
    row.appendChild(label);
    row.appendChild(dest);
    row.appendChild(btn);
    box.appendChild(row);
  });
}

async function autoStart() {
  const r = await fetch('/api/auto/start', {method:'POST'});
  const d = await r.json();
  addLog(d.ok ? 'Auto mission started' : 'Start failed: ' + d.reason);
}

async function autoStop() {
  const r = await fetch('/api/auto/stop', {method:'POST'});
  addLog((await r.json()).ok ? 'Auto mission stopping…' : 'Stop failed');
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

// State polling fallback — keeps packages/stations live without WebSocket
setInterval(async () => {
  try {
    const s = await (await fetch('/api/status')).json();
    applyState(s);
  } catch(_){}
}, 2000);

// ── parking modal ─────────────────────────────────────────────────────────────
let _parkInfo  = null;   // base QR info from server
let _parkClick = null;   // {x, y} clicked in image coords
let _parkImgEl = null;

function _drawParkCanvas() {
  const canvas = document.getElementById('park-canvas');
  if (!_parkImgEl) return;
  const ctx = canvas.getContext('2d');
  canvas.width  = _parkImgEl.naturalWidth;
  canvas.height = _parkImgEl.naturalHeight;
  ctx.drawImage(_parkImgEl, 0, 0);

  if (!_parkInfo || !_parkInfo.found) {
    ctx.fillStyle = '#ff4444';
    ctx.font = 'bold 16px sans-serif';
    ctx.fillText('Base QR not detected — refresh or check camera.', 16, 36);
    return;
  }

  const {cx, cy, x, y, w, h, offset} = _parkInfo;

  // Base QR box
  ctx.strokeStyle = '#00ff88'; ctx.lineWidth = 3;
  ctx.strokeRect(x, y, w, h);
  ctx.fillStyle = '#00ff88';
  ctx.beginPath(); ctx.arc(cx, cy, 5, 0, Math.PI*2); ctx.fill();

  // Existing parking offset
  if (offset) {
    const px = cx + offset.dx, py = cy + offset.dy;
    ctx.fillStyle = '#ff8c00';
    ctx.beginPath(); ctx.arc(px, py, 10, 0, Math.PI*2); ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(px, py, 10, 0, Math.PI*2); ctx.stroke();
    ctx.fillStyle = '#ff8c00'; ctx.font = 'bold 12px sans-serif';
    ctx.fillText('current', px + 14, py + 5);
  }

  // New click position
  if (_parkClick) {
    ctx.fillStyle = '#38bdf8';
    ctx.beginPath(); ctx.arc(_parkClick.x, _parkClick.y, 10, 0, Math.PI*2); ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(_parkClick.x, _parkClick.y, 10, 0, Math.PI*2); ctx.stroke();
    ctx.fillStyle = '#38bdf8'; ctx.font = 'bold 12px sans-serif';
    ctx.fillText('new', _parkClick.x + 14, _parkClick.y + 5);
  }
}

async function openParkModal() {
  document.getElementById('park-modal').style.display = 'flex';
  document.getElementById('park-status').textContent  = 'Loading…';
  _parkClick = null;
  document.getElementById('park-confirm').disabled = true;
  await refreshParkFrame();
}

async function refreshParkFrame() {
  const [infoRes, imgUrl] = await Promise.all([
    fetch('/api/setup/base-parking-info').then(r => r.json()),
    Promise.resolve('/api/setup/snapshot?' + Date.now()),
  ]);
  _parkInfo = infoRes;

  const status = document.getElementById('park-status');
  if (!infoRes.found) {
    status.textContent = '⚠ ' + (infoRes.reason || 'Base QR not found');
  } else {
    const off = infoRes.offset;
    status.textContent = off
      ? `Current offset: (${off.dx}, ${off.dy}) px from base centre`
      : 'No parking position set — click to place one.';
    _applyParkingOffset(off);
  }

  const img = new Image();
  img.onload = () => { _parkImgEl = img; _drawParkCanvas(); };
  img.src = imgUrl;
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('park-canvas').addEventListener('click', e => {
    if (!_parkInfo || !_parkInfo.found) return;
    const canvas = document.getElementById('park-canvas');
    const rect   = canvas.getBoundingClientRect();
    const scaleX = canvas.width  / rect.width;
    const scaleY = canvas.height / rect.height;
    _parkClick = {
      x: (e.clientX - rect.left) * scaleX,
      y: (e.clientY - rect.top)  * scaleY,
    };
    _drawParkCanvas();
    document.getElementById('park-confirm').disabled = false;
    document.getElementById('park-status').textContent =
      `New position set — click Save to confirm.`;
  });
});

async function confirmParking() {
  if (!_parkClick || !_parkInfo || !_parkInfo.found) return;
  const dx = _parkClick.x - _parkInfo.cx;
  const dy = _parkClick.y - _parkInfo.cy;
  const r = await fetch('/api/setup/base-parking', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({dx, dy}),
  });
  const d = await r.json();
  if (d.ok) {
    addLog(`Parking position saved (offset ${dx.toFixed(0)}, ${dy.toFixed(0)} px)`);
    _applyParkingOffset({dx, dy});
    closeParkModal();
  } else {
    document.getElementById('park-status').textContent = 'Error: ' + d.reason;
  }
}

async function clearParking() {
  const r = await fetch('/api/setup/base-parking/clear', {method: 'POST'});
  if ((await r.json()).ok) {
    addLog('Parking position cleared');
    _applyParkingOffset(null);
  }
}

function _applyParkingOffset(off) {
  const el = document.getElementById('setup-parking-offset');
  if (!el) return;
  el.textContent = off ? `(${off.dx.toFixed(0)}, ${off.dy.toFixed(0)}) px` : 'not set';
}

// ── ArUco setup ───────────────────────────────────────────────────────────────
function _applyArucoInfo(info) {
  const configured = document.getElementById('setup-aruco-id');
  const visible = document.getElementById('setup-aruco-visible');
  const input = document.getElementById('aruco-id');
  const result = document.getElementById('aruco-result');
  if (configured) configured.textContent = info.configured_id != null ? String(info.configured_id) : 'not set';
  if (visible) {
    visible.textContent = info.visible_ids && info.visible_ids.length ? info.visible_ids.join(', ') : 'none';
  }
  if (input && info.configured_id != null) input.value = info.configured_id;
  if (result) {
    result.style.color = info.found ? '#4ade80' : '#555';
    result.textContent = info.found
      ? `Configured marker ${info.configured_id} is visible and locked to bot heading.`
      : (info.reason || 'No configured ArUco marker visible.');
  }
}

async function refreshArucoInfo() {
  try {
    const info = await (await fetch('/api/setup/aruco-info')).json();
    _applyArucoInfo(info);
  } catch(_) {}
}

async function saveBotAruco() {
  const id = parseInt(document.getElementById('aruco-id').value, 10);
  const result = document.getElementById('aruco-result');
  if (isNaN(id) || id < 0 || id > 49) {
    result.style.color = '#ef4444';
    result.textContent = 'ArUco ID must be between 0 and 49.';
    return;
  }
  const r = await fetch('/api/setup/aruco-config', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id})
  });
  const d = await r.json();
  result.style.color = d.ok ? '#4ade80' : '#ef4444';
  result.textContent = d.ok ? `Bot front ArUco set to ID ${id}.` : ('✗ ' + d.reason);
  if (d.ok) {
    addLog(`Bot front ArUco set to ID ${id}`);
    refreshArucoInfo();
    refreshClawCenterInfo();
  }
}

async function clearBotAruco() {
  const r = await fetch('/api/setup/aruco-clear', {method:'POST'});
  const d = await r.json();
  const result = document.getElementById('aruco-result');
  result.style.color = d.ok ? '#4ade80' : '#ef4444';
  result.textContent = d.ok ? 'Bot front ArUco cleared.' : ('✗ ' + d.reason);
  if (d.ok) {
    addLog('Bot front ArUco cleared');
    refreshArucoInfo();
    refreshClawCenterInfo();
  }
}

async function generateAruco() {
  const id = parseInt(document.getElementById('aruco-id').value, 10);
  const result = document.getElementById('aruco-result');
  if (isNaN(id) || id < 0 || id > 49) {
    result.style.color = '#ef4444';
    result.textContent = 'ArUco ID must be between 0 and 49.';
    return;
  }
  const r = await fetch('/api/setup/generate-aruco', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id})
  });
  const d = await r.json();
  result.style.color = d.ok ? '#4ade80' : '#ef4444';
  result.textContent = d.ok
    ? `Generated ArUco ID ${id} at ${d.path}`
    : ('✗ ' + d.reason);
  if (d.ok) {
    addLog(`Generated ArUco ID ${id} → ${d.path}`);
  }
}

// ── claw center setup ────────────────────────────────────────────────────────
let _clawCenterInfo  = null;
let _clawCenterClick = null;
let _clawCenterImgEl = null;

function _drawClawCenterCanvas() {
  const canvas = document.getElementById('claw-center-canvas');
  if (!_clawCenterImgEl) return;
  const ctx = canvas.getContext('2d');
  canvas.width  = _clawCenterImgEl.naturalWidth;
  canvas.height = _clawCenterImgEl.naturalHeight;
  ctx.drawImage(_clawCenterImgEl, 0, 0);

  if (!_clawCenterInfo || !_clawCenterInfo.found) {
    ctx.fillStyle = '#ff4444';
    ctx.font = 'bold 16px sans-serif';
    ctx.fillText('Bot QR / ArUco not detected — refresh or check setup.', 16, 36);
    return;
  }

  const {bot_x, bot_y, bot_w, bot_h, bot_cx, bot_cy, aruco_cx, aruco_cy, axis_dx, axis_dy, offset_px} = _clawCenterInfo;
  ctx.strokeStyle = '#00ff88';
  ctx.lineWidth = 3;
  ctx.strokeRect(bot_x, bot_y, bot_w, bot_h);
  ctx.fillStyle = '#00ff88';
  ctx.beginPath();
  ctx.arc(bot_cx, bot_cy, 5, 0, Math.PI * 2);
  ctx.fill();

  ctx.strokeStyle = '#fde047';
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.arc(aruco_cx, aruco_cy, 11, 0, Math.PI * 2);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(bot_cx, bot_cy);
  ctx.lineTo(aruco_cx, aruco_cy);
  ctx.stroke();

  ctx.strokeStyle = '#4ade80';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(aruco_cx, aruco_cy);
  ctx.lineTo(aruco_cx + axis_dx * 140, aruco_cy + axis_dy * 140);
  ctx.stroke();

  if (offset_px != null) {
    const px = aruco_cx + axis_dx * offset_px;
    const py = aruco_cy + axis_dy * offset_px;
    ctx.fillStyle = '#ff8c00';
    ctx.beginPath();
    ctx.arc(px, py, 10, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(px, py, 10, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = '#ff8c00';
    ctx.font = 'bold 12px sans-serif';
    ctx.fillText('current', px + 14, py + 5);
  }

  if (_clawCenterClick) {
    ctx.fillStyle = '#38bdf8';
    ctx.beginPath();
    ctx.arc(_clawCenterClick.x, _clawCenterClick.y, 10, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(_clawCenterClick.x, _clawCenterClick.y, 10, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = '#38bdf8';
    ctx.font = 'bold 12px sans-serif';
    ctx.fillText('new', _clawCenterClick.x + 14, _clawCenterClick.y + 5);
  }
}

function _applyClawCenterInfo(info) {
  const offsetEl = document.getElementById('setup-claw-center-offset');
  const axisEl = document.getElementById('setup-claw-center-axis');
  const resultEl = document.getElementById('claw-center-result');
  if (offsetEl) {
    offsetEl.textContent = info.offset_px != null ? `${Number(info.offset_px).toFixed(1)} px` : 'not set';
  }
  if (axisEl) {
    axisEl.textContent = info.found
      ? `${Number(info.axis_dx).toFixed(3)}, ${Number(info.axis_dy).toFixed(3)}`
      : '—';
  }
  if (resultEl) {
    resultEl.style.color = info.found ? '#4ade80' : '#555';
    resultEl.textContent = info.found
      ? 'Bot QR and front ArUco are visible. You can calibrate the claw center from this view.'
      : (info.reason || 'Bot QR and configured ArUco must both be visible.');
  }
}

async function refreshClawCenterInfo() {
  try {
    const info = await (await fetch('/api/setup/claw-center-info')).json();
    _applyClawCenterInfo(info);
  } catch(_) {}
}

async function openClawCenterModal() {
  _clawCenterClick = null;
  document.getElementById('claw-center-modal').style.display = 'flex';
  document.getElementById('claw-center-status').textContent = 'Loading…';
  document.getElementById('claw-center-confirm').disabled = true;
  await refreshClawCenterFrame();
}

async function refreshClawCenterFrame() {
  const [infoRes, imgUrl] = await Promise.all([
    fetch('/api/setup/claw-center-info').then(r => r.json()),
    Promise.resolve('/api/setup/snapshot?' + Date.now()),
  ]);
  _clawCenterInfo = infoRes;
  _applyClawCenterInfo(infoRes);
  const status = document.getElementById('claw-center-status');
  if (!infoRes.found) {
    status.textContent = '⚠ ' + (infoRes.reason || 'Bot QR / ArUco not found');
  } else {
    status.textContent = infoRes.offset_px != null
      ? `Current claw center offset: ${Number(infoRes.offset_px).toFixed(1)} px from ArUco centre along forward axis`
      : 'No claw center set — click the claw center to calibrate it.';
  }

  const img = new Image();
  img.onload = () => { _clawCenterImgEl = img; _drawClawCenterCanvas(); };
  img.src = imgUrl;
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('claw-center-canvas').addEventListener('click', e => {
    if (!_clawCenterInfo || !_clawCenterInfo.found) return;
    const canvas = document.getElementById('claw-center-canvas');
    const rect   = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    _clawCenterClick = {
      x: (e.clientX - rect.left) * scaleX,
      y: (e.clientY - rect.top) * scaleY,
    };
    _drawClawCenterCanvas();
    const relX = _clawCenterClick.x - _clawCenterInfo.aruco_cx;
    const relY = _clawCenterClick.y - _clawCenterInfo.aruco_cy;
    const offsetPx = relX * _clawCenterInfo.axis_dx + relY * _clawCenterInfo.axis_dy;
    const lateralPx = -relX * _clawCenterInfo.axis_dy + relY * _clawCenterInfo.axis_dx;
    document.getElementById('claw-center-confirm').disabled = false;
    document.getElementById('claw-center-status').textContent =
      `Projected offset ${offsetPx.toFixed(1)} px, lateral miss ${lateralPx.toFixed(1)} px — click Save to confirm.`;
  });
});

async function confirmClawCenter() {
  if (!_clawCenterInfo || !_clawCenterInfo.found || !_clawCenterClick) return;
  const relX = _clawCenterClick.x - _clawCenterInfo.aruco_cx;
  const relY = _clawCenterClick.y - _clawCenterInfo.aruco_cy;
  const offsetPx = relX * _clawCenterInfo.axis_dx + relY * _clawCenterInfo.axis_dy;
  const lateralPx = -relX * _clawCenterInfo.axis_dy + relY * _clawCenterInfo.axis_dx;
  const r = await fetch('/api/setup/claw-center', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({offset_px: offsetPx}),
  });
  const d = await r.json();
  if (d.ok) {
    addLog(`Claw center saved at ${offsetPx.toFixed(1)} px from ArUco (lateral miss ${lateralPx.toFixed(1)} px)`);
    closeClawCenterModal();
    refreshClawCenterInfo();
  } else {
    document.getElementById('claw-center-status').textContent = 'Error: ' + d.reason;
  }
}

async function clearClawCenterOffset() {
  const r = await fetch('/api/setup/claw-center/clear', {method: 'POST'});
  const d = await r.json();
  if (d.ok) {
    addLog('Claw center calibration cleared');
    _applyClawCenterInfo({found: false, reason: 'Claw center cleared.', offset_px: null});
    if (document.getElementById('claw-center-modal').style.display === 'flex') {
      document.getElementById('claw-center-status').textContent = 'Claw center cleared.';
      document.getElementById('claw-center-confirm').disabled = true;
    }
  } else {
    const status = document.getElementById('claw-center-status');
    if (status) status.textContent = 'Error: ' + d.reason;
  }
}

function closeClawCenterModal() {
  document.getElementById('claw-center-modal').style.display = 'none';
  _clawCenterInfo = null;
  _clawCenterClick = null;
  _clawCenterImgEl = null;
}

function closeParkModal() {
  document.getElementById('park-modal').style.display = 'none';
  _parkClick = null; _parkInfo = null; _parkImgEl = null;
}

// ── station drop modal ───────────────────────────────────────────────────────
let _stationDropKey   = null;
let _stationDropInfo  = null;
let _stationDropClick = null;
let _stationDropImgEl = null;

function _drawStationDropCanvas() {
  const canvas = document.getElementById('station-drop-canvas');
  if (!_stationDropImgEl) return;
  const ctx = canvas.getContext('2d');
  canvas.width  = _stationDropImgEl.naturalWidth;
  canvas.height = _stationDropImgEl.naturalHeight;
  ctx.drawImage(_stationDropImgEl, 0, 0);

  if (!_stationDropInfo || !_stationDropInfo.found) {
    ctx.fillStyle = '#ff4444';
    ctx.font = 'bold 16px sans-serif';
    ctx.fillText('Station QR not detected — refresh or check camera.', 16, 36);
    return;
  }

  const {cx, cy, x, y, w, h, offset} = _stationDropInfo;
  ctx.strokeStyle = '#00ff88'; ctx.lineWidth = 3;
  ctx.strokeRect(x, y, w, h);
  ctx.fillStyle = '#00ff88';
  ctx.beginPath(); ctx.arc(cx, cy, 5, 0, Math.PI*2); ctx.fill();

  if (offset) {
    const px = cx + offset.dx, py = cy + offset.dy;
    ctx.fillStyle = '#ff8c00';
    ctx.beginPath(); ctx.arc(px, py, 10, 0, Math.PI*2); ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(px, py, 10, 0, Math.PI*2); ctx.stroke();
    ctx.fillStyle = '#ff8c00'; ctx.font = 'bold 12px sans-serif';
    ctx.fillText('current', px + 14, py + 5);
  }

  if (_stationDropClick) {
    ctx.fillStyle = '#38bdf8';
    ctx.beginPath(); ctx.arc(_stationDropClick.x, _stationDropClick.y, 10, 0, Math.PI*2); ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(_stationDropClick.x, _stationDropClick.y, 10, 0, Math.PI*2); ctx.stroke();
    ctx.fillStyle = '#38bdf8'; ctx.font = 'bold 12px sans-serif';
    ctx.fillText('new', _stationDropClick.x + 14, _stationDropClick.y + 5);
  }
}

async function openStationDropModal(station) {
  _stationDropKey = station;
  _stationDropClick = null;
  document.getElementById('station-drop-modal').style.display = 'flex';
  document.getElementById('station-drop-title').textContent = `Set ${STATION_LABELS[station]} Drop Target`;
  document.getElementById('station-drop-status').textContent = 'Loading…';
  document.getElementById('station-drop-confirm').disabled = true;
  await refreshStationDropFrame();
}

async function refreshStationDropFrame() {
  if (!_stationDropKey) return;
  const [infoRes, imgUrl] = await Promise.all([
    fetch('/api/setup/station-drop-info?station=' + encodeURIComponent(_stationDropKey)).then(r => r.json()),
    Promise.resolve('/api/setup/snapshot?' + Date.now()),
  ]);
  _stationDropInfo = infoRes;
  const status = document.getElementById('station-drop-status');
  if (!infoRes.found) {
    status.textContent = '⚠ ' + (infoRes.reason || 'Station QR not found');
  } else {
    const off = infoRes.offset;
    status.textContent = off
      ? `Current drop offset: (${off.dx}, ${off.dy}) px from station QR centre`
      : 'No drop target set — click to place one.';
  }
  const img = new Image();
  img.onload = () => { _stationDropImgEl = img; _drawStationDropCanvas(); };
  img.src = imgUrl;
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('station-drop-canvas').addEventListener('click', e => {
    if (!_stationDropInfo || !_stationDropInfo.found) return;
    const canvas = document.getElementById('station-drop-canvas');
    const rect   = canvas.getBoundingClientRect();
    const scaleX = canvas.width  / rect.width;
    const scaleY = canvas.height / rect.height;
    _stationDropClick = {
      x: (e.clientX - rect.left) * scaleX,
      y: (e.clientY - rect.top)  * scaleY,
    };
    _drawStationDropCanvas();
    document.getElementById('station-drop-confirm').disabled = false;
    document.getElementById('station-drop-status').textContent =
      'New drop target set — click Save to confirm.';
  });
});

async function confirmStationDrop() {
  if (!_stationDropKey || !_stationDropClick || !_stationDropInfo || !_stationDropInfo.found) return;
  const dx = _stationDropClick.x - _stationDropInfo.cx;
  const dy = _stationDropClick.y - _stationDropInfo.cy;
  const r = await fetch('/api/setup/station-drop', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({station: _stationDropKey, dx, dy}),
  });
  const d = await r.json();
  if (d.ok) {
    stationDropOffsets[_stationDropKey] = {dx, dy};
    addLog(`${STATION_LABELS[_stationDropKey]} drop target saved (${dx.toFixed(0)}, ${dy.toFixed(0)} px)`);
    renderStations();
    closeStationDropModal();
  } else {
    document.getElementById('station-drop-status').textContent = 'Error: ' + d.reason;
  }
}

async function clearStationDrop() {
  if (!_stationDropKey) return;
  const r = await fetch('/api/setup/station-drop/clear', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({station: _stationDropKey}),
  });
  const d = await r.json();
  if (d.ok) {
    delete stationDropOffsets[_stationDropKey];
    addLog(`${STATION_LABELS[_stationDropKey]} drop target cleared`);
    renderStations();
    closeStationDropModal();
  } else {
    document.getElementById('station-drop-status').textContent = 'Error: ' + d.reason;
  }
}

function closeStationDropModal() {
  document.getElementById('station-drop-modal').style.display = 'none';
  _stationDropKey = null;
  _stationDropInfo = null;
  _stationDropClick = null;
  _stationDropImgEl = null;
}

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
    const pinfo = await (await fetch('/api/setup/base-parking-info')).json();
    if (pinfo.offset) _applyParkingOffset(pinfo.offset);
  } catch(_) {}
  refreshArucoInfo();
  refreshClawCenterInfo();
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
            try:
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
            except Exception as exc:
                log.warning("stream: frame error: %s", exc)
                time.sleep(0.05)
                continue
            time.sleep(0.04)
    return StreamingResponse(generate(),
                             media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/nxt-status")
async def api_nxt_status():
    return {"connected": _nxt_connected()}


@app.get("/api/nxt-battery")
async def api_nxt_battery():
    if _navigator is None:
        return {"ok": False, "reason": "navigator not running"}
    drive = getattr(_navigator, "_drive", None)
    brick = getattr(drive, "_brick", None)
    if brick is None:
        return {"ok": False, "reason": "NXT not connected"}
    try:
        mv = brick.get_battery_level()
        return {"ok": True, "mv": mv, "v": round(mv / 1000, 2)}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


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
        "bot_aruco_id":    _registry.get_bot_aruco_id() if _registry else None,
        "claw_center_offset_px": _registry.get_claw_center_offset_px() if _registry else None,
        "stations":        _registry.all() if _registry else {},
        "station_drop_offsets": _registry.all_station_drop_offsets() if _registry else {},
        "full_station_qrs": list(s.full_station_qrs) if s else [],
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
        if station == "base":
            _apply_base_parking()
        if station in ("station_1", "station_2", "station_3"):
            _registry.clear_station_drop_offset(station)
            _apply_station_qrs()
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
    if station in ("station_1", "station_2", "station_3"):
        _registry.clear_station_drop_offset(station)
        _apply_station_qrs()
    asyncio.create_task(_broadcast_state())
    return {"ok": True}

# ── mission routes ────────────────────────────────────────────────────────────

@app.post("/api/mission/start")
async def api_mission_start(body: dict):
    package_qr = body.get("package_qr", "").strip()
    station    = body.get("station", "").strip()
    if not package_qr:
        return {"ok": False, "reason": "package_qr required"}
    if not station:
        return {"ok": False, "reason": "station required"}
    if not _registry.get("base"):
        return {"ok": False, "reason": "base QR not registered"}
    if _registry and not _registry.is_package_qr(package_qr):
        return {"ok": False, "reason": f'"{package_qr}" is registered as a station/base, not a package'}
    if not _registry.get(station):
        return {"ok": False, "reason": f'station "{station}" not registered'}
    log.info("Mission start: package_qr=%s destination=%s", package_qr, station)
    ok = _mission.start(package_qr, station)
    if ok:
        asyncio.create_task(_broadcast_state())
    return {"ok": ok, "reason": "" if ok else "mission already running"}


@app.post("/api/mission/abort")
async def api_mission_abort():
    _mission.abort()
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


# ── package queue routes ──────────────────────────────────────────────────────

@app.get("/api/packages")
async def api_packages_list():
    return {"packages": _registry.all_packages() if _registry else {}}

@app.post("/api/packages/register")
async def api_packages_register(body: dict):
    qr      = body.get("qr", "").strip()
    station = body.get("station", "").strip()
    if not qr:
        return {"ok": False, "reason": "qr required"}
    if station not in ("station_1", "station_2", "station_3"):
        return {"ok": False, "reason": "invalid station"}
    if not _registry.get(station):
        return {"ok": False, "reason": f'station "{station}" not registered'}
    if _registry.qr_set() and qr in _registry.qr_set():
        return {"ok": False, "reason": "QR is already registered as a station or base"}
    _registry.register_package(qr, station)
    log.info("Package registered: %s → %s", qr, station)
    asyncio.create_task(_broadcast_state())
    return {"ok": True}

@app.post("/api/packages/unregister")
async def api_packages_unregister(body: dict):
    qr = body.get("qr", "").strip()
    if not qr:
        return {"ok": False, "reason": "qr required"}
    _registry.unregister_package(qr)
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


# ── auto mission routes ───────────────────────────────────────────────────────

@app.post("/api/auto/start")
async def api_auto_start():
    if not _auto_mission:
        return {"ok": False, "reason": "auto mission not initialized"}
    if not _registry.get("base"):
        return {"ok": False, "reason": "base QR not registered"}
    ok = _auto_mission.start()
    asyncio.create_task(_broadcast_state())
    return {"ok": ok, "reason": "" if ok else "already running"}

@app.post("/api/auto/stop")
async def api_auto_stop():
    if _auto_mission:
        _auto_mission.stop()
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


def _generate_aruco_png(marker_id: int, size_px: int = 400) -> str:
    """Generate an ArUco marker PNG and return its file path."""
    aruco = getattr(cv2, "aruco", None)
    if aruco is None:
        raise RuntimeError("OpenCV ArUco module is not available in this build")
    if not (0 <= int(marker_id) <= 49):
        raise ValueError("marker_id must be between 0 and 49 for DICT_4X4_50")
    os.makedirs(QR_CODES_DIR, exist_ok=True)
    dictionary = aruco.getPredefinedDictionary(aruco.DICT_4X4_50)
    if hasattr(aruco, "generateImageMarker"):
        img = aruco.generateImageMarker(dictionary, int(marker_id), int(size_px))
    else:
        img = np.zeros((int(size_px), int(size_px)), dtype=np.uint8)
        aruco.drawMarker(dictionary, int(marker_id), int(size_px), img, 1)
    path = os.path.join(QR_CODES_DIR, f"aruco_4x4_50_{int(marker_id)}.png")
    cv2.imwrite(path, img)
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


@app.get("/api/setup/aruco-info")
async def api_aruco_info():
    configured_id = _registry.get_bot_aruco_id() if _registry else None
    if _navigator is None:
        return {"found": False, "reason": "navigator not running", "configured_id": configured_id, "visible_ids": []}
    with _navigator._frame_lock:
        frame = _navigator._raw_frame.copy() if _navigator._raw_frame is not None else None
    if frame is None:
        return {"found": False, "reason": "no camera frame yet", "configured_id": configured_id, "visible_ids": []}
    items = _detect_aruco(frame)
    visible_ids = sorted(i["id"] for i in items)
    marker = next((i for i in items if i["id"] == configured_id), None) if configured_id is not None else None
    if marker is None:
        return {
            "found": False,
            "reason": "configured ArUco marker not visible" if configured_id is not None else "no ArUco marker configured",
            "configured_id": configured_id,
            "visible_ids": visible_ids,
        }
    c = marker["corners"]
    return {
        "found": True,
        "configured_id": configured_id,
        "visible_ids": visible_ids,
        "cx": marker["cx"],
        "cy": marker["cy"],
        "x": int(c[:, 0].min()),
        "y": int(c[:, 1].min()),
        "w": int(c[:, 0].max() - c[:, 0].min()),
        "h": int(c[:, 1].max() - c[:, 1].min()),
    }


@app.post("/api/setup/aruco-config")
async def api_aruco_config(body: dict):
    marker_id = body.get("id")
    if marker_id is None:
        return {"ok": False, "reason": "id required"}
    marker_id = int(marker_id)
    if not (0 <= marker_id <= 49):
        return {"ok": False, "reason": "id must be between 0 and 49"}
    _registry.set_bot_aruco_id(marker_id)
    _apply_bot_qr()
    asyncio.create_task(_broadcast_state())
    return {"ok": True, "id": marker_id}


@app.post("/api/setup/aruco-clear")
async def api_aruco_clear():
    _registry.clear_bot_aruco_id()
    _apply_bot_qr()
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


def _get_claw_center_info_from_frame(frame: np.ndarray) -> dict:
    bot_qr = _registry.get("bot") if _registry else None
    configured_id = _registry.get_bot_aruco_id() if _registry else None
    saved_offset = _registry.get_claw_center_offset_px() if _registry else None
    if not bot_qr:
        return {"found": False, "reason": "bot QR not registered yet", "offset_px": saved_offset}
    if configured_id is None:
        return {"found": False, "reason": "no ArUco marker configured for the bot front", "offset_px": saved_offset}

    items = _detect_overhead(frame)
    aruco_items = _detect_aruco(frame)
    bot = next((i for i in items if i["payload"] == bot_qr), None)
    if bot is None:
        return {"found": False, "reason": "bot QR not visible in frame", "offset_px": saved_offset}
    marker = next((i for i in aruco_items if i["id"] == configured_id), None)
    if marker is None:
        return {"found": False, "reason": "configured front ArUco not visible", "offset_px": saved_offset}

    vx = float(marker["cx"] - bot["cx"])
    vy = float(marker["cy"] - bot["cy"])
    norm = math.hypot(vx, vy)
    if norm < 1e-6:
        return {"found": False, "reason": "bot QR centre and ArUco centre overlap", "offset_px": saved_offset}

    c = bot["corners"]
    return {
        "found": True,
        "bot_cx": float(bot["cx"]),
        "bot_cy": float(bot["cy"]),
        "bot_x": int(c[:, 0].min()),
        "bot_y": int(c[:, 1].min()),
        "bot_w": int(c[:, 0].max() - c[:, 0].min()),
        "bot_h": int(c[:, 1].max() - c[:, 1].min()),
        "aruco_cx": float(marker["cx"]),
        "aruco_cy": float(marker["cy"]),
        "axis_dx": vx / norm,
        "axis_dy": vy / norm,
        "offset_px": saved_offset,
        "configured_id": configured_id,
    }


@app.get("/api/setup/claw-center-info")
async def api_claw_center_info():
    if _navigator is None:
        return {"found": False, "reason": "navigator not running", "offset_px": _registry.get_claw_center_offset_px() if _registry else None}
    with _navigator._frame_lock:
        frame = _navigator._raw_frame.copy() if _navigator._raw_frame is not None else None
    if frame is None:
        return {"found": False, "reason": "no camera frame yet", "offset_px": _registry.get_claw_center_offset_px() if _registry else None}
    return _get_claw_center_info_from_frame(frame)


@app.post("/api/setup/claw-center")
async def api_claw_center_set(body: dict):
    offset_px = body.get("offset_px")
    if offset_px is None:
        return {"ok": False, "reason": "offset_px required"}
    if not _registry.get("bot"):
        return {"ok": False, "reason": "bot QR not registered"}
    if _registry.get_bot_aruco_id() is None:
        return {"ok": False, "reason": "configure the bot ArUco first"}
    try:
        offset_px = float(offset_px)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "offset_px must be numeric"}
    _registry.set_claw_center_offset_px(offset_px)
    _apply_bot_qr()
    asyncio.create_task(_broadcast_state())
    return {"ok": True, "offset_px": round(offset_px, 1)}


@app.post("/api/setup/claw-center/clear")
async def api_claw_center_clear():
    _registry.clear_claw_center_offset_px()
    _apply_bot_qr()
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


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


@app.post("/api/setup/generate-aruco")
async def api_generate_aruco(body: dict):
    """Generate an ArUco PNG in qr_codes/ for the requested marker ID."""
    try:
        marker_id = int(body.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "id required"}
    size_px = int(body.get("size_px", 400))
    try:
        path = _generate_aruco_png(marker_id, size_px=size_px)
        return {"ok": True, "id": marker_id, "path": path}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


# ── base parking routes ───────────────────────────────────────────────────────

@app.get("/api/setup/base-parking-info")
async def api_base_parking_info():
    """Base QR position in current frame + stored parking offset."""
    base_qr = _registry.get("base") if _registry else None
    if not base_qr:
        return {"found": False, "reason": "base QR not registered"}
    if _navigator is None:
        return {"found": False, "reason": "navigator not running"}
    with _navigator._frame_lock:
        frame = _navigator._raw_frame.copy() if _navigator._raw_frame is not None else None
    if frame is None:
        return {"found": False, "reason": "no camera frame yet"}
    items = _detect_overhead(frame)
    base  = next((i for i in items if i["payload"] == base_qr), None)
    offset = _registry.get_base_parking_offset() if _registry else None
    if not base:
        return {
            "found": False,
            "reason": "base QR not visible in frame",
            "offset": {"dx": offset[0], "dy": offset[1]} if offset else None,
        }
    c = base["corners"]
    return {
        "found":  True,
        "cx":     base["cx"],
        "cy":     base["cy"],
        "x":      int(c[:, 0].min()),
        "y":      int(c[:, 1].min()),
        "w":      int(c[:, 0].max() - c[:, 0].min()),
        "h":      int(c[:, 1].max() - c[:, 1].min()),
        "offset": {"dx": offset[0], "dy": offset[1]} if offset else None,
    }


@app.post("/api/setup/base-parking")
async def api_base_parking_set(body: dict):
    """Save parking offset (dx, dy) relative to base QR centre."""
    dx = body.get("dx")
    dy = body.get("dy")
    if dx is None or dy is None:
        return {"ok": False, "reason": "dx and dy required"}
    _registry.set_base_parking_offset(float(dx), float(dy))
    _apply_base_parking()
    return {"ok": True}


@app.post("/api/setup/base-parking/clear")
async def api_base_parking_clear():
    """Remove stored parking offset (robot will stop at base QR centre)."""
    _registry.clear_base_parking_offset()
    _apply_base_parking()
    return {"ok": True}


@app.get("/api/setup/station-drop-info")
async def api_station_drop_info(station: str):
    """Station QR position in current frame + saved claw-centre drop target offset."""
    if station not in ("station_1", "station_2", "station_3"):
        return {"found": False, "reason": "unknown station"}
    station_qr = _registry.get(station) if _registry else None
    if not station_qr:
        return {"found": False, "reason": f"{station} QR not registered"}
    if _navigator is None:
        return {"found": False, "reason": "navigator not running"}
    with _navigator._frame_lock:
        frame = _navigator._raw_frame.copy() if _navigator._raw_frame is not None else None
    if frame is None:
        return {"found": False, "reason": "no camera frame yet"}
    items = _detect_overhead(frame)
    station_item = next((i for i in items if i["payload"] == station_qr), None)
    offset = _registry.get_station_drop_offset(station) if _registry else None
    if not station_item:
        return {
            "found": False,
            "reason": "station QR not visible in frame",
            "offset": {"dx": offset[0], "dy": offset[1]} if offset else None,
        }
    c = station_item["corners"]
    return {
        "found": True,
        "cx": station_item["cx"],
        "cy": station_item["cy"],
        "x": int(c[:, 0].min()),
        "y": int(c[:, 1].min()),
        "w": int(c[:, 0].max() - c[:, 0].min()),
        "h": int(c[:, 1].max() - c[:, 1].min()),
        "offset": {"dx": offset[0], "dy": offset[1]} if offset else None,
    }


@app.post("/api/setup/station-drop")
async def api_station_drop_set(body: dict):
    station = body.get("station", "")
    dx = body.get("dx")
    dy = body.get("dy")
    if station not in ("station_1", "station_2", "station_3"):
        return {"ok": False, "reason": "unknown station"}
    if dx is None or dy is None:
        return {"ok": False, "reason": "dx and dy required"}
    _registry.set_station_drop_offset(station, float(dx), float(dy))
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


@app.post("/api/setup/station-drop/clear")
async def api_station_drop_clear(body: dict):
    station = body.get("station", "")
    if station not in ("station_1", "station_2", "station_3"):
        return {"ok": False, "reason": "unknown station"}
    _registry.clear_station_drop_offset(station)
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


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
    _navigator.set_motion_policy(allow_reverse=(station == "base"), force_reverse=False)
    _navigator.set_navigating(True)
    log.info("Test: navigating to %s (QR=%s, allow_reverse=%s)", station, qr, station == "base")
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
    _navigator.set_motion_policy(allow_reverse=False, force_reverse=False)
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
        _navigator.set_motion_policy(allow_reverse=True, force_reverse=False)
    asyncio.create_task(_broadcast_state())
    return {"ok": True}


# ── manual drive routes ───────────────────────────────────────────────────────

@app.post("/api/manual/drive")
async def api_manual_drive(body: dict):
    if _navigator is None:
        return {"ok": False, "reason": "navigator not running"}
    left  = max(-100, min(100, int(body.get("left",  0))))
    right = max(-100, min(100, int(body.get("right", 0))))
    _navigator.set_manual_drive(left, right)
    return {"ok": True, "left": left, "right": right}


@app.post("/api/manual/stop")
async def api_manual_stop():
    if _navigator is None:
        return {"ok": False, "reason": "navigator not running"}
    _navigator.clear_manual_drive()
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
        is_capture = False
        try:
            info_result = subprocess.run(
                ["v4l2-ctl", f"--device={path}", "--info"],
                capture_output=True, text=True, timeout=2,
            )
            for line in info_result.stdout.splitlines():
                if "Card type" in line:
                    label = line.split(":", 1)[1].strip()
                    break
            # Only include nodes that actually have capture formats (not metadata nodes)
            fmt_result = subprocess.run(
                ["v4l2-ctl", f"--device={path}", "--list-formats"],
                capture_output=True, text=True, timeout=2,
            )
            is_capture = any(line.strip().startswith("[") for line in fmt_result.stdout.splitlines())
        except Exception:
            pass
        if not is_capture:
            log.debug("camera/list: skipping %s (no capture formats)", path)
            continue
        devices.append({"index": idx, "path": path, "label": label})
    current = getattr(_app_config, "camera_index", 0) if _app_config else 0
    log.info("camera/list: found capture devices %s (current=%d)",
             [d["index"] for d in devices], current)
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
    log.info("Camera switch requested: → index %d", idx)
    if _navigator:
        old_nav = _navigator
        _navigator = None   # stream serves placeholder immediately while we wait
        old_nav.stop()
        stopped_ok = old_nav.wait_stopped(timeout=6.0)
        log.info("Camera switch: old navigator stopped cleanly=%s", stopped_ok)
        if not stopped_ok:
            log.warning("Camera switch: old navigator did not stop within 6s — proceeding anyway")
        time.sleep(0.3)  # give V4L2 driver time to release the device
    log.info("Camera switch: opening new navigator on camera %d", idx)
    _app_config.camera_index = idx
    _navigator = build_navigator(_app_config, on_state_change=_on_state_change)
    _apply_bot_qr()
    _apply_base_parking()
    _apply_station_qrs()

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
    """Push current bot QR, ArUco heading marker, offsets, and claw geometry into the navigator."""
    if _navigator and _registry:
        bot_qr            = _registry.get("bot")
        bot_aruco_id      = _registry.get_bot_aruco_id()
        offset            = _registry.get_heading_offset()
        claw_offset       = _registry.get_claw_offset_px()
        claw_center_offset = _registry.get_claw_center_offset_px()
        _navigator.set_bot_qr(bot_qr)
        _navigator.set_bot_aruco_id(bot_aruco_id)
        _navigator.set_claw_center_offset_px(claw_center_offset)
        _navigator._heading_offset  = offset
        _navigator._smoothed_heading = None  # force re-init with new offset
        if claw_offset > 0:
            _navigator._claw_offset_px = claw_offset
        log.info("Navigator bot_qr=%s bot_aruco_id=%s heading_offset=%.1f° claw_offset=%dpx claw_center=%s",
                 bot_qr or "None", str(bot_aruco_id) if bot_aruco_id is not None else "None",
                 math.degrees(offset), _navigator._claw_offset_px,
                 f"{claw_center_offset:.1f}px" if claw_center_offset is not None else "None")


def _apply_base_parking() -> None:
    """Push base QR and parking offset from registry into the navigator."""
    if _navigator and _registry:
        base_qr = _registry.get("base")
        offset  = _registry.get_base_parking_offset()
        _navigator.set_base_qr(base_qr)
        _navigator.set_parking_offset(offset)
        log.info("Navigator base_qr=%s parking_offset=%s", base_qr or "None", offset)


def _apply_station_qrs() -> None:
    """Push registered delivery station QRs into the navigator for occlusion tracking."""
    if _navigator and _registry:
        qrs = {_registry.get(k) for k in ("station_1", "station_2", "station_3")
               if _registry.get(k)}
        _navigator.set_station_qrs(qrs)
        log.info("Navigator station_qrs=%s", qrs)



@app.on_event("startup")
async def _startup():
    global _loop, _navigator, _nav_thread, _mission, _auto_mission, _registry, _app_config
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if _app_config is None:
        _app_config = load_config()
    _loop     = asyncio.get_event_loop()
    _registry = StationsRegistry()

    _navigator = build_navigator(_app_config, on_state_change=_on_state_change)
    _apply_bot_qr()
    _apply_base_parking()
    _apply_station_qrs()


    _mission   = MissionController(_navigator, _registry)
    _mission.set_on_change(
        lambda: asyncio.run_coroutine_threadsafe(_broadcast_state(), _loop)
    )

    _auto_mission = AutoMissionController(_mission, _navigator, _registry)
    _auto_mission.set_on_change(
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
