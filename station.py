"""Single-station cycle loop. Wires camera->vision->qr->robot->log with local cycle lock.

Phase 1 success criteria covered:
 1. Package detected -> class string in terminal within ~2s (detect + decode)
 2. Robot picks up, moves to bin, deposits, returns home (robot cycle)
 3. Every cycle writes JSONL entry with class + dest + time (event_log.write)
 5. Unknown package -> log + skip robot (D-06 branch)

Success criterion 4 (calibration +/-2 deg) lives in Plan 06. This loop CALLS
robot.calibrate_home() on startup (ROB-05 hook) but the routine itself is Plan 06.

Phase 2 handoff: StationState + status_listener param (per 01-CONTEXT integration_points).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Optional
import logging
import signal
import sys
import threading
import time

import cv2
import numpy as np

from config import Config, load_config
from robot import RobotInterface, build_robot
from vision import VisionPipeline, Detection, load_vision
from qr import decode_from_frame
from event_log import EventLogger, LogEntry
from vision_confirm import find_robot_qr, compute_drift
from obstacle_detector import build_obstacle_detector

log = logging.getLogger(__name__)


@dataclass
class StationState:
    """Public state exposed for Phase 2 orchestrator wiring."""
    station_id: str
    status: str = "free"                 # free | processing | unknown_package | error | path_blocked
    last_class: Optional[str] = None
    last_destination: Optional[int] = None
    last_cycle_ms: Optional[int] = None
    cycle_count: int = 0
    path_blocked: bool = False           # OBS-01: True while travel zone is occupied
    blocking_object: Optional[str] = None  # COCO class name of blocking object


StatusListener = Callable[[StationState], None]


class Station:
    def __init__(
        self,
        config: Config,
        robot: RobotInterface,
        vision: VisionPipeline,
        event_logger: EventLogger,
        status_listener: Optional[StatusListener] = None,
    ):
        self.config = config
        self.robot = robot
        self.vision = vision
        self.log = event_logger
        self.listener = status_listener
        self.state = StationState(station_id=getattr(config, "station_id", "station-1"))
        self._cycle_lock = False        # D-10 / ROB-04: local cycle lock
        self._stop = False
        self._halted = False           # D-04: permanent halt flag (ERR-02/03)
        self._obstacle_detector = build_obstacle_detector(config)  # OBS-01; None if disabled
        self._obs_blocked_streak: int = 0   # consecutive frames with obstacle
        self._obs_clear_streak: int = 0     # consecutive frames without obstacle
        # D-17: one-time startup warning when enabled but no targets calibrated
        if config.vision_confirm_enabled:
            targets = config.robot_vision_targets or {}
            if not any(v is not None for v in targets.values()):
                log.warning(
                    "vision_confirm_enabled=true but no targets calibrated — skipping checks"
                )

    def _vision_check(self, frame, target_name: str):
        """Return (vision_confirmed, drift_px, vision_reason).
        Called only when config.vision_confirm_enabled is True.
        Reuses the current cycle's frame — Station has no self._camera (D-19
        forbids Phase 2 plumbing changes; camera stays local to run())."""
        expected = (self.config.robot_vision_targets or {}).get(target_name)
        if expected is None:
            return (None, None, "no_target_calibrated")   # D-17
        try:
            if frame is None:
                return (False, None, "robot_qr_not_found")
            observed = find_robot_qr(frame)
            if observed is None:
                return (False, None, "robot_qr_not_found")   # D-09
            drift = compute_drift(observed, tuple(expected))
            if drift is None:
                return (False, None, "robot_qr_not_found")
            tol = self.config.vision_confirm_tolerance_px
            if drift <= tol:
                return (True, drift, "ok")
            return (False, drift, "drift_exceeded")          # D-14
        except Exception as e:                                # D-15 observability-only
            return (False, None, f"error:{type(e).__name__}")

    def _set_status(self, status: str) -> None:
        self.state.status = status
        if self.listener is not None:
            try:
                self.listener(self.state)
            except Exception as e:
                log.warning("status_listener raised: %s", e)

    def _halt(self, reason: str) -> None:
        """Mark station permanently halted. Called from watchdog timer thread or robot-exception handler.
        Thread-safe: GIL protects bool assignment and _set_status (D-03, D-07).
        Process restart required to resume cycles."""
        self._cycle_lock = False     # release lock so callers waiting on it don't deadlock
        self._halted = True
        self._set_status("error")
        log.error("Station %s halted: %s", self.state.station_id, reason)

    def _watchdog_fire(self, cls: str, dest_bin, cycle_start: float) -> None:
        """Called by threading.Timer when a cycle exceeds cycle_watchdog_timeout_s.
        Writes the JSONL error entry, then halts. (D-01, D-03)"""
        elapsed_ms = int((time.monotonic() - cycle_start) * 1000)
        try:
            self.log.write(cls, dest_bin, elapsed_ms, "error", error="watchdog_timeout")
        except Exception:
            pass   # do not mask the halt
        self._halt("watchdog_timeout")

    def _run_cycle(self, frame: np.ndarray, detection: Detection) -> LogEntry:
        """Execute one detection-to-home cycle. Writes exactly one log entry.
        Cycle lock owner: try/finally releases lock on any exit (PITFALLS #8)."""
        self._cycle_lock = True
        cycle_start = time.monotonic()
        self._set_status("processing")
        _watchdog = threading.Timer(
            self.config.cycle_watchdog_timeout_s,
            self._watchdog_fire,
            args=["unknown", None, cycle_start],
        )
        _watchdog.daemon = True
        _watchdog.start()
        try:
            # PITFALLS #3: settle delay before QR decode
            settle_ms = getattr(self.config, "qr_settle_delay_ms", 150)
            time.sleep(settle_ms / 1000.0)

            class_letter = decode_from_frame(frame, detection.bbox, self.config)
            if class_letter is None:
                # VIS-04 / D-06: unknown package -- no robot motion
                self.state.last_class = "unknown"
                self.state.last_destination = None
                elapsed_ms = int((time.monotonic() - cycle_start) * 1000)
                self.state.last_cycle_ms = elapsed_ms
                self._set_status("unknown_package")
                entry = self.log.write("unknown", None, elapsed_ms, "unknown_package",
                                       error="yolo_hit_but_qr_fail")
                self._set_status("free")
                return entry

            if class_letter not in self.config.class_to_bin:
                elapsed_ms = int((time.monotonic() - cycle_start) * 1000)
                self._set_status("unknown_package")
                entry = self.log.write("unknown", None, elapsed_ms, "unknown_package",
                                       error=f"class_not_mapped:{class_letter}")
                self._set_status("free")
                return entry

            dest_bin = self.config.class_to_bin[class_letter]

            # Robot cycle (ROB-01..03). Any exception below is caught and logged as error.
            try:
                self.robot.move_to_bin(dest_bin)
                if self.config.vision_confirm_enabled:
                    vc_bin = self._vision_check(frame, f"bin_{dest_bin}")
                else:
                    vc_bin = (None, None, None)
                self.robot.deposit()
                self.robot.return_home()
                if self.config.vision_confirm_enabled:
                    vc_home = self._vision_check(frame, "home")
                else:
                    vc_home = (None, None, None)
            except Exception as e:
                elapsed_ms = int((time.monotonic() - cycle_start) * 1000)
                log.exception("Robot cycle failed")
                self._halt(f"robot_exception:{type(e).__name__}")
                return self.log.write(class_letter, dest_bin, elapsed_ms, "error", error=str(e))

            elapsed_ms = int((time.monotonic() - cycle_start) * 1000)
            self.state.last_class = class_letter
            self.state.last_destination = dest_bin
            self.state.last_cycle_ms = elapsed_ms
            self.state.cycle_count += 1
            # D-03: if watchdog fired mid-cycle and set _halted, do not overwrite "error"
            if not self._halted:
                self._set_status("free")
            entry = self.log.write(
                class_letter, dest_bin, elapsed_ms, "completed", None,
                vision_confirmed=vc_home[0],
                drift_px=vc_home[1],
                vision_reason=vc_home[2],
            )
            return entry
        finally:
            _watchdog.cancel()
            self._cycle_lock = False   # PITFALLS #8: always release

    def run_once(self, frame: np.ndarray):
        """Process one frame. Returns (state, log_entry_if_written).
        Returns (state, None) if no detection or already locked."""
        if self._cycle_lock:
            return (self.state, None)   # ROB-04: ignore detections mid-cycle
        if self._halted:
            return (self.state, None)   # D-04: permanent halt — frames consumed, cycles suppressed

        # OBS-01: obstacle check with debounce to prevent single-frame flicker
        if self._obstacle_detector is not None:
            blocked, obj_name = self._obstacle_detector.is_blocked(frame)
            if blocked:
                self._obs_blocked_streak += 1
                self._obs_clear_streak = 0
                if self._obs_blocked_streak >= self.config.obstacle_block_frames:
                    if not self.state.path_blocked:
                        self.state.path_blocked = True
                        self.state.blocking_object = obj_name
                        self._set_status("path_blocked")
                        log.warning("Path blocked by '%s' — robot paused", obj_name)
                    return (self.state, None)
                # streak not yet met — treat as clear for now
            else:
                self._obs_clear_streak += 1
                self._obs_blocked_streak = 0
                if self.state.path_blocked:
                    if self._obs_clear_streak >= self.config.obstacle_clear_frames:
                        self.state.path_blocked = False
                        self.state.blocking_object = None
                        self._set_status("free")
                        log.info("Path clear — resuming cycles")
                    else:
                        return (self.state, None)  # still in clear grace period

        detection = self.vision.detect(frame)
        if detection is None:
            return (self.state, None)
        entry = self._run_cycle(frame, detection)
        return (self.state, entry)

    def run(self, loop_count: Optional[int] = None) -> None:
        """Main camera polling loop. loop_count=None means run forever."""
        log.info("Station %s starting. robot_impl=%s",
                 self.state.station_id,
                 getattr(self.config, "robot_implementation", "stub"))
        self.robot.calibrate_home()   # ROB-05 hook

        cap = cv2.VideoCapture(self.config.camera_index)
        if not cap.isOpened():
            log.error("Could not open camera %d", self.config.camera_index)
            self._set_status("error")
            return

        iterations = 0
        try:
            while not self._stop:
                if loop_count is not None and iterations >= loop_count:
                    break
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                self.run_once(frame)
                iterations += 1
                time.sleep(0.05)
        finally:
            cap.release()
            self.robot.shutdown()
            self.log.close()

    def stop(self) -> None:
        self._stop = True


def run_station(
    config: Optional[Config] = None,
    status_listener: Optional[StatusListener] = None,
) -> Station:
    cfg = config or load_config()
    if status_listener is None and getattr(cfg, "orchestrator_enabled", False):
        from ws_client import build_status_listener  # lazy import — D-06 / ORC-04
        status_listener = build_status_listener(cfg)
    robot = build_robot(cfg)
    vision = load_vision(cfg)
    log_dir = getattr(cfg, "resolved_log_dir", None) or "logs"
    event_logger = EventLogger(log_dir)
    station = Station(cfg, robot, vision, event_logger, status_listener=status_listener)

    def _handle_signal(signum, _frame):
        log.info("Received signal %d, stopping station", signum)
        station.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    return station


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None, help="Path to station config YAML")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config) if args.config else load_config()
    station = run_station(config=cfg)
    station.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
