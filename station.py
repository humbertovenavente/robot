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
import time

import cv2
import numpy as np

from config import Config, load_config
from robot import RobotInterface, build_robot
from vision import VisionPipeline, Detection, load_vision
from qr import decode_from_frame
from event_log import EventLogger, LogEntry

log = logging.getLogger(__name__)


@dataclass
class StationState:
    """Public state exposed for Phase 2 orchestrator wiring."""
    station_id: str
    status: str = "free"                 # free | processing | unknown_package | error
    last_class: Optional[str] = None
    last_destination: Optional[int] = None
    last_cycle_ms: Optional[int] = None
    cycle_count: int = 0


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

    def _set_status(self, status: str) -> None:
        self.state.status = status
        if self.listener is not None:
            try:
                self.listener(self.state)
            except Exception as e:
                log.warning("status_listener raised: %s", e)

    def _run_cycle(self, frame: np.ndarray, detection: Detection) -> LogEntry:
        """Execute one detection-to-home cycle. Writes exactly one log entry.
        Cycle lock owner: try/finally releases lock on any exit (PITFALLS #8)."""
        self._cycle_lock = True
        cycle_start = time.monotonic()
        self._set_status("processing")
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
                return entry

            if class_letter not in self.config.class_to_bin:
                elapsed_ms = int((time.monotonic() - cycle_start) * 1000)
                self._set_status("unknown_package")
                entry = self.log.write("unknown", None, elapsed_ms, "unknown_package",
                                       error=f"class_not_mapped:{class_letter}")
                return entry

            dest_bin = self.config.class_to_bin[class_letter]

            # Robot cycle (ROB-01..03). Any exception below is caught and logged as error.
            try:
                self.robot.move_to_bin(dest_bin)
                self.robot.deposit()
                self.robot.return_home()
            except Exception as e:
                elapsed_ms = int((time.monotonic() - cycle_start) * 1000)
                self._set_status("error")
                log.exception("Robot cycle failed")
                return self.log.write(class_letter, dest_bin, elapsed_ms, "error", error=str(e))

            elapsed_ms = int((time.monotonic() - cycle_start) * 1000)
            self.state.last_class = class_letter
            self.state.last_destination = dest_bin
            self.state.last_cycle_ms = elapsed_ms
            self.state.cycle_count += 1
            self._set_status("free")
            entry = self.log.write(class_letter, dest_bin, elapsed_ms, "completed", None)
            return entry
        finally:
            self._cycle_lock = False   # PITFALLS #8: always release

    def run_once(self, frame: np.ndarray):
        """Process one frame. Returns (state, log_entry_if_written).
        Returns (state, None) if no detection or already locked."""
        if self._cycle_lock:
            return (self.state, None)   # ROB-04: ignore detections mid-cycle
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    station = run_station()
    station.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
