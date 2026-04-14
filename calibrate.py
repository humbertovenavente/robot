"""Calibration CLI (ROB-05). Operator runs this before each session.

Flow:
 1. Connect to the robot (stub / ev3 / spike per config).
 2. Operator positions the robot at HOME; presses Enter; script records encoder reading.
 3. For each bin 1..N (N = len(config.class_to_bin)): operator positions at bin; Enter; record.
 4. Script prints the proposed values and a diff vs current config.
 5. Unless --dry-run, writes back to station_config.yaml (top-level keys preserved).
 6. Remind operator: restart station.py to pick up new values (D-14).

Usage:
    python calibrate.py                 # interactive, writes to station_config.yaml
    python calibrate.py --dry-run       # prints proposed values, does NOT write
    python calibrate.py --config station_config.local.yaml
    python calibrate.py --yes           # non-interactive stub test mode (for CI)

With StubRobot: the encoder "reading" is the simulated current_position. We offer a
--stub-preset mode that writes synthetic values for smoke tests without operator input.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, Optional

import yaml

from config import load_config, REPO_ROOT
from robot import build_robot, RobotInterface


def _prompt(msg: str) -> None:
    """Wait for the operator. Allow Ctrl+C to abort cleanly."""
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        raise SystemExit(130)


def run_calibration(
    config_path: Path,
    dry_run: bool = False,
    non_interactive: bool = False,
    stub_preset: Optional[Dict[int, int]] = None,
) -> dict:
    """Execute calibration and optionally write updated YAML.

    Returns the updated YAML dict (whether or not write occurred). Raises on any error.

    Args:
        config_path: Path to the YAML config file to read and (optionally) write.
        dry_run: If True, prints proposed values but does NOT write the file.
        non_interactive: If True, skips interactive prompts (for CI / --yes mode).
        stub_preset: Optional dict mapping position key (0=home, 1..N=bins) to encoder value.
            When provided, values are used directly instead of querying the robot.
    """
    cfg = load_config(config_path)
    robot: RobotInterface = build_robot(cfg)

    measured_bins: Dict[int, int] = {}
    measured_home: int = cfg.home_encoder_target

    try:
        # --- HOME ---
        if stub_preset is not None:
            measured_home = stub_preset.get(0, 0)
        else:
            if not non_interactive:
                _prompt("1. Move the robot to HOME position. Press Enter when ready... ")
            measured_home = robot.get_current_position()
        print(f"  HOME encoder = {measured_home}")

        # --- BINS ---
        # Derive the set of bin indices from the class_to_bin mapping.
        bin_indices = sorted(set(cfg.class_to_bin.values()))
        for bin_idx in bin_indices:
            if stub_preset is not None:
                measured_bins[bin_idx] = stub_preset.get(bin_idx, bin_idx * 120)
            else:
                if not non_interactive:
                    _prompt(f"2. Move the robot to BIN {bin_idx}. Press Enter when ready... ")
                measured_bins[bin_idx] = robot.get_current_position()
            print(f"  BIN {bin_idx} encoder = {measured_bins[bin_idx]}")
    finally:
        robot.shutdown()

    # Load the YAML as a plain dict so we preserve unrelated top-level keys.
    with open(config_path, "r") as f:
        doc = yaml.safe_load(f)

    print("\n--- Proposed changes ---")
    print(f"  home_encoder_target: {doc.get('home_encoder_target')!r} -> {measured_home!r}")
    for bin_idx, val in sorted(measured_bins.items()):
        current = (doc.get("bin_encoder_targets") or {}).get(bin_idx)
        print(f"  bin_encoder_targets[{bin_idx}]: {current!r} -> {val!r}")

    doc["home_encoder_target"] = int(measured_home)
    doc["bin_encoder_targets"] = {int(k): int(v) for k, v in sorted(measured_bins.items())}

    if dry_run:
        print("\n[dry-run] Not writing. Re-run without --dry-run to persist.")
        return doc

    with open(config_path, "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)

    print(f"\nWrote calibration to {config_path}")
    print("Reminder: restart station.py to pick up the new values (D-14).")
    return doc


def main() -> int:
    """Entry point for the calibration CLI."""
    parser = argparse.ArgumentParser(
        description="Calibrate robot home + bin encoder targets (ROB-05).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python calibrate.py                              # interactive, reads and writes station_config.yaml
  python calibrate.py --dry-run                    # print proposed values without writing
  python calibrate.py --yes                        # non-interactive (uses current robot position)
  python calibrate.py --stub-preset 0=0 1=90 2=180 3=270  # CI smoke-test with synthetic values
  python calibrate.py --config station_config.local.yaml  # target a local override file
        """,
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "station_config.yaml"),
        help="Config file to read and update (default: station_config.yaml in repo root)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print proposed values but do not write to the config file",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Non-interactive: use current robot position at each step without prompting (CI)",
    )
    parser.add_argument(
        "--stub-preset",
        nargs="+",
        default=None,
        metavar="KEY=VALUE",
        help="For CI: pass like `--stub-preset 0=0 1=90 2=180 3=270` to synthesize values "
             "without operator input. 0 = home, 1..N = bins.",
    )
    args = parser.parse_args()

    preset: Optional[Dict[int, int]] = None
    if args.stub_preset is not None:
        preset = {}
        for pair in args.stub_preset:
            try:
                k, v = pair.split("=", 1)
                preset[int(k)] = int(v)
            except ValueError:
                print(
                    f"ERROR: --stub-preset values must be KEY=VALUE integers, got: {pair!r}",
                    file=sys.stderr,
                )
                return 2

    try:
        run_calibration(
            config_path=Path(args.config),
            dry_run=args.dry_run,
            non_interactive=args.yes or (preset is not None),
            stub_preset=preset,
        )
    except SystemExit:
        raise
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
