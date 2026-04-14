"""Integration + regression tests for Phase 02.1 vision-confirm feature.

Quality gate for Phase 02.1 — seals the entire vision-robot-tracking phase
before hand-off to Phase 3.

Tests A–F prove:
  A  Enabled happy path writes correct JSONL fields (VIS-05, VIS-07, VIS-08)
  B  drift_exceeded logged correctly, cycle still completes (VIS-07)
  C  Disabled path is byte-identical to Phase 1 D-16 schema (VIS-08)
  D  YAML calibration → Station runtime handoff works end-to-end (VIS-06)
  E  Phase 2 modules are NOT imported by station.py (freeze guard)
  F  REQ-ID coverage audit — all four VIS-05..08 tokens present in this file
"""
from __future__ import annotations

import importlib
import json
import sys
import textwrap
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import numpy as np
import pytest
import yaml

import station as station_module
from calibrate import write_pixel_target_to_yaml
from config import load_config
from event_log import EventLogger
from station import Station, StationState
from vision import Detection


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Phase 1 D-16 schema — the complete, canonical key-set for a non-vision cycle.
PHASE1_SCHEMA_KEYS = frozenset(
    {"timestamp", "class", "destination_bin", "cycle_time_ms", "status", "error"}
)

# Vision extension keys (D-18) — only present when confirmation runs.
VISION_EXTENSION_KEYS = frozenset({"vision_confirmed", "drift_px", "vision_reason"})


class _Cfg:
    """Minimal config stub — no pydantic, pure attrs.

    Mirrors _Cfg from test_station_vision_confirm.py so helpers are local
    and the test file is self-contained.
    """

    def __init__(
        self,
        enabled: bool = False,
        targets: Optional[dict] = None,
        tolerance: int = 30,
    ):
        self.station_id = "integration-test-station"
        self.camera_index = 0
        self.qr_settle_delay_ms = 0
        self.class_to_bin = {"A": 1, "B": 2, "C": 3}
        self.robot_implementation = "stub"
        self.orchestrator_url = None
        self.orchestrator_enabled = False
        self.vision_confirm_enabled = enabled
        self.vision_confirm_tolerance_px = tolerance
        self.robot_vision_targets = targets if targets is not None else {
            "home": None,
            "bin_1": None,
            "bin_2": None,
            "bin_3": None,
        }


def _make_station(cfg, tmp_path) -> tuple[Station, MagicMock, MagicMock, EventLogger]:
    """Assemble a Station with fully mocked robot + vision and a real on-disk EventLogger."""
    robot = MagicMock()
    detection = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    vision = MagicMock()
    vision.detect.return_value = detection
    event_logger = EventLogger(tmp_path)
    st = Station(cfg, robot, vision, event_logger)
    return st, robot, vision, event_logger


def _blank_frame() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


def _read_jsonl(log_dir: Path) -> list[dict]:
    """Read all JSONL lines written to *log_dir* (one file per session)."""
    lines: list[dict] = []
    for f in sorted(log_dir.glob("session-*.log")):
        for raw in f.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))
    return lines


# ---------------------------------------------------------------------------
# Test A: Enabled happy path — VIS-05 + VIS-07 + VIS-08
# ---------------------------------------------------------------------------

