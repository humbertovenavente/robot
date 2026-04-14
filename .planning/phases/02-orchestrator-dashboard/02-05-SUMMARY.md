---
phase: 02-orchestrator-dashboard
plan: 05
subsystem: testing
tags: [pytest, fastapi, websocket, testclient, integration-tests, tdd]

# Dependency graph
requires:
  - phase: 02-orchestrator-dashboard
    provides: orchestrator.py FastAPI app, ws_client.StationWsClient, build_status_listener, ws_protocol message models
  - phase: 01-vision-pipeline-robot-core
    provides: StationState dataclass, run_station() factory, status_listener hook contract

provides:
  - "End-to-end Phase 2 test suite (tests/test_phase2_integration.py) proving all 4 ROADMAP success criteria"
  - "ORC-03 concurrent two-station test: registration + broadcast + REST snapshot"
  - "ORC-04 standalone + orchestrator-down resilience tests"
  - "Phase 1 regression guard: run_station() with no orchestrator_url spawns no ws thread"

affects: [03-watchdog-error-ux, phase3-handoff]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "FastAPI TestClient + websocket_connect for integration testing of WS endpoints"
    - "Registry.reset() in client fixture ensures test isolation"
    - "StationWsClient pointed at 127.0.0.1:1 for orchestrator-down resilience test (unreachable port)"
    - "TDD: test file written first, verified all 7 pass against existing implementation"

key-files:
  created:
    - tests/test_phase2_integration.py
  modified: []

key-decisions:
  - "Added registry.reset() in client fixture — same pattern as test_orchestrator.py to ensure clean state per test"
  - "Used _Cfg helper class (matching test_ws_client.py _StubConfig pattern) to avoid importing Config for standalone tests"
  - "Orchestrator-down test uses port 1 (always closed/refused on macOS) rather than a random ephemeral port"

patterns-established:
  - "Integration test fixture: import orchestrator module, call registry.reset(), wrap with TestClient context manager"
  - "Standalone listener test: _Cfg.orchestrator_enabled returns False for None/whitespace url — no _client attribute attached"

requirements-completed: [ORC-03, ORC-04]

# Metrics
duration: 10min
completed: 2026-04-13
---

# Phase 02 Plan 05: Integration Tests Summary

**7 end-to-end integration tests prove all 4 Phase 2 ROADMAP criteria via FastAPI TestClient WebSocket connections against the live orchestrator, ws_client backoff, and standalone no-op listener**

## Performance

- **Duration:** ~10 min
- **Started:** 2026-04-13
- **Completed:** 2026-04-13
- **Tasks:** 1 (TDD — write tests, verify GREEN against existing implementation)
- **Files modified:** 1

## Accomplishments

- Wrote `tests/test_phase2_integration.py` with 7 tests covering all Phase 2 ROADMAP success criteria
- All 7 new tests pass; full suite grows from 74 to 81 passing tests with zero regressions
- ORC-03 (concurrent stations) proven: two `/ws?role=station` sockets + one dashboard, interleaved StatusMsg, both seen in dashboard, REST confirms both in registry
- ORC-04 (standalone + orchestrator-down) proven: `build_status_listener` with None/whitespace url returns no-op; `StationWsClient` pointed at closed port starts/enqueues/stops without raising

## Test Inventory

| Test name | ROADMAP criterion | Requirement |
|-----------|-------------------|-------------|
| `test_two_stations_concurrent_broadcast_to_dashboard` | #1 Two stations register, dashboard sees both immediately; #3 concurrent without interference | ORC-03 |
| `test_dashboard_receives_live_updates_in_order` | #2 State changes update dashboard live without page reload | ORC-03 |
| `test_standalone_listener_opens_no_socket` | #4 Killing orchestrator does not stop station (standalone mode) | ORC-04 |
| `test_empty_url_is_standalone` | #4 Whitespace url treated same as None | ORC-04 |
| `test_client_survives_unreachable_orchestrator` | #4 Station ws_client retries silently, stop() returns cleanly | ORC-04 |
| `test_backoff_schedule_is_capped_at_8s` | Design decision D-08 enforced | ORC-04 |
| `test_phase1_station_standalone_still_works_when_orchestrator_url_unset` | Phase 1 regression guard: no extra threads when orchestrator_url is commented out | ORC-04 |

## Task Commits

1. **Task 1: End-to-end Phase 2 integration tests** - `197a766` (feat)

**Plan metadata commit:** (following this summary)

## Files Created/Modified

- `tests/test_phase2_integration.py` — 7 integration tests covering Phase 2 ROADMAP success criteria (163 lines)

## Decisions Made

- Added `registry.reset()` in the `client` fixture to ensure test isolation, matching the pattern from `test_orchestrator.py`
- Used `_Cfg` helper class (not importing Config from config.py) to keep standalone-mode tests lightweight with no YAML file dependency
- Orchestrator-down resilience test uses `ws://127.0.0.1:1/ws` — port 1 is always connection-refused on macOS/Linux, making the test deterministic and fast

## Deviations from Plan

None - plan executed exactly as written. The test file matches the plan's `<action>` block with one intentional addition: `registry.reset()` in the client fixture (same pattern established in `test_orchestrator.py` for test isolation).

## Flakiness Notes

No flakiness observed on the executor machine (macOS 25.3.0, Python 3.9.6). The orchestrator-down test (`test_client_survives_unreachable_orchestrator`) completed in under 200ms — the ws_client thread starts, immediately fails to connect to port 1, and stop() signals it within the 3s budget. Backoff sleep of 1s is interrupted by the stop flag so stop() returns in ~50ms.

## Manual Smoke Test Recipe (Demo Rehearsal)

Run all three processes in separate terminals on the orchestrator host machine:

```bash
# Terminal 1 — orchestrator
uvicorn orchestrator:app --host 0.0.0.0 --port 8000

# Terminal 2 — station A (edit station_config.yaml: uncomment orchestrator_url)
python3 -m station

# Terminal 3 — station B (use station_config.local.yaml with station_id: station-2)
python3 station.py
```

Then open `http://localhost:8000` in a browser. Expected: two station cards appear within 1s, status badges update in real-time as each station's YOLO/QR cycle progresses.

For other laptops on the LAN: replace `localhost` with the host machine's LAN IP (e.g., `http://192.168.1.10:8000`). Verify from each station laptop that the orchestrator is reachable: `curl http://192.168.1.10:8000/api/stations`.

## Phase 3 Handoff Note

Phase 3 (watchdog + error UX, ERR-01/02/03) will reuse the same `status_listener` path established in Phase 2:
- `station.py` already calls `listener(state)` on every `_set_status()` call including `"error"` status
- `ws_client.StationWsClient.enqueue_state()` will forward the error state to the orchestrator without any Phase 2 changes
- Dashboard already handles `status="error"` with a red badge (Plan 04 CSS)
- **No Phase 2 changes required for Phase 3 error wiring.** Phase 3 only needs to set `status="error"` in the right places in `station.py` and add ERR-01/02/03 test coverage.

## Issues Encountered

None.

## Next Phase Readiness

Phase 2 is complete and ready to ship. All 4 ROADMAP success criteria have passing automated tests.

Phase 3 (watchdog + error UX) can start immediately:
- `status_listener` path is proven and stable
- Orchestrator broadcasts `"error"` status correctly (already handled in `_handle_station`)
- Dashboard renders error badge in red (Plan 04)
- Test patterns established: `client` fixture + TestClient WS for orchestrator tests, `_Cfg` helper for ws_client standalone tests

---
*Phase: 02-orchestrator-dashboard*
*Completed: 2026-04-13*
