from __future__ import annotations
from typing import Protocol, runtime_checkable
import logging
import time
from config import Config

log = logging.getLogger(__name__)


@runtime_checkable
class RobotInterface(Protocol):
    """Contract every robot implementation must honor. Station loop depends only on this."""

    def calibrate_home(self) -> None:
        """Zero the encoders at the current physical home position. Call before each session (ROB-05)."""
        ...

    def move_to_bin(self, bin_index: int) -> None:
        """Drive from current position to the encoder target for bin_index. Encoder-based (D-09, Pitfall #5). Raises ValueError if bin_index unknown."""
        ...

    def deposit(self) -> None:
        """Release the package at the current position (D-07 pickup-and-carry)."""
        ...

    def return_home(self) -> None:
        """Drive back to home_encoder_target. Encoder-based (D-09)."""
        ...

    def get_current_position(self) -> int:
        """Current encoder reading in degrees. Used by calibration and diagnostics."""
        ...

    def move_to_qr_point(self) -> None:
        """Drive to the QR scan position when the station camera is first recognized. (CAM-01)"""
        ...

    def shutdown(self) -> None:
        """Release hardware resources (motor brake, close connection). Safe to call multiple times."""
        ...


class StubRobot:
    """In-memory simulated robot. D-12: first-class citizen, used whenever brick isn't present
    (dataset capture, vision dev, CI). Prints commands + simulates motion time."""

    def __init__(self, config: Config):
        self.config = config
        self._current_position = config.home_encoder_target
        self._shutdown = False
        log.info("StubRobot initialized at home=%d", self._current_position)

    def _simulate_travel(self, target_deg: int) -> None:
        travel = abs(target_deg - self._current_position)
        seconds = travel / max(self.config.motor_speed_deg_per_sec, 1)
        # Cap simulated sleep at 0.5s so tests run fast; real hardware is NOT capped.
        time.sleep(min(seconds, 0.5))
        self._current_position = target_deg

    def calibrate_home(self) -> None:
        log.info("StubRobot: calibrate_home -> %d", self.config.home_encoder_target)
        self._current_position = self.config.home_encoder_target

    def move_to_bin(self, bin_index: int) -> None:
        if bin_index not in self.config.bin_encoder_targets:
            raise ValueError(f"Unknown bin_index {bin_index}; known: {list(self.config.bin_encoder_targets)}")
        target = self.config.bin_encoder_targets[bin_index]
        log.info("StubRobot: move_to_bin %d (target=%d deg)", bin_index, target)
        self._simulate_travel(target)

    def deposit(self) -> None:
        log.info("StubRobot: deposit at position %d", self._current_position)
        time.sleep(0.1)  # simulated release time

    def return_home(self) -> None:
        log.info("StubRobot: return_home")
        self._simulate_travel(self.config.home_encoder_target)

    def move_to_qr_point(self) -> None:
        target = self.config.qr_approach_encoder_target
        if target is None:
            target = self.config.home_encoder_target
        log.info("StubRobot: move_to_qr_point -> %d°", target)
        self._simulate_travel(target)

    def get_current_position(self) -> int:
        return self._current_position

    def shutdown(self) -> None:
        if not self._shutdown:
            log.info("StubRobot: shutdown")
            self._shutdown = True


def build_robot(config: Config) -> RobotInterface:
    """Factory: dispatches on config.robot_implementation. Raises RuntimeError on unknown impl."""
    impl = config.robot_implementation
    if impl == "stub":
        return StubRobot(config)
    if impl == "ev3":
        from ev3_robot import EV3Robot
        return EV3Robot(config)
    if impl == "spike":
        from spike_robot import SpikeRobot
        return SpikeRobot(config)
    raise RuntimeError(f"Unknown robot_implementation: {impl!r}")
