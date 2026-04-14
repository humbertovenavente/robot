"""Tests for calibration CLI — ensure yaml round-trip preserves unrelated fields
and --stub-preset path works without a physical robot."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from calibrate import run_calibration


@pytest.fixture
def tmp_config(tmp_path) -> Path:
    """Copy the real station_config.yaml into tmp_path for mutation."""
    src = Path(__file__).resolve().parent.parent / "station_config.yaml"
    dst = tmp_path / "station_config.yaml"
    shutil.copy(src, dst)
    return dst


def test_calibration_updates_home_and_bins(tmp_config):
    """--stub-preset path must write the provided values into yaml."""
    preset = {0: 5, 1: 100, 2: 210, 3: 320}
    run_calibration(tmp_config, dry_run=False, non_interactive=True, stub_preset=preset)
    doc = yaml.safe_load(tmp_config.read_text())
    assert doc["home_encoder_target"] == 5
    assert doc["bin_encoder_targets"] == {1: 100, 2: 210, 3: 320}


def test_calibration_preserves_unrelated_keys(tmp_config):
    """class_to_bin, camera_index, yolo_model_path etc. must NOT be dropped."""
    before = yaml.safe_load(tmp_config.read_text())
    preset = {0: 0, 1: 1, 2: 2, 3: 3}
    run_calibration(tmp_config, dry_run=False, non_interactive=True, stub_preset=preset)
    after = yaml.safe_load(tmp_config.read_text())
    # Every non-calibration key must be preserved exactly
    for key in before:
        if key in ("home_encoder_target", "bin_encoder_targets"):
            continue
        assert after.get(key) == before[key], f"{key} was modified"


def test_dry_run_does_not_write(tmp_config):
    """--dry-run must not mutate the file."""
    before_text = tmp_config.read_text()
    preset = {0: 999, 1: 999, 2: 999, 3: 999}
    run_calibration(tmp_config, dry_run=True, non_interactive=True, stub_preset=preset)
    after_text = tmp_config.read_text()
    assert before_text == after_text, "dry-run must not mutate the file"


def test_dry_run_returns_proposed_dict(tmp_config):
    """run_calibration with dry_run=True returns the proposed config dict."""
    preset = {0: 10, 1: 90, 2: 180, 3: 270}
    result = run_calibration(tmp_config, dry_run=True, non_interactive=True, stub_preset=preset)
    assert result["home_encoder_target"] == 10
    assert result["bin_encoder_targets"][1] == 90
    assert result["bin_encoder_targets"][2] == 180
    assert result["bin_encoder_targets"][3] == 270


def test_stub_preset_missing_bin_uses_fallback(tmp_config):
    """If a bin is missing from stub_preset, fallback is bin_idx * 120."""
    # preset only provides home (0) — bins will use bin_idx * 120 fallback
    preset = {0: 0}
    result = run_calibration(tmp_config, dry_run=True, non_interactive=True, stub_preset=preset)
    assert result["bin_encoder_targets"][1] == 1 * 120
    assert result["bin_encoder_targets"][2] == 2 * 120
    assert result["bin_encoder_targets"][3] == 3 * 120


def test_non_interactive_stub_reads_robot_position(tmp_config):
    """--yes (non_interactive=True, no preset) uses StubRobot.get_current_position()
    which is home_encoder_target at init. We just verify no prompt is raised."""
    # With robot_implementation=stub and non_interactive=True, this must complete
    # without blocking on input().
    result = run_calibration(tmp_config, dry_run=True, non_interactive=True, stub_preset=None)
    # StubRobot starts at home_encoder_target (0). All readings are 0.
    assert result["home_encoder_target"] == 0
    # All bins will also read 0 because StubRobot hasn't moved
    for val in result["bin_encoder_targets"].values():
        assert val == 0
