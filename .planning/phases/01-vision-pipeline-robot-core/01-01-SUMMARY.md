---
phase: 01-vision-pipeline-robot-core
plan: 01
subsystem: infra
tags: [python, pydantic, yaml, yolo, ultralytics, pyzbar, ev3dev, scaffolding]

requires: []
provides:
  - Pinned requirements.txt with Python 3.11 stack (ultralytics, opencv, pyzbar, pydantic, PyYAML)
  - pyproject.toml with minimal metadata and requires-python >=3.11,<3.12
  - .gitignore covering logs/, runs/, models/*.pt, .venv/, data dirs
  - station_config.yaml with all D-13 config fields and example values
  - config.py exporting load_config() and Config (pydantic v2 BaseModel)
  - logs/, runs/, models/, data/ directories tracked via .gitkeep
affects: [01-02-robot-abstraction, 01-03-yolo-dataset-training, 01-04-vision-qr-pipeline, 01-05-station-loop-logging, 01-06-calibration]

tech-stack:
  added:
    - ultralytics==8.4.37 (YOLOv8 training + inference)
    - opencv-python==4.13.0.92 (frame capture)
    - pyzbar==0.1.9 (QR decoding)
    - numpy==2.0.2
    - Pillow==11.3.0
    - pydantic==2.13.0 (typed config model)
    - PyYAML==6.0.2 (config file parsing)
    - python-ev3dev2==2.1.0.post1 (linux-only, EV3 brick control)
  patterns:
    - Flat module layout: all modules importable from repo root
    - Config-by-yaml: station_config.yaml parsed once at startup into typed pydantic model
    - REPO_ROOT via pathlib.Path(__file__).resolve().parent — no hardcoded absolute paths
    - Local override pattern: station_config.local.yaml takes precedence if present (gitignored)

key-files:
  created:
    - requirements.txt
    - pyproject.toml
    - .gitignore
    - README.md
    - station_config.yaml
    - config.py
    - logs/.gitkeep
    - runs/.gitkeep
    - models/.gitkeep
    - data/.gitkeep
  modified: []

key-decisions:
  - "Flat module layout chosen (no src/) — small project, 3-day deadline, everything importable from root"
  - "python-ev3dev2 uses sys_platform linux marker — installs on macOS but must only run on EV3 brick"
  - ".gitkeep files force-added for logs/, runs/, models/, data/ despite gitignore entries"
  - "Pydantic v2 BaseModel used for typed config — Field(ge=0.0, le=1.0) validates float ranges"

patterns-established:
  - "Config pattern: load once via load_config(), typed Config object, pathlib-based path resolution"
  - "Gitignore pattern: runtime output dirs (logs/, runs/) gitignored but tracked via .gitkeep"
  - "Platform marker: python-ev3dev2 ; sys_platform == 'linux' prevents macOS install failures"

requirements-completed: []

duration: 15min
completed: 2026-04-13
---

# Phase 1 Plan 01: Scaffolding Summary

**Greenfield Python project scaffolded with pinned requirements (ultralytics 8.4.37, pyzbar 0.1.9), pydantic v2 typed config loader, YAML-driven station configuration, and gitignored runtime directories.**

## Performance

- **Duration:** ~15 min
- **Started:** 2026-04-13
- **Completed:** 2026-04-13
- **Tasks:** 2
- **Files modified:** 10

## Accomplishments

- requirements.txt with exact pinned versions from STACK.md; python-ev3dev2 linux-only via platform marker
- station_config.yaml with all D-13 fields (class_to_bin, camera_index, yolo_model_path, encoder targets, robot_implementation: stub)
- config.py with load_config() and pydantic v2 Config model; precedence: explicit > local > default yaml
- README documents `brew install zbar` prerequisite prominently (STACK.md macOS pitfall #1)
- Runtime directories (logs/, runs/, models/, data/) exist in git via .gitkeep

## Task Commits

Each task was committed atomically:

1. **Task 1: Create pinned requirements.txt and pyproject.toml** - `85baeb6` (chore)
2. **Task 2: Create station_config.yaml and config.py loader** - `f388d62` (feat)

## Files Created/Modified

- `requirements.txt` - Pinned Python 3.11 dependency stack; linux-only marker for python-ev3dev2
- `pyproject.toml` - Project metadata; requires-python >=3.11,<3.12; no build-system (flat layout)
- `.gitignore` - Covers logs/, runs/, models/*.pt, .venv/, __pycache__/, data dirs, station_config.local.yaml
- `README.md` - Install steps including brew install zbar prerequisite and venv setup
- `station_config.yaml` - Example station configuration with all D-13 fields; robot_implementation: stub
- `config.py` - load_config() + Config pydantic model; pathlib REPO_ROOT; local yaml override support
- `logs/.gitkeep` - Ensures logs/ directory tracked in git
- `runs/.gitkeep` - Ensures runs/ directory tracked in git
- `models/.gitkeep` - Ensures models/ directory tracked in git
- `data/.gitkeep` - Ensures data/ directory tracked in git

## Decisions Made

- Used pydantic v2 BaseModel with Field validators for config — catches misconfiguration at startup
- Added local override pattern (station_config.local.yaml) per CONTEXT.md specifics; gitignored by default
- Normalised bin_encoder_targets keys to int in load_config() because YAML parses integer keys inconsistently
- Force-added .gitkeep files with `git add -f` because parent dirs are gitignored

## Deviations from Plan

None — plan executed exactly as written. The .gitkeep force-add was required due to gitignore entries but is the standard mechanism for this pattern; no plan deviation occurred.

## Issues Encountered

- .gitkeep files for logs/ and runs/ required `git add -f` because those directories are listed in .gitignore. This is expected and intentional — the directories must exist in git while their runtime contents remain gitignored.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- All downstream plans (01-02 through 01-06) can now `from config import load_config` and receive a typed Config
- Flat module layout is established — new modules go at repo root (vision.py, qr.py, robot.py, station.py, calibrate.py)
- Station must confirm EV3 vs SPIKE Prime hardware before 01-02 robot abstraction (D-11 from CONTEXT.md)
- Dataset capture (01-03) should use `robot_implementation: stub` until hardware is confirmed

---
*Phase: 01-vision-pipeline-robot-core*
*Completed: 2026-04-13*
