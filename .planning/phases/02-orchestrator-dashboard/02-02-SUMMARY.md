---
phase: 02-orchestrator-dashboard
plan: "02"
subsystem: orchestrator-server
tags: [orchestrator, fastapi, websocket, registry, broadcast, phase2]
dependency_graph:
  requires:
    - ws_protocol.py (RegisterMsg, StatusMsg, StationUpdateMsg, decode_inbound, encode)
  provides:
    - orchestrator.py (FastAPI app with /ws and /api/stations)
    - Registry class with asyncio.Lock-guarded state
    - StationRecord dataclass
  affects:
    - Plan 02-03 (ws_client.py connects to /ws?role=station)
    - Plan 02-04 (dashboard.html connects to /ws?role=dashboard)
tech_stack:
  added: []
  patterns:
    - FastAPI WebSocket with Query param role discriminator
    - asyncio.Lock for in-memory registry mutation safety (PITFALLS #9)
    - dataclass StationRecord serialized via asdict()
    - Registry singleton with reset() for test isolation
    - FastAPI TestClient synchronous WS integration tests
key_files:
  created:
    - orchestrator.py
    - tests/test_orchestrator.py
  modified: []
decisions:
  - "Single module orchestrator.py at repo root (flat layout per Phase 1 convention)"
  - "Registry.reset() method added for test isolation without requiring app restart"
  - "last-writer-wins closes prev WS with code 4000 before claiming new connection (D-12)"
  - "Malformed/missing-type messages close station socket with code 4002 (fail-fast, don't poison loop)"
  - "Invalid role closes with code 4003 before accept() so no WS frame is ever sent"
  - "Station disconnect broadcasts online=False for that station_id to all dashboards (ORC-04)"
  - "No blocking I/O (no requests import) per PITFALLS #7"
metrics:
  duration_seconds: 180
  completed_date: "2026-04-13"
  tasks_completed: 1
  tasks_total: 1
  files_created: 2
  files_modified: 0
  tests_added: 7
---

# Phase 2 Plan 02: Orchestrator Server Summary

FastAPI orchestrator with a single `/ws` WebSocket endpoint that multiplexes stations and dashboards by `role` query param. In-memory registry guarded by `asyncio.Lock`. Broadcasts `station_update` messages to all connected dashboards on every station status change. REST snapshot endpoint for debugging.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 (RED) | Failing tests for orchestrator.py | 99ee868 | tests/test_orchestrator.py |
| 1 (GREEN) | orchestrator.py implementation + test fix | 845fa7c | orchestrator.py, tests/test_orchestrator.py |

## Endpoint Inventory

| Endpoint | Role | Direction | Description |
|----------|------|-----------|-------------|
| `GET /api/stations` | REST | → client | JSON snapshot of all known stations (D-13) |
| `WS /ws?role=station` | Station | station → orch | Accepts RegisterMsg then StatusMsg*; broadcasts StationUpdateMsg to dashboards |
| `WS /ws?role=dashboard` | Dashboard | orch → dash | Sends registry snapshot on connect; live StationUpdateMsg broadcasts |

## Close Code Conventions

| Code | Trigger | Meaning |
|------|---------|---------|
| 4000 | New station connects with same station_id | Previous connection preempted (last-writer-wins D-12) |
| 4002 | Station sends malformed JSON or unknown type | Protocol error — close to avoid poisoning loop |
| 4003 | WebSocket connects without valid `role=station\|dashboard` | Bad role — close before accept |

## Registry Data Shape (StationRecord)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `station_id` | str | — | Unique station identifier |
| `status` | str | "free" | free \| processing \| unknown_package \| error |
| `last_class` | Optional[str] | None | Last classified package class |
| `last_destination` | Optional[int] | None | Last destination bin number |
| `last_cycle_ms` | Optional[int] | None | Last cycle duration in milliseconds |
| `cycle_count` | int | 0 | Total cycles processed |
| `online` | bool | True | False when station WS disconnects |

## Test Results

```
tests/test_orchestrator.py    7 passed
tests/test_ws_protocol.py    10 passed
Total: 17 passed
```

### Test Coverage

| Test | Covers |
|------|--------|
| `test_missing_role_rejected` | Invalid role → code 4003 |
| `test_station_registers_and_status_broadcasts_to_dashboard` | ORC-01, ORC-02 |
| `test_two_stations_coexist_in_registry` | ORC-03 + disconnect → online=False |
| `test_last_writer_wins_same_station_id` | D-12 preemption |
| `test_dashboard_receives_snapshot_on_connect` | Snapshot on dashboard connect |
| `test_malformed_message_closes_station_socket` | Code 4002 fail-fast |
| `test_api_stations_returns_json` | D-13 REST snapshot |

## Requirements Fulfilled

| Requirement | Description | Evidence |
|-------------|-------------|---------|
| ORC-01 | Station registration over WebSocket | RegisterMsg accepted on /ws?role=station |
| ORC-02 | Status updates flow station→dashboard | StatusMsg → broadcast StationUpdateMsg |
| ORC-03 | Two stations register and both appear | test_two_stations_coexist_in_registry |
| ORC-04 | Orchestrator tolerates station disconnects | release_station sets online=False, process continues |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical Functionality] Registry test isolation**
- **Found during:** Task 1 GREEN phase (test_dashboard_receives_snapshot_on_connect failed with stale state from prior test)
- **Issue:** Module-level `registry` singleton retains state across pytest test cases
- **Fix:** Added `Registry.reset()` method + called it in test fixture before each test
- **Files modified:** orchestrator.py, tests/test_orchestrator.py
- **Commit:** 845fa7c

## Self-Check: PASSED

- orchestrator.py exists: FOUND
- tests/test_orchestrator.py exists: FOUND
- Commits 99ee868, 845fa7c: FOUND
- `@app.websocket("/ws")`: FOUND (line 134)
- `@app.get("/api/stations")`: FOUND (line 128)
- `asyncio.Lock()`: FOUND (line 43)
- `close(code=4003)`: FOUND (line 137)
- `close(code=4000)`: FOUND (line 180)
- No blocking `requests` import: CONFIRMED (0 matches)
- `python3 -c "from orchestrator import app; print(type(app).__name__)"` → FastAPI: CONFIRMED
