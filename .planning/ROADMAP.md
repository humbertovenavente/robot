# Roadmap: LEGO MINDSTORMS + YOLO Package Sorter

## Overview

Three phases aligned to the 3-day deadline (2026-04-13 to 2026-04-16). Phase 1 builds the single-station happy path — the irreducible core value. Phase 2 layers the orchestrator and instructor dashboard on top of that working station loop. Phase 3 hardens error handling and runs the pre-demo checklist to convert a working prototype into a demo-ready system. Every phase delivers a verifiable capability; if the deadline compresses, Phase 1 alone satisfies the minimum passing bar.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Vision Pipeline + Robot Core** - Single-station end-to-end: camera sees package, YOLO+QR classifies it, robot delivers to correct bin and returns home
- [ ] **Phase 2: Orchestrator + Dashboard** - Multi-station coordination via FastAPI WebSocket; live instructor dashboard shows all station statuses
- [ ] **Phase 3: Error Handling + Demo Prep** - Full RF-12 error states, cycle-lock watchdog, calibration hardening, and pre-demo checklist execution

## Phase Details

### Phase 1: Vision Pipeline + Robot Core
**Goal**: One physical station sorts all 3 package classes autonomously — no orchestrator required
**Depends on**: Nothing (first phase)
**Requirements**: VIS-01, VIS-02, VIS-03, VIS-04, ROB-01, ROB-02, ROB-03, ROB-04, ROB-05, LOG-01, LOG-02
**Success Criteria** (what must be TRUE):
  1. A package placed in front of the camera is detected by YOLO (val mAP50 ≥ 0.85) and its QR code is decoded — class string appears in terminal within 2 seconds
  2. The robot picks up the package, moves to the bin mapped to that class, deposits it, and returns to home position without manual intervention
  3. Every completed cycle produces a log entry with class, destination bin, cycle time, and timestamp visible in the JSONL file
  4. Calibration routine runs before the first cycle and resets home and bin offsets correctly (robot reaches home within ±2°)
  5. Station correctly reports "unknown package" when YOLO misses or QR decode fails and does not attempt a robot move
**Plans**: 6 plans
- [x] 01-01-scaffolding-PLAN.md — Project scaffolding: requirements.txt, config.py, station_config.yaml
- [x] 01-02-robot-abstraction-PLAN.md — RobotInterface protocol + StubRobot + EV3/SPIKE placeholders (ROB-01..04 structural)
- [x] 01-03-yolo-dataset-training-PLAN.md — Dataset capture CLI + YOLOv8n training + mAP50 gate (VIS-01)
- [x] 01-04-vision-qr-pipeline-PLAN.md — vision.py YOLO inference + qr.py padded decode with retry (VIS-01..04)
- [ ] 01-05-station-loop-logging-PLAN.md — station.py end-to-end loop + JSONL event_log.py (ROB-01..04, VIS-04, LOG-01, LOG-02)
- [x] 01-06-calibration-PLAN.md — calibrate.py interactive CLI with yaml write-back (ROB-05)

### Phase 2: Orchestrator + Dashboard
**Goal**: Two stations run simultaneously under one orchestrator; instructor sees live status of both in a browser without refreshing
**Depends on**: Phase 1
**Requirements**: ORC-01, ORC-02, ORC-03, ORC-04, DSH-01, DSH-02
**Success Criteria** (what must be TRUE):
  1. Two station processes register with the orchestrator over WebSocket and their cards appear in the dashboard immediately
  2. When either station changes state (free → processing → completed), the dashboard card updates live without a page reload
  3. Both stations process packages concurrently without interfering with each other's cycle lock or event log
  4. If the orchestrator process is killed, both station processes continue running autonomously and log events locally
**Plans**: 5 plans
- [x] 02-01-protocol-config-PLAN.md — ws_protocol.py (Pydantic RegisterMsg/StatusMsg/StationUpdateMsg) + config.orchestrator_url + requirements.txt pins
- [x] 02-02-orchestrator-server-PLAN.md — orchestrator.py FastAPI app: /ws multiplexed by role, in-memory registry, broadcast, GET /api/stations (ORC-01..04)
- [x] 02-03-station-ws-client-PLAN.md — ws_client.py background-thread client with 1→8s reconnect backoff; wire into run_station via status_listener (ORC-01, ORC-02, ORC-04)
- [x] 02-04-dashboard-ui-PLAN.md — templates/dashboard.html + static/dashboard.js + static/dashboard.css + GET / in orchestrator (DSH-01, DSH-02)
- [x] 02-05-integration-tests-PLAN.md — end-to-end tests proving the 4 ROADMAP success criteria (ORC-03, ORC-04)
**UI hint**: yes

### Phase 02.1: vision-robot-tracking (INSERTED)

**Goal:** Camera confirms that the LEGO robot reached the expected encoder target after each motion command — observability overlay that logs pixel drift per cycle without driving motion. Default-off (`vision_confirm_enabled: false`) preserves Phase 1 behavior byte-identically.
**Requirements**: VIS-05, VIS-06, VIS-07, VIS-08
**Depends on:** Phase 2
**Plans:** 3/5 plans executed

Plans:
- [x] 02.1-01-config-eventlog-requirements-PLAN.md — Config fields, station_config.yaml keys, EventLogger optional vision kwargs, REQ-IDs (VIS-08)
- [x] 02.1-02-vision-confirm-module-PLAN.md — New vision_confirm.py with find_robot_qr + compute_drift (VIS-05, VIS-07)
- [x] 02.1-03-calibrate-pixel-capture-PLAN.md — calibrate.py pixel-target sub-flow with yaml round-trip (VIS-06)
- [ ] 02.1-04-station-integration-PLAN.md — Station post-motion hooks + JSONL extension + lock-preserving try/except (VIS-05, VIS-06, VIS-07, VIS-08)
- [ ] 02.1-05-integration-regression-PLAN.md — End-to-end + disabled-path regression + REQ-ID coverage audit (VIS-05..08)

### Phase 3: Error Handling + Demo Prep
**Goal**: The system handles the three RF-12 failure modes gracefully and passes the full pre-demo checklist on actual demo hardware in the actual demo room
**Depends on**: Phase 2
**Requirements**: ERR-01, ERR-02, ERR-03, DEM-01, DEM-02, DEM-03
**Success Criteria** (what must be TRUE):
  1. An unknown package (YOLO miss or QR fail) causes the station to log the error, skip the robot move, and return to ready state — cycle lock clears and the next package is accepted normally
  2. A simulated return-home failure triggers the watchdog: cycle lock auto-clears after the configured timeout and station status updates to "error" in the dashboard
  3. One physical station demonstrates all 3 package classes sorted correctly in a single uninterrupted session (DEM-01 happy path)
  4. Two stations run simultaneously under the orchestrator for ≥5 minutes without a crash or stuck cycle lock (DEM-02 multi-station demo)
  5. The 30-minute pre-demo checklist (paths, lighting, ports, battery) completes with zero blocking failures on demo-day hardware

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Vision Pipeline + Robot Core | 5/6 | In Progress|  |
| 2. Orchestrator + Dashboard | 4/5 | In Progress|  |
| 3. Error Handling + Demo Prep | 0/? | Not started | - |
