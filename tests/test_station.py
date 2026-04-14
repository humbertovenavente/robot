"""Integration-ish tests for Station.run_once using mocks for camera/YOLO/pyzbar.
These tests do not require a webcam or a trained model."""
from __future__ import annotations
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from config import load_config
from event_log import EventLogger
from station import Station, StationState
from vision import Detection


def _make_vision_returning(detection):
    vp = MagicMock()
    vp.detect.return_value = detection
    return vp


def test_run_once_returns_none_on_no_detection(tmp_path):
    cfg = load_config()
    robot = MagicMock()
    vision = _make_vision_returning(None)
    logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, logger)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    state, entry = st.run_once(frame)
    assert entry is None
    assert state.status == "free"
    robot.move_to_bin.assert_not_called()


def test_run_once_completes_happy_path(tmp_path):
    cfg = load_config()
    robot = MagicMock()
    det = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    vision = _make_vision_returning(det)
    logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, logger)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    with patch("station.decode_from_frame", return_value="B"):
        state, entry = st.run_once(frame)
    assert entry is not None
    assert entry.status == "completed"
    assert entry.cls == "B"
    assert entry.destination_bin == cfg.class_to_bin["B"]
    assert state.status == "free"
    robot.move_to_bin.assert_called_once_with(cfg.class_to_bin["B"])
    robot.deposit.assert_called_once()
    robot.return_home.assert_called_once()


def test_run_once_unknown_package_skips_robot(tmp_path):
    """VIS-04 / D-06: YOLO hit but QR decode fails -> no robot motion, log unknown_package."""
    cfg = load_config()
    robot = MagicMock()
    det = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    vision = _make_vision_returning(det)
    logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, logger)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    with patch("station.decode_from_frame", return_value=None):
        state, entry = st.run_once(frame)
    assert entry is not None
    assert entry.status == "unknown_package"
    assert entry.cls == "unknown"
    assert entry.destination_bin is None
    assert state.status == "free"
    robot.move_to_bin.assert_not_called()


def test_cycle_lock_blocks_reentry(tmp_path):
    """ROB-04: while locked, a second frame must not trigger a second cycle."""
    cfg = load_config()
    robot = MagicMock()
    det = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    vision = _make_vision_returning(det)
    logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, logger)
    st._cycle_lock = True  # simulate mid-cycle
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    state, entry = st.run_once(frame)
    assert entry is None
    vision.detect.assert_not_called()
    robot.move_to_bin.assert_not_called()


def test_lock_released_on_robot_failure(tmp_path):
    """PITFALLS #8: robot exception must not deadlock the lock."""
    cfg = load_config()
    robot = MagicMock()
    robot.move_to_bin.side_effect = RuntimeError("motor stalled")
    det = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    vision = _make_vision_returning(det)
    logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, logger)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    with patch("station.decode_from_frame", return_value="A"):
        state, entry = st.run_once(frame)
    assert entry.status == "error"
    assert "motor stalled" in (entry.error or "")
    assert st._cycle_lock is False  # lock released despite exception
    assert state.status == "error"


def test_status_listener_called_on_transition(tmp_path):
    cfg = load_config()
    robot = MagicMock()
    det = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    vision = _make_vision_returning(det)
    logger = EventLogger(tmp_path)
    seen_statuses = []
    st = Station(cfg, robot, vision, logger, status_listener=lambda s: seen_statuses.append(s.status))
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    with patch("station.decode_from_frame", return_value="C"):
        st.run_once(frame)
    assert "processing" in seen_statuses
    assert "free" in seen_statuses
