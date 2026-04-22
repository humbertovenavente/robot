"""QR-guided visual navigation — supports two camera modes.

ONBOARD mode  (nav_mode: onboard)
  Camera is on the bot, steers toward whichever QR matches target_qr.
  Proportional differential drive; slow rotation when target not in frame.

OVERHEAD mode (nav_mode: overhead)
  Stationary camera above the arena sees ALL QR codes simultaneously.
  Bot identity is tracked with a QR code on top (bot_qr). Bot forward
  direction can be tracked either from the QR orientation itself or, more
  robustly, from a configured ArUco marker mounted at the front of the bot.
  Navigator computes heading error between the bot heading and the direction
  to target_qr, then drives with proportional differential control.
  Requires: pip install opencv-python  (cv2.QRCodeDetector + cv2.aruco)
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
DEFAULT_BASE_PARK_ARRIVED_DIST_PX = 28  # tighter stop when parking bot QR onto saved base slot
DEFAULT_CLAW_CENTER_CM = 10.0     # fallback guess when claw centre has not been image-calibrated
DEFAULT_CLAW_CENTER_ARRIVED_DIST_PX = 18  # calibrated claw centre may come this close to target before stopping
DEFAULT_PICKUP_CONTACT_MIN_PX = 24
DEFAULT_PICKUP_CONTACT_RATIO = 0.35

_REPULSE_RADIUS_PX   = 150   # px from obstacle centre where repulsion starts
_OCCLUDE_FULL_FRAMES  = 45   # consecutive missing frames → station "full"  (~1.8 s at 25 fps)
_OCCLUDE_CLEAR_FRAMES = 8    # consecutive visible frames → station cleared
_HEADING_ALPHA        = 0.45  # EMA blend for heading smoother (higher = more responsive)
_HEADING_MAX_DELTA    = 0.45  # max radians of heading change accepted per frame
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
    parking_target_pos: Optional[Tuple[float, float]] = None  # base park spot
    full_station_qrs: List[str] = field(default_factory=list) # occluded stations


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

def _pyzbar_decode_region(frame: np.ndarray, corners: np.ndarray) -> Optional[str]:
    """Crop to QR bounding box and attempt pyzbar decode with mild upscale."""
    try:
        from pyzbar.pyzbar import decode as zbar_decode
    except ImportError:
        return None
    fh, fw = frame.shape[:2]
    pad = 12
    x1 = max(0, int(corners[:, 0].min()) - pad)
    y1 = max(0, int(corners[:, 1].min()) - pad)
    x2 = min(fw, int(corners[:, 0].max()) + pad)
    y2 = min(fh, int(corners[:, 1].max()) + pad)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    # upscale small crops so pyzbar has enough pixels to work with
    ch, cw = crop.shape[:2]
    if max(ch, cw) < 120:
        scale = 120 / max(ch, cw)
        crop = cv2.resize(crop, (int(cw * scale), int(ch * scale)), interpolation=cv2.INTER_CUBIC)
    try:
        results = zbar_decode(crop)
        if results:
            return results[0].data.decode("utf-8", errors="replace").strip() or None
    except Exception:
        pass
    return None


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as [top-left, top-right, bottom-right, bottom-left]."""
    s    = pts.sum(axis=1)
    diff = pts[:, 0] - pts[:, 1]
    return np.array([pts[np.argmin(s)], pts[np.argmax(diff)],
                     pts[np.argmax(s)], pts[np.argmin(diff)]], dtype=float)


def _quad_score(corners: np.ndarray, frame_shape: Tuple[int, int], source_bias: float = 0.0) -> Optional[float]:
    """Lower is better. Reject clearly non-QR quads by returning None."""
    if corners.shape != (4, 2) or not np.isfinite(corners).all():
        return None
    corners = _order_corners(corners.astype(float))
    fh, fw = frame_shape

    sides = np.array([
        np.linalg.norm(corners[1] - corners[0]),
        np.linalg.norm(corners[2] - corners[1]),
        np.linalg.norm(corners[3] - corners[2]),
        np.linalg.norm(corners[0] - corners[3]),
    ], dtype=float)
    min_side = float(sides.min())
    max_side = float(sides.max())
    if min_side < 18.0 or max_side / max(min_side, 1.0) > 2.4:
        return None

    def _corner_angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
        v1 = a - b
        v2 = c - b
        denom = np.linalg.norm(v1) * np.linalg.norm(v2)
        if denom <= 1e-6:
            return 180.0
        cosang = float(np.clip(np.dot(v1, v2) / denom, -1.0, 1.0))
        return math.degrees(math.acos(cosang))

    angles = np.array([
        _corner_angle(corners[3], corners[0], corners[1]),
        _corner_angle(corners[0], corners[1], corners[2]),
        _corner_angle(corners[1], corners[2], corners[3]),
        _corner_angle(corners[2], corners[3], corners[0]),
    ], dtype=float)
    if np.any(angles < 55.0) or np.any(angles > 125.0):
        return None

    x1, y1 = float(corners[:, 0].min()), float(corners[:, 1].min())
    x2, y2 = float(corners[:, 0].max()), float(corners[:, 1].max())
    bbox_w = x2 - x1
    bbox_h = y2 - y1
    bbox_area = bbox_w * bbox_h
    quad_area = abs(float(cv2.contourArea(corners.astype(np.float32))))
    if bbox_area <= 1.0 or quad_area <= 40.0:
        return None

    img_area = float(fw * fh)
    fill_ratio = quad_area / bbox_area
    if fill_ratio < 0.42 or bbox_area / img_area > 0.42:
        return None

    # Border-clipped false detections often latch onto the white backing cards.
    border_pad = 6.0
    touches = sum((
        x1 <= border_pad,
        y1 <= border_pad,
        x2 >= fw - border_pad,
        y2 >= fh - border_pad,
    ))
    if touches >= 2 and bbox_area / img_area > 0.08:
        return None

    side_penalty = (max_side / max(min_side, 1.0)) - 1.0
    angle_penalty = float(np.mean(np.abs(angles - 90.0)) / 90.0)
    area_penalty = bbox_area / img_area
    return side_penalty + angle_penalty + area_penalty + source_bias


