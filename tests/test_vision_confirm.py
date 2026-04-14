"""Unit tests for vision_confirm.py (Phase 02.1).

Covers:
  - find_robot_qr: ROBOT detection, ignore A/B/C, mixed results, no QR, exception, None frame
  - compute_drift: correct math, None inputs

All pyzbar interactions are mocked — no camera or zbar binary required.

Mock strategy
-------------
vision_confirm._pyzbar may be None when pyzbar is not installed.
Each test that exercises decode sets up a MagicMock module as _pyzbar
and patches _PYZBAR_AVAILABLE=True so the code follows the normal path.
"""
from __future__ import annotations

import math
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helper: build a minimal pyzbar-style decoded result object
# ---------------------------------------------------------------------------

def _make_decoded(payload_bytes: bytes, left: int = 0, top: int = 0, width: int = 40, height: int = 40):
    """Return a mock pyzbar-decoded object with .data and .rect attributes."""
    rect = SimpleNamespace(left=left, top=top, width=width, height=height)
    return SimpleNamespace(data=payload_bytes, rect=rect)


def _make_mock_pyzbar(return_value=None, side_effect=None):
    """Return a MagicMock suitable as a stand-in for the pyzbar module.

    Sets up mock.decode with the given return_value or side_effect so tests
    can patch vision_confirm._pyzbar with this object regardless of whether
    the real pyzbar library is installed.
    """
    mock_mod = MagicMock()
    if side_effect is not None:
        mock_mod.decode.side_effect = side_effect
    else:
        mock_mod.decode.return_value = return_value if return_value is not None else []
    return mock_mod


# ---------------------------------------------------------------------------
# Tests for find_robot_qr
# ---------------------------------------------------------------------------

class TestFindRobotQr(unittest.TestCase):

    def _make_fake_frame(self):
        """Return a dummy non-None frame object (content irrelevant — pyzbar is mocked)."""
        return object()

    # Test 1: frame with single ROBOT QR → returns correct center
    def test_single_robot_qr_returns_center(self):
        """Rect(left=100, top=200, width=40, height=40) → center (120, 220)."""
        decoded_result = _make_decoded(b"ROBOT", left=100, top=200, width=40, height=40)
        mock_pyzbar = _make_mock_pyzbar(return_value=[decoded_result])

        import vision_confirm
        with patch.object(vision_confirm, "_PYZBAR_AVAILABLE", True), \
             patch.object(vision_confirm, "_pyzbar", mock_pyzbar):
            result = vision_confirm.find_robot_qr(self._make_fake_frame())

        self.assertEqual(result, (120, 220))

    # Test 2: frame with only A/B/C QRs → returns None (ignores package QRs)
    def test_only_package_qrs_returns_none(self):
        decoded_results = [
            _make_decoded(b"A"),
            _make_decoded(b"B"),
            _make_decoded(b"C"),
        ]
        mock_pyzbar = _make_mock_pyzbar(return_value=decoded_results)

        import vision_confirm
        with patch.object(vision_confirm, "_PYZBAR_AVAILABLE", True), \
             patch.object(vision_confirm, "_pyzbar", mock_pyzbar):
            result = vision_confirm.find_robot_qr(self._make_fake_frame())

        self.assertIsNone(result)

    # Test 3: mixed ROBOT + A → returns ROBOT center, ignores A
    def test_mixed_robot_and_package_returns_robot_center(self):
        robot_qr = _make_decoded(b"ROBOT", left=50, top=60, width=20, height=30)
        package_qr = _make_decoded(b"A", left=200, top=300, width=40, height=40)
        decoded_results = [robot_qr, package_qr]
        mock_pyzbar = _make_mock_pyzbar(return_value=decoded_results)

        import vision_confirm
        with patch.object(vision_confirm, "_PYZBAR_AVAILABLE", True), \
             patch.object(vision_confirm, "_pyzbar", mock_pyzbar):
            result = vision_confirm.find_robot_qr(self._make_fake_frame())

        # cx = 50 + 20/2 = 60, cy = 60 + 30/2 = 75
        self.assertEqual(result, (60, 75))

    # Test 4: zero decoded results → returns None
    def test_no_qr_results_returns_none(self):
        mock_pyzbar = _make_mock_pyzbar(return_value=[])

        import vision_confirm
        with patch.object(vision_confirm, "_PYZBAR_AVAILABLE", True), \
             patch.object(vision_confirm, "_pyzbar", mock_pyzbar):
            result = vision_confirm.find_robot_qr(self._make_fake_frame())

        self.assertIsNone(result)

    # Test 5: pyzbar.decode raises ValueError → returns None (never propagates)
    def test_decode_raises_returns_none(self):
        mock_pyzbar = _make_mock_pyzbar(side_effect=ValueError("zbar error"))

        import vision_confirm
        with patch.object(vision_confirm, "_PYZBAR_AVAILABLE", True), \
             patch.object(vision_confirm, "_pyzbar", mock_pyzbar):
            result = vision_confirm.find_robot_qr(self._make_fake_frame())

        self.assertIsNone(result)

    # Test 6: frame=None → returns None without calling pyzbar
    def test_none_frame_returns_none_without_decode(self):
        mock_pyzbar = _make_mock_pyzbar(return_value=[])

        import vision_confirm
        with patch.object(vision_confirm, "_PYZBAR_AVAILABLE", True), \
             patch.object(vision_confirm, "_pyzbar", mock_pyzbar):
            result = vision_confirm.find_robot_qr(None)

        self.assertIsNone(result)
        mock_pyzbar.decode.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for compute_drift
# ---------------------------------------------------------------------------

class TestComputeDrift(unittest.TestCase):

    def setUp(self):
        import vision_confirm
        self.compute_drift = vision_confirm.compute_drift

    # Test 7: 3-4-5 right triangle → 5
    def test_3_4_5_triangle(self):
        self.assertEqual(self.compute_drift((0, 0), (3, 4)), 5)

    # Test 8: same point → 0
    def test_same_point_returns_zero(self):
        self.assertEqual(self.compute_drift((100, 200), (100, 200)), 0)

    # Test 9: observed is None → returns None
    def test_observed_none_returns_none(self):
        self.assertIsNone(self.compute_drift(None, (0, 0)))

    # Test 10: expected is None → returns None
    def test_expected_none_returns_none(self):
        self.assertIsNone(self.compute_drift((0, 0), None))

    # Test 11: (10,10) → (13,14): dx=3, dy=4, hypot=5 → 5
    def test_offset_point_returns_rounded_5(self):
        self.assertEqual(self.compute_drift((10, 10), (13, 14)), 5)


# ---------------------------------------------------------------------------
# Tests for ROBOT_PAYLOAD constant
# ---------------------------------------------------------------------------

class TestConstants(unittest.TestCase):

    def test_robot_payload_constant_value(self):
        import vision_confirm
        self.assertEqual(vision_confirm.ROBOT_PAYLOAD, "ROBOT")

    def test_no_package_qr_allowlist_in_module(self):
        """vision_confirm.py must not leak the A/B/C allow-list (D-04)."""
        import vision_confirm
        import inspect
        source = inspect.getsource(vision_confirm)
        # 'class_to_bin' is the Phase 1 config dict — must not appear in this module
        self.assertNotIn("class_to_bin", source)
        # Ensure module doesn't hardcode package-QR set
        self.assertNotIn('{"A", "B", "C"}', source)
        self.assertNotIn("frozenset", source)


if __name__ == "__main__":
    unittest.main()
