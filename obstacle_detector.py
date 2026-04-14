"""Obstacle detector using COCO-pretrained YOLOv8n.

Detects generic objects (person, box, chair, etc.) in the robot's travel zone.
Used by Station to pause cycles when the path is blocked (OBS-01).

ROI is expressed as percentages [x1_pct, y1_pct, x2_pct, y2_pct] (0-100) so the
same config works regardless of camera resolution.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

# COCO class names — index matches class_id in YOLOv8 COCO weights.
COCO_NAMES: List[str] = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]


def _coco_name(cls_id: int) -> str:
    return COCO_NAMES[cls_id] if cls_id < len(COCO_NAMES) else f"object_{cls_id}"


class ObstacleDetector:
    """Lazy-loaded COCO YOLOv8n wrapper for obstacle detection.

    Args:
        model_path: Path to yolov8n.pt (COCO pretrained).
        confidence_threshold: Minimum detection confidence to count as an obstacle.
        class_filter: List of COCO class IDs to treat as obstacles.
            Empty list or None means ALL 80 COCO classes count.
        roi: [x1_pct, y1_pct, x2_pct, y2_pct] (0–100).
            The sub-region of the frame representing the robot's travel zone.
            None means the full frame is checked.
    """

    def __init__(
        self,
        model_path: str,
        confidence_threshold: float = 0.50,
        class_filter: Optional[List[int]] = None,
        roi: Optional[List[int]] = None,
    ) -> None:
        self._model_path = model_path
        self._conf = confidence_threshold
        self._class_filter: Optional[List[int]] = class_filter if class_filter else None
        self._roi = roi  # list [x1%, y1%, x2%, y2%] or None
        self._model = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        from ultralytics import YOLO  # heavy import — deferred until first use
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        log.info("ObstacleDetector: loading model from %s", self._model_path)
        self._model = YOLO(str(self._model_path))

    def _crop_roi(self, frame: np.ndarray) -> np.ndarray:
        if self._roi is None:
            return frame
        h, w = frame.shape[:2]
        x1 = max(0, int(self._roi[0] * w / 100))
        y1 = max(0, int(self._roi[1] * h / 100))
        x2 = min(w, int(self._roi[2] * w / 100))
        y2 = min(h, int(self._roi[3] * h / 100))
        if x2 <= x1 or y2 <= y1:
            return frame  # degenerate ROI — fall back to full frame
        return frame[y1:y2, x1:x2]

    def is_blocked(self, frame: np.ndarray) -> Tuple[bool, Optional[str]]:
        """Check whether the robot's path zone contains an obstacle.

        Returns:
            (True, "object name") if an obstacle is detected.
            (False, None)         if the path is clear.
        """
        self._ensure_model()
        region = self._crop_roi(frame)
        if region.size == 0:
            return (False, None)

        results = self._model(region, conf=self._conf, verbose=False)
        if not results:
            return (False, None)

        r = results[0]
        if r.boxes is None or len(r.boxes) == 0:
            return (False, None)

        # Use model's own class names if available (custom models), else fall back to COCO
        model_names = getattr(self._model, "names", {})
        for box in r.boxes:
            cls_id = int(box.cls[0].item())
            if self._class_filter is not None and cls_id not in self._class_filter:
                continue
            name = model_names.get(cls_id) or _coco_name(cls_id)
            return (True, name)

        return (False, None)


def build_obstacle_detector(config) -> Optional[ObstacleDetector]:
    """Construct an ObstacleDetector from config, or return None if disabled."""
    if not getattr(config, "obstacle_detection_enabled", False):
        return None
    from pathlib import Path
    model_path = Path(getattr(config, "obstacle_model_path", "yolov8n.pt"))
    if not model_path.is_absolute():
        from config import REPO_ROOT
        model_path = REPO_ROOT / model_path
    if not model_path.exists():
        log.warning(
            "ObstacleDetector disabled: model not found at %s. "
            "Download with: python -c \"from ultralytics import YOLO; YOLO('yolov8n.pt')\"",
            model_path,
        )
        return None
    return ObstacleDetector(
        model_path=str(model_path),
        confidence_threshold=getattr(config, "obstacle_confidence_threshold", 0.50),
        class_filter=getattr(config, "obstacle_classes", None) or None,
        roi=getattr(config, "obstacle_roi", None),
    )