def _detect_overhead(frame: np.ndarray) -> List[Dict]:
    """Detect all QR codes with reliable corner ordering (needed for heading).

    Primary: cv2.QRCodeDetector (consistent TL→TR→BR→BL corner order).
    Fallback: pyzbar on a CLAHE-enhanced grayscale image catches codes that
    OpenCV's detector misses (small size, low contrast, perspective distortion).

    Returns list of {payload: str|None, cx, cy, corners: ndarray(4,2)}.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    detector = cv2.QRCodeDetector()
    retval, decoded_info, points, _ = detector.detectAndDecodeMulti(gray)
    candidates: List[Dict] = []

    if retval and points is not None:
        for payload, corners in zip(decoded_info, points):
            corners = _order_corners(corners.reshape(4, 2).astype(float))
            score = _quad_score(corners, gray.shape, source_bias=0.0)
            if score is None:
                continue
            cx = float(corners[:, 0].mean())
            cy = float(corners[:, 1].mean())
            if not payload:
                payload = _pyzbar_decode_region(frame, corners)
            if not payload:
                continue
            candidates.append({
                "payload": payload,
                "cx": cx,
                "cy": cy,
                "corners": corners,
                "_score": score,
            })

    # pyzbar multi-pass fallback: decode only from 4-point polygons that also pass geometry checks.
    try:
        from pyzbar.pyzbar import decode as zbar_decode
        clahe    = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        kernel   = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
        sharp    = cv2.filter2D(gray, -1, kernel)
        for img in (gray, enhanced, sharp):
            for r in zbar_decode(img):
                payload = r.data.decode("utf-8", errors="replace").strip() or None
                pts = np.array([[p.x, p.y] for p in r.polygon], dtype=float)
                if len(pts) != 4 or not payload:
                    continue
                corners = _order_corners(pts)
                score = _quad_score(corners, gray.shape, source_bias=0.12)
                if score is None:
                    continue
                cx = float(corners[:, 0].mean())
                cy = float(corners[:, 1].mean())
                candidates.append({
                    "payload": payload,
                    "cx": cx,
                    "cy": cy,
                    "corners": corners,
                    "_score": score,
                })
    except Exception:
        pass

    # Deduplicate by payload first, then suppress near-duplicate centres.
    best_by_payload: Dict[str, Dict] = {}
    for item in candidates:
        payload = item["payload"]
        prev = best_by_payload.get(payload)
        if prev is None or item["_score"] < prev["_score"]:
            best_by_payload[payload] = item

    out: List[Dict] = []
    seen_cx_cy: List[Tuple[float, float]] = []
    for item in sorted(best_by_payload.values(), key=lambda i: i["_score"]):
        if any(math.hypot(item["cx"] - ox, item["cy"] - oy) < 20 for ox, oy in seen_cx_cy):
            continue
        out.append({
            "payload": item["payload"],
            "cx": item["cx"],
            "cy": item["cy"],
            "corners": item["corners"],
        })
        seen_cx_cy.append((item["cx"], item["cy"]))

    return out


def _detect_aruco(frame: np.ndarray) -> List[Dict]:
    """Detect ArUco markers and return {id, cx, cy, corners} for each one."""
    aruco = getattr(cv2, "aruco", None)
    if aruco is None:
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    dictionary = aruco.getPredefinedDictionary(aruco.DICT_4X4_50)
    if hasattr(aruco, "ArucoDetector"):
        detector = aruco.ArucoDetector(dictionary, aruco.DetectorParameters())
        corners_list, ids, _ = detector.detectMarkers(gray)
    else:
        params = aruco.DetectorParameters_create()
        corners_list, ids, _ = aruco.detectMarkers(gray, dictionary, parameters=params)
    if ids is None or corners_list is None:
        return []
    out: List[Dict] = []
    for marker_id, corners in zip(ids.flatten().tolist(), corners_list):
        corners = corners.reshape(4, 2).astype(float)
        out.append({
            "id": int(marker_id),
            "cx": float(corners[:, 0].mean()),
            "cy": float(corners[:, 1].mean()),
            "corners": corners,
        })
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


def _heading_from_points(ax: float, ay: float, bx: float, by: float, offset: float = 0.0) -> float:
    """Heading from point A to point B in image coords."""
    return math.atan2(float(by - ay), float(bx - ax)) + offset


def _marker_size_px(corners: np.ndarray) -> float:
    """Average side length of a 4-corner marker in pixels."""
    w = (np.linalg.norm(corners[1] - corners[0]) + np.linalg.norm(corners[2] - corners[3])) / 2
    h = (np.linalg.norm(corners[3] - corners[0]) + np.linalg.norm(corners[2] - corners[1])) / 2
    return float((w + h) / 2)


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


# ── obstacle-avoidance heading ────────────────────────────────────────────────

def _avoidance_heading(bx: float, by: float, tx: float, ty: float,
                        items: List[Dict], exclude: set,
                        extra_repellers: list = (),
                        repulse_radius: float = _REPULSE_RADIUS_PX) -> float:
    """Potential-field heading: attract toward (tx,ty), repel from obstacle QRs + bg blobs.

    Items whose payload is in `exclude` (bot + current target) are ignored.
    extra_repellers: list of (ox, oy) pixel coordinates for non-QR obstacles.
    Returns angle in image coords (0 = right, π/2 = down).
    """
    adx, ady = tx - bx, ty - by
    d_target = math.hypot(adx, ady)
    if d_target > 0:
        adx /= d_target
        ady /= d_target

    rx, ry = 0.0, 0.0
    for item in items:
        if item["payload"] in exclude:
            continue
        ox, oy = item["cx"], item["cy"]
        d = math.hypot(ox - bx, oy - by)
        if d < 1 or d >= repulse_radius:
            continue
        strength = ((repulse_radius - d) / repulse_radius) ** 2
        rx += strength * (bx - ox) / d
        ry += strength * (by - oy) / d

    for ox, oy in extra_repellers:
        d = math.hypot(ox - bx, oy - by)
        if d < 1 or d >= repulse_radius:
            continue
        strength = ((repulse_radius - d) / repulse_radius) ** 2
        rx += strength * (bx - ox) / d
        ry += strength * (by - oy) / d

    return math.atan2(ady + ry, adx + rx)


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
        claw_center_offset_px: Optional[float] = None,
        center_tol_px: int     = DEFAULT_CENTER_TOL_PX,
        drive_speed: int       = DEFAULT_DRIVE_SPEED,
        turn_speed: int        = DEFAULT_TURN_SPEED,
        align_threshold: float = DEFAULT_ALIGN_THRESHOLD,
        heading_offset: float  = 0.0,
        target_qr: Optional[str]  = None,
        bot_qr: Optional[str]     = None,
        bot_aruco_id: Optional[int] = None,
        on_state_change: Optional[StateCallback] = None,
    ):
        self._camera_index     = camera_index
        self._drive            = drive
        self._arrived_area_pct = arrived_area_pct
        self._arrived_dist_px  = arrived_dist_px
        self._claw_offset_px   = claw_offset_px   # pixels ≡ 11cm claw reach
        self._claw_center_offset_px = claw_center_offset_px
        self._use_claw_arrived = False             # toggled by mission controller
        self._claw_arrived_adjust_px = 0          # signed pixel adjustment for claw-mode arrival
        self._pickup_contact_arrived = False
        self._allow_reverse = True
        self._force_reverse = False
        self._center_tol_px    = center_tol_px
        self._drive_speed      = drive_speed
        self._turn_speed       = turn_speed
        self._align_threshold  = align_threshold
        self._heading_offset   = heading_offset
        self._target_qr        = target_qr
        self._bot_qr           = bot_qr
        self._bot_aruco_id     = bot_aruco_id
        self._navigating       = False
        self._on_state         = on_state_change
        self._base_qr:         Optional[str]                    = None
        self._parking_offset:  Optional[Tuple[float, float]]    = None
        self._target_offset:   Optional[Tuple[float, float]]    = None
        self._station_qrs:     set                              = set()
        self._qr_missing_frames: Dict[str, int]                 = {}
        self._qr_clear_frames:  Dict[str, int]                  = {}
        self._full_stations:   set                              = set()
        self._smoothed_heading:   Optional[float]                = None
        self._in_place_turning:   bool                          = False
        self._committed_turn_dir: Optional[float]               = None  # +1 or -1, locked on entry
        self._in_reverse_steer:   bool                          = False
        self._reverse_steer_dir:  Optional[float]               = None  # +1 or -1, locked while backing
        self._last_search_log:    float                         = 0.0
        self._manual_mode:        bool                          = False
        self._last_target_pos:    Optional[Tuple[float, float]] = None
        self._last_target_metric: Optional[float]               = None
        self._last_target_seen_t: float                         = 0.0

        self.state             = NavigatorState()
        self._running          = False
        self._cap: Optional[object] = None          # set while run() holds the camera
        self._stopped_event    = threading.Event()
        self._stopped_event.set()   # not running yet
        self._raw_frame: Optional[np.ndarray] = None
        self._frame_lock       = threading.Lock()
        self._motor_override   = False  # True = frame loop must not call stop_motors()

    # ── public control ────────────────────────────────────────────────────────

    def set_target(self, qr: Optional[str]) -> None:
        self._target_qr = qr
        self._target_offset = None
        self._last_target_pos = None
        self._last_target_metric = None
        self._last_target_seen_t = 0.0

    def set_bot_qr(self, qr: Optional[str]) -> None:
        self._bot_qr = qr

    def set_bot_aruco_id(self, marker_id: Optional[int]) -> None:
        self._bot_aruco_id = marker_id

    def set_base_qr(self, qr: Optional[str]) -> None:
        self._base_qr = qr

    def set_parking_offset(self, offset: Optional[Tuple[float, float]]) -> None:
        self._parking_offset = offset

    def set_target_offset(self, offset: Optional[Tuple[float, float]]) -> None:
        self._target_offset = offset

    def set_claw_center_offset_px(self, offset_px: Optional[float]) -> None:
        self._claw_center_offset_px = float(offset_px) if offset_px is not None else None

    def set_station_qrs(self, qrs: set) -> None:
        self._station_qrs = set(qrs)
        # Drop tracking data for QRs no longer registered
        for qr in list(self._qr_missing_frames):
            if qr not in self._station_qrs:
                del self._qr_missing_frames[qr]
                self._qr_clear_frames.pop(qr, None)
        self._full_stations &= self._station_qrs

    @property
    def full_stations(self) -> set:
        return frozenset(self._full_stations)

    def set_navigating(self, active: bool) -> None:
        self._navigating = active
        if active:
            # Reset turn state so a stale committed direction from a prior maneuver
            # never carries over into the first frame of the new navigation.
            self._in_place_turning   = False
            self._committed_turn_dir = None
            self._in_reverse_steer   = False
            self._reverse_steer_dir  = None
        else:
            try:
                self._drive.stop_motors()
            except Exception:
                pass

    def set_allow_reverse(self, active: bool) -> None:
        """Enable or disable the reverse-approach mode (back into target)."""
        self._allow_reverse = active

    def set_claw_arrived(self, active: bool, adjust_px: int = 0) -> None:
        """When True, arrived uses claw_offset_px plus an optional signed pixel adjustment."""
        self._use_claw_arrived = active
        self._claw_arrived_adjust_px = int(adjust_px) if active else 0

    def set_pickup_contact_arrived(self, active: bool) -> None:
        """When True, package pickup can stop on claw contact before QR occlusion."""
        self._pickup_contact_arrived = bool(active)

    def set_motion_policy(self, *, allow_reverse: bool = True, force_reverse: bool = False) -> None:
        """Control whether nav may reverse, or must reverse for the current target."""
        self._allow_reverse = bool(allow_reverse)
        self._force_reverse = bool(force_reverse)
        if not self._allow_reverse and not self._force_reverse:
            self._in_reverse_steer = False
            self._reverse_steer_dir = None

    def set_manual_drive(self, left: int, right: int) -> None:
        """Enter manual mode and drive wheels at given power. Nav loop won't override."""
        self._manual_mode = True
        try:
            self._drive.drive(left, right)
        except Exception as exc:
            log.warning("manual drive error: %s", exc)

    def clear_manual_drive(self) -> None:
        """Exit manual mode and stop motors."""
        self._manual_mode = False
        try:
            self._drive.stop_motors()
        except Exception as exc:
            log.warning("manual stop error: %s", exc)

    def set_motor_override(self, active: bool) -> None:
        """When True the frame loop will not call stop_motors() while idle/not-navigating.
        Use this to let mission-controller code drive directly without the loop fighting it."""
        self._motor_override = active

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
            arrow_len = 60
            ax = int(bx + arrow_len * math.cos(st.bot_heading))
            ay = int(by + arrow_len * math.sin(st.bot_heading))
            cv2.arrowedLine(frame, (bx, by), (ax, ay), (255, 100, 0), 2, tipLength=0.25)
            cv2.circle(frame, (ax, ay), 8, (255, 80, 0), -1)   # blue forward dot (BGR)
            if st.target_pos:
                tx, ty = int(st.target_pos[0]), int(st.target_pos[1])
                cv2.line(frame, (bx, by), (tx, ty), (0, 220, 220), 1)
                mid = ((bx + tx) // 2, (by + ty) // 2)
                if st.dist_to_target is not None:
                    cv2.putText(frame, f"{st.dist_to_target:.0f}px",
                                mid, cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 220, 220), 1)

        # Configured front ArUco marker
        if self._bot_aruco_id is not None:
            try:
                items = _detect_aruco(frame)
                marker = next((i for i in items if i["id"] == self._bot_aruco_id), None)
                if marker:
                    mx, my = int(marker["cx"]), int(marker["cy"])
                    cv2.circle(frame, (mx, my), 9, (0, 255, 255), 2)
                    cv2.putText(frame, f"A{self._bot_aruco_id}", (mx + 10, my - 6),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
                    if st.bot_pos:
                        bx, by = int(st.bot_pos[0]), int(st.bot_pos[1])
                        cv2.line(frame, (bx, by), (mx, my), (0, 255, 255), 2)
            except Exception:
                pass

        # Parking position marker (orange filled circle with label)
        if st.parking_target_pos:
            px, py = int(st.parking_target_pos[0]), int(st.parking_target_pos[1])
            cv2.circle(frame, (px, py), 10, (0, 140, 255), -1)
            cv2.circle(frame, (px, py), 10, (255, 255, 255), 2)
            cv2.putText(frame, "PARK", (px + 13, py + 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 140, 255), 1)

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
        if self._manual_mode:
            return
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
        if self._manual_mode:
            return
        fh, fw = frame.shape[:2]
        items = _detect_overhead(frame)
        aruco_items = _detect_aruco(frame)

        qr_items = [i for i in items if i["payload"]]

        all_payloads = [i["payload"] for i in qr_items]
        all_rects    = []
        for i in qr_items:
            c = i["corners"]
            x1, y1 = int(c[:, 0].min()), int(c[:, 1].min())
            x2, y2 = int(c[:, 0].max()), int(c[:, 1].max())
            all_rects.append((i["payload"], x1, y1, x2 - x1, y2 - y1))

        bot    = next((i for i in qr_items if i["payload"] == self._bot_qr),    None)
        target = next((i for i in qr_items if i["payload"] == self._target_qr), None) \
                 if self._target_qr else None
        bot_aruco = next((i for i in aruco_items if i["id"] == self._bot_aruco_id), None) \
                    if self._bot_aruco_id is not None else None

        # ── Occlusion tracking (station QRs only) ────────────────────────────
        visible_set = set(all_payloads)
        camera_ok   = bool(visible_set)
        for qr in self._station_qrs:
            if qr in visible_set:
                self._qr_missing_frames[qr] = 0
                self._qr_clear_frames[qr]   = self._qr_clear_frames.get(qr, 0) + 1
                if self._qr_clear_frames[qr] >= _OCCLUDE_CLEAR_FRAMES:
                    self._full_stations.discard(qr)
            elif camera_ok:
                self._qr_clear_frames[qr]   = 0
                self._qr_missing_frames[qr] = self._qr_missing_frames.get(qr, 0) + 1
                if self._qr_missing_frames[qr] >= _OCCLUDE_FULL_FRAMES:
                    self._full_stations.add(qr)
        full_list = list(self._full_stations)

        # ── Parking marker ────────────────────────────────────────────────────
        park_pos: Optional[Tuple[float, float]] = None
        if self._base_qr and self._parking_offset:
            base_item = next((i for i in items if i["payload"] == self._base_qr), None)
            if base_item:
                park_pos = (
                    base_item["cx"] + self._parking_offset[0],
                    base_item["cy"] + self._parking_offset[1],
                )

        if bot is None:
            self._smoothed_heading   = None
            self._in_place_turning   = False
            self._committed_turn_dir = None
            self._in_reverse_steer   = False
            self._reverse_steer_dir  = None
            self._drive.stop_motors()
            if self._navigating:
                now = time.time()
                if now - self._last_search_log >= 2.0:
                    self._last_search_log = now
                    log.warning("Navigator: bot QR '%s' not visible — seen: %s",
                                self._bot_qr, all_payloads or ["(none)"])
            self._set_state(
                status="idle" if not self._navigating else "searching",
                qr_payload=None, qr_cx=None, qr_area_pct=None,
                frame_w=fw, frame_h=fh,
                all_qr_payloads=all_payloads, all_qr_rects=all_rects,
                bot_pos=None, bot_heading=None,
                target_pos=None, dist_to_target=None, heading_error=None,
                parking_target_pos=park_pos, full_station_qrs=full_list,
            )
            return

        bx, by = bot["cx"], bot["cy"]
        if bot_aruco is not None:
            # ArUco front marker already defines the forward axis directly.
            # Do not reuse the legacy QR-corner heading offset here; that offset
            # was calibrated against QR orientation and can invert front/back.
            raw_hdg = _heading_from_points(bx, by, bot_aruco["cx"], bot_aruco["cy"], 0.0)
        else:
            raw_hdg = _qr_heading(bot["corners"], self._heading_offset)

        # EMA heading smoother: blend small changes; during in-place turns allow large deltas
        if self._smoothed_heading is None:
            self._smoothed_heading = raw_hdg
        else:
            delta = _angle_diff(raw_hdg, self._smoothed_heading)
            max_delta = math.pi if self._in_place_turning else _HEADING_MAX_DELTA
            if abs(delta) <= max_delta:
                self._smoothed_heading += _HEADING_ALPHA * delta
                self._smoothed_heading = math.atan2(
                    math.sin(self._smoothed_heading), math.cos(self._smoothed_heading))
            # else: large delta during forward drive → likely noise, hold current estimate
        bot_hdg = self._smoothed_heading

        if not self._navigating:
            if not self._motor_override:
                self._drive.stop_motors()
            self._set_state(
                status="idle",
                qr_payload=None, qr_cx=int(bx), qr_area_pct=None,
                frame_w=fw, frame_h=fh,
                all_qr_payloads=all_payloads, all_qr_rects=all_rects,
                bot_pos=(bx, by), bot_heading=bot_hdg,
                target_pos=None, dist_to_target=None, heading_error=None,
                parking_target_pos=park_pos, full_station_qrs=full_list,
            )
            return

        claw_x, claw_y = bx, by
        has_calibrated_claw_center = bot_aruco is not None and self._claw_center_offset_px is not None
        if bot_aruco is not None:
            # Prefer the image-calibrated claw centre; fall back to the 10cm estimate if unset.
            claw_center_px = self._claw_center_offset_px
            if claw_center_px is None:
                claw_center_px = (self._claw_offset_px / 11.0) * DEFAULT_CLAW_CENTER_CM if self._claw_offset_px > 0 else 0.0
            claw_x = bot_aruco["cx"] + claw_center_px * math.cos(bot_hdg)
            claw_y = bot_aruco["cy"] + claw_center_px * math.sin(bot_hdg)

        if target is None:
            _CLAW_COAST_S = 2.0   # seconds to drive blind after station QR is occluded
            in_claw_delivery = self._use_claw_arrived and (has_calibrated_claw_center or self._claw_offset_px > 0)
            time_since_target = time.time() - self._last_target_seen_t
            last_tx, last_ty = self._last_target_pos if self._last_target_pos else (claw_x, claw_y)

            if in_claw_delivery and self._last_target_seen_t > 0:
                if time_since_target <= _CLAW_COAST_S:
                    # Station QR likely occluded by the package — keep driving forward
                    self._drive.drive(self._drive_speed, self._drive_speed)
                    self._set_state(
                        status="approaching",
                        qr_payload=self._target_qr, qr_cx=int(bx), qr_area_pct=None,
                        frame_w=fw, frame_h=fh,
                        all_qr_payloads=all_payloads, all_qr_rects=all_rects,
                        bot_pos=(bx, by), bot_heading=bot_hdg,
                        target_pos=(last_tx, last_ty), dist_to_target=self._last_target_metric,
                        heading_error=0.0, parking_target_pos=park_pos, full_station_qrs=full_list,
                    )
                    return
                else:
                    # Coast expired → package is at station, declare arrived
                    self._last_target_seen_t = 0.0  # prevent re-triggering
                    self._drive.stop_motors()
                    self._set_state(
                        status="arrived",
                        qr_payload=self._target_qr, qr_cx=int(bx), qr_area_pct=1.0,
                        frame_w=fw, frame_h=fh,
                        all_qr_payloads=all_payloads, all_qr_rects=all_rects,
                        bot_pos=(bx, by), bot_heading=bot_hdg,
                        target_pos=(last_tx, last_ty), dist_to_target=self._last_target_metric,
                        heading_error=0.0, parking_target_pos=park_pos, full_station_qrs=full_list,
                    )
                    log.info("Navigator: claw coast ended → arrived at '%s' (station QR occluded)",
                             self._target_qr)
                    return

            self._drive.stop_motors()
            now = time.time()
            if now - self._last_search_log >= 2.0:
                self._last_search_log = now
                log.warning("Navigator: target QR '%s' not visible — seen: %s",
                            self._target_qr, all_payloads or ["(none)"])
            self._set_state(
                status="searching",
                qr_payload=None, qr_cx=int(bx), qr_area_pct=None,
                frame_w=fw, frame_h=fh,
                all_qr_payloads=all_payloads, all_qr_rects=all_rects,
                bot_pos=(bx, by), bot_heading=bot_hdg,
                target_pos=None, dist_to_target=None, heading_error=None,
                parking_target_pos=park_pos, full_station_qrs=full_list,
            )
            return

        tx, ty = target["cx"], target["cy"]
        is_base_parking = bool(self._parking_offset and self._target_qr == self._base_qr)
        if is_base_parking:
            eff_tx = tx + self._parking_offset[0]
            eff_ty = ty + self._parking_offset[1]
        elif self._target_offset is not None:
            eff_tx = tx + self._target_offset[0]
            eff_ty = ty + self._target_offset[1]
        else:
            eff_tx, eff_ty = tx, ty

        dx, dy   = eff_tx - bx, eff_ty - by
        distance = math.hypot(dx, dy)
        claw_distance = math.hypot(eff_tx - claw_x, eff_ty - claw_y)
        target_size_px = _marker_size_px(target["corners"])

        if self._use_claw_arrived and has_calibrated_claw_center:
            arrived_threshold = max(8, DEFAULT_CLAW_CENTER_ARRIVED_DIST_PX + self._claw_arrived_adjust_px)
        elif self._use_claw_arrived and self._claw_offset_px > 0:
            arrived_threshold = max(5, self._claw_offset_px + self._claw_arrived_adjust_px)
        else:
            arrived_threshold = self._arrived_dist_px
        if (not self._use_claw_arrived) and is_base_parking:
            arrived_threshold = min(arrived_threshold, DEFAULT_BASE_PARK_ARRIVED_DIST_PX)
        distance_metric = claw_distance if (self._use_claw_arrived and (has_calibrated_claw_center or self._claw_offset_px > 0)) else distance
        self._last_target_pos = (eff_tx, eff_ty)
        self._last_target_metric = distance_metric
        self._last_target_seen_t = time.time()
        pickup_contact = (
            self._pickup_contact_arrived
            and self._use_claw_arrived
            and not is_base_parking
            and claw_distance <= max(DEFAULT_PICKUP_CONTACT_MIN_PX, target_size_px * DEFAULT_PICKUP_CONTACT_RATIO)
        )
        if pickup_contact or distance_metric < arrived_threshold:
            self._drive.stop_motors()
            self._set_state(
                status="arrived",
                qr_payload=self._target_qr, qr_cx=int(bx), qr_area_pct=1.0,
                frame_w=fw, frame_h=fh,
                all_qr_payloads=all_payloads, all_qr_rects=all_rects,
                bot_pos=(bx, by), bot_heading=bot_hdg,
                target_pos=(eff_tx, eff_ty), dist_to_target=distance_metric, heading_error=0.0,
                parking_target_pos=park_pos, full_station_qrs=full_list,
            )
            if pickup_contact:
                log.info("Navigator (overhead): pickup contact at '%s' claw_dist=%.1fpx qr_size=%.1fpx",
                         self._target_qr, claw_distance, target_size_px)
            else:
                log.info("Navigator (overhead): arrived at '%s' dist=%.1fpx",
                         self._target_qr, distance_metric)
            return

        front_only_target = (not self._allow_reverse) and (not is_base_parking)
        align_x, align_y = bx, by
        if front_only_target and bot_aruco is not None:
            # For station/package runs, align the front axis defined by QR -> ArUco.
            align_x, align_y = bot_aruco["cx"], bot_aruco["cy"]
        is_station_target = (
            self._target_qr is not None
            and self._target_qr in self._station_qrs
            and self._target_qr != self._base_qr
        )
        if front_only_target and is_station_target:
            # Stations are clustered together, so treating neighboring station QRs as
            # repellers can bend the approach around the strip and make the robot
            # appear to "prefer" its back side. For delivery, point the claw axis
            # straight at the selected station instead.
            desired = _heading_from_points(align_x, align_y, eff_tx, eff_ty, 0.0)
        else:
            desired = _avoidance_heading(align_x, align_y, eff_tx, eff_ty, qr_items,
                                         exclude={self._bot_qr, self._target_qr})
        err     = _angle_diff(desired, bot_hdg)
        reverse_err = _angle_diff(desired, bot_hdg + math.pi)
        rel_dx, rel_dy = eff_tx - align_x, eff_ty - align_y
        forward_proj = math.cos(bot_hdg) * rel_dx + math.sin(bot_hdg) * rel_dy
        side_err = math.cos(bot_hdg) * rel_dy - math.sin(bot_hdg) * rel_dx
        forward_turn_err = err
        if front_only_target and abs(side_err) > 1e-6:
            # For forward-only delivery runs, choose left/right from the visible side
            # of the target relative to the bot. This is more stable than relying on
            # the wrapped sign of ±pi when the target is nearly behind.
            forward_turn_err = abs(err) if side_err > 0 else -abs(err)

        # ── Reverse mode policies ────────────────────────────────────────────
        _REVERSE_ZONE_PX = 220
        _REVERSE_ENTER_ERR = math.pi * 0.55
        _REVERSE_EXIT_ERR  = math.pi * 0.45
        # Reverse is only ever valid when backing into the base parking spot.
        # Gate on the target matching base_qr so no accidental reverse to stations/packages.
        _target_is_base = (self._target_qr is not None and
                           self._base_qr is not None and
                           self._target_qr == self._base_qr)
        reverse_mode = False
        if self._force_reverse and _target_is_base:
            reverse_mode = True
        elif self._allow_reverse and _target_is_base:
            reverse_mode = (
                (distance < _REVERSE_ZONE_PX and abs(err) > _REVERSE_ENTER_ERR) or
                (self._in_reverse_steer and distance < _REVERSE_ZONE_PX and abs(err) > _REVERSE_EXIT_ERR)
            )
        if reverse_mode:
            reverse_heading_err = reverse_err
            reverse_turn_threshold = self._align_threshold
            reverse_exit_threshold = reverse_turn_threshold * 0.7
            reverse_entering_turn = abs(reverse_heading_err) > reverse_turn_threshold
            reverse_staying_turn = self._in_reverse_steer and abs(reverse_heading_err) > reverse_exit_threshold

            if reverse_entering_turn or reverse_staying_turn:
                if not self._in_reverse_steer:
                    self._reverse_steer_dir = 1.0 if reverse_heading_err > 0 else -1.0
                elif abs(reverse_heading_err) < math.pi * 0.35:
                    self._reverse_steer_dir = 1.0 if reverse_heading_err > 0 else -1.0
                self._in_reverse_steer = True
                frac = min(1.0, abs(reverse_heading_err) / math.pi)
                spd  = max(40, int(frac * self._turn_speed))
                d = self._reverse_steer_dir or (1.0 if reverse_heading_err > 0 else -1.0)
                # Back into the target while keeping shortest backward-facing rotation.
                self._drive.drive(int(spd * d), int(-spd * d))
                self._set_state(
                    status="centering",
                    qr_payload=self._target_qr, qr_cx=int(bx),
                    qr_area_pct=max(0.0, 1.0 - distance / max(fw, fh)),
                    frame_w=fw, frame_h=fh,
                    all_qr_payloads=all_payloads, all_qr_rects=all_rects,
                    bot_pos=(bx, by), bot_heading=bot_hdg,
                    target_pos=(eff_tx, eff_ty), dist_to_target=distance,
                    heading_error=reverse_heading_err,
                    parking_target_pos=park_pos, full_station_qrs=full_list,
                )
                return

            self._in_reverse_steer = True
            turn = int((reverse_heading_err / self._align_threshold) * self._turn_speed)
            left_speed  = max(-100, min(100, -self._drive_speed + turn))
            right_speed = max(-100, min(100, -self._drive_speed - turn))
            self._drive.drive(left_speed, right_speed)
            self._set_state(
                status="approaching",
                qr_payload=self._target_qr, qr_cx=int(bx),
                qr_area_pct=max(0.0, 1.0 - distance / max(fw, fh)),
                frame_w=fw, frame_h=fh,
                all_qr_payloads=all_payloads, all_qr_rects=all_rects,
                bot_pos=(bx, by), bot_heading=bot_hdg,
                target_pos=(eff_tx, eff_ty), dist_to_target=distance,
                heading_error=reverse_heading_err,
                parking_target_pos=park_pos, full_station_qrs=full_list,
            )
            return

        self._in_reverse_steer  = False
        self._reverse_steer_dir = None

        force_front_turn = front_only_target and (forward_proj <= 0.0)
        turn_enter_threshold = self._align_threshold
        if front_only_target:
            # Delivery targets should be brought much closer to the claw-facing axis
            # before we allow any forward motion.
            turn_enter_threshold = min(self._align_threshold, math.radians(25))

        # Hysteresis: enter in-place turn at align_threshold, exit only at 70% of it
        _exit_threshold = turn_enter_threshold * 0.7
        entering_turn = abs(err) > turn_enter_threshold or force_front_turn
        staying_in_turn = self._in_place_turning and (abs(err) > _exit_threshold or force_front_turn)

        if entering_turn or staying_in_turn:
            if not self._in_place_turning:
                # Commit to a direction on entry — prevents ±π sign-flip oscillation
                self._committed_turn_dir = 1.0 if forward_turn_err > 0 else -1.0
            elif front_only_target:
                # Forward-only runs must keep rotating the front axis toward the target.
                # Do not reconsider the opposite turn while the target is still behind
                # the ArUco-facing hemisphere.
                if forward_proj > 0.0 and abs(side_err) > 1e-6:
                    self._committed_turn_dir = 1.0 if forward_turn_err > 0 else -1.0
            elif abs(err) < math.pi * 0.5:
                # Within ±90° of target — safe to update direction
                self._committed_turn_dir = 1.0 if forward_turn_err > 0 else -1.0
            self._in_place_turning = True
            frac = min(1.0, abs(err) / math.pi)
            spd  = max(40, int(frac * self._turn_speed))
            d    = self._committed_turn_dir or (1.0 if forward_turn_err > 0 else -1.0)
            # d>0 = need CW/right turn → drive(+spd, -spd); d<0 = CCW/left → drive(-spd, +spd)
            self._drive.drive(int(spd * d), int(-spd * d))
            new_status = "centering"
        else:
            # Small error → proportional differential drive (always moving forward)
            self._in_place_turning   = False
            self._committed_turn_dir = None
            turn        = int((forward_turn_err / self._align_threshold) * self._turn_speed)
            if front_only_target:
                left_speed  = max(0, min(100, self._drive_speed + turn))
                right_speed = max(0, min(100, self._drive_speed - turn))
            else:
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
            target_pos=(eff_tx, eff_ty), dist_to_target=distance, heading_error=err,
            parking_target_pos=park_pos, full_station_qrs=full_list,
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
        self._stopped_event.clear()
        cap = cv2.VideoCapture(self._camera_index)
        if not cap.isOpened():
            log.error("QRNavigator: cannot open camera %d", self._camera_index)
            self._stopped_event.set()
            self._set_state(status="idle")
            return

        # Minimise internal buffer so cap.read() always returns the latest frame
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self._cap = cap   # expose so stop() can interrupt a blocking read

        mode = "overhead" if self._bot_qr else "onboard"
        log.info("QRNavigator: camera %d open [%s mode] %.0fx%.0f",
                 self._camera_index, mode,
                 cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self._set_state(status="idle")

        consecutive_failures = 0
        try:
            while self._running:
                # Drain any extra buffered frames so we always process the freshest one
                cap.grab()
                ok, frame = cap.read()
                if not ok:
                    consecutive_failures += 1
                    if consecutive_failures == 1:
                        log.warning("QRNavigator: cap.read() failed on camera %d (running=%s, cap_opened=%s)",
                                    self._camera_index, self._running, cap.isOpened())
                    elif consecutive_failures % 50 == 0:
                        log.warning("QRNavigator: camera %d still failing after %d consecutive reads",
                                    self._camera_index, consecutive_failures)
                    time.sleep(0.02)
                    continue
                consecutive_failures = 0
                self.process_frame(frame)
        finally:
            self._cap = None
            self._drive.stop_motors()
            cap.release()
            self._set_state(status="idle")
            self._stopped_event.set()
            log.info("QRNavigator: stopped (camera %d, failures=%d)", self._camera_index, consecutive_failures)

    def stop(self) -> None:
        self._running = False
        cap = self._cap
        if cap is not None:
            log.info("QRNavigator.stop(): releasing camera %d to interrupt blocking read", self._camera_index)
            try:
                cap.release()
            except Exception as exc:
                log.warning("QRNavigator.stop(): cap.release() raised: %s", exc)

    def wait_stopped(self, timeout: float = 6.0) -> bool:
        """Block until run() finishes. Returns True if stopped cleanly within timeout."""
        return self._stopped_event.wait(timeout=timeout)


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

    nav = QRNavigator(
        camera_index     = getattr(config, "camera_index",          0),
        drive            = drive,
        arrived_area_pct = getattr(config, "nav_arrived_area_pct",  DEFAULT_ARRIVED_AREA_PCT),
        arrived_dist_px  = getattr(config, "nav_arrived_dist_px",   DEFAULT_ARRIVED_DIST_PX),
        claw_offset_px   = getattr(config, "nav_claw_offset_px",    DEFAULT_CLAW_OFFSET_PX),
        claw_center_offset_px = getattr(config, "nav_claw_center_offset_px", None),
        center_tol_px    = getattr(config, "nav_center_tol_px",     DEFAULT_CENTER_TOL_PX),
        drive_speed      = getattr(config, "nav_drive_speed",        DEFAULT_DRIVE_SPEED),
        turn_speed       = getattr(config, "nav_turn_speed",         DEFAULT_TURN_SPEED),
        align_threshold  = getattr(config, "nav_align_threshold",    DEFAULT_ALIGN_THRESHOLD),
        heading_offset   = getattr(config, "nav_heading_offset",     0.0),
        bot_qr           = bot_qr if nav_mode == "overhead" else None,
        on_state_change  = on_state_change,
    )
    nav._claw_arrived_adjust_px = getattr(config, "nav_claw_arrived_bias_px", 0)
    return nav
