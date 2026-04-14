# Dataset Capture Protocol (Phase 1, Plan 03)

## Target
- 50 images per class minimum (D-02). Expand to 100+ only if val mAP50 < 0.85.
- Classes: A, B, C (D-04: single-letter QR payload).
- Total: 150 images initial; 300+ if gate fails.

## Capture
1. Run on the DEMO venue laptop + webcam + lighting (D-03).
2. `python scripts/capture_dataset.py --class A --count 50`
3. Repeat for B and C.
4. During capture: vary distance (close / medium / far) and angle (±15°, per PITFALLS #14).
5. Include diverse lighting (PITFALLS #12): overhead fluorescent, natural light, lamp, mixed.

## Label + Split
1. Upload `data/raw/` to Roboflow (or label manually with LabelImg).
2. Draw bounding boxes around the package (the QR-labeled item).
3. **IMPORTANT (PITFALLS #2):** Set the train/val/test split BEFORE enabling augmentation. Source images must not leak across splits.
4. Augmentation budget (per PITFALLS #14): rotation ±15° max, brightness ±30%, contrast ±20%. No random erasing. No horizontal flip (QR codes are asymmetric).
5. Export as YOLOv8 format to `data/dataset/` with structure:
   ```
   data/dataset/
     images/train/*.jpg
     images/val/*.jpg
     images/test/*.jpg       (optional)
     labels/train/*.txt
     labels/val/*.txt
     labels/test/*.txt       (optional)
   ```

## Gate (PITFALLS #1)
Val mAP50 must be ≥ 0.85 before Plan 04 (vision pipeline) integrates the model.
Enforced by `scripts/val_yolo.py` — exits non-zero if gate fails.
