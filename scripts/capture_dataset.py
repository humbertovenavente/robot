"""Dataset capture CLI.

Usage:
    python scripts/capture_dataset.py --class A --count 50
    python scripts/capture_dataset.py --class B --count 50
    python scripts/capture_dataset.py --class C --count 50

Writes JPEGs to data/raw/<CLASS>/NNNN.jpg. Split + label is a later step (Roboflow or manual).

Per D-02 start with count=50/class. Per D-03 run this on the demo venue laptop+webcam+lighting.
Per PITFALLS.md #3: camera mounted perpendicular to package plane; capture AFTER settle.
Per PITFALLS.md #12: capture across diverse lighting conditions (overhead, lamp, mixed).
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

# Ensure repo root is importable when running as `python scripts/capture_dataset.py` from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import cv2

from config import load_config, REPO_ROOT


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture labeled webcam images for YOLO dataset.")
    parser.add_argument("--class", dest="cls", required=True, choices=["A", "B", "C"],
                        help="Package class letter (D-04: single letter A/B/C)")
    parser.add_argument("--count", type=int, default=50,
                        help="Number of images to capture (D-02: 50 per class minimum)")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Seconds between captures (PITFALLS #3: >=0.5 so package settles)")
    parser.add_argument("--camera-index", type=int, default=None,
                        help="Override camera index; default is from station_config.yaml")
    args = parser.parse_args()

    cfg = load_config()
    cam_index = args.camera_index if args.camera_index is not None else cfg.camera_index

    out_dir = REPO_ROOT / "data" / "raw" / args.cls
    out_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(cam_index)
    if not cap.isOpened():
        print(f"ERROR: could not open camera index {cam_index}. "
              "On macOS check System Settings > Privacy & Security > Camera (STACK.md macOS pitfall #2).",
              file=sys.stderr)
        return 2

    print(f"Capturing {args.count} frames for class {args.cls} into {out_dir}")
    print(f"Press SPACE to capture, q to quit. Delay between auto-captures: {args.delay}s")
    print(f"Per PITFALLS.md #12: vary lighting (overhead, lamp, mixed) during capture.")

    existing = sorted(out_dir.glob("*.jpg"))
    next_idx = (int(existing[-1].stem) + 1) if existing else 0

    captured = 0
    try:
        while captured < args.count:
            ok, frame = cap.read()
            if not ok:
                print("WARN: failed frame grab, retrying", file=sys.stderr)
                time.sleep(0.1)
                continue
            cv2.imshow(f"capture class={args.cls} [{captured}/{args.count}]", frame)
            key = cv2.waitKey(int(args.delay * 1000)) & 0xFF
            if key == ord('q'):
                break
            # Auto-capture on timeout OR manual capture on space
            path = out_dir / f"{next_idx:04d}.jpg"
            cv2.imwrite(str(path), frame)
            captured += 1
            next_idx += 1
            print(f"  saved {path.name} ({captured}/{args.count})")
    finally:
        cap.release()
        cv2.destroyAllWindows()

    print(f"Done. {captured} images saved to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