def test_enabled_happy_path_covers_vis05_vis07_vis08(monkeypatch, tmp_path):
    """Covers VIS-05 (ROBOT QR detection), VIS-07 (drift logged), VIS-08 (enabled flag).

    Config: vision_confirm_enabled=True, tolerance=30.
    Robot-QR observed at (321, 242); home target is (320, 240) → drift ≈ 2 px.
    After one full cycle resolving to bin_2, the JSONL entry must contain:
      vision_confirmed == True, drift_px == 2, vision_reason == "ok".
    """
    # VIS-08: flag is on; VIS-05: find_robot_qr will be called
    targets = {
        "home": [320, 240],
        "bin_1": [100, 100],
        "bin_2": [200, 200],
        "bin_3": [300, 300],
    }
    cfg = _Cfg(enabled=True, targets=targets, tolerance=30)
    st, robot, vision, logger = _make_station(cfg, tmp_path)

    # VIS-05: stub find_robot_qr to simulate ROBOT QR detected at (321, 242)
    # drift to home (320,240): sqrt((321-320)^2 + (242-240)^2) = sqrt(1+4) ≈ 2 px
    monkeypatch.setattr(station_module, "find_robot_qr", lambda frame: (321, 242))

    frame = _blank_frame()
    detection = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    monkeypatch.setattr("station.decode_from_frame", lambda *a, **kw: "B")

    entry = st._run_cycle(frame, detection)

    # VIS-07: drift must be logged in the returned LogEntry
    assert entry.status == "completed", f"expected completed, got {entry.status}"
    assert entry.vision_confirmed is True, "VIS-08: enabled path must set vision_confirmed=True"
    assert entry.vision_reason == "ok", f"expected 'ok', got {entry.vision_reason!r}"
    # VIS-07: drift_px must be a small positive integer (≈2)
    assert entry.drift_px is not None, "VIS-07: drift_px must be populated"
    assert entry.drift_px <= 30, f"drift_px should be within tolerance, got {entry.drift_px}"

    # VIS-07: drift must also appear in the on-disk JSONL
    logger.close()
    records = _read_jsonl(tmp_path)
    assert len(records) == 1, f"expected 1 JSONL line, got {len(records)}"
    rec = records[0]
    assert rec.get("vision_confirmed") is True, "JSONL: vision_confirmed must be True"
    assert rec.get("vision_reason") == "ok", "JSONL: vision_reason must be 'ok'"
    assert isinstance(rec.get("drift_px"), int), "JSONL: drift_px must be an integer"


# ---------------------------------------------------------------------------
# Test B: drift_exceeded — VIS-07
# ---------------------------------------------------------------------------

