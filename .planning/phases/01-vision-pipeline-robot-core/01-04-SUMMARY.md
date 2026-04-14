---
phase: 01-vision-pipeline-robot-core
plan: 04
subsystem: vision
tags: [yolo, qr, pyzbar, opencv, vision-pipeline]
dependency_graph:
  requires: [01-01-scaffolding]
  provides: [vision.py, qr.py, Detection dataclass]
  affects: [01-05-station-loop-logging]
tech_stack:
  added: []
  patterns:
    - Lazy YOLO model loading on first detect() call
    - sys.modules injection for CI-friendly pyzbar mocking
    - Progressive preprocessing retry (raw -> Otsu -> adaptive threshold)
key_files:
  created:
    - vision.py
    - qr.py
    - tests/test_vision.py
    - tests/test_qr.py
    - tests/fixtures/README.md
    - tests/__init__.py
  modified: []
decisions:
  - Detection dataclass is frozen (immutable) to prevent accidental mutation downstream
  - pyzbar lazy import inside decode_qr_from_crop isolates zbar native lib failures
  - sys.modules injection chosen over `patch("pyzbar.pyzbar.decode")` because pyzbar may not be installed in CI
metrics:
  duration_minutes: 3
  completed_date: "2026-04-14"
  tasks_completed: 2
  files_created: 6
---

# Phase 01 Plan 04: Vision QR Pipeline Summary

Stand-alone YOLO inference wrapper and QR decode modules built without robot or station loop dependencies. Both modules are fully unit-tested without hardware or trained weights.

## One-liner

YOLO-first + pyzbar-on-padded-crop pipeline with retry preprocessing and strict A/B/C payload validation, embedded pitfall mitigations from PITFALLS.md #3 and #4.

## What Was Built

### vision.py

- `Detection(bbox, confidence, class_id)` — frozen dataclass; `bbox` is `(x1, y1, x2, y2)` pixel coords
- `VisionPipeline(config)` — lazy YOLO loader; model loads on first `detect()` call
- `VisionPipeline.detect(frame)` — runs YOLOv8 inference, returns highest-confidence `Detection` above `config.yolo_confidence_threshold` or `None`
- `load_vision(config)` — factory function returning a `VisionPipeline`
- Sets `PYTORCH_ENABLE_MPS_FALLBACK=1` env var for Apple Silicon macOS (STACK.md pitfall #3)
- Raises `FileNotFoundError` with actionable message if `best.pt` not found, referencing `scripts/train_yolo.py`

**Import path (Plan 05 dependency):**
```python
from vision import Detection, VisionPipeline, load_vision
```

### qr.py

- `VALID_CLASSES = frozenset({"A", "B", "C"})` — per D-04; anything else is rejected
- `pad_bbox(bbox, pct, frame_w, frame_h)` — expands bbox by pct of its dimensions, clamped to frame bounds (PITFALLS #4)
- `crop_padded_region(frame, bbox, padding_pct)` — returns padded ROI ndarray
- `decode_qr_from_crop(crop, retry=N)` — retries with progressive preprocessing: raw → Otsu threshold → adaptive threshold with histogram equalization (PITFALLS #3)
- `decode_from_frame(frame, bbox, config)` — convenience: reads `config.qr_padding_pct` and `config.qr_retry_count`
- Non-A/B/C pyzbar payloads return `None` (D-06 unknown package sentinel)
- Empty crops (size==0) return `None` immediately

**Import path (Plan 05 dependency):**
```python
from qr import decode_from_frame, VALID_CLASSES
```

## Test Results

```
16 passed in 0.12s
- tests/test_vision.py: 5 tests
- tests/test_qr.py: 11 tests
```

All tests run without trained model, without pyzbar installed (CI-friendly).

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| 1 | 9343f27 | feat(01-04): vision.py YOLO loader + detect_package with confidence threshold |
| 2 | aa1c9a5 | feat(01-04): qr.py padded crop + pyzbar decode with retry; unit tests |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed pyzbar mock approach for CI without pyzbar installed**
- **Found during:** Task 2 (GREEN phase)
- **Issue:** The plan specified `patch("pyzbar.pyzbar.decode")` but this fails at patch setup with `ModuleNotFoundError` when pyzbar/libzbar is not installed in the test environment
- **Fix:** Inject a mock `pyzbar` package into `sys.modules` at test module load time using `types.ModuleType`, then use `patch.object(_pyzbar_mod, "decode", ...)` which patches the already-injected module attribute; the lazy `from pyzbar.pyzbar import decode as zbar_decode` inside `decode_qr_from_crop` resolves from `sys.modules` each invocation and picks up the patch correctly
- **Files modified:** `tests/test_qr.py`
- **Commit:** aa1c9a5

## Known Stubs

None — both modules implement full production logic. Real YOLO inference is deferred to integration when `best.pt` exists (Plan 05), not stubbed.

## Threat Flags

No new threat surface introduced beyond what is documented in the plan's threat model. All T-01-04-* mitigations implemented:
- T-01-04-01: `VALID_CLASSES` validation in `decode_qr_from_crop` — any non-A/B/C payload returns `None`
- T-01-04-02: retry bounded by `config.qr_retry_count`; exceptions caught per-attempt; loop always terminates
- T-01-04-03: accepted (local lab only, no network egress)

## macOS / pyzbar Notes

- `pyzbar` requires `brew install zbar` on macOS BEFORE `pip install pyzbar` (STACK.md #1)
- If `from pyzbar.pyzbar import decode` raises `ImportError: Unable to find zbar shared library` at runtime: `brew install zbar`
- Tests are CI-friendly (no libzbar needed) via `sys.modules` injection

## Self-Check: PASSED

- vision.py: FOUND
- qr.py: FOUND
- tests/test_vision.py: FOUND
- tests/test_qr.py: FOUND
- tests/fixtures/README.md: FOUND
- Commit 9343f27: FOUND
- Commit aa1c9a5: FOUND
- 16 tests passing: VERIFIED
