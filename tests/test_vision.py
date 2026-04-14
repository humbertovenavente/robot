"""Unit tests for vision.VisionPipeline.

These tests MUST run without a trained model (CI-friendly). We test the pure-Python
logic path (configuration wiring, Detection dataclass, None-return behavior) by
monkey-patching the YOLO model. Real-weights tests go in Plan 05 integration tests.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from config import load_config
from vision import Detection, VisionPipeline, load_vision


def _fake_result(boxes=None):
    r = MagicMock()
    if boxes is None:
        r.boxes = None
    else:
        r.boxes = MagicMock()
        r.boxes.__len__ = lambda self: len(boxes)
        # xyxy, conf, cls all torch-like with .cpu().numpy()
        import numpy as _np
        xyxy_arr = _np.array([b["bbox"] for b in boxes], dtype=float)
        conf_arr = _np.array([b["conf"] for b in boxes], dtype=float)
        cls_arr = _np.array([b["cls"] for b in boxes], dtype=int)
        r.boxes.xyxy = xyxy_arr
        r.boxes.conf = conf_arr
        r.boxes.cls = cls_arr
    return [r]


def test_detection_is_frozen():
    d = Detection(bbox=(1, 2, 3, 4), confidence=0.9, class_id=0)
    with pytest.raises(Exception):
        d.confidence = 0.1  # frozen dataclass


def test_load_vision_returns_pipeline():
    cfg = load_config()
    vp = load_vision(cfg)
    assert isinstance(vp, VisionPipeline)


def test_detect_returns_none_when_no_boxes():
    cfg = load_config()
    vp = VisionPipeline(cfg)
    fake_model = MagicMock(return_value=_fake_result(boxes=None))
    vp._model = fake_model  # skip lazy load
    result = vp.detect(np.zeros((480, 640, 3), dtype=np.uint8))
    assert result is None


def test_detect_returns_highest_confidence_box():
    cfg = load_config()
    vp = VisionPipeline(cfg)
    boxes = [
        {"bbox": (10, 10, 100, 100), "conf": 0.6, "cls": 0},
        {"bbox": (20, 20, 120, 120), "conf": 0.9, "cls": 1},
        {"bbox": (30, 30, 130, 130), "conf": 0.4, "cls": 2},
    ]
    fake_model = MagicMock(return_value=_fake_result(boxes=boxes))
    vp._model = fake_model
    result = vp.detect(np.zeros((480, 640, 3), dtype=np.uint8))
    assert result is not None
    assert result.confidence == pytest.approx(0.9)
    assert result.class_id == 1
    assert result.bbox == (20, 20, 120, 120)


def test_detect_calls_model_with_configured_threshold_and_imgsz():
    cfg = load_config()
    vp = VisionPipeline(cfg)
    fake_model = MagicMock(return_value=_fake_result(boxes=None))
    vp._model = fake_model
    vp.detect(np.zeros((480, 640, 3), dtype=np.uint8))
    _, kwargs = fake_model.call_args
    assert kwargs["imgsz"] == cfg.yolo_imgsz
    assert kwargs["conf"] == cfg.yolo_confidence_threshold
    assert kwargs["verbose"] is False
