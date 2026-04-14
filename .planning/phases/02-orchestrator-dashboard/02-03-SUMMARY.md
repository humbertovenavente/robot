---
phase: 02-orchestrator-dashboard
plan: "03"
subsystem: station-ws-client
tags: [websocket, asyncio, threading, backoff, standalone-mode]
dependency_graph:
  requires: [02-01, 02-02]
  provides: [ws_client.build_status_listener, StationWsClient]
  affects: [station.run_station, orchestrator inbound registration]
tech_stack:
  added: [websockets (lazy import via _connect_and_pump)]
  patterns: [background-thread-asyncio-loop, coalescing-queue, exponential-backoff, lazy-factory]
key_files:
  created:
    - ws_client.py
    - tests/test_ws_client.py
  modified:
    - station.py (run_station only — main loop untouched)
decisions:
  - "Lazy websockets import inside _connect_and_pump keeps ws_client importable in CI without the websockets package"
  - "Coalesce to latest-wins via single _pending_msg slot — dashboard only needs current state, not history"
  - "build_status_listener returns a plain _noop closure (no _client attr) for standalone mode — test assertion is hasattr check"
  - "start_in_thread is lazy (first listener call) so tests can construct StationWsClient without spawning threads"
metrics:
  duration_minutes: 15
  completed_date: "2026-04-13"
  tasks_completed: 1
  tasks_total: 1
  files_changed: 3
---

# Phase 02 Plan 03: Station WS Client Summary

Station-side WebSocket client in a background asyncio thread with exponential backoff, state coalescing, D-09 replay on reconnect, and ORC-04 standalone-mode safety net via no-op listener factory.

## What Was Built

### ws_client.py

**Listener factory signature:**
```python
build_status_listener(config: Config) -> Callable[[StationState], None]
```

Returns a no-op callable (no `_client` attribute) when `config.orchestrator_enabled` is False. Returns a `_listener` closure with `_listener._client = StationWsClient(...)` when enabled. The client is lazily started on first invocation.

**StationWsClient public surface:**
- `__init__(url: str, station_id: str)` — appends `?role=station` to URL if no `?` present
- `start_in_thread()` — spawns daemon thread running its own asyncio event loop
- `enqueue_state(state: StationState)` — thread-safe via `loop.call_soon_threadsafe`; coalesces to latest (single `_pending_msg` slot)
- `stop()` — signals stop flag, wakes event, joins thread (2s timeout)

### Thread + Loop Topology

```
station main thread
    │
    │  status_listener(state)          [_set_status hook, D-10 single funnel]
    ▼
ws_client._listener closure
    │
    │  loop.call_soon_threadsafe(_put)  [thread-safe handoff, D-07 no blocking I/O]
    ▼
ws-client daemon thread
    └── asyncio event loop
            └── _main() coroutine
                    └── _connect_and_pump()
                            └── websockets.connect(url?role=station)
                                    ├── send RegisterMsg(station_id)    [D-09 register]
                                    ├── send state_to_status_msg(last_state) [D-09 replay]
                                    └── loop: wait _state_event → send _pending_msg
```

### Backoff Schedule (D-08)

```
_BACKOFF_SCHEDULE = [1.0, 2.0, 4.0, 8.0]
```

Indexed by attempt count, capped at index 3 (8s). Resets to attempt=0 on clean disconnect (clean = no exception from `_connect_and_pump`). Retries forever — no max attempt count.

### station.py Change (run_station only)

```python
if status_listener is None and getattr(cfg, "orchestrator_enabled", False):
    from ws_client import build_status_listener  # lazy import — D-06 / ORC-04
    status_listener = build_status_listener(cfg)
```

The `Station` class, `_run_cycle`, `run_once`, `_set_status`, and `run()` are **unchanged** — regression guard confirmed by `grep -c` returning 3 (same as Phase 1).

### Coalescing Semantics

The `_pending_msg` slot holds at most one outbound JSON string. When `enqueue_state` is called twice before the asyncio loop drains it, the second call overwrites the first. This is safe for dashboard UX because the dashboard renders current state, not a changelog — showing only the latest transition is correct behavior and prevents queue buildup under rapid state changes.

## Test Coverage

| Test | Purpose |
|------|---------|
| `test_standalone_listener_is_noop` | orchestrator_url=None → no _client, no crash |
| `test_empty_string_url_is_standalone` | empty string treated as standalone |
| `test_enabled_listener_creates_client_and_coalesces` | coalesce: second enqueue replaces first |
| `test_url_gets_role_query_appended` | URL suffix `?role=station` applied |
| `test_backoff_schedule` | schedule is exactly [1.0, 2.0, 4.0, 8.0] |

## Phase 1 Regression

```
tests/test_station.py: 6 passed
tests/test_ws_client.py: 5 passed
tests/test_ws_protocol.py: 11 passed
tests/test_config_orchestrator_url.py: 4 passed
Total: 26 passed
```

Station main loop methods (`_run_cycle`, `run_once`, `run`) count: 3 — matches Phase 1 exactly.

## Standalone Mode Verification (ORC-04)

With `orchestrator_url` unset in `station_config.yaml`:
```
station.run_station().listener  →  None
```

The `_noop` closure is only stored in `build_status_listener`'s local scope; `Station.listener` remains `None` because `run_station` does not call `build_status_listener` when `orchestrator_enabled` is False. The station process starts, runs cycles, logs — zero WS activity.

## Deviations from Plan

None — plan executed exactly as written.

## Commits

| Commit | Type | Description |
|--------|------|-------------|
| `7700c9c` | test | Failing tests for ws_client (RED phase) |
| `c8ca13f` | feat | StationWsClient + run_station wiring (GREEN phase) |

## Self-Check: PASSED
