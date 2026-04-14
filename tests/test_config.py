"""Tests for Phase 02.1 vision-confirm config fields.

Tests verify:
- Backward compatibility: loading yaml without new keys returns documented defaults
- New fields parse correctly when provided
- robot_vision_targets validates 2-element [cx, cy] lists

Regression: all existing Phase 1/2 config tests still pass (no removed/renamed fields).
"""
from __future__ import annotations
from pathlib import Path
import textwrap

import pytest

from config import load_config


# ---------------------------------------------------------------------------
# Minimal valid YAML template (no new vision keys → tests defaults)
# ---------------------------------------------------------------------------

MINIMAL_YAML = textwrap.dedent("""\
    station_id: station-test
    camera_index: 0
    yolo_model_path: models/best.pt
    yolo_confidence_threshold: 0.40
    class_to_bin:
      A: 1
      B: 2
      C: 3
    bin_encoder_targets:
      1: 0
      2: 120
      3: 240
""")


def _write_yaml(tmp_path: Path, extra: str = "") -> Path:
    """Write a minimal valid YAML config to tmp_path and return its path."""
    p = tmp_path / "station_config.yaml"
    p.write_text(MINIMAL_YAML + extra)
    return p


# ---------------------------------------------------------------------------
# Test 1: Defaults when new keys are absent from yaml
# ---------------------------------------------------------------------------

def test_vision_confirm_defaults_when_keys_absent(tmp_path):
    """Yaml without any vision keys → documented defaults (Phase 1 behavior preserved)."""
    yaml_path = _write_yaml(tmp_path)
    cfg = load_config(yaml_path)
    assert cfg.vision_confirm_enabled is False
    assert cfg.vision_confirm_tolerance_px == 30
    assert cfg.robot_vision_targets == {
        "home": None,
        "bin_1": None,
        "bin_2": None,
        "bin_3": None,
    }


# ---------------------------------------------------------------------------
# Test 2: vision_confirm_enabled and tolerance parse from yaml
# ---------------------------------------------------------------------------

def test_vision_confirm_fields_parse_from_yaml(tmp_path):
    """`vision_confirm_enabled: true` and custom tolerance parse correctly."""
    extra = textwrap.dedent("""\
        vision_confirm_enabled: true
        vision_confirm_tolerance_px: 45
    """)
    yaml_path = _write_yaml(tmp_path, extra)
    cfg = load_config(yaml_path)
    assert cfg.vision_confirm_enabled is True
    assert cfg.vision_confirm_tolerance_px == 45


# ---------------------------------------------------------------------------
# Test 3: robot_vision_targets with mixed null / coordinate values
# ---------------------------------------------------------------------------

def test_robot_vision_targets_mixed_values(tmp_path):
    """`robot_vision_targets` with home + bin_1 coords and null bin_2/bin_3."""
    extra = textwrap.dedent("""\
        robot_vision_targets:
          home: [320, 240]
          bin_1: [100, 200]
          bin_2: null
          bin_3: null
    """)
    yaml_path = _write_yaml(tmp_path, extra)
    cfg = load_config(yaml_path)
    # home and bin_1 come back as lists of ints (yaml parses lists natively)
    assert list(cfg.robot_vision_targets["home"]) == [320, 240]
    assert list(cfg.robot_vision_targets["bin_1"]) == [100, 200]
    assert cfg.robot_vision_targets["bin_2"] is None
    assert cfg.robot_vision_targets["bin_3"] is None


# ---------------------------------------------------------------------------
# Test 4: Regression — actual station_config.yaml still loads without error
# ---------------------------------------------------------------------------

def test_actual_station_config_yaml_loads():
    """Loading the repo's station_config.yaml succeeds and new fields have defaults."""
    cfg = load_config()
    # Phase 1 / Phase 2 fields still present
    assert cfg.station_id == "station-1"
    assert cfg.camera_index == 0
    assert cfg.yolo_confidence_threshold == 0.40
    assert cfg.robot_implementation == "stub"
    assert cfg.orchestrator_url is None
    # New Phase 02.1 fields at defaults
    assert cfg.vision_confirm_enabled is False
    assert cfg.vision_confirm_tolerance_px == 30
    assert cfg.robot_vision_targets == {
        "home": None,
        "bin_1": None,
        "bin_2": None,
        "bin_3": None,
    }
