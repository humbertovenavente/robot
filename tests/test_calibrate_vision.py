"""Tests for the pixel-capture sub-flow added to calibrate.py (VIS-06).

Covers:
  - capture_pixel_target: success, retry, skip, immediate hit
  - write_pixel_target_to_yaml: round-trip preserves all existing keys
  - Phase 1 + Phase 2 key regression (class_to_bin, orchestrator_url)
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

REALISTIC_YAML = textwrap.dedent("""\
    station_id: station-1
    camera_index: 0
    yolo_model_path: models/best.pt
    yolo_confidence_threshold: 0.40
    yolo_imgsz: 640
    qr_padding_pct: 0.15
    qr_retry_count: 3
    qr_settle_delay_ms: 500
    robot_implementation: stub
    class_to_bin:
      A: 1
      B: 2
      C: 3
    home_encoder_target: 0
    bin_encoder_targets:
      1: 0
      2: 120
      3: 240
    motor_speed_deg_per_sec: 180
    cycle_watchdog_timeout_s: 30
    log_dir: logs
    orchestrator_url: ws://127.0.0.1:8000/ws
    vision_confirm_enabled: false
    vision_confirm_tolerance_px: 30
    robot_vision_targets:
      home: null
      bin_1: null
      bin_2: null
      bin_3: null
""")


def _make_yaml(tmp_path: Path, content: str = REALISTIC_YAML) -> Path:
    p = tmp_path / "station_config.yaml"
    p.write_text(content)
    return p


def _make_cap(frames):
    """Build a mock cv2.VideoCapture that returns successive frames.

    Each element in *frames* is yielded as the frame argument to find_robot_qr
    via cap.read() → (True, frame).  If frames is exhausted, returns (False, None).
    """
    cap = MagicMock()
    side_effects = [(True, f) for f in frames] + [(False, None)] * 10
    cap.read.side_effect = side_effects
    return cap


# --------------------------------------------------------------------------- #
# capture_pixel_target                                                         #
# --------------------------------------------------------------------------- #

class TestCapturePixelTarget:
    """Unit tests for capture_pixel_target(cap, target_name, max_retries)."""

    def test_success_on_first_frame(self):
        """Test 1: find_robot_qr returns (320, 240) immediately → return (320, 240)."""
        from calibrate import capture_pixel_target

        cap = _make_cap(["frame1"])
        with patch("calibrate.find_robot_qr", return_value=(320, 240)) as mock_qr:
            result = capture_pixel_target(cap, "home")

        assert result == (320, 240)
        mock_qr.assert_called_once_with("frame1")
        assert cap.read.call_count == 1

    def test_retry_three_times_then_skip(self):
        """Test 2: find_robot_qr returns None 3×; operator enters retry, retry, skip → None."""
        from calibrate import capture_pixel_target

        cap = _make_cap(["f1", "f2", "f3"])
        with patch("calibrate.find_robot_qr", return_value=None), \
             patch("builtins.input", side_effect=["r", "r", "s"]):
            result = capture_pixel_target(cap, "bin_1", max_retries=3)

        assert result is None
        assert cap.read.call_count == 3

    def test_skip_immediately(self):
        """Test 3: find_robot_qr returns None once; operator enters skip → None, 1 grab."""
        from calibrate import capture_pixel_target

        cap = _make_cap(["frame1"])
        with patch("calibrate.find_robot_qr", return_value=None), \
             patch("builtins.input", return_value="s"):
            result = capture_pixel_target(cap, "bin_2")

        assert result is None
        assert cap.read.call_count == 1

    def test_max_retries_not_reached(self):
        """Test 4: find_robot_qr returns (100, 100) on first call; no retries needed."""
        from calibrate import capture_pixel_target

        cap = _make_cap(["frame1", "frame2"])
        with patch("calibrate.find_robot_qr", return_value=(100, 100)) as mock_qr:
            result = capture_pixel_target(cap, "bin_3", max_retries=3)

        assert result == (100, 100)
        # Only one frame grab needed
        assert cap.read.call_count == 1
        mock_qr.assert_called_once()


# --------------------------------------------------------------------------- #
# write_pixel_target_to_yaml                                                   #
# --------------------------------------------------------------------------- #

class TestWritePixelTargetToYaml:
    """Unit tests for write_pixel_target_to_yaml(yaml_path, target_name, center)."""

    def test_write_center_updates_home(self, tmp_path):
        """Test 5: Write (320, 240) to 'home'; yaml now has robot_vision_targets.home == [320, 240]."""
        from calibrate import write_pixel_target_to_yaml

        yaml_path = _make_yaml(tmp_path)
        write_pixel_target_to_yaml(str(yaml_path), "home", (320, 240))
        doc = yaml.safe_load(yaml_path.read_text())

        assert doc["robot_vision_targets"]["home"] == [320, 240]
        # Sibling targets remain null
        assert doc["robot_vision_targets"]["bin_1"] is None
        assert doc["robot_vision_targets"]["bin_2"] is None
        assert doc["robot_vision_targets"]["bin_3"] is None

    def test_write_none_preserves_null(self, tmp_path):
        """Test 6: Calling with (name, None) writes null and preserves siblings."""
        from calibrate import write_pixel_target_to_yaml

        # Pre-populate bin_1 with a value; writing None to bin_2 should not touch bin_1.
        content = REALISTIC_YAML.replace("bin_1: null", "bin_1: [10, 20]")
        yaml_path = _make_yaml(tmp_path, content)
        write_pixel_target_to_yaml(str(yaml_path), "bin_2", None)
        doc = yaml.safe_load(yaml_path.read_text())

        assert doc["robot_vision_targets"]["bin_2"] is None
        assert doc["robot_vision_targets"]["bin_1"] == [10, 20]

    def test_round_trip_preserves_orchestrator_url(self, tmp_path):
        """Test 7: orchestrator_url must survive the yaml write byte-identically."""
        from calibrate import write_pixel_target_to_yaml

        yaml_path = _make_yaml(tmp_path)
        before = yaml.safe_load(yaml_path.read_text())
        write_pixel_target_to_yaml(str(yaml_path), "bin_3", (50, 60))
        after = yaml.safe_load(yaml_path.read_text())

        assert after["orchestrator_url"] == before["orchestrator_url"]

    def test_round_trip_preserves_class_to_bin(self, tmp_path):
        """Test 8: class_to_bin (Phase 1 ROB-05 key) must be byte-identical after write."""
        from calibrate import write_pixel_target_to_yaml

        yaml_path = _make_yaml(tmp_path)
        before = yaml.safe_load(yaml_path.read_text())
        write_pixel_target_to_yaml(str(yaml_path), "home", (1, 2))
        after = yaml.safe_load(yaml_path.read_text())

        assert after["class_to_bin"] == before["class_to_bin"]
        # Spot-check the individual mappings (regression for ROB-05)
        assert after["class_to_bin"]["A"] == 1
        assert after["class_to_bin"]["B"] == 2
        assert after["class_to_bin"]["C"] == 3
