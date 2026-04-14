"""Unit tests for StubRobot. Run with: python -m pytest tests/test_stub_robot.py -x"""
import pytest
from config import load_config
from robot import StubRobot, RobotInterface, build_robot


def test_stub_satisfies_protocol():
    cfg = load_config()
    r = StubRobot(cfg)
    assert isinstance(r, RobotInterface)


def test_calibrate_home_resets_position():
    cfg = load_config()
    r = StubRobot(cfg)
    r.move_to_bin(2)
    assert r.get_current_position() != cfg.home_encoder_target
    r.calibrate_home()
    assert r.get_current_position() == cfg.home_encoder_target


def test_move_to_bin_updates_position():
    cfg = load_config()
    r = StubRobot(cfg)
    r.move_to_bin(1)
    assert r.get_current_position() == cfg.bin_encoder_targets[1]
    r.move_to_bin(3)
    assert r.get_current_position() == cfg.bin_encoder_targets[3]


def test_unknown_bin_raises():
    cfg = load_config()
    r = StubRobot(cfg)
    with pytest.raises(ValueError):
        r.move_to_bin(99)


def test_return_home():
    cfg = load_config()
    r = StubRobot(cfg)
    r.move_to_bin(2)
    r.return_home()
    assert r.get_current_position() == cfg.home_encoder_target


def test_shutdown_idempotent():
    cfg = load_config()
    r = StubRobot(cfg)
    r.shutdown()
    r.shutdown()  # must not raise


def test_build_robot_returns_stub_for_stub_config():
    cfg = load_config()
    assert cfg.robot_implementation == "stub"
    r = build_robot(cfg)
    assert isinstance(r, StubRobot)


def test_build_robot_raises_for_ev3_placeholder():
    cfg = load_config()
    cfg_ev3 = cfg.model_copy(update={"robot_implementation": "ev3"})
    with pytest.raises(NotImplementedError):
        build_robot(cfg_ev3)
