"""
config.py — Station configuration loader.

Usage:
    from config import load_config, Config

    cfg = load_config()              # loads station_config.local.yaml if present, else station_config.yaml
    cfg = load_config("path/to.yaml")  # explicit path override

Config is read-only once loaded. Reload by calling load_config() again.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator

# Repo root is the directory containing this file.
# Use pathlib — no absolute hardcoded paths anywhere (PITFALLS.md #11).
REPO_ROOT = Path(__file__).resolve().parent


class Config(BaseModel):
    """Typed station configuration loaded from station_config.yaml."""

    station_id: str
    camera_index: int
    yolo_model_path: str
    yolo_confidence_threshold: float = Field(ge=0.0, le=1.0)
    yolo_imgsz: int = 640
    qr_padding_pct: float = Field(ge=0.0, le=0.5, default=0.15)
    qr_retry_count: int = Field(ge=1, default=3)
    qr_settle_delay_ms: int = 500
    robot_implementation: Literal["stub", "ev3", "spike"] = "stub"
    orchestrator_url: Optional[str] = None  # D-05: ws://host:port/ws; None=standalone (D-06)
    class_to_bin: Dict[str, int]
    home_encoder_target: int = 0
    bin_encoder_targets: Dict[int, int]
    motor_speed_deg_per_sec: int = 180
    cycle_watchdog_timeout_s: int = 30
    log_dir: str = "logs"

    # Phase 02.1 — vision-confirm fields (D-11, D-13, D-16)
    vision_confirm_enabled: bool = False  # D-16: default off; Phase 1 behavior preserved when False
    vision_confirm_tolerance_px: int = 30  # D-13: Euclidean pixel drift threshold
    robot_vision_targets: Dict[str, Optional[List[int]]] = Field(
        default_factory=lambda: {"home": None, "bin_1": None, "bin_2": None, "bin_3": None}
    )  # D-11: expected ROBOT-QR pixel centers per position; null = not calibrated

    @field_validator("robot_vision_targets", mode="before")
    @classmethod
    def _validate_vision_targets(
        cls, v: Optional[Dict]
    ) -> Dict[str, Optional[List[int]]]:
        """Ensure each non-null target is a 2-element list of ints."""
        if v is None:
            return {"home": None, "bin_1": None, "bin_2": None, "bin_3": None}
        validated: Dict[str, Optional[List[int]]] = {}
        for key, val in v.items():
            if val is None:
                validated[key] = None
            else:
                coords = list(val)
                if len(coords) != 2:
                    raise ValueError(
                        f"robot_vision_targets[{key!r}] must be a 2-element [cx, cy] list, got {coords!r}"
                    )
                validated[key] = [int(coords[0]), int(coords[1])]
        return validated

    @property
    def orchestrator_enabled(self) -> bool:
        """True iff orchestrator_url is non-empty. D-06: empty/None = standalone mode."""
        return bool(self.orchestrator_url and self.orchestrator_url.strip())

    @property
    def resolved_model_path(self) -> Path:
        """Absolute path to the YOLO model weights file."""
        p = Path(self.yolo_model_path)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def resolved_log_dir(self) -> Path:
        """Absolute path to the log directory."""
        p = Path(self.log_dir)
        return p if p.is_absolute() else REPO_ROOT / p


def load_config(path: Path | str | None = None) -> Config:
    """Load station config from YAML.

    Precedence:
        1. Explicit *path* argument (if given)
        2. station_config.local.yaml (venue-specific override, gitignored)
        3. station_config.yaml (checked-in defaults)

    Config is read once at startup (D-14 — no hot-reload).
    """
    if path is not None:
        cfg_path = Path(path)
    else:
        local = REPO_ROOT / "station_config.local.yaml"
        cfg_path = local if local.exists() else REPO_ROOT / "station_config.yaml"

    with open(cfg_path, "r") as f:
        data = yaml.safe_load(f)

    # yaml.safe_load parses integer-keyed mappings as int already on some loaders,
    # but may parse them as strings — normalise explicitly to be safe.
    if "bin_encoder_targets" in data:
        data["bin_encoder_targets"] = {
            int(k): int(v) for k, v in data["bin_encoder_targets"].items()
        }

    return Config(**data)
