# Pre-Demo Checklist

**Estimated total: ~25 minutes** | **Must complete before demo starts**

Tick each box as it passes. If any Step 1-4 item cannot be ticked, resolve before proceeding to the Demo Script.

---

## Step 1 — Ports and Camera Index (~5 min)

- [ ] USB camera is physically connected. Run `python -c "import cv2; cap=cv2.VideoCapture(0); print(cap.isOpened())"` — prints True. If False, try index 1 or 2 and update `camera_index` in station_config.yaml.
- [ ] station_config.yaml `camera_index` value matches the index that returned True.
- [ ] LEGO brick USB/Bluetooth connection is reachable. Run `ls /dev/ttyUSB*` (Linux) or `ls /dev/tty.usbmodem*` (macOS) and confirm at least one device appears.
- [ ] station_config.yaml `robot_implementation` is set to `ev3` (or `spike`) — not `stub` — for the physical demo run.

---

## Step 2 — YOLO Model File (~2 min)

- [ ] station_config.yaml `yolo_model_path` points to the trained weights file (e.g. `models/best.pt`).
- [ ] The file exists and is non-zero bytes: `python -c "from pathlib import Path; p=Path('models/best.pt'); print(p.exists(), p.stat().st_size)"` — first value True, second value > 0.
- [ ] Replace `models/best.pt` above with the actual path in `yolo_model_path` if different.

---

## Step 3 — Lighting and QR Readability (~10 min)

- [ ] Start a 10-second camera preview: `python -c "import cv2; cap=cv2.VideoCapture(0); [cv2.imshow('p',cap.read()[1]) or cv2.waitKey(1) for _ in range(300)]; cv2.destroyAllWindows(); cap.release()"`. Preview window opens without error.
- [ ] Hold Package A QR in front of the camera. QR appears clearly in the preview — no motion blur, not washed out.
- [ ] Repeat for Package B QR.
- [ ] Repeat for Package C QR.
- [ ] Hold the ROBOT QR (affixed to the robot arm) in front of the camera. Confirm it appears clearly.
- [ ] If any QR does not scan clearly, adjust lighting or reprint at higher contrast before continuing — do NOT skip this check.
- [ ] Smoke-test: `python -c "from qr import decode_from_frame; print('qr module ok')"` — prints `qr module ok`.

---

## Step 4 — Multi-Station Connectivity (~8 min)

*(Skip if running single-station mode)*

- [ ] Ping test: `ping -c 3 <orchestrator-ip>` from each station laptop — 0% packet loss.
- [ ] Start orchestrator: `python orchestrator.py` — terminal shows `Uvicorn running on http://0.0.0.0:8000`.
- [ ] Open dashboard in browser: `http://[orchestrator-ip]:8000/` — dashboard grid loads.
- [ ] Start Station 1 with `orchestrator_url: ws://[orchestrator-ip]:8000/ws` in station_config.yaml. Within 5 seconds, Station 1 card appears in the dashboard with status `free`.
- [ ] Start Station 2 on a second laptop with a different `station_id`. Within 5 seconds, Station 2 card appears alongside Station 1.
- [ ] Both station cards show `free` status simultaneously — no duplicate cards, no missing cards.

---

## Demo Script

### DEM-01 — Single-Station Happy Path (~5 min, one station only)

- [ ] Place Package A (class A) in front of the camera. Robot moves to Bin 1, deposits, returns home. Dashboard shows `processing` then `free`. JSONL entry: `status completed`, class A.
- [ ] Place Package B. Robot delivers to Bin 2, returns home. JSONL entry: `status completed`, class B.
- [ ] Place Package C. Robot delivers to Bin 3, returns home. JSONL entry: `status completed`, class C.
- [ ] All 3 packages sorted in a single uninterrupted session — no manual intervention, no stuck cycle lock.

### DEM-02 — Multi-Station Concurrent Run (≥5 min, both stations)

- [ ] Both stations are running under the orchestrator (Step 4 complete).
- [ ] Place packages alternately on Station 1 and Station 2 at roughly 30-second intervals for at least 5 minutes.
- [ ] No crash or stuck cycle lock on either station during the 5-minute window.
- [ ] Dashboard continues to update both cards throughout — no stale or frozen cards.
- [ ] After 5 minutes: check JSONL logs on both station laptops — each shows multiple `completed` entries.

---

## Optional

- [ ] Battery level checked (operator discretion — not mandatory).

---

*Phase 3 — DEM-03 | Deadline: 2026-04-16*
