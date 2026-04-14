"""Tests for vision-confirm integration in station._run_cycle.

Covers all 8 behaviors from Plan 02.1-04:
  1. Regression: disabled path never calls find_robot_qr, no vision fields in JSONL
  2. Enabled+ok: correct check writes vision_confirmed=True, vision_reason='ok'
  3. drift_exceeded: logged correctly, cycle completes
  4. robot_qr_not_found: find_robot_qr returns None
  5. no_target_calibrated: target is None in config
  6. pyzbar crash: exception caught, lock released, next cycle runs
  7. startup warning: enabled+all-null-targets logs exactly one warning
  8. no Phase 2 top-level import in station.py
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call

import numpy as np
import pytest

import station as station_module
from event_log import EventLogger
from station import Station, StationState
from vision import Detection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Cfg:
    """Minimal config stub — no pydantic, pure attrs."""
    station_id: str = "test-station"
    camera_index: int = 0
    qr_settle_delay_ms: int = 0
    class_to_bin: dict = None
    robot_implementation: str = "stub"
    orchestrator_url: Optional[str] = None
    orchestrator_enabled: bool = False

    # Phase 02.1 fields
    vision_confirm_enabled: bool = False
    vision_confirm_tolerance_px: int = 30
    robot_vision_targets: dict = None

    def __init__(
        self,
        enabled: bool = False,
        targets: Optional[dict] = None,
        tolerance: int = 30,
    ):
        self.station_id = "test-station"
        self.camera_index = 0
        self.qr_settle_delay_ms = 0
        self.class_to_bin = {"A": 1, "B": 2, "C": 3}
        self.robot_implementation = "stub"
        self.orchestrator_url = None
        self.orchestrator_enabled = False
        self.vision_confirm_enabled = enabled
        self.vision_confirm_tolerance_px = tolerance
        self.robot_vision_targets = targets if targets is not None else {
            "home": None, "bin_1": None, "bin_2": None, "bin_3": None
        }


def _make_station(cfg, tmp_path) -> tuple[Station, MagicMock, MagicMock, EventLogger]:
    """Build a Station with mocked robot + vision + real EventLogger."""
    robot = MagicMock()
    detection = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    vision = MagicMock()
    vision.detect.return_value = detection
    event_logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, event_logger)
    return st, robot, vision, event_logger


def _blank_frame() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


def _happy_detection():
    return Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)


# ---------------------------------------------------------------------------
# Test 1: Regression — disabled path, no vision calls, no vision fields in JSONL
# ---------------------------------------------------------------------------

def test_regression_disabled_no_vision_calls(tmp_path, monkeypatch):
    """vision_confirm_enabled=False → find_robot_qr never called; JSONL has no vision fields."""
    cfg = _Cfg(enabled=False)
    st, robot, vision, logger = _make_station(cfg, tmp_path)

    mock_find_qr = MagicMock(return_value=None)
    monkeypatch.setattr(station_module, "find_robot_qr", mock_find_qr)

    frame = _blank_frame()
    detection = _happy_detection()

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("station.decode_from_frame", lambda *a, **kw: "B")
        entry = st._run_cycle(frame, detection)

    assert mock_find_qr.call_count == 0, "find_robot_qr must not be called when disabled"
    # JSONL line must NOT have vision fields (Phase 1 byte-identical)
    d = entry.to_dict()
    assert "vision_confirmed" not in d, "vision_confirmed must not appear when disabled"
    assert "drift_px" not in d, "drift_px must not appear when disabled"
    assert "vision_reason" not in d, "vision_reason must not appear when disabled"


# ---------------------------------------------------------------------------
# Test 2: Enabled + ok path
# ---------------------------------------------------------------------------

def test_enabled_ok_writes_vision_confirmed_true(tmp_path, monkeypatch):
    """Enabled with both targets set; robot lands within tolerance → confirmed=True."""
    targets = {
        "home": [320, 240],
        "bin_2": [100, 200],
    }
    cfg = _Cfg(enabled=True, targets=targets, tolerance=30)
    st, robot, vision, logger = _make_station(cfg, tmp_path)

    # find_robot_qr returns a position close to home target (within 30px)
    mock_find_qr = MagicMock(return_value=(319, 242))
    monkeypatch.setattr(station_module, "find_robot_qr", mock_find_qr)

    frame = _blank_frame()
    detection = _happy_detection()

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("station.decode_from_frame", lambda *a, **kw: "B")
        entry = st._run_cycle(frame, detection)

    assert entry.status == "completed"
    assert entry.vision_confirmed is True
    assert entry.vision_reason == "ok"
    assert entry.drift_px is not None
    assert entry.drift_px <= 30


# ---------------------------------------------------------------------------
# Test 3: drift_exceeded — cycle still completes, robot actions still run
# ---------------------------------------------------------------------------

def test_drift_exceeded_cycle_still_completes(tmp_path, monkeypatch):
    """drift > tolerance → logged as drift_exceeded; robot.deposit + return_home still called."""
    targets = {
        "home": [100, 200],
        "bin_2": [100, 200],
    }
    cfg = _Cfg(enabled=True, targets=targets, tolerance=30)
    st, robot, vision, logger = _make_station(cfg, tmp_path)

    # Returns 100px away from expected (100,200) → drift=100 > 30
    mock_find_qr = MagicMock(return_value=(200, 200))
    monkeypatch.setattr(station_module, "find_robot_qr", mock_find_qr)

    frame = _blank_frame()
    detection = _happy_detection()

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("station.decode_from_frame", lambda *a, **kw: "B")
        entry = st._run_cycle(frame, detection)

    assert entry.status == "completed", "cycle must complete despite drift"
    assert entry.vision_confirmed is False
    assert entry.vision_reason == "drift_exceeded"
    assert entry.drift_px == 100
    robot.deposit.assert_called_once()
    robot.return_home.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4: robot_qr_not_found
# ---------------------------------------------------------------------------

def test_robot_qr_not_found(tmp_path, monkeypatch):
    """find_robot_qr returns None → vision_confirmed=False, drift_px=None, reason=robot_qr_not_found."""
    targets = {"home": [320, 240], "bin_2": [100, 200]}
    cfg = _Cfg(enabled=True, targets=targets, tolerance=30)
    st, robot, vision, logger = _make_station(cfg, tmp_path)

    mock_find_qr = MagicMock(return_value=None)
    monkeypatch.setattr(station_module, "find_robot_qr", mock_find_qr)

    frame = _blank_frame()
    detection = _happy_detection()

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("station.decode_from_frame", lambda *a, **kw: "B")
        entry = st._run_cycle(frame, detection)

    assert entry.vision_confirmed is False
    assert entry.drift_px is None
    assert entry.vision_reason == "robot_qr_not_found"


# ---------------------------------------------------------------------------
# Test 5: no_target_calibrated — target for bin is None
# ---------------------------------------------------------------------------

def test_no_target_calibrated(tmp_path, monkeypatch):
    """Target for destination bin is None → vision_reason='no_target_calibrated'."""
    targets = {"home": [320, 240], "bin_2": None}  # bin_2 not calibrated
    cfg = _Cfg(enabled=True, targets=targets, tolerance=30)
    st, robot, vision, logger = _make_station(cfg, tmp_path)

    mock_find_qr = MagicMock(return_value=(100, 200))
    monkeypatch.setattr(station_module, "find_robot_qr", mock_find_qr)

    frame = _blank_frame()
    detection = _happy_detection()

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("station.decode_from_frame", lambda *a, **kw: "B")
        entry = st._run_cycle(frame, detection)

    # Home check should use the home target which IS calibrated
    # But the bin check for bin_2 should give no_target_calibrated
    # The JSONL entry reflects the home check (D-08)
    # Let's check _vision_check directly for the bin_2 case
    vc_confirmed, vc_drift, vc_reason = st._vision_check(frame, "bin_2")
    assert vc_confirmed is None
    assert vc_drift is None
    assert vc_reason == "no_target_calibrated"


# ---------------------------------------------------------------------------
# Test 6: pyzbar crash — exception caught, lock released, next cycle runs
# ---------------------------------------------------------------------------

def test_pyzbar_crash_lock_released(tmp_path, monkeypatch):
    """find_robot_qr raises RuntimeError → reason starts with 'error:'; lock released."""
    targets = {"home": [320, 240], "bin_2": [100, 200]}
    cfg = _Cfg(enabled=True, targets=targets, tolerance=30)
    st, robot, vision, logger = _make_station(cfg, tmp_path)

    def _raise(*a, **kw):
        raise RuntimeError("pyzbar boom")

    monkeypatch.setattr(station_module, "find_robot_qr", _raise)

    frame = _blank_frame()
    detection = _happy_detection()

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("station.decode_from_frame", lambda *a, **kw: "B")
        entry = st._run_cycle(frame, detection)

    assert entry.vision_reason is not None
    assert entry.vision_reason.startswith("error:"), (
        f"expected reason starting with 'error:' but got {entry.vision_reason!r}"
    )
    assert st._cycle_lock is False, "lock must be released even after pyzbar crash"

    # Second cycle must not deadlock
    mock_find_qr2 = MagicMock(return_value=(320, 240))
    monkeypatch.setattr(station_module, "find_robot_qr", mock_find_qr2)
    with pytest.MonkeyPatch().context() as mp2:
        mp2.setattr("station.decode_from_frame", lambda *a, **kw: "B")
        entry2 = st._run_cycle(frame, detection)
    assert entry2.status == "completed"


# ---------------------------------------------------------------------------
# Test 7: startup warning — enabled=True + all-null-targets → one warning logged
# ---------------------------------------------------------------------------

def test_startup_warning_all_null_targets(tmp_path, caplog):
    """enabled=True + all targets None → exactly one WARNING about no calibrated targets."""
    cfg = _Cfg(enabled=True, targets={
        "home": None, "bin_1": None, "bin_2": None, "bin_3": None
    })

    with caplog.at_level(logging.WARNING, logger="station"):
        robot = MagicMock()
        vision = MagicMock()
        vision.detect.return_value = None
        logger = EventLogger(tmp_path)
        st = Station(cfg, robot, vision, logger)

    warning_lines = [r for r in caplog.records
                     if r.levelno >= logging.WARNING and "no targets calibrated" in r.message]
    assert len(warning_lines) == 1, (
        f"Expected exactly 1 warning about 'no targets calibrated', got {len(warning_lines)}: "
        f"{[r.message for r in warning_lines]}"
    )


# ---------------------------------------------------------------------------
# Test 8: No Phase 2 top-level import in station.py
# ---------------------------------------------------------------------------

def test_no_phase2_toplevel_import_in_station():
    """station.py must not contain top-level imports from orchestrator / ws_protocol."""
    source = Path(station_module.__file__).read_text(encoding="utf-8")

    assert "from orchestrator" not in source, (
        "station.py has 'from orchestrator' import — Phase 2 modules are frozen (D-19)"
    )
    assert "from ws_protocol" not in source, (
        "station.py has 'from ws_protocol' import — Phase 2 modules are frozen (D-19)"
    )
    # The existing lazy 'from ws_client import build_status_listener' inside run_station()
    # is pre-existing scaffolding and is permitted — do NOT flag it.
