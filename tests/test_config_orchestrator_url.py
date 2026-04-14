"""Tests for Config.orchestrator_url field and orchestrator_enabled property.

Tests verify:
- Backward compatibility: existing station_config.yaml loads without orchestrator_url
- New field parses correctly when provided
- orchestrator_enabled property correctly reflects whether URL is set
"""
from __future__ import annotations
from pathlib import Path
import textwrap

import pytest

from config import load_config


# ---------------------------------------------------------------------------
# Minimal valid YAML template (mirrors station_config.yaml structure)
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
# Tests
# ---------------------------------------------------------------------------

def test_existing_yaml_loads_without_orchestrator_url():
    """Loading the repo's actual station_config.yaml (no orchestrator_url key) succeeds."""
    cfg = load_config()
    assert cfg.orchestrator_url is None
    assert cfg.orchestrator_enabled is False


def test_orchestrator_url_explicit_value(tmp_path):
    """orchestrator_url set to a ws:// URL → field populated, orchestrator_enabled True."""
    yaml_path = _write_yaml(tmp_path, "orchestrator_url: ws://foo:8000/ws\n")
    cfg = load_config(yaml_path)
    assert cfg.orchestrator_url == "ws://foo:8000/ws"
    assert cfg.orchestrator_enabled is True


def test_orchestrator_url_empty_string_is_disabled(tmp_path):
    """orchestrator_url: \"\" → orchestrator_enabled is False."""
    yaml_path = _write_yaml(tmp_path, 'orchestrator_url: ""\n')
    cfg = load_config(yaml_path)
    assert cfg.orchestrator_enabled is False


def test_orchestrator_url_null_is_disabled(tmp_path):
    """orchestrator_url: (YAML null) → orchestrator_url is None, orchestrator_enabled False."""
    yaml_path = _write_yaml(tmp_path, "orchestrator_url:\n")
    cfg = load_config(yaml_path)
    assert cfg.orchestrator_url is None
    assert cfg.orchestrator_enabled is False


def test_orchestrator_url_whitespace_only_is_disabled(tmp_path):
    """orchestrator_url with only spaces → orchestrator_enabled is False."""
    yaml_path = _write_yaml(tmp_path, 'orchestrator_url: "   "\n')
    cfg = load_config(yaml_path)
    assert cfg.orchestrator_enabled is False
