---
phase: 02-orchestrator-dashboard
plan: "04"
subsystem: dashboard-ui
tags: [dashboard, jinja2, vanilla-js, css-grid, websocket, fastapi-static]
dependency_graph:
  requires: [02-02, 02-03]
  provides: [DSH-01, DSH-02]
  affects: [orchestrator.py]
tech_stack:
  added: [Jinja2Templates, StaticFiles, vanilla-JS-WS-client]
  patterns: [CSS-grid-auto-fit, exponential-backoff-reconnect, in-place-DOM-update]
key_files:
  created:
    - templates/dashboard.html
    - static/dashboard.css
    - static/dashboard.js
    - tests/test_dashboard_routes.py
  modified:
    - orchestrator.py
decisions:
  - "Plain CSS grid with auto-fit minmax(260px,1fr) satisfies D-14 responsive 1-4 columns with zero build step"
  - "IIFE + ES5-compatible JS (var, addEventListener) for broadest demo-day laptop support"
  - "TemplateResponse(request, name) positional API used to avoid Starlette deprecation warning"
metrics:
  duration: "~15 min"
  completed: "2026-04-13"
  tasks_completed: 2
  files_changed: 5
---

# Phase 2 Plan 04: Dashboard UI Summary

Single-page live dashboard served by the FastAPI orchestrator using Jinja2 template + plain CSS grid + vanilla JS WebSocket client. No npm, no React, no build step.

## What Was Built

### File Inventory

| File | Role |
|------|------|
| `templates/dashboard.html` | Jinja2 template: title, topbar, disconnect banner, empty cards container, single `<script>` tag |
| `static/dashboard.css` | Pure CSS: dark theme, CSS grid layout, 4 badge color classes, offline overlay, hidden banner |
| `static/dashboard.js` | Vanilla JS IIFE: WebSocket client, card create/update, 1-8s reconnect backoff |
| `orchestrator.py` | Added `StaticFiles` mount at `/static`, `Jinja2Templates`, `GET /` returning `dashboard.html` |
| `tests/test_dashboard_routes.py` | 4 integration tests: HTML served, JS served, CSS served, WS snapshot + live update |

### Badge Color Choices (hex)

| Status | Background | Text |
|--------|-----------|------|
| `free` | `#22c55e` (green-500) | `#052e16` (green-950) |
| `processing` | `#3b82f6` (blue-500) | `#0c1e42` (blue-950) |
| `unknown_package` | `#eab308` (yellow-500) | `#3a2a05` (amber-950) |
| `error` | `#ef4444` (red-500) | `#3b0a0a` (red-950) |

Colors follow conventional traffic-light semantics (green/blue/yellow/red) visible from across a room on demo day.

### Browser Compatibility

`dashboard.js` uses only ES5 constructs (`var`, `function`, `addEventListener`, `JSON.parse`, `classList`) and the WebSocket API. Compatible with any Chromium-based or Firefox browser from the past 10 years. No polyfills required. Unicode arrow `\u2192` used in JS string to avoid HTML entity issues.

### How to Smoke-Test on Demo Day

```bash
# 1. Start the orchestrator (from the host machine):
uvicorn orchestrator:app --host 0.0.0.0 --port 8000

# 2. Verify the REST snapshot (curl-friendly):
curl http://localhost:8000/api/stations

# 3. Open the dashboard in any browser on the LAN:
# http://<host-machine-IP>:8000/

# 4. Connect a station (from another terminal or laptop):
# python3 -c "from ws_client import StationWsClient; ..."
# Or run the full station process with orchestrator_url configured in station_config.yaml.

# 5. Confirm a card appears with the station's status badge.
# 6. Stop uvicorn → banner "Disconnected — reconnecting…" appears.
# 7. Restart uvicorn → banner disappears, cards restore last-known state.
```

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed Starlette TemplateResponse deprecated signature**
- **Found during:** Task 2 (TDD GREEN phase) — `DeprecationWarning` in test output
- **Issue:** `TemplateResponse("dashboard.html", {"request": request})` uses the old name-first signature deprecated in Starlette; raises a warning and will break in a future release
- **Fix:** Changed to `TemplateResponse(request, "dashboard.html")` (new positional API)
- **Files modified:** `orchestrator.py`
- **Commit:** `4da0320`

## Known Stubs

None — all card fields (`station_id`, `status`, `last_class`, `last_destination`, `online`) are wired to live WebSocket data from the orchestrator. The `cycle_count` and `last_cycle_ms` fields are intentionally omitted from card display per D-15 (deferred to v1.1).

## Threat Flags

None — no new network endpoints beyond `GET /` and `GET /static/*` (read-only static files). The dashboard WS path `/ws?role=dashboard` is receive-only (D-16) and was already in scope from Plan 02-02.

## Self-Check: PASSED

| Item | Status |
|------|--------|
| `templates/dashboard.html` | FOUND |
| `static/dashboard.css` | FOUND |
| `static/dashboard.js` | FOUND |
| `tests/test_dashboard_routes.py` | FOUND |
| Commit `3918506` (Task 1) | FOUND |
| Commit `4da0320` (Task 2 + Rule 1 fix) | FOUND |
