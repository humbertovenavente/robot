"""QR decode from a YOLO bounding box crop.

Honors:
 - D-05: operates on the padded YOLO crop, not full-frame
 - D-04: payload is a single letter A/B/C; anything else -> None (unknown per D-06)
 - PITFALLS #3: retry up to config.qr_retry_count times with contrast preprocessing
 - PITFALLS #4: 10-15% padding around YOLO bbox (config.qr_padding_pct)
"""
from __future__ import annotations
from typing import Optional
import logging

import cv2
import numpy as np

from config import Config

log = logging.getLogger(__name__)

VALID_CLASSES = frozenset({"A", "B", "C"})


def pad_bbox(
    bbox: tuple[int, int, int, int],
    pct: float,
    frame_w: int,
    frame_h: int,
) -> tuple[int, int, int, int]:
    """Expand bbox by pct of its dimensions, clamped to frame bounds (PITFALLS #4)."""
    x1, y1, x2, y2 = bbox
    w = max(1, x2 - x1)
    h = max(1, y2 - y1)
    px = int(round(w * pct))
    py = int(round(h * pct))
    return (
        max(0, x1 - px),
        max(0, y1 - py),
        min(frame_w, x2 + px),
        min(frame_h, y2 + py),
    )


def crop_padded_region(
    frame: np.ndarray,
    bbox: tuple[int, int, int, int],
    padding_pct: float,
) -> np.ndarray:
    """Crop a padded ROI from the frame. Shape: (H, W, 3) BGR."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = pad_bbox(bbox, padding_pct, w, h)
    return frame[y1:y2, x1:x2].copy()


def _preprocess_variant(bgr: np.ndarray, attempt: int) -> np.ndarray:
    """Return a preprocessed version of the crop for the Nth retry attempt.
    attempt=0 returns the raw crop; higher attempts apply increasingly aggressive
    contrast enhancement (PITFALLS #3 recovery)."""
    if attempt == 0:
        return bgr
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if attempt == 1:
        # Otsu threshold — binarizes QR cells
        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return th
    # attempt >= 2: histogram equalization then adaptive threshold
    eq = cv2.equalizeHist(gray)
    th = cv2.adaptiveThreshold(eq, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 11, 2)
    return th


def decode_qr_from_crop(crop: np.ndarray, retry: int = 3) -> Optional[str]:
    """Decode a QR code from a cropped ROI. Returns A/B/C or None.

    Retries up to `retry` times with progressively more aggressive preprocessing.
    Per D-06, non-A/B/C payloads are treated as None (unknown package)."""
    from pyzbar.pyzbar import decode as zbar_decode  # lazy import to isolate zbar failures

    if crop.size == 0:
        return None

    for attempt in range(max(1, retry)):
        try:
            variant = _preprocess_variant(crop, attempt)
            results = zbar_decode(variant)
        except Exception as e:
            log.warning("pyzbar decode attempt %d raised: %s", attempt, e)
            continue
        if not results:
            continue
        # Take the first QR result; payload should be a single letter
        payload = results[0].data.decode("utf-8", errors="replace").strip()
        if payload in VALID_CLASSES:
            return payload
        log.info("QR decoded but payload %r not in A/B/C — treating as unknown (D-06)", payload)
        return None
    return None


def decode_from_frame(frame: np.ndarray, bbox: tuple[int, int, int, int], config: Config) -> Optional[str]:
    """Convenience: crop + decode in one call using config for padding and retry count."""
    crop = crop_padded_region(frame, bbox, config.qr_padding_pct)
    return decode_qr_from_crop(crop, retry=config.qr_retry_count)
