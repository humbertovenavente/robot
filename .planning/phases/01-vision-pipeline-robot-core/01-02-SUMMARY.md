---
phase: 01-vision-pipeline-robot-core
plan: "02"
subsystem: robot-abstraction
tags: [robot, protocol, stub, abstraction, tdd]
dependency_graph:
  requires: [01-01-scaffolding]
  provides: [RobotInterface, StubRobot, build_robot, EV3Robot-placeholder, SpikeRobot-placeholder]
  affects: [01-04-vision-qr-pipeline, 01-05-station-loop-logging, 01-07-real-robot]
tech_stack:
  added: []
  patterns: [Protocol runtime_checkable, factory-function, encoder-based-motion, TDD-red-green]
key_files:
  created:
    - robot.py
    - ev3_robot.py
    - spike_robot.py
    - tests/__init__.py
    - tests/test_stub_robot.py
  modified:
    - requirements.txt
decisions:
  - "StubRobot simulated sleep capped at 0.5s per travel segment so tests run fast"
  - "EV3Robot and SpikeRobot raise NotImplementedError on __init__ to fail loudly not silently"
  - "RobotInterface uses @runtime_checkable Protocol enabling isinstance() checks without inheritance"
metrics:
  duration: "~15 minutes"
  completed: "2026-04-14T05:46:40Z"
  tasks_completed: 2
  files_created: 5
  files_modified: 1
---

# Phase 01 Plan 02: Robot Abstraction Summary

**One-liner:** `@runtime_checkable` RobotInterface Protocol with StubRobot (encoder-based simulation) and NotImplementedError placeholder adapters for EV3/SPIKE, validated by 8-test pytest suite.

## What Was Built

### RobotInterface (robot.py)

`@runtime_checkable Protocol` with 6 method signatures:

```python
def calibrate_home(self) -> None
def move_to_bin(self, bin_index: int) -> None
def deposit(self) -> None
def return_home(self) -> None
def get_current_position(self) -> int
def shutdown(self) -> None
```

Config fields the interface depends on:
- `home_encoder_target: int` — reset target for calibrate_home() and return_home()
- `bin_encoder_targets: Dict[int, int]` — maps bin index → encoder degrees for move_to_bin()
- `motor_speed_deg_per_sec: int` — controls simulated travel duration in StubRobot

### StubRobot (robot.py)

- Encoder-based simulation (D-09): all motion uses `bin_encoder_targets` / `home_encoder_target`, never hardcoded durations
- Travel time = `abs(delta_degrees) / motor_speed_deg_per_sec`, capped at 0.5s for fast tests
- `move_to_bin(unknown_index)` raises `ValueError` — T-01-02-02 DoS mitigation
- `shutdown()` is idempotent — safe for `try/finally` cleanup in station loop (Pitfall #13)
- D-10 compliant: no cycle-lock logic here; station loop owns it

### build_robot() factory (robot.py)

Dispatches on `config.robot_implementation` (`"stub"` | `"ev3"` | `"spike"`). Raises `RuntimeError` on unknown value. Key link: `robot.build_robot` → `config.Config.robot_implementation`.

### EV3Robot (ev3_robot.py)

Placeholder — `__init__` raises `NotImplementedError` with message pointing to Plan 07 and `ev3dev2-over-SSH`. Method stubs present for type-checker compatibility.

### SpikeRobot (spike_robot.py)

Placeholder — `__init__` raises `NotImplementedError` with message pointing to Plan 07 and `pybricks BLE/serial`. Method stubs present for type-checker compatibility.

## Test Suite

7 test functions in `tests/test_stub_robot.py` (8 functions total, all passing):

1. `test_stub_satisfies_protocol` — StubRobot passes `isinstance(r, RobotInterface)`
2. `test_calibrate_home_resets_position` — after move, calibrate_home resets to `home_encoder_target`
3. `test_move_to_bin_updates_position` — position matches `bin_encoder_targets[n]` after move
4. `test_unknown_bin_raises` — `move_to_bin(99)` raises `ValueError`
5. `test_return_home` — after move, `return_home()` resets to `home_encoder_target`
6. `test_shutdown_idempotent` — calling `shutdown()` twice does not raise
7. `test_build_robot_returns_stub_for_stub_config` — factory returns `StubRobot` for default config
8. `test_build_robot_raises_for_ev3_placeholder` — factory raises `NotImplementedError` for `ev3` config

**Plan 07 requirement:** The real EV3/SPIKE implementation must pass all 8 tests with no modifications.

## Commits

| Task | Commit | Message |
|------|--------|---------|
| RED (TDD tests) | 6c30da5 | test(01-02): add failing tests for StubRobot RobotInterface protocol |
| GREEN (implementation) | 5da944e | feat(01-02): implement RobotInterface protocol and StubRobot |
| Task 2 | f9c602b | feat(01-02): add EV3/SPIKE placeholder adapters and complete test suite |

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None. StubRobot is intentionally a first-class simulation (D-12), not a data stub. All methods are fully functional.

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes introduced. `move_to_bin` ValueError mitigation (T-01-02-02) is in place.

## Self-Check: PASSED
