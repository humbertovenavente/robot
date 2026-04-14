"""YOLOv8 inference wrapper. D-01: yolov8n CPU baseline. D-05: YOLO first, then QR."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import logging
import os

import numpy as np

from config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Detection:
    bbox: tuple[int, int, int, int]   # (x1, y1, x2, y2) in image pixel coords
    confidence: float
    class_id: int


class VisionPipeline:
    """Lazy-loaded YOLO wrapper. Model loads on first detect() call so import is cheap."""

    def __init__(self, config: Config):
        self.config = config
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            # Lazy import: torch/ultralytics boot is heavy.
            from ultralytics import YOLO
            model_path = self.config.resolved_model_path
            if not model_path.exists():
                raise FileNotFoundError(
                    f"YOLO weights not found at {model_path}. "
                    f"Run `python scripts/train_yolo.py` first (Plan 03) and copy "
                    f"runs/detect/train/weights/best.pt -> {model_path}."
                )
            # STACK.md macOS pitfall #3: set MPS fallback env for Apple Silicon.
            os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
            log.info("Loading YOLO weights from %s", model_path)
            self._model = YOLO(str(model_path))

    def detect(self, frame: np.ndarray) -> Optional[Detection]:
        """Run inference on a BGR frame. Returns the single highest-confidence Detection
        at or above the configured threshold, or None if no qualifying detection."""
        self._ensure_model()
        results = self._model(
            frame,
            imgsz=self.config.yolo_imgsz,
            conf=self.config.yolo_confidence_threshold,
            verbose=False,
        )
        if not results:
            return None
        # Ultralytics Results: first element for single-frame inference
        r = results[0]
        if r.boxes is None or len(r.boxes) == 0:
            return None
        # Pick the highest-confidence box
        boxes = r.boxes
        confs = boxes.conf.cpu().numpy() if hasattr(boxes.conf, "cpu") else np.asarray(boxes.conf)
        best_i = int(np.argmax(confs))
        xyxy = boxes.xyxy[best_i].cpu().numpy() if hasattr(boxes.xyxy, "cpu") else np.asarray(boxes.xyxy[best_i])
        cls = boxes.cls[best_i].cpu().numpy() if hasattr(boxes.cls, "cpu") else np.asarray(boxes.cls[best_i])
        x1, y1, x2, y2 = (int(v) for v in xyxy)
        return Detection(bbox=(x1, y1, x2, y2), confidence=float(confs[best_i]), class_id=int(cls))


def load_vision(config: Config) -> VisionPipeline:
    return VisionPipeline(config)
