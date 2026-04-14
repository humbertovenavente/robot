# Requirements: LEGO MINDSTORMS + YOLO Package Sorter

**Defined:** 2026-04-13
**Milestone:** v1.0 — Demo-Ready Package Sorter
**Core Value:** Camera sees package → YOLO + QR identify class → robot delivers to correct bin → returns home → orchestrator logs event.
**Hard deadline:** 2026-04-16

## v1 Requirements

Requirements for the v1.0 milestone. Each maps to a roadmap phase.

### Vision

- [ ] **VIS-01**: YOLOv8n model detects packages in camera frame (3 classes, val mAP50 ≥ 0.85)
- [ ] **VIS-02**: Detected bounding box is cropped with padding and passed to QR decoder
- [ ] **VIS-03**: pyzbar decodes QR from the cropped region and returns the package class string
- [ ] **VIS-04**: Station reports "unknown package" when YOLO misses or QR decode fails (supports RF-12)
- [ ] **VIS-05**: Station detects a QR with payload `ROBOT` in each camera frame, separately from package QRs (A/B/C) (Phase 02.1, D-03..D-05)
- [ ] **VIS-06**: `calibrate.py` captures and persists the expected ROBOT-QR pixel position for home and for each of the 3 bins (Phase 02.1, D-10..D-11)
- [ ] **VIS-07**: After each motion command (`move_to_bin`, `return_home`), station compares observed ROBOT-QR pixel center to expected and records Euclidean pixel drift in the per-cycle JSONL entry (Phase 02.1, D-06..D-09, D-12, D-18)
- [x] **VIS-08**: Vision confirmation is gated by config flag `vision_confirm_enabled` (default `false`); when disabled, Phase 1 behavior is preserved exactly (Phase 02.1, D-16)

### Robot Control

- [ ] **ROB-01**: Station controller executes pickup/push cycle on detected package (RF-04)
- [ ] **ROB-02**: Robot delivers package to the bin mapped to its class via a class→bin lookup table (RF-03, RF-05)
- [ ] **ROB-03**: Robot returns to home position after each delivery using encoder-based positioning (RF-06)
- [ ] **ROB-04**: Cycle lock prevents a new detection from triggering while robot is busy (RF-07)
- [ ] **ROB-05**: Calibration routine runs before each session to set home and bin offsets (RNF-06)

### Orchestration

- [x] **ORC-01**: Central orchestrator (FastAPI) accepts station registration over WebSocket (RF-10)
- [x] **ORC-02**: Orchestrator receives station status updates: free / processing / error / completed (RF-09, RF-11)
- [x] **ORC-03**: Orchestrator coordinates ≥2 stations running simultaneously without cross-interference (RF-10)
- [x] **ORC-04**: Station process runs standalone (env flag) if orchestrator is unavailable — demo-day safety net

### Event Logging

- [ ] **LOG-01**: Each cycle is logged with detection class, destination bin, cycle time, and timestamp (RF-08)
- [ ] **LOG-02**: Logs persist to a local JSONL file per session for post-demo review

### Dashboard

- [x] **DSH-01**: Instructor dashboard (Jinja2 + vanilla JS + WebSocket) shows live status of every registered station (RNF-08)
- [x] **DSH-02**: Dashboard displays station state (free/processing/error/completed) and latest event per station

### Error Handling

- [ ] **ERR-01**: Unknown package → station logs error, aborts cycle, returns to ready state (RF-12)
- [ ] **ERR-02**: Return-home failure → station reports error, cycle lock clears after watchdog timeout (RF-12)
- [ ] **ERR-03**: Blocked destination bin detected or handled gracefully (best-effort per RF-12)

### Demo Readiness

- [ ] **DEM-01**: End-to-end happy path demonstrated on one physical station with all 3 classes
- [ ] **DEM-02**: Multi-station demo with ≥2 physical stations running under one orchestrator (RF-10, RF-11)
- [ ] **DEM-03**: 30-min pre-demo checklist executed (paths, lighting, ports, battery) — prevents the top 3 demo-day failure modes from PITFALLS.md

## v2 Requirements

Deferred beyond the v1.0 demo.

### Advanced Dashboard

- **DSH2-01**: Real-time video stream from each station
- **DSH2-02**: Historical charts (throughput, error rate over time)
- **DSH2-03**: Per-class throughput metrics view

### Recovery and Resilience

- **REC-01**: Automated error recovery (retry + quarantine failed packages)
- **REC-02**: Persistent state recovery after orchestrator restart
- **REC-03**: Station auto-reconnect with backoff

### Scaling

- **SCL-01**: Support for 5+ stations (spec §9 upper bound)
- **SCL-02**: Config-driven class→bin mapping (currently hard-coded)

## Out of Scope

Explicitly excluded from v1.0 to protect the 3-day deadline.

| Feature | Reason |
|---------|--------|
| MQTT broker (Mosquitto) | Zero-broker FastAPI WebSocket is sufficient at 2-6 stations; broker adds setup time |
| Docker / docker-compose | Adds demo-day friction; plain `pip install` is faster |
| React / Vue / Node build step | No time for SPA tooling; Jinja2 + vanilla JS is enough |
| Real-time video stream on dashboard | 6-8h of work, not required by spec |
| Automated error recovery | 4-6h; manual operator restart is acceptable for demo |
| Historical charts | 3-4h; not required by spec |
| OAuth / multi-user dashboard auth | Single-instructor local network only (PROJECT.md) |
| Cloud deployment | Local lab network only (PROJECT.md) |
| Industrial PLC integration | Out of scope per spec §4 |
| 24/7 operation, heavy loads | LEGO prototype constraint per spec §4 |
| High-speed industrial barcode scanners | pyzbar on consumer camera is sufficient per spec §4 |
| YOLOv9/v10/v11 | Stick with YOLOv8n (nano) for fastest training on tiny dataset |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| VIS-01 | Phase 1 | Pending |
| VIS-02 | Phase 1 | Pending |
| VIS-03 | Phase 1 | Pending |
| VIS-04 | Phase 1 | Pending |
| VIS-05 | Phase 02.1 | Pending |
| VIS-06 | Phase 02.1 | Pending |
| VIS-07 | Phase 02.1 | Pending |
| VIS-08 | Phase 02.1 | Complete |
| ROB-01 | Phase 1 | Pending |
| ROB-02 | Phase 1 | Pending |
| ROB-03 | Phase 1 | Pending |
| ROB-04 | Phase 1 | Pending |
| ROB-05 | Phase 1 | Pending |
| LOG-01 | Phase 1 | Pending |
| LOG-02 | Phase 1 | Pending |
| ORC-01 | Phase 2 | Complete |
| ORC-02 | Phase 2 | Complete |
| ORC-03 | Phase 2 | Complete |
| ORC-04 | Phase 2 | Complete |
| DSH-01 | Phase 2 | Complete |
| DSH-02 | Phase 2 | Complete |
| ERR-01 | Phase 3 | Pending |
| ERR-02 | Phase 3 | Pending |
| ERR-03 | Phase 3 | Pending |
| DEM-01 | Phase 3 | Pending |
| DEM-02 | Phase 3 | Pending |
| DEM-03 | Phase 3 | Pending |

**Coverage:**
- v1 requirements: 27 total
- Mapped to phases: 27
- Unmapped: 0

---
*Requirements defined: 2026-04-13*
*Last updated: 2026-04-13 after roadmap creation*
*Updated: 2026-04-14 — added VIS-05..08 for Phase 02.1 vision-robot-tracking*
