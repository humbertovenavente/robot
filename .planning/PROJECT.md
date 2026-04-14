# LEGO MINDSTORMS + YOLO Package Sorter

## What This Is

An automated package-sorting system where a LEGO MINDSTORMS robot classifies packages using computer vision (YOLO) and QR codes, routes each package to the correct destination bin, and returns to home position. A central orchestrator coordinates multiple stations running simultaneously in the lab. Academic project — visual demo for class.

## Current Milestone: v1.0 Demo-Ready Package Sorter

**Goal:** Ship a working LEGO + YOLO package sorter — single-station happy path plus multi-station orchestration — by Thursday 2026-04-16.

**Target features:**
- YOLO model trained on 3 QR-coded package classes
- QR decoding pipeline extracting class from detected region
- Station controller: pickup → destination bin → return home
- Cycle lock, event logging, station status reporting
- Central orchestrator coordinating ≥2 stations
- Live instructor dashboard
- Basic error handling and calibration routine

## Core Value

End-to-end autonomous cycle: **camera sees package → YOLO + QR identify class → robot delivers it to the right bin → robot returns home → orchestrator logs the event.** If nothing else works, this single-station happy path must work on demo day.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] YOLO model trained to detect packages with QR-coded labels (3 classes)
- [ ] QR decoding pipeline extracts package class from detected region
- [ ] Station controller drives LEGO robot: pickup/push → destination bin → return home
- [ ] Lock prevents new cycle while robot is busy (RF-07)
- [ ] Event log records detection, class, destination, cycle time (RF-08)
- [ ] Station status reporting: free / processing / error / completed (RF-09)
- [ ] Central orchestrator registers stations and coordinates cycles (RF-10, RF-11)
- [ ] Dashboard shows live status of all stations to the instructor (RNF-08)
- [ ] Basic error handling: unknown package, blocked destination, return failure (RF-12)
- [ ] Multi-station demo (≥2 stations running simultaneously under one orchestrator)
- [ ] Calibration routine before each run (RNF-06)

### Out of Scope

- Industrial PLC integration — academic prototype only (spec §4)
- 24/7 operation, heavy loads — LEGO prototype constraints (spec §4)
- High-speed industrial barcode scanners — pyzbar on consumer camera is sufficient
- Mobile app / cloud deployment — local lab network only
- OAuth / multi-user auth on dashboard — single-instructor view, local network

## Context

- Academic project for Robotics / Computer Vision / Automation course
- Team divides work across vision, robot control, orchestration, dashboard, testing
- Hardware: LEGO MINDSTORMS ("totally resolved" — team already has the bricks sorted)
- Reference spec: `requerimientos.md` in project root — full RF/RNF list, architecture, metrics
- Simultaneous operation is a course requirement: instructor observes all stations in one dashboard
- Python ecosystem: `ultralytics` (YOLOv8), `pyzbar` + `opencv-python` (QR), `pybricks` or `ev3dev` (robot), `fastapi` + WebSocket (orchestrator), React or similar (dashboard)

## Constraints

- **Timeline**: Ship by Thursday 2026-04-16 — ~3 days from today (2026-04-13). Scope must stay ruthlessly minimal.
- **Tech stack**: Python everywhere (vision, robot, orchestrator); web dashboard in a lightweight framework
- **Hardware**: LEGO MINDSTORMS (model settled by team) + standard USB/laptop webcam per station
- **Scale v1**: 1 station × 3 package classes end-to-end; orchestrator + dashboard proven with ≥2 stations
- **Compatibility**: Runs on lab laptops (local network), no cloud dependency
- **Course requirement**: Must demonstrate multi-station simultaneous operation (spec §11)

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| YOLO trained on QR-labeled packages (not color/shape) | QR survives lighting variation, acts as ground truth for class labels, meets spec § requirement to use YOLO | — Pending |
| Python end-to-end (ultralytics, pyzbar, pybricks/ev3dev, FastAPI) | Single language simplifies integration under tight deadline | — Pending |
| Orchestrator comms: TBD in research phase (WebSocket vs MQTT vs REST) | Defer to research; all three viable, pick based on simplicity for Thursday ship | — Pending |
| v1 scope = 1 station fully working + orchestrator demo-ready for multi-station | Deadline pressure; single-station happy path is the Core Value | — Pending |
| Error handling kept minimal per RF-12 (unknown package, blocked bin, return fail) | Academic demo, not production | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-13 after starting milestone v1.0*
