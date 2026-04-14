"""Standalone validation script — runs mAP50 gate against an existing weights file.

Usage:
    python scripts/val_yolo.py --weights runs/detect/train/weights/best.pt

Exits 0 if val mAP50 >= 0.85, exits 1 otherwise. Used as the VIS-01 integration gate.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

# Ensure repo root is importable when running as `python scripts/val_yolo.py` from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import REPO_ROOT


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate YOLO weights against the 0.85 mAP50 gate.")
    parser.add_argument("--weights", required=True, help="Path to best.pt (or last.pt)")
    parser.add_argument("--data", default=str(REPO_ROOT / "data" / "dataset" / "packages.yaml"))
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threshold", type=float, default=0.85,
                        help="mAP50 threshold (PITFALLS #1 says 0.85)")
    args = parser.parse_args()

    from ultralytics import YOLO
    model = YOLO(args.weights)
    metrics = model.val(data=args.data, imgsz=args.imgsz, device=args.device, split="val")
    map50 = float(metrics.box.map50)
    print(f"val mAP50 = {map50:.4f} (threshold = {args.threshold:.2f})")
    if map50 < args.threshold:
        print(f"FAIL: val mAP50 {map50:.4f} below threshold {args.threshold}", file=sys.stderr)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
