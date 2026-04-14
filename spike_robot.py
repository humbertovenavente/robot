"""SPIKE Prime / Robot Inventor robot adapter. Per D-11 the brick model is NOT confirmed at
Phase 1 planning time. Real implementation lands in Plan 07 after the team confirms EV3 vs
SPIKE at kickoff. Until then, instantiation raises NotImplementedError with a clear message
so accidental selection fails loudly, not silently."""
from __future__ import annotations
from config import Config


class SpikeRobot:
    def __init__(self, config: Config):
        raise NotImplementedError(
            "SpikeRobot is a placeholder. Set robot_implementation: stub in station_config.yaml "
            "until Plan 07 delivers the pybricks BLE/serial implementation."
        )

    # Method stubs so type-checkers and Protocol isinstance() checks don't false-pass.
    def calibrate_home(self) -> None: raise NotImplementedError
    def move_to_bin(self, bin_index: int) -> None: raise NotImplementedError
    def deposit(self) -> None: raise NotImplementedError
    def return_home(self) -> None: raise NotImplementedError
    def get_current_position(self) -> int: raise NotImplementedError
    def shutdown(self) -> None: raise NotImplementedError