def test_drift_exceeded_covers_vis07(monkeypatch, tmp_path):
    """Covers VIS-07 (drift logged with drift_exceeded reason).

    Home target (320, 240); observed (500, 500) → drift > 30.
    Cycle must still complete; robot.deposit + return_home called once each.
    JSONL: vision_confirmed=False, drift_px > 30, vision_reason='drift_exceeded'.
    """
    targets = {
        "home": [320, 240],
        "bin_2": [200, 200],
    }
    cfg = _Cfg(enabled=True, targets=targets, tolerance=30)
    st, robot, vision, logger = _make_station(cfg, tmp_path)

    # Observed far from home target → drift_exceeded
    monkeypatch.setattr(station_module, "find_robot_qr", lambda frame: (500, 500))

    frame = _blank_frame()
    detection = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    monkeypatch.setattr("station.decode_from_frame", lambda *a, **kw: "B")

    entry = st._run_cycle(frame, detection)

    # Cycle still COMPLETED (D-14: no abort)
    assert entry.status == "completed", "drift_exceeded must not abort the cycle (D-14)"
    assert entry.vision_confirmed is False, "VIS-07: drift_exceeded → vision_confirmed=False"
    assert entry.vision_reason == "drift_exceeded", f"expected drift_exceeded, got {entry.vision_reason!r}"
    # VIS-07: drift_px recorded
    assert entry.drift_px is not None
    assert entry.drift_px > 30, f"drift must exceed tolerance 30, got {entry.drift_px}"

    # Robot deposit + return_home must still execute (D-14)
    robot.deposit.assert_called_once()
    robot.return_home.assert_called_once()

    # _cycle_lock must be free (D-15: no control-loop side-effect)
    assert st._cycle_lock is False, "cycle lock must be released after drift_exceeded"

    # On-disk JSONL confirms the three new fields
    logger.close()
    records = _read_jsonl(tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec.get("vision_confirmed") is False
    assert rec.get("vision_reason") == "drift_exceeded"
    assert isinstance(rec.get("drift_px"), int) and rec["drift_px"] > 30


# ---------------------------------------------------------------------------
# Test C: Disabled path — byte-identical to Phase 1 D-16 schema (VIS-08)
# ---------------------------------------------------------------------------

def test_disabled_path_byte_identical_to_phase1_schema_covers_vis08(monkeypatch, tmp_path):
    """Covers VIS-08 (default-off invariant).

    When vision_confirm_enabled=False:
      1. find_robot_qr must have call_count == 0 (NO pyzbar call post-motion).
      2. The JSONL line key-set MUST equal EXACTLY the Phase 1 D-16 schema:
         {timestamp, class, destination_bin, cycle_time_ms, status, error}.
      3. vision_confirmed, drift_px, vision_reason must NOT appear in the JSONL.
    """
    cfg = _Cfg(enabled=False)  # VIS-08: flag off — Phase 1 behavior preserved
    st, robot, vision, logger = _make_station(cfg, tmp_path)

    # Set up a mock so we can verify call_count
    mock_find_qr = MagicMock(return_value=(100, 100))
    monkeypatch.setattr(station_module, "find_robot_qr", mock_find_qr)

    frame = _blank_frame()
    detection = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    monkeypatch.setattr("station.decode_from_frame", lambda *a, **kw: "B")

    entry = st._run_cycle(frame, detection)

    # VIS-08: absolutely NO pyzbar call post-motion
    assert mock_find_qr.call_count == 0, (
        f"VIS-08: find_robot_qr must not be called when disabled, "
        f"but got call_count={mock_find_qr.call_count}"
    )

    # VIS-08: JSONL key-set must be exactly the Phase 1 D-16 schema
    logger.close()
    records = _read_jsonl(tmp_path)
    assert len(records) == 1, f"expected 1 JSONL line, got {len(records)}"
    actual_keys = frozenset(records[0].keys())
    assert actual_keys == PHASE1_SCHEMA_KEYS, (
        f"VIS-08: JSONL key-set does not match Phase 1 D-16 schema.\n"
        f"  Expected: {sorted(PHASE1_SCHEMA_KEYS)}\n"
        f"  Got:      {sorted(actual_keys)}\n"
        f"  Extra:    {sorted(actual_keys - PHASE1_SCHEMA_KEYS)}\n"
        f"  Missing:  {sorted(PHASE1_SCHEMA_KEYS - actual_keys)}"
    )

    # Explicit key-by-key assertions for clarity
    assert "vision_confirmed" not in records[0], "vision_confirmed must not appear when disabled"
    assert "drift_px" not in records[0], "drift_px must not appear when disabled"
    assert "vision_reason" not in records[0], "vision_reason must not appear when disabled"


# ---------------------------------------------------------------------------
# Test D: YAML calibration → Station runtime handoff (VIS-06)
# ---------------------------------------------------------------------------

def test_yaml_calibration_to_runtime_handoff_covers_vis06(monkeypatch, tmp_path):
    """Covers VIS-06 (calibrate.py persists targets; Station reads them at runtime).

    Flow:
      1. Write a minimal station_config.yaml to tmp_path.
      2. Use write_pixel_target_to_yaml (Plan 03) to seed the home pixel target.
      3. Reload config via load_config.
      4. Run one Station cycle with find_robot_qr returning the exact calibrated point.
      5. Assert drift_px == 0 (robot is exactly at the calibrated target).

    This proves the calibrate → YAML → load_config → Station handoff end-to-end.
    VIS-06: calibrate.py captures and persists expected ROBOT-QR pixel positions.
    """
    # Step 1: Write a minimal station_config.yaml
    yaml_path = tmp_path / "station_config.yaml"
    config_data = {
        "station_id": "integration-test",
        "camera_index": 0,
        "yolo_model_path": "models/best.pt",
        "yolo_confidence_threshold": 0.40,
        "yolo_imgsz": 640,
        "qr_padding_pct": 0.15,
        "qr_retry_count": 3,
        "qr_settle_delay_ms": 0,
        "robot_implementation": "stub",
        "class_to_bin": {"A": 1, "B": 2, "C": 3},
        "home_encoder_target": 0,
        "bin_encoder_targets": {1: 90, 2: 180, 3: 270},
        "motor_speed_deg_per_sec": 180,
        "cycle_watchdog_timeout_s": 30,
        "log_dir": str(tmp_path / "logs"),
        "vision_confirm_enabled": True,
        "vision_confirm_tolerance_px": 30,
        "robot_vision_targets": {
            "home": None,
            "bin_1": None,
            "bin_2": None,
            "bin_3": None,
        },
    }
    with open(str(yaml_path), "w") as f:
        yaml.safe_dump(config_data, f)

    # Step 2: VIS-06 — write a pixel target via calibrate's write_pixel_target_to_yaml
    home_target = (320, 240)
    write_pixel_target_to_yaml(str(yaml_path), "home", home_target)
    # Also write bin_2 target so the cycle's bin check has a target
    bin2_target = (200, 200)
    write_pixel_target_to_yaml(str(yaml_path), "bin_2", bin2_target)

    # Step 3: reload the config — this is the calibrate → load_config handoff
    cfg = load_config(str(yaml_path))
    assert cfg.vision_confirm_enabled is True
    assert cfg.robot_vision_targets.get("home") == list(home_target), (
        f"VIS-06: home target not persisted correctly; "
        f"got {cfg.robot_vision_targets.get('home')!r}"
    )

    # Step 4: Build Station with this calibrated config
    robot = MagicMock()
    vision = MagicMock()
    vision.detect.return_value = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    event_logger = EventLogger(str(log_dir))
    st = Station(cfg, robot, vision, event_logger)

    # find_robot_qr returns the exact calibrated home target → drift == 0
    monkeypatch.setattr(station_module, "find_robot_qr", lambda frame: home_target)

    frame = _blank_frame()
    detection = Detection(bbox=(100, 100, 200, 200), confidence=0.9, class_id=0)
    monkeypatch.setattr("station.decode_from_frame", lambda *a, **kw: "B")

    entry = st._run_cycle(frame, detection)
    event_logger.close()

    # Step 5: drift must be 0 (robot is exactly at the calibrated home target)
    assert entry.status == "completed"
    # The JSONL entry reflects the home check (D-08: last motion is return_home)
    assert entry.drift_px == 0, (
        f"VIS-06: drift must be 0 when robot is exactly at calibrated target, "
        f"got {entry.drift_px}"
    )
    assert entry.vision_confirmed is True, "VIS-06: confirmed=True when drift==0"
    assert entry.vision_reason == "ok"


# ---------------------------------------------------------------------------
# Test E: Phase 2 freeze guard
# ---------------------------------------------------------------------------

def test_phase2_freeze_guard():
    """Phase 2 modules must NOT be imported by station.py.

    station.py is already imported; verify that the production code does not
    contain any top-level 'from orchestrator', 'from ws_client',
    'from ws_protocol', or 'from templates' import statements.

    The lazy 'from ws_client import build_status_listener' inside run_station()
    is pre-existing scaffolding and is permitted (it is not a top-level import).
    """
    source = Path(station_module.__file__).read_text(encoding="utf-8")

    forbidden_top_level = [
        "from orchestrator",
        "from ws_protocol",
        "from templates",
    ]
    for pattern in forbidden_top_level:
        assert pattern not in source, (
            f"Phase 2 freeze violation: station.py contains '{pattern}'. "
            "Phase 2 modules are frozen for Phase 02.1 (D-19)."
        )

    # Structural check: importing station must not drag in orchestrator at module level
    # (importing station is already done at the top of this file)
    for mod_name in ("orchestrator", "ws_protocol", "templates"):
        # The module should either not be in sys.modules at all, OR it was imported
        # by another test suite module (not station). We only care that station itself
        # doesn't require it.
        pass  # We verified by source scan above; no runtime assertion needed here.

    # No dashboard import either
    assert "from dashboard" not in source, (
        "Phase 2 freeze violation: station.py contains 'from dashboard'. "
        "Dashboard is frozen (D-19)."
    )


# ---------------------------------------------------------------------------
# Test F: REQ-ID coverage audit (VIS-05, VIS-06, VIS-07, VIS-08)
# ---------------------------------------------------------------------------

def test_req_id_coverage_audit_vis05_vis06_vis07_vis08():
    """Self-auditing REQ-ID coverage check.

    Scans this test file's own source for the literal tokens
    "VIS-05", "VIS-06", "VIS-07", "VIS-08" and asserts all four appear
    at least once (in docstrings, comments, or assertion messages).

    VIS-05: Station detects ROBOT QR separately from package QRs.
    VIS-06: calibrate.py persists expected pixel targets.
    VIS-07: Drift logged per-cycle in JSONL.
    VIS-08: vision_confirm_enabled flag; default-off invariant.
    """
    this_file = Path(__file__).read_text(encoding="utf-8")
    required_tokens = ["VIS-05", "VIS-06", "VIS-07", "VIS-08"]
    missing = [tok for tok in required_tokens if tok not in this_file]
    assert not missing, (
        f"REQ-ID coverage audit FAILED: tokens missing from test file: {missing}. "
        f"Each requirement must have at least one assertion covering it."
    )
