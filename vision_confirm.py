"""Vision confirmation layer (Phase 02.1).

Observability overlay — camera confirms the robot reached the expected
pixel target after a motion command. Vision DOES NOT drive motion
(D-01). See .planning/phases/02.1-vision-robot-tracking/02.1-CONTEXT.md.

Public API
----------
ROBOT_PAYLOAD : str
    Fixed QR payload printed on the top of the robot ("ROBOT"). D-03.

find_robot_qr(frame) -> tuple[int, int] | None
    Decode the full frame with pyzbar; return the pixel center (cx, cy) of
    the single QR whose decoded bytes equal b'ROBOT'. Return None if no such
    QR exists in the frame (D-05, D-09). Never raises on empty / malformed
    frames — returns None.

compute_drift(observed, expected) -> int | None
    Euclidean pixel distance between two (cx, cy) points, rounded to int.
    Returns None if either input is None (D-12).
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

try:
    from pyzbar import pyzbar as _pyzbar   # full-frame decode for ROBOT QR
    _PYZBAR_AVAILABLE = True
except ImportError:                        # pragma: no cover
    _pyzbar = None                         # type: ignore[assignment]
    _PYZBAR_AVAILABLE = False

# D-03: single fixed payload affixed flat to the top center of the robot.
# Strictly separate from the A/B/C package-QR allow-list used by qr.py (D-04).
ROBOT_PAYLOAD: str = "ROBOT"


def find_robot_qr(frame) -> Optional[Tuple[int, int]]:
    """Decode the full frame and return the pixel center (cx, cy) of the
    QR whose payload bytes equal b'ROBOT', or None. Never raises.

    D-05: full-frame decode (robot position unknown a-priori).
    D-04: does NOT touch the A/B/C package-QR allow-list used by qr.py.
    D-09: if QR not found, returns None; caller logs 'robot_qr_not_found'.
    """
    if frame is None or not _PYZBAR_AVAILABLE:
        return None
    try:
        decoded = _pyzbar.decode(frame)
    except Exception:
        return None
    for d in decoded:
        if getattr(d, "data", None) == ROBOT_PAYLOAD.encode("ascii"):
            rect = d.rect
            cx = int(rect.left + rect.width / 2)
            cy = int(rect.top + rect.height / 2)
            return (cx, cy)
    return None


def compute_drift(
    observed: Optional[Tuple[int, int]],
    expected: Optional[Tuple[int, int]],
) -> Optional[int]:
    """Euclidean pixel distance between observed and expected centers.

    Returns None if either input is None (D-12). The integer result avoids
    sub-pixel noise when comparing against vision_confirm_tolerance_px (D-13).
    """
    if observed is None or expected is None:
        return None
    dx = observed[0] - expected[0]
    dy = observed[1] - expected[1]
    return int(round(math.hypot(dx, dy)))
