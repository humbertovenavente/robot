"""QR-guided visual navigation — supports two camera modes.

ONBOARD mode  (nav_mode: onboard)
  Camera is on the bot, steers toward whichever QR matches target_qr.
  Proportional differential drive; slow rotation when target not in frame.

OVERHEAD mode (nav_mode: overhead)
  Stationary camera above the arena sees ALL QR codes simultaneously.
  Bot has a QR code on top (bot_qr). Navigator computes heading error
  between bot's current orientation and the direction to target_qr,
  then drives with proportional differential control.
  Requires: pip install opencv-python  (cv2.QRCodeDetector for corner ordering)
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

log = logging.getLogger(__name__)

DEFAULT_CENTER_TOL_PX    = 40
DEFAULT_ARRIVED_AREA_PCT = 0.15   # onboard mode
DEFAULT_ARRIVED_DIST_PX  = 80     # overhead mode: pixels bot→target = arrived
DEFAULT_CLAW_OFFSET_PX   = 0      # 0 = disabled; set to px≡11cm for claw delivery
DEFAULT_DRIVE_SPEED      = 50
DEFAULT_TURN_SPEED       = 35
DEFAULT_ALIGN_THRESHOLD  = math.pi / 3   # 60°: rotate-in-place above this error


# ── state ─────────────────────────────────────────────────────────────────────

@dataclass
class NavigatorState:
    status: str = "idle"
    qr_payload: Optional[str] = None
    qr_cx: Optional[int] = None
    qr_area_pct: Optional[float] = None
    frame_w: Optional[int] = None
    frame_h: Optional[int] = None
    all_qr_payloads: List[str] = field(default_factory=list)
    all_qr_rects: List[Tuple] = field(default_factory=list)  # (payload, x, y, w, h)
    # overhead extras
    bot_pos: Optional[Tuple[float, float]] = None   # (cx, cy) of bot QR in frame
    bot_heading: Optional[float] = None             # radians
    target_pos: Optional[Tuple[float, float]] = None
    dist_to_target: Optional[float] = None
    heading_error: Optional[float] = None


StateCallback = Callable[[NavigatorState], None]


# ── QR detection — onboard (pyzbar, fast) ────────────────────────────────────

def _detect_all_qr(frame: np.ndarray) -> list:
    """Detect all QR codes. Returns [(payload, cx, cy, area_pct, (x,y,w,h))]."""
    try:
        from pyzbar import pyzbar
    except ImportError:
        log.warning("pyzbar not installed")
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    codes = pyzbar.decode(gray)
    if not codes:
        return []
    fh, fw = frame.shape[:2]
    out = []
    for qr in codes:
        x, y, w, h = qr.rect
        cx, cy = x + w // 2, y + h // 2
        area_pct = (w * h) / max(fw * fh, 1)
        payload = qr.data.decode("utf-8", errors="replace")
        out.append((payload, cx, cy, area_pct, (x, y, w, h)))
    return out


# ── QR detection — overhead (OpenCV, gives ordered corners) ──────────────────

def _detect_overhead(frame: np.ndarray) -> List[Dict]:
    """Detect all QR codes with reliable corner ordering (needed for heading).

    Uses cv2.QRCodeDetector which returns corners in consistent order:
    [top-left, top-right, bottom-right, bottom-left].

    Returns list of {payload, cx, cy, corners: ndarray(4,2)}.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    detector = cv2.QRCodeDetector()
    retval, decoded_info, points, _ = detector.detectAndDecodeMulti(gray)
    out = []
    if not retval or points is None:
        return out
    for payload, corners in zip(decoded_info, points):
        if not payload:
            continue
        corners = corners.reshape(4, 2).astype(float)
        cx = float(corners[:, 0].mean())
        cy = float(corners[:, 1].mean())
        out.append({"payload": payload, "cx": cx, "cy": cy, "corners": corners})
    return out


