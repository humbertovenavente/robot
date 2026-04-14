"""Unit tests for qr module. Uses monkeypatch on pyzbar.pyzbar.decode so tests
run without the native zbar library installed (CI-friendly).

Because pyzbar may not be installed in CI (requires libzbar native lib), we inject
a mock pyzbar package into sys.modules before any import from qr occurs.
"""
from __future__ import annotations
import sys
import types
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Inject a mock pyzbar package BEFORE importing qr so that the lazy
# `from pyzbar.pyzbar import decode` inside decode_qr_from_crop resolves.
# This avoids ModuleNotFoundError when libzbar is not installed.
# ---------------------------------------------------------------------------
_pyzbar_pkg = types.ModuleType("pyzbar")
_pyzbar_mod = types.ModuleType("pyzbar.pyzbar")
_pyzbar_mod.decode = MagicMock(return_value=[])  # default: no QR found
_pyzbar_pkg.pyzbar = _pyzbar_mod
sys.modules.setdefault("pyzbar", _pyzbar_pkg)
sys.modules.setdefault("pyzbar.pyzbar", _pyzbar_mod)

from config import load_config  # noqa: E402
from qr import pad_bbox, crop_padded_region, decode_qr_from_crop, decode_from_frame, VALID_CLASSES  # noqa: E402


def test_valid_classes():
    assert VALID_CLASSES == frozenset({"A", "B", "C"})


def test_pad_bbox_adds_percentage():
    padded = pad_bbox((100, 100, 200, 200), pct=0.1, frame_w=640, frame_h=480)
    # 10% of 100 = 10 px padding each side
    assert padded == (90, 90, 210, 210)


def test_pad_bbox_clamps_to_frame():
    padded = pad_bbox((0, 0, 100, 100), pct=0.5, frame_w=640, frame_h=480)
    assert padded[0] == 0 and padded[1] == 0
    assert padded[2] == 150 and padded[3] == 150  # unclamped high side


def test_pad_bbox_clamps_high():
    padded = pad_bbox((600, 450, 640, 480), pct=0.5, frame_w=640, frame_h=480)
    assert padded[2] == 640 and padded[3] == 480


def test_crop_padded_region_returns_array():
    frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
    crop = crop_padded_region(frame, (100, 100, 200, 200), 0.1)
    assert crop.shape[0] == 120 and crop.shape[1] == 120


def _fake_zbar_result(payload: str):
    m = MagicMock()
    m.data = payload.encode("utf-8")
    return [m]


def test_decode_qr_returns_class_A():
    crop = np.zeros((100, 100, 3), dtype=np.uint8)
    with patch.object(_pyzbar_mod, "decode", return_value=_fake_zbar_result("A")):
        assert decode_qr_from_crop(crop, retry=3) == "A"


def test_decode_qr_rejects_unknown_payload():
    crop = np.zeros((100, 100, 3), dtype=np.uint8)
    with patch.object(_pyzbar_mod, "decode", return_value=_fake_zbar_result("XYZ")):
        assert decode_qr_from_crop(crop, retry=3) is None


def test_decode_qr_returns_none_on_empty():
    crop = np.zeros((100, 100, 3), dtype=np.uint8)
    with patch.object(_pyzbar_mod, "decode", return_value=[]):
        assert decode_qr_from_crop(crop, retry=3) is None


def test_decode_qr_retries_then_succeeds():
    crop = np.zeros((100, 100, 3), dtype=np.uint8)
    call_results = [[], [], _fake_zbar_result("B")]  # two empties then success
    with patch.object(_pyzbar_mod, "decode", side_effect=call_results):
        assert decode_qr_from_crop(crop, retry=3) == "B"


def test_decode_qr_returns_none_on_zero_sized_crop():
    empty = np.zeros((0, 0, 3), dtype=np.uint8)
    assert decode_qr_from_crop(empty, retry=3) is None


def test_decode_from_frame_uses_config_padding_and_retry():
    cfg = load_config()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    with patch.object(_pyzbar_mod, "decode", return_value=_fake_zbar_result("C")):
        result = decode_from_frame(frame, (100, 100, 200, 200), cfg)
    assert result == "C"
