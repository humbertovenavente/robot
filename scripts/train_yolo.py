"""Train YOLOv8n on the 3-class package dataset.

Usage:
    python scripts/train_yolo.py                         # defaults: yolov8n.pt, 50 epochs, imgsz=640
    python scripts/train_yolo.py --epochs 100 --device cpu
    python scripts/train_yolo.py --device mps            # Apple Silicon
    python scripts/train_yolo.py --device 0              # CUDA GPU 0

Writes weights to runs/detect/train/weights/best.pt.
After training finishes, automatically runs val and enforces mAP50 >= 0.85 (PITFALLS #1).
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

# Ensure repo root is importable when running as `python scripts/train_yolo.py` from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import REPO_ROOT


def main() -> int:
    parser = argparse.ArgumentParser(description="Train YOLOv8n on the package-sorter dataset.")
    parser.add_argument("--data", default=str(REPO_ROOT / "data" / "dataset" / "packages.yaml"),
                        help="Path to dataset descriptor yaml")
    parser.add_argument("--model", default="yolov8n.pt",
                        help="Base model (yolov8n is the only viable choice on CPU, STACK.md)")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--device", default="cpu", help="cpu | mps | 0 (CUDA)")
    parser.add_argument("--project", default=str(REPO_ROOT / "runs" / "detect"),
                        help="Output root")
    parser.add_argument("--name", default="train", help="Run name subdirectory")
    # PITFALLS #14 caps. Defaults below are within safe bounds.
    parser.add_argument("--degrees", type=float, default=15.0, help="Rotation aug cap, PITFALLS #14 <= 15")
    parser.add_argument("--hsv_v", type=float, default=0.3, help="Brightness aug, PITFALLS #14 <= 0.3")
    parser.add_argument("--fliplr", type=float, default=0.0, help="Horizontal flip prob (0 for QR, PITFALLS #14)")
    parser.add_argument("--erasing", type=float, default=0.0, help="Random erasing prob (0 for QR)")
    args = parser.parse_args()

    # Lazy import so --help works without heavy torch load.
    from ultralytics import YOLO

    model = YOLO(args.model)
    results = model.train(
        data=args.data,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        project=args.project,
        name=args.name,
        exist_ok=False,
        degrees=args.degrees,
        hsv_v=args.hsv_v,
        fliplr=args.fliplr,
        erasing=args.erasing,
    )
    print(f"Training complete. Best weights: {model.trainer.best}")

    # Immediately run val and enforce the mAP50 gate.
    print("Running validation to check mAP50 >= 0.85 gate (PITFALLS #1)...")
    val_metrics = model.val(data=args.data, imgsz=args.imgsz, device=args.device)
    map50 = float(val_metrics.box.map50)
    print(f"val mAP50 = {map50:.4f}")
    if map50 < 0.85:
        print(f"FAIL: val mAP50 {map50:.4f} below 0.85 gate. Capture more images and retrain.",
              file=sys.stderr)
        return 1
    print("PASS: gate satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