def _qr_heading(corners: np.ndarray, offset: float = 0.0) -> float:
    """Bot heading from QR corners.

    Assumes the QR code is mounted so that the top-left → top-right edge of
    the QR points in the same direction as the bot's forward axis.
    Tune nav_heading_offset (radians) in config if the QR is mounted at an angle.

    Image coordinate system: x right, y down.
    Returns angle in that system (0 = pointing right, +π/2 = pointing down).
    """
    tl, tr = corners[0], corners[1]
    return math.atan2(float(tr[1] - tl[1]), float(tr[0] - tl[0])) + offset


def _angle_diff(a: float, b: float) -> float:
    """Signed difference a − b normalised to (−π, π]."""
    return (a - b + math.pi) % (2 * math.pi) - math.pi


# ── drive interface ───────────────────────────────────────────────────────────

class DriveInterface:
    def drive(self, left: int, right: int) -> None:
        """Set left and right wheel speeds independently (−100..100)."""
        raise NotImplementedError

    def stop_motors(self) -> None:
        raise NotImplementedError

    def open_claw(self) -> None:
        raise NotImplementedError

    def close_claw(self) -> None:
        raise NotImplementedError

    def move_forward(self, speed: int = DEFAULT_DRIVE_SPEED) -> None:
        self.drive(speed, speed)

    def steer(self, turn_rate: int) -> None:
        """Pure tank rotation: negative = left, positive = right."""
        p = abs(turn_rate)
        self.drive(p, -p) if turn_rate > 0 else self.drive(-p, p)


class StubDrive(DriveInterface):
    def drive(self, left: int, right: int) -> None:
        log.info("StubDrive: drive  L=%d  R=%d", left, right)

    def stop_motors(self) -> None:
        log.info("StubDrive: stop_motors")

    def open_claw(self) -> None:
        log.info("StubDrive: open_claw")

    def close_claw(self) -> None:
        log.info("StubDrive: close_claw")


# ── navigator ─────────────────────────────────────────────────────────────────

