"""Tests for Phase 3 error-handling: ERR-01, ERR-02, ERR-03.
All tests use MagicMock robot/vision and real EventLogger with tmp_path.
No hardware or trained model required."""
from __future__ import annotations
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from config import load_config
from event_log import EventLogger
from station import Station
from vision import Detection


def _make_vision_returning(detection):
    vp = MagicMock()
    vp.detect.return_value = detection
    return vp


def _frame():
    return np.zeros((480, 640, 3), dtype=np.uint8)


def _det():
    return Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)


# ERR-01 / D-05: unknown package -> status resets to "free"
def test_unknown_package_returns_to_free(tmp_path):
    cfg = load_config()
    robot = MagicMock()
    vision = _make_vision_returning(_det())
    logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, logger)
    with patch("station.decode_from_frame", return_value=None):
        state, entry = st.run_once(_frame())
    assert entry is not None
    assert entry.status == "unknown_package"
    assert state.status == "free"          # D-05: reset to free after logging
    robot.move_to_bin.assert_not_called()


# D-04: halted station ignores all detections
def test_halted_station_ignores_detection(tmp_path):
    cfg = load_config()
    robot = MagicMock()
    vision = _make_vision_returning(_det())
    logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, logger)
    st._halted = True
    state, entry = st.run_once(_frame())
    assert entry is None
    vision.detect.assert_not_called()      # frame read but no detection check
    robot.move_to_bin.assert_not_called()


# D-07, ERR-03 complement: robot exception -> halted=True, status="error", lock released
def test_robot_exception_sets_halted(tmp_path):
    cfg = load_config()
    robot = MagicMock()
    robot.move_to_bin.side_effect = RuntimeError("motor stalled")
    vision = _make_vision_returning(_det())
    logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, logger)
    with patch("station.decode_from_frame", return_value="A"):
        state, entry = st.run_once(_frame())
    assert entry is not None
    assert entry.status == "error"
    assert "motor stalled" in (entry.error or "")
    assert st._halted is True              # D-07
    assert st._cycle_lock is False         # PITFALLS #8: lock released despite halt


# ERR-02 / D-01/D-03: watchdog fires -> station halted + status="error"
def test_watchdog_fires_and_halts(tmp_path):
    cfg = load_config()
    cfg = cfg.model_copy(update={"cycle_watchdog_timeout_s": 0})  # immediate fire
    robot = MagicMock()
    robot.return_home.side_effect = lambda: time.sleep(0.05)       # outlasts 0s timer
    vision = _make_vision_returning(_det())
    logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, logger)
    with patch("station.decode_from_frame", return_value="B"):
        st.run_once(_frame())
    time.sleep(0.15)   # let daemon timer thread execute
    assert st._halted is True
    assert st.state.status == "error"


# ERR-02 lock safety: cycle lock must not be stuck after watchdog fires
def test_cycle_lock_released_after_watchdog(tmp_path):
    cfg = load_config()
    cfg = cfg.model_copy(update={"cycle_watchdog_timeout_s": 0})
    robot = MagicMock()
    robot.return_home.side_effect = lambda: time.sleep(0.05)
    vision = _make_vision_returning(_det())
    logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, logger)
    with patch("station.decode_from_frame", return_value="C"):
        st.run_once(_frame())
    time.sleep(0.15)
    assert st._cycle_lock is False         # lock must not be stuck
