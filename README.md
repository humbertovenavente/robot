# LEGO MINDSTORMS + YOLO Package Sorter

Automated package-sorting system where a LEGO MINDSTORMS robot classifies packages
using computer vision (YOLOv8) and QR codes, routes each package to the correct
destination bin, and returns to home position.

## Prerequisites

### macOS

```bash
# IMPORTANT: Install zbar native library BEFORE pip installing pyzbar.
# Without this, pyzbar installs silently but fails at import time.
brew install zbar
```

### Ubuntu / Debian

```bash
sudo apt-get install -y libzbar0
```

## Installation

```bash
# 1. Create and activate a virtual environment
python3.11 -m venv .venv && source .venv/bin/activate

# 2. Install all dependencies
pip install -r requirements.txt

# 3. (Optional) Create a local config override for your venue
cp station_config.yaml station_config.local.yaml
# Edit station_config.local.yaml with your camera_index, motor targets, etc.

# 4. Quick sanity check — should print the station_id from station_config.yaml
python -c "from config import load_config; print(load_config().camera_index)"
```

## Project Structure

```
.
├── config.py               # Config loader (load_config, Config)
├── station_config.yaml     # Example station configuration (checked in)
├── station_config.local.yaml  # Local override — gitignored, not checked in
├── requirements.txt        # Pinned Python dependencies
├── pyproject.toml          # Project metadata
├── logs/                   # Runtime JSONL event logs — gitignored
├── runs/                   # YOLO training run artifacts — gitignored
├── models/                 # Trained model weights (*.pt) — gitignored
└── data/                   # Dataset for YOLO training — gitignored
```

## Configuration

Copy `station_config.yaml` and edit for your setup. Key fields:

| Field | Description |
|-------|-------------|
| `station_id` | Unique station name |
| `camera_index` | OpenCV camera index (usually 0) |
| `yolo_model_path` | Path to trained YOLOv8 weights (relative to repo root) |
| `yolo_confidence_threshold` | Minimum detection confidence (0.0–1.0) |
| `robot_implementation` | `stub` (dev), `ev3` (EV3 brick), or `spike` (SPIKE Prime) |
| `class_to_bin` | Maps package class (A/B/C) to bin number (1/2/3) |

## Notes

- python-ev3dev2 installs on macOS but API calls only work on the EV3 brick running ev3dev Linux.
- On Apple Silicon (M1/M2/M3), set `PYTORCH_ENABLE_MPS_FALLBACK=1` if YOLO inference crashes.
- The orchestrator runs on port 8080 to avoid conflicts with macOS AirPlay (which uses 5000/7000).
