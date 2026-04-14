---
phase: 01-vision-pipeline-robot-core
plan: 03
subsystem: vision-dataset-training
tags: [yolo, dataset, training, validation, mAP50]
dependency_graph:
  requires: [01-01-config-robot-stub]
  provides: [scripts/capture_dataset.py, scripts/train_yolo.py, scripts/val_yolo.py, data/dataset/packages.yaml]
  affects: [01-04-vision-pipeline]
tech_stack:
  added: [ultralytics-yolov8n, opencv-python]
  patterns: [argparse-cli, pathlib-repo-root, sys-path-injection, lazy-import-for-help]
key_files:
  created:
    - scripts/capture_dataset.py
    - scripts/train_yolo.py
    - scripts/val_yolo.py
    - data/dataset/packages.yaml
    - data/dataset/README.md
    - scripts/__init__.py
  modified: []
decisions:
  - "sys.path injection added to all scripts so they work when invoked as `python scripts/foo.py` from repo root without PYTHONPATH setup"
  - "augmentation defaults enforce PITFALLS #14 caps: degrees=15, fliplr=0, erasing=0"
  - "val_yolo.py uses split='val' explicitly to prevent accidentally validating on train split (PITFALLS #1)"
metrics:
  duration_minutes: 8
  completed_date: "2026-04-14T05:47:38Z"
  tasks_completed: 2
  files_created: 6
---

# Phase 1 Plan 03: YOLO Dataset Training Pipeline Summary

YOLOv8n training pipeline with webcam capture CLI, ultralytics training wrapper with PITFALLS #14 augmentation caps, and a standalone mAP50 gate enforcement script (exits 1 if val mAP50 < 0.85).

## Tasks Completed

| Task | Name | Commit | Key Files |
|------|------|--------|-----------|
| 1 | Dataset capture CLI + dataset descriptor + capture protocol README | 792e2ff | scripts/capture_dataset.py, data/dataset/packages.yaml, data/dataset/README.md, scripts/__init__.py |
| 2 | YOLO train + val scripts with mAP50 gate enforcement | 4e68f7b | scripts/train_yolo.py, scripts/val_yolo.py |

## What Was Built

### scripts/capture_dataset.py
Webcam dataset capture CLI. Usage: `python scripts/capture_dataset.py --class A --count 50`. Writes JPEGs to `data/raw/<CLASS>/NNNN.jpg`. Uses `camera_index` from `station_config.yaml` by default (overridable with `--camera-index`). Output path uses `REPO_ROOT / "data" / "raw" / cls` — no absolute paths (PITFALLS #11).

### scripts/train_yolo.py
YOLOv8n training CLI wrapping ultralytics. Defaults: `yolov8n.pt`, 50 epochs, imgsz=640, device=cpu. Augmentation defaults enforce PITFALLS #14 caps: `--degrees 15.0`, `--fliplr 0.0`, `--erasing 0.0`. After training completes, automatically calls `model.val()` and exits 1 if `box.map50 < 0.85` (PITFALLS #1 gate).

### scripts/val_yolo.py
Standalone validation script. Usage: `python scripts/val_yolo.py --weights runs/detect/train/weights/best.pt`. Explicitly uses `split="val"` to prevent label leakage (PITFALLS #2). `--threshold` defaults to 0.85. Exits 1 if gate fails; exits 0 if gate passes.

### data/dataset/packages.yaml
Ultralytics YOLO dataset descriptor. `nc: 3`, names `A`, `B`, `C`. Path set as `../data/dataset` (repo-relative).

### data/dataset/README.md
Capture protocol documenting: 50/class minimum (D-02), demo venue hardware requirement (D-03), split-before-augment rule (PITFALLS #2), augmentation caps (PITFALLS #14), and 0.85 mAP50 gate (PITFALLS #1). Expected dataset structure for YOLOv8 format export.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added sys.path injection to all three scripts**
- **Found during:** Task 2 verification
- **Issue:** Running `python scripts/train_yolo.py --help` from the repo root failed with `ModuleNotFoundError: No module named 'config'` because the scripts directory is not the repo root and Python does not auto-add the parent to sys.path.
- **Fix:** Added `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` at the top of all three scripts. This ensures `config.py` (which lives at repo root) is importable when scripts are invoked as `python scripts/foo.py` from any working directory.
- **Files modified:** scripts/train_yolo.py, scripts/val_yolo.py, scripts/capture_dataset.py
- **Commit:** 4e68f7b

## Training Notes (for future runs)

When the team captures and labels the dataset, the expected invocation sequence is:

```bash
# Capture
python scripts/capture_dataset.py --class A --count 50
python scripts/capture_dataset.py --class B --count 50
python scripts/capture_dataset.py --class C --count 50

# Label in Roboflow (split BEFORE augmenting), export to data/dataset/

# Train + auto-gate
python scripts/train_yolo.py --device mps   # Apple Silicon
python scripts/train_yolo.py --device cpu   # Intel / lab laptops

# Re-validate anytime
python scripts/val_yolo.py --weights runs/detect/train/weights/best.pt
```

Expected outputs for Plan 04 consumption:
- `runs/detect/train/weights/best.pt` — model weights file
- Val mAP50 >= 0.85 confirmed via exit code 0 of `val_yolo.py`

## Known Stubs

None. All scripts are fully wired for their intended function. Dataset and trained weights do not yet exist (the team must run the capture and training workflow), but that is the expected state at this point in the plan sequence — not a stub.

## Self-Check: PASSED

Files created:
- scripts/capture_dataset.py: FOUND
- scripts/train_yolo.py: FOUND
- scripts/val_yolo.py: FOUND
- data/dataset/packages.yaml: FOUND
- data/dataset/README.md: FOUND
- scripts/__init__.py: FOUND

Commits:
- 792e2ff: FOUND (feat(01-03): dataset capture CLI...)
- 4e68f7b: FOUND (feat(01-03): YOLO train and val scripts...)
