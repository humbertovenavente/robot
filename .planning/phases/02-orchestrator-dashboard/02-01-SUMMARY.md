---
phase: 02-orchestrator-dashboard
plan: "01"
subsystem: protocol-config
tags: [ws_protocol, config, pydantic, json-schema, phase2]
dependency_graph:
  requires: []
  provides:
    - ws_protocol.py (Pydantic message models: RegisterMsg, StatusMsg, StationUpdateMsg)
    - Config.orchestrator_url (optional field, default None)
    - Config.orchestrator_enabled (bool property)
  affects:
    - Plan 02-02 (orchestrator.py imports from ws_protocol)
    - Plan 02-03 (ws_client.py imports from ws_protocol + uses config.orchestrator_url)
tech_stack:
  added:
    - pydantic==2.13.0 (already pinned, now used for ws_protocol models)
    - uvicorn[standard]==0.39.0
    - websockets==15.0.1
    - Jinja2==3.1.6
    - httpx==0.28.1
    - pytest-asyncio==0.24.0
  patterns:
    - Pydantic v2 BaseModel with Literal type discriminators
    - Union[RegisterMsg, StatusMsg] for inbound dispatch
    - Optional[str] config field with bool property guard
key_files:
  created:
    - ws_protocol.py
    - tests/test_ws_protocol.py
    - tests/test_config_orchestrator_url.py
  modified:
    - config.py
    - station_config.yaml
    - requirements.txt
decisions:
  - "ws_protocol.py has zero imports from fastapi/uvicorn/websockets — pure Pydantic+stdlib so station client never pulls in server deps"
  - "orchestrator_enabled property uses bool(url and url.strip()) to treat both None and whitespace-only as standalone mode (D-06)"
  - "decode_inbound wraps JSONDecodeError as ValueError to give callers a single exception type"
metrics:
  duration_seconds: 124
  completed_date: "2026-04-14"
  tasks_completed: 2
  tasks_total: 2
  files_created: 3
  files_modified: 3
  tests_added: 15
---

# Phase 2 Plan 01: Protocol Config Summary

Established shared WebSocket protocol contract and config plumbing using Pydantic v2 message models and an extended Config class. Plans 02-02 (orchestrator) and 02-03 (ws_client) can now both `from ws_protocol import ...` without any shared imports between each other.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Create ws_protocol.py with Pydantic message models | 6b12121 | ws_protocol.py, tests/test_ws_protocol.py |
| 2 | Extend Config with orchestrator_url + update station_config.yaml + requirements.txt | 60311e1 | config.py, station_config.yaml, requirements.txt, tests/test_config_orchestrator_url.py |

## ws_protocol.py Model Field List

### RegisterMsg
- `type: Literal["register"] = "register"`
- `station_id: str`

### StatusMsg
- `type: Literal["status"] = "status"`
- `station_id: str`
- `status: str` — `free | processing | unknown_package | error`
- `last_class: Optional[str] = None`
- `last_destination: Optional[int] = None`
- `last_cycle_ms: Optional[int] = None`
- `cycle_count: int = 0`

### StationUpdateMsg
- `type: Literal["station_update"] = "station_update"`
- `station_id: str`
- `status: str`
- `last_class: Optional[str] = None`
- `last_destination: Optional[int] = None`
- `last_cycle_ms: Optional[int] = None`
- `cycle_count: int = 0`
- `online: bool = True`

## JSON Wire Shapes (sample encode() output)

**RegisterMsg:**
```json
{"type":"register","station_id":"station-1"}
```

**StatusMsg (full fields):**
```json
{"type":"status","station_id":"station-1","status":"processing","last_class":"A","last_destination":3,"last_cycle_ms":420,"cycle_count":7}
```

**StationUpdateMsg (defaults):**
```json
{"type":"station_update","station_id":"station-1","status":"free","last_class":null,"last_destination":null,"last_cycle_ms":null,"cycle_count":0,"online":true}
```

## Config.orchestrator_url

- Field: `orchestrator_url: Optional[str] = None` — placed after `robot_implementation`, before `class_to_bin`
- Property: `orchestrator_enabled -> bool` — `True` iff `orchestrator_url` is non-empty and non-whitespace
- Default `None` preserves Phase 1 behavior: `load_config()` on the existing `station_config.yaml` gives `orchestrator_enabled = False`
- Phase 1 regression confirmed: `tests/test_station.py` — 6 passed

## Test Results

```
tests/test_ws_protocol.py      10 passed
tests/test_config_orchestrator_url.py  5 passed
tests/test_station.py           6 passed
Total: 21 passed
```

## Deviations from Plan

None — plan executed exactly as written. TDD RED→GREEN cycle followed for both tasks.

## Self-Check: PASSED

- ws_protocol.py exists: FOUND
- tests/test_ws_protocol.py exists: FOUND
- tests/test_config_orchestrator_url.py exists: FOUND
- config.py has orchestrator_url + orchestrator_enabled: FOUND
- station_config.yaml has commented orchestrator_url: FOUND
- requirements.txt has uvicorn/websockets/Jinja2: FOUND
- Commits 6b12121, 60311e1: FOUND