class QRNavigator:
    """Camera loop + QR-based steering.

    Set _bot_qr to enable overhead mode. Leave None for onboard mode.
    """

    def __init__(
        self,
        camera_index: int,
        drive: DriveInterface,
        arrived_area_pct: float = DEFAULT_ARRIVED_AREA_PCT,
        arrived_dist_px: int   = DEFAULT_ARRIVED_DIST_PX,
        claw_offset_px: int    = DEFAULT_CLAW_OFFSET_PX,
        center_tol_px: int     = DEFAULT_CENTER_TOL_PX,
        drive_speed: int       = DEFAULT_DRIVE_SPEED,
        turn_speed: int        = DEFAULT_TURN_SPEED,
        align_threshold: float = DEFAULT_ALIGN_THRESHOLD,
        heading_offset: float  = 0.0,
        target_qr: Optional[str]  = None,
        bot_qr: Optional[str]     = None,
        on_state_change: Optional[StateCallback] = None,
    ):
        self._camera_index     = camera_index
        self._drive            = drive
        self._arrived_area_pct = arrived_area_pct
        self._arrived_dist_px  = arrived_dist_px
        self._claw_offset_px   = claw_offset_px   # pixels ≡ 11cm claw reach
        self._use_claw_arrived = False             # toggled by mission controller
        self._center_tol_px    = center_tol_px
        self._drive_speed      = drive_speed
        self._turn_speed       = turn_speed
        self._align_threshold  = align_threshold
        self._heading_offset   = heading_offset
        self._target_qr        = target_qr
        self._bot_qr           = bot_qr
        self._navigating       = False
        self._on_state         = on_state_change

        self.state             = NavigatorState()
        self._running          = False
        self._raw_frame: Optional[np.ndarray] = None
        self._frame_lock       = threading.Lock()

    # ── public control ────────────────────────────────────────────────────────

    def set_target(self, qr: Optional[str]) -> None:
        self._target_qr = qr

    def set_bot_qr(self, qr: Optional[str]) -> None:
        self._bot_qr = qr

    def set_navigating(self, active: bool) -> None:
        self._navigating = active
        if not active:
            try:
                self._drive.stop_motors()
            except Exception:
                pass

    def set_claw_arrived(self, active: bool) -> None:
        """When True, arrived is triggered at claw_offset_px distance instead of arrived_dist_px."""
        self._use_claw_arrived = active

    # ── frame annotation ──────────────────────────────────────────────────────

    def get_annotated_frame(self, station_qrs: set = None) -> Optional[np.ndarray]:
        with self._frame_lock:
            if self._raw_frame is None:
                return None
            frame = self._raw_frame.copy()

        st = self.state
        h, w = frame.shape[:2]
        station_qrs = station_qrs or set()

        # QR bounding boxes
        for (payload, rx, ry, rw, rh) in st.all_qr_rects:
            is_bot    = (payload == self._bot_qr)
            is_target = (payload == self._target_qr)
            is_station = (payload in station_qrs)
            if is_bot:
                color, thick = (255, 100, 0), 3      # blue = bot
            elif is_target:
                color, thick = (0, 255, 0), 3         # bright green = target
            elif is_station:
                color, thick = (0, 180, 0), 1         # dim green = station
            else:
                color, thick = (0, 165, 255), 2       # orange = package
            cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), color, thick)
            cv2.putText(frame, payload[:14], (rx, max(ry - 4, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)

        # Overhead extras: bot heading arrow + line to target
        if st.bot_pos and st.bot_heading is not None:
            bx, by = int(st.bot_pos[0]), int(st.bot_pos[1])
            arrow_len = 40
            ax = int(bx + arrow_len * math.cos(st.bot_heading))
            ay = int(by + arrow_len * math.sin(st.bot_heading))
            cv2.arrowedLine(frame, (bx, by), (ax, ay), (255, 100, 0), 2, tipLength=0.3)
            if st.target_pos:
                tx, ty = int(st.target_pos[0]), int(st.target_pos[1])
                cv2.line(frame, (bx, by), (tx, ty), (0, 220, 220), 1)
                mid = ((bx + tx) // 2, (by + ty) // 2)
                if st.dist_to_target is not None:
                    cv2.putText(frame, f"{st.dist_to_target:.0f}px",
                                mid, cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 220, 220), 1)

        # Onboard: center guide + QR marker
        if not self._bot_qr and st.qr_cx is not None:
            cv2.line(frame, (w // 2, 0), (w // 2, h), (0, 200, 0), 1)
            cv2.circle(frame, (int(st.qr_cx), h // 2), 10, (0, 255, 0), 2)

        # Status banner
        color_map = {
            "idle":        (150, 150, 150),
            "searching":   (0, 200, 255),
            "centering":   (0, 165, 255),
            "approaching": (0, 255, 0),
            "arrived":     (255, 0, 200),
        }
        color = color_map.get(st.status, (255, 255, 255))
        mode  = "OVH" if self._bot_qr else "OBD"
        tgt   = f" → {self._target_qr}" if self._target_qr else ""
        label = f"[{mode}] {st.status.upper()}{tgt}"
        if st.heading_error is not None:
            label += f"  Δθ={math.degrees(st.heading_error):.0f}°"
        elif st.qr_area_pct is not None:
            label += f"  {st.qr_area_pct * 100:.1f}%"
        cv2.rectangle(frame, (0, 0), (w, 28), (0, 0, 0), -1)
        cv2.putText(frame, label, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        return frame

    # ── frame processing ──────────────────────────────────────────────────────

    def process_frame(self, frame: np.ndarray) -> None:
        with self._frame_lock:
            self._raw_frame = frame
        if self._bot_qr is not None:
            self._process_overhead(frame)
        else:
            self._process_onboard(frame)

    # ── onboard mode ──────────────────────────────────────────────────────────

    def _process_onboard(self, frame: np.ndarray) -> None:
        fh, fw = frame.shape[:2]
        all_results = _detect_all_qr(frame)
        all_payloads = [r[0] for r in all_results]
        all_rects    = [(r[0], r[4][0], r[4][1], r[4][2], r[4][3]) for r in all_results]

        result = None
        if self._navigating:
            if self._target_qr is not None:
                result = next((r for r in all_results if r[0] == self._target_qr), None)
            elif all_results:
                result = all_results[0]

        if not self._navigating or result is None:
            if self._navigating:
                self._drive.drive(-self._turn_speed // 2, self._turn_speed // 2)
            else:
                self._drive.stop_motors()
            self._set_state(
                status="searching" if self._navigating else "idle",
                qr_payload=None, qr_cx=None, qr_area_pct=None,
                frame_w=fw, frame_h=fh,
                all_qr_payloads=all_payloads, all_qr_rects=all_rects,
            )
            return

        payload, cx, cy, area_pct, rect = result

        if area_pct >= self._arrived_area_pct:
            self._drive.stop_motors()
            self._set_state(
                status="arrived",
                qr_payload=payload, qr_cx=cx, qr_area_pct=area_pct,
                frame_w=fw, frame_h=fh,
                all_qr_payloads=all_payloads, all_qr_rects=all_rects,
            )
            log.info("Navigator (onboard): arrived at '%s'", payload)
            return

        # Proportional differential drive
        error_norm  = max(-1.0, min(1.0, (cx - fw // 2) / max(fw // 2, 1)))
        turn        = int(error_norm * self._turn_speed)
        left_speed  = max(-100, min(100, self._drive_speed + turn))
        right_speed = max(-100, min(100, self._drive_speed - turn))
        self._drive.drive(left_speed, right_speed)

        new_status = "approaching" if abs(error_norm) <= 0.15 else "centering"
        self._set_state(
            status=new_status,
            qr_payload=payload, qr_cx=cx, qr_area_pct=area_pct,
            frame_w=fw, frame_h=fh,
            all_qr_payloads=all_payloads, all_qr_rects=all_rects,
        )

    # ── overhead mode ─────────────────────────────────────────────────────────

    def _process_overhead(self, frame: np.ndarray) -> None:
        fh, fw = frame.shape[:2]
        items = _detect_overhead(frame)

        # Build flat lists for state / annotation from OpenCV detections
        all_payloads = [i["payload"] for i in items]
        all_rects    = []
        for i in items:
            c = i["corners"]
            x1, y1 = int(c[:, 0].min()), int(c[:, 1].min())
            x2, y2 = int(c[:, 0].max()), int(c[:, 1].max())
            all_rects.append((i["payload"], x1, y1, x2 - x1, y2 - y1))

        bot    = next((i for i in items if i["payload"] == self._bot_qr),    None)
        target = next((i for i in items if i["payload"] == self._target_qr), None) \
                 if self._target_qr else None

        if not self._navigating or bot is None:
            self._drive.stop_motors()
            self._set_state(
                status="idle" if not self._navigating else "searching",
                qr_payload=None, qr_cx=None, qr_area_pct=None,
                frame_w=fw, frame_h=fh,
                all_qr_payloads=all_payloads, all_qr_rects=all_rects,
                bot_pos=None, bot_heading=None,
                target_pos=None, dist_to_target=None, heading_error=None,
            )
            return

        bx, by   = bot["cx"], bot["cy"]
        bot_hdg  = _qr_heading(bot["corners"], self._heading_offset)

        if target is None:
            # Can see bot but not target — stop and wait
            self._drive.stop_motors()
            self._set_state(
                status="searching",
                qr_payload=None, qr_cx=int(bx), qr_area_pct=None,
                frame_w=fw, frame_h=fh,
                all_qr_payloads=all_payloads, all_qr_rects=all_rects,
                bot_pos=(bx, by), bot_heading=bot_hdg,
                target_pos=None, dist_to_target=None, heading_error=None,
            )
            return

        tx, ty   = target["cx"], target["cy"]
        dx, dy   = tx - bx, ty - by
        distance = math.hypot(dx, dy)

        # Claw mode: stop when bot QR is claw_offset_px from target (claw tip on target)
        arrived_threshold = (
            self._claw_offset_px
            if (self._use_claw_arrived and self._claw_offset_px > 0)
            else self._arrived_dist_px
        )
        if distance < arrived_threshold:
            self._drive.stop_motors()
            self._set_state(
                status="arrived",
                qr_payload=self._target_qr, qr_cx=int(bx), qr_area_pct=1.0,
                frame_w=fw, frame_h=fh,
                all_qr_payloads=all_payloads, all_qr_rects=all_rects,
                bot_pos=(bx, by), bot_heading=bot_hdg,
                target_pos=(tx, ty), dist_to_target=distance, heading_error=0.0,
            )
            log.info("Navigator (overhead): arrived at '%s' dist=%.1fpx",
                     self._target_qr, distance)
            return

        # Desired heading: vector from bot to target (image y-down coords)
        desired = math.atan2(dy, dx)
        err     = _angle_diff(desired, bot_hdg)

        if abs(err) > self._align_threshold:
            # Large error → rotate in place
            spd = self._turn_speed if err > 0 else -self._turn_speed
            self._drive.drive(spd, -spd)
            new_status = "centering"
        else:
            # Small error → proportional differential drive (always moving forward)
            turn       = int((err / self._align_threshold) * self._turn_speed)
            left_speed  = max(-100, min(100, self._drive_speed + turn))
            right_speed = max(-100, min(100, self._drive_speed - turn))
            self._drive.drive(left_speed, right_speed)
            new_status  = "approaching" if abs(err) < 0.15 else "centering"

        self._set_state(
            status=new_status,
            qr_payload=self._target_qr, qr_cx=int(bx),
            qr_area_pct=max(0.0, 1.0 - distance / max(fw, fh)),
            frame_w=fw, frame_h=fh,
            all_qr_payloads=all_payloads, all_qr_rects=all_rects,
            bot_pos=(bx, by), bot_heading=bot_hdg,
            target_pos=(tx, ty), dist_to_target=distance, heading_error=err,
        )

    # ── internal ──────────────────────────────────────────────────────────────

    def _set_state(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self.state, k, v)
        if self._on_state:
            try:
                self._on_state(self.state)
            except Exception as exc:
                log.warning("on_state_change raised: %s", exc)

    # ── blocking run loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        cap = cv2.VideoCapture(self._camera_index)
        if not cap.isOpened():
            log.error("QRNavigator: cannot open camera %d", self._camera_index)
            self._set_state(status="idle")
            return

        mode = "overhead" if self._bot_qr else "onboard"
        log.info("QRNavigator: camera %d open [%s mode]", self._camera_index, mode)
        self._set_state(status="idle")

        try:
            while self._running:
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                self.process_frame(frame)
                time.sleep(0.04)
        finally:
            self._drive.stop_motors()
            cap.release()
            self._set_state(status="idle")
            log.info("QRNavigator: stopped")

    def stop(self) -> None:
        self._running = False


# ── factory ───────────────────────────────────────────────────────────────────

def build_navigator(config, on_state_change: Optional[StateCallback] = None) -> QRNavigator:
    robot_impl = getattr(config, "nav_robot", "stub")

    if robot_impl == "nxt":
        from nxt_drive import build_nxt_drive
        drive = build_nxt_drive(config)
        log.info("QRNavigator usando NXT drive")
    else:
        drive = StubDrive()
        log.info("QRNavigator usando StubDrive (simulado)")

    nav_mode = getattr(config, "nav_mode", "onboard")
    bot_qr   = getattr(config, "nav_bot_qr", None)

    return QRNavigator(
        camera_index     = getattr(config, "camera_index",          0),
        drive            = drive,
        arrived_area_pct = getattr(config, "nav_arrived_area_pct",  DEFAULT_ARRIVED_AREA_PCT),
        arrived_dist_px  = getattr(config, "nav_arrived_dist_px",   DEFAULT_ARRIVED_DIST_PX),
        claw_offset_px   = getattr(config, "nav_claw_offset_px",    DEFAULT_CLAW_OFFSET_PX),
        center_tol_px    = getattr(config, "nav_center_tol_px",     DEFAULT_CENTER_TOL_PX),
        drive_speed      = getattr(config, "nav_drive_speed",        DEFAULT_DRIVE_SPEED),
        turn_speed       = getattr(config, "nav_turn_speed",         DEFAULT_TURN_SPEED),
        align_threshold  = getattr(config, "nav_align_threshold",    DEFAULT_ALIGN_THRESHOLD),
        heading_offset   = getattr(config, "nav_heading_offset",     0.0),
        bot_qr           = bot_qr if nav_mode == "overhead" else None,
        on_state_change  = on_state_change,
    )
