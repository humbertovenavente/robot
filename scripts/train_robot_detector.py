"""Train a custom YOLOv8n obstacle-detector for the LEGO robot.

Steps:
  1. Auto-label each photo using COCO YOLOv8n — takes the largest detected bbox
     as the robot's bounding box (fallback: 90% centre crop of the full frame).
  2. Split 80/20 train/val.
  3. Train YOLOv8n (fine-tuned from COCO weights) for 60 epochs.
  4. Copy best.pt to models/robot_detector.pt

Usage:
    python3 scripts/train_robot_detector.py
"""
from __future__ import annotations

import os
import random
import shutil
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT    = Path(__file__).resolve().parent.parent
PHOTO_DIRS   = [REPO_ROOT / "Robot ", REPO_ROOT / "Chido "]   # both robots
DATASET_DIR  = REPO_ROOT / "datasets" / "robot_detector"
MODEL_OUT    = REPO_ROOT / "models" / "robot_detector.pt"

TRAIN_IMG = DATASET_DIR / "images" / "train"
VAL_IMG   = DATASET_DIR / "images" / "val"
TRAIN_LBL = DATASET_DIR / "labels" / "train"
VAL_LBL   = DATASET_DIR / "labels" / "val"

TRAIN_RATIO = 0.80
EPOCHS      = 60
IMGSZ       = 640
SEED        = 42

# ── Helpers ───────────────────────────────────────────────────────────────────

def bbox_area(box) -> float:
    """Return pixel area of a YOLO box (xyxy)."""
    x1, y1, x2, y2 = box
    return max(0.0, float(x2 - x1)) * max(0.0, float(y2 - y1))


def largest_box(r):
    """Return xyxy of the largest detected box, or None."""
    if r.boxes is None or len(r.boxes) == 0:
        return None
    boxes = r.boxes.xyxy.cpu().numpy()
    areas = [bbox_area(b) for b in boxes]
    return boxes[int(np.argmax(areas))]


def xyxy_to_yolo(x1, y1, x2, y2, w, h) -> tuple[float, float, float, float]:
    """Convert pixel xyxy to YOLO normalised cx cy bw bh."""
    cx = (x1 + x2) / 2 / w
    cy = (y1 + y2) / 2 / h
    bw = (x2 - x1) / w
    bh = (y2 - y1) / h
    return (cx, cy, bw, bh)


def fallback_box(h, w, pct=0.90):
    """Centre-crop fallback when COCO finds nothing."""
    margin_x = w * (1 - pct) / 2
    margin_y = h * (1 - pct) / 2
    return (margin_x, margin_y, w - margin_x, h - margin_y)


# ── 1. Collect images (convert HEIC→JPG via macOS sips) ──────────────────────
import subprocess

CONVERTED_DIR = REPO_ROOT / "datasets" / "robot_detector_raw"
CONVERTED_DIR.mkdir(parents=True, exist_ok=True)

exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
photos: list[Path] = []

for src_dir in PHOTO_DIRS:
    if not src_dir.exists():
        print(f"  SKIP (not found): {src_dir}")
        continue
    for p in sorted(src_dir.iterdir()):
        if p.suffix.lower() in exts:
            photos.append(p)
        elif p.suffix.lower() in {".heic", ".heif"}:
            dst = CONVERTED_DIR / (p.stem + ".jpg")
            if not dst.exists():
                subprocess.run(
                    ["sips", "-s", "format", "jpeg", str(p), "--out", str(dst)],
                    check=True, capture_output=True,
                )
            photos.append(dst)

if not photos:
    raise FileNotFoundError(f"No images found in {PHOTO_DIRS}")
print(f"Found {len(photos)} photos total ({', '.join(str(d) for d in PHOTO_DIRS)})")

# ── 2. Auto-label ─────────────────────────────────────────────────────────────
print("Loading COCO YOLOv8n for auto-labelling…")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
coco_model = YOLO(str(REPO_ROOT / "yolov8n.pt"))

labels: list[tuple[Path, str]] = []   # (image_path, yolo_label_line)
fallback_count = 0

for img_path in photos:
    img = cv2.imread(str(img_path))
    if img is None:
        print(f"  SKIP (unreadable): {img_path.name}")
        continue
    h, w = img.shape[:2]

    results = coco_model(img, conf=0.20, verbose=False)
    box = largest_box(results[0]) if results else None

    if box is not None:
        x1, y1, x2, y2 = box
    else:
        x1, y1, x2, y2 = fallback_box(h, w)
        fallback_count += 1

    cx, cy, bw, bh = xyxy_to_yolo(x1, y1, x2, y2, w, h)
    # Clamp to [0,1]
    cx, cy, bw, bh = (min(max(v, 0.0), 1.0) for v in (cx, cy, bw, bh))
    labels.append((img_path, f"0 {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"))

print(f"  Auto-labelled {len(labels)} images  ({fallback_count} used fallback centre-crop)")

# ── 3. Train / val split ──────────────────────────────────────────────────────
random.seed(SEED)
random.shuffle(labels)
split = int(len(labels) * TRAIN_RATIO)
train_set, val_set = labels[:split], labels[split:]
print(f"Split: {len(train_set)} train / {len(val_set)} val")

# ── 4. Write dataset to disk ──────────────────────────────────────────────────
for d in (TRAIN_IMG, VAL_IMG, TRAIN_LBL, VAL_LBL):
    d.mkdir(parents=True, exist_ok=True)

def write_split(items, img_dir: Path, lbl_dir: Path):
    for img_path, label_line in items:
        dst_img = img_dir / img_path.name
        dst_lbl = lbl_dir / (img_path.stem + ".txt")
        shutil.copy2(img_path, dst_img)
        dst_lbl.write_text(label_line + "\n")

write_split(train_set, TRAIN_IMG, TRAIN_LBL)
write_split(val_set,   VAL_IMG,   VAL_LBL)

dataset_yaml = DATASET_DIR / "dataset.yaml"
dataset_yaml.write_text(
    f"path: {DATASET_DIR}\n"
    f"train: images/train\n"
    f"val:   images/val\n"
    f"nc: 1\n"
    f"names: ['robot']\n"
)
print(f"Dataset written to {DATASET_DIR}")

# ── 5. Train ──────────────────────────────────────────────────────────────────
print(f"\nTraining YOLOv8n for {EPOCHS} epochs…")
model = YOLO(str(REPO_ROOT / "yolov8n.pt"))
results = model.train(
    data=str(dataset_yaml),
    epochs=EPOCHS,
    imgsz=IMGSZ,
    batch=8,
    patience=15,
    name="robot_detector",
    project=str(REPO_ROOT / "runs" / "detect"),
    exist_ok=True,
    verbose=False,
)

# ── 6. Copy best weights ──────────────────────────────────────────────────────
best_pt = REPO_ROOT / "runs" / "detect" / "robot_detector" / "weights" / "best.pt"
if best_pt.exists():
    MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best_pt, MODEL_OUT)
    print(f"\nModel saved to {MODEL_OUT}")
else:
    print(f"\nWARNING: best.pt not found at {best_pt}")

print("\nDone! Update station configs:")
print("  obstacle_model_path: models/robot_detector.pt")
print("  obstacle_confidence_threshold: 0.55")
