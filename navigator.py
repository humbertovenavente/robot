"""QR-guided visual navigation.

The camera is mounted on the robot/bug. When a QR code appears in the frame
the navigator steers the robot toward it:

  - No QR in frame            → status "searching"  (motors stopped)
  - QR visible, off-center    → status "centering"   (steer left/right)
  - QR visible, centered      → status "approaching" (drive forward)
  - QR area >= arrived_area_pct → status "arrived"   (motors stopped)

Drive commands are sent through a DriveInterface so the same logic works
with StubDrive (dev/CI) or a real EV3/SPIKE motor controller.

Web layer imports QRNavigator and calls get_latest_frame() to serve MJPEG.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

import cv2
import numpy as np

log = logging.getLogger(__name__)

# ── tuneable defaults ────────────────────────────────────────────────────────
DEFAULT_CENTER_TOL_PX   = 40    # horizontal dead-band (pixels) around frame cx
DEFAULT_ARRIVED_AREA_PCT = 0.15  # QR box area / frame area → considered arrived
DEFAULT_DRIVE_SPEED     = 50    # 0-100 arbitrary units forwarded to DriveInterface
DEFAULT_TURN_SPEED      = 35    # same units, for steering corrections


# ── state ────────────────────────────────────────────────────────────────────
@dataclass
class NavigatorState:
    status: str = "idle"                 # idle | searching | centering | approaching | arrived
    qr_payload: Optional[str] = None
    qr_cx: Optional[int] = None          # QR center-X in frame (pixels)
    qr_area_pct: Optional[float] = None  # fraction of frame area occupied by QR
    frame_w: Optional[int] = None
    frame_h: Optional[int] = None


StateCallback = Callable[[NavigatorState], None]


# ── QR detection helper ──────────────────────────────────────────────────────
def _detect_qr(frame: np.ndarray) -> Optional[Tuple[str, int, int, float]]:
    """Decode the first QR in *frame*.

    Returns (payload, center_x, center_y, area_fraction) or None.
    Uses pyzbar so no model weights needed.
    """
    try:
        from pyzbar import pyzbar  # lazy — not everyone has it installed
    except ImportError:
        log.warning("pyzbar not installed — QR detection unavailable")
        return None

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    codes = pyzbar.decode(gray)
    if not codes:
        return None

    qr = codes[0]
    x, y, w, h = qr.rect
    cx = x + w // 2
    cy = y + h // 2
    fh, fw = frame.shape[:2]
    area_pct = (w * h) / max(fw * fh, 1)
    payload = qr.data.decode("utf-8", errors="replace")
    return payload, cx, cy, area_pct


# ── drive interface ──────────────────────────────────────────────────────────
class DriveInterface:
    """Protocol-style base; real implementations override these methods."""

    def move_forward(self, speed: int = DEFAULT_DRIVE_SPEED) -> None:
        raise NotImplementedError

    def steer(self, turn_rate: int) -> None:
        """Negative = left, positive = right. |turn_rate| 0-100."""
        raise NotImplementedError

    def stop_motors(self) -> None:
        raise NotImplementedError


class StubDrive(DriveInterface):
    """Simulated drive — logs commands. Used for dev/CI."""

    def move_forward(self, speed: int = DEFAULT_DRIVE_SPEED) -> None:
        log.info("StubDrive: move_forward speed=%d", speed)

    def steer(self, turn_rate: int) -> None:
        direction = "right" if turn_rate > 0 else "left"
        log.info("StubDrive: steer %s  rate=%d", direction, abs(turn_rate))

    def stop_motors(self) -> None:
        log.info("StubDrive: stop_motors")


# ── navigator ────────────────────────────────────────────────────────────────
class QRNavigator:
    """Reads camera frames, detects a QR code, steers the robot toward it.

    Usage::

        drive  = StubDrive()
        nav    = QRNavigator(camera_index=0, drive=drive)
        thread = threading.Thread(target=nav.run, daemon=True)
        thread.start()
        # …later…
        nav.stop()

    The web layer calls ``get_annotated_frame()`` to get the latest frame with
    the QR bounding box and status overlay drawn on it.
    """

    def __init__(
        self,
        camera_index: int,
        drive: DriveInterface,
        arrived_area_pct: float = DEFAULT_ARRIVED_AREA_PCT,
        center_tol_px: int = DEFAULT_CENTER_TOL_PX,
        drive_speed: int = DEFAULT_DRIVE_SPEED,
        turn_speed: int = DEFAULT_TURN_SPEED,
        on_state_change: Optional[StateCallback] = None,
    ):
        self._camera_index = camera_index
        self._drive = drive
        self._arrived_area_pct = arrived_area_pct
        self._center_tol_px = center_tol_px
        self._drive_speed = drive_speed
        self._turn_speed = turn_speed
        self._on_state = on_state_change

        self.state = NavigatorState()
        self._running = False
        self._raw_frame: Optional[np.ndarray] = None
        self._frame_lock = threading.Lock()

    # ── internal helpers ─────────────────────────────────────────────────────
    def _set_state(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self.state, k, v)
        if self._on_state:
            try:
                self._on_state(self.state)
            except Exception as exc:
                log.warning("on_state_change raised: %s", exc)

    # ── public: frame access for web layer ───────────────────────────────────
    def get_annotated_frame(self) -> Optional[np.ndarray]:
        """Return latest frame with QR box + status text drawn on it."""
        with self._frame_lock:
            if self._raw_frame is None:
                return None
            frame = self._raw_frame.copy()

        st = self.state
        h, w = frame.shape[:2]

        # Draw QR center marker when visible
        if st.qr_cx is not None and st.frame_w:
            scale = w / st.frame_w
            cx_draw = int(st.qr_cx * scale)
            cy_draw = h // 2  # approximate
            cv2.circle(frame, (cx_draw, cy_draw), 10, (0, 255, 0), 2)
            cv2.line(frame, (w // 2, 0), (w // 2, h), (0, 200, 0), 1)  # center guide

        # Status banner
        color_map = {
            "idle":        (150, 150, 150),
            "searching":   (0, 200, 255),
            "centering":   (0, 165, 255),
            "approaching": (0, 255, 0),
            "arrived":     (255, 0, 200),
        }
        color = color_map.get(st.status, (255, 255, 255))
        label = st.status.upper()
        if st.qr_payload:
            label += f"  QR={st.qr_payload}"
        if st.qr_area_pct is not None:
            label += f"  {st.qr_area_pct*100:.1f}%"
        cv2.rectangle(frame, (0, 0), (w, 28), (0, 0, 0), -1)
        cv2.putText(frame, label, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        return frame

    # ── main processing ──────────────────────────────────────────────────────
    def process_frame(self, frame: np.ndarray) -> None:
        """Detect QR, compute steering command, update state. Called per frame."""
        fh, fw = frame.shape[:2]
        frame_cx = fw // 2

        with self._frame_lock:
            self._raw_frame = frame

        result = _detect_qr(frame)

        if result is None:
            if self.state.status not in ("idle", "searching"):
                self._drive.stop_motors()
            self._set_state(
                status="searching",
                qr_payload=None,
                qr_cx=None,
                qr_area_pct=None,
                frame_w=fw,
                frame_h=fh,
            )
            return

        payload, cx, cy, area_pct = result

        # ── arrived? ─────────────────────────────────────────────────────────
        if area_pct >= self._arrived_area_pct:
            self._drive.stop_motors()
            self._set_state(
                status="arrived",
                qr_payload=payload,
                qr_cx=cx,
                qr_area_pct=area_pct,
                frame_w=fw,
                frame_h=fh,
            )
            log.info("QRNavigator: arrived at QR '%s'", payload)
            return

        offset = cx - frame_cx  # negative = QR is left, positive = right

        # ── steer or go straight ──────────────────────────────────────────────
        if abs(offset) <= self._center_tol_px:
            self._drive.move_forward(self._drive_speed)
            self._set_state(
                status="approaching",
                qr_payload=payload,
                qr_cx=cx,
                qr_area_pct=area_pct,
                frame_w=fw,
                frame_h=fh,
            )
        else:
            turn = self._turn_speed if offset > 0 else -self._turn_speed
            self._drive.steer(turn)
            self._set_state(
                status="centering",
                qr_payload=payload,
                qr_cx=cx,
                qr_area_pct=area_pct,
                frame_w=fw,
                frame_h=fh,
            )

    # ── blocking run loop ─────────────────────────────────────────────────────
    def run(self) -> None:
        """Open camera and loop until stop() is called."""
        self._running = True
        cap = cv2.VideoCapture(self._camera_index)
        if not cap.isOpened():
            log.error("QRNavigator: cannot open camera %d", self._camera_index)
            self._set_state(status="idle")
            return

        log.info("QRNavigator: camera %d open — scanning for QR code", self._camera_index)
        self._set_state(status="searching")

        try:
            while self._running:
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                self.process_frame(frame)
                time.sleep(0.04)   # ~25 fps cap
        finally:
            self._drive.stop_motors()
            cap.release()
            self._set_state(status="idle")
            log.info("QRNavigator: stopped")

    def stop(self) -> None:
        self._running = False


# ── factory ───────────────────────────────────────────────────────────────────
def build_navigator(config, on_state_change: Optional[StateCallback] = None) -> QRNavigator:
    """Build a QRNavigator from station config.

    nav_robot: "stub" (default) | "nxt"
    NXT requires nxt-python and a connected brick (USB or Bluetooth).
    """
    robot_impl = getattr(config, "nav_robot", "stub")

    if robot_impl == "nxt":
        from nxt_drive import build_nxt_drive
        drive = build_nxt_drive(config)
        log.info("QRNavigator usando NXT drive")
    else:
        drive = StubDrive()
        log.info("QRNavigator usando StubDrive (simulado)")

    return QRNavigator(
        camera_index=getattr(config, "camera_index", 0),
        drive=drive,
        arrived_area_pct=getattr(config, "nav_arrived_area_pct", DEFAULT_ARRIVED_AREA_PCT),
        center_tol_px=getattr(config, "nav_center_tol_px", DEFAULT_CENTER_TOL_PX),
        drive_speed=getattr(config, "nav_drive_speed", DEFAULT_DRIVE_SPEED),
        turn_speed=getattr(config, "nav_turn_speed", DEFAULT_TURN_SPEED),
        on_state_change=on_state_change,
    )
