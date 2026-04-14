"""Unit tests for event_log.EventLogger. Uses tmp_path fixture for isolation."""
from __future__ import annotations
import json
from datetime import datetime, timezone

import pytest

from event_log import EventLogger, LogEntry


def test_session_file_named_with_timestamp(tmp_path):
    ts = datetime(2026, 4, 13, 14, 30, 0, tzinfo=timezone.utc)
    with EventLogger(tmp_path, session_ts=ts) as el:
        pass
    files = list(tmp_path.glob("session-*.log"))
    assert len(files) == 1
    assert "session-20260413-143000.log" == files[0].name


def test_write_produces_one_jsonl_line_per_call(tmp_path):
    with EventLogger(tmp_path) as el:
        el.write("A", 1, 4500, "completed", None)
        el.write("B", 2, 5100, "completed", None)
    log_file = next(tmp_path.glob("session-*.log"))
    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # must parse


def test_write_schema_matches_D16(tmp_path):
    with EventLogger(tmp_path) as el:
        el.write("A", 1, 4500, "completed", None)
    line = next(tmp_path.glob("session-*.log")).read_text().strip()
    entry = json.loads(line)
    assert set(entry.keys()) == {"timestamp", "class", "destination_bin", "cycle_time_ms", "status", "error"}
    assert entry["class"] == "A"
    assert entry["destination_bin"] == 1
    assert entry["cycle_time_ms"] == 4500
    assert entry["status"] == "completed"
    assert entry["error"] is None


def test_unknown_package_entry(tmp_path):
    with EventLogger(tmp_path) as el:
        el.write("unknown", None, 800, "unknown_package", "pyzbar_no_decode")
    entry = json.loads(next(tmp_path.glob("session-*.log")).read_text().strip())
    assert entry["class"] == "unknown"
    assert entry["destination_bin"] is None
    assert entry["status"] == "unknown_package"
    assert entry["error"] == "pyzbar_no_decode"


def test_invalid_status_raises(tmp_path):
    with EventLogger(tmp_path) as el:
        with pytest.raises(ValueError):
            el.write("A", 1, 100, "bogus", None)


def test_close_idempotent(tmp_path):
    el = EventLogger(tmp_path)
    el.close()
    el.close()  # must not raise


def test_timestamp_iso8601(tmp_path):
    with EventLogger(tmp_path) as el:
        el.write("A", 1, 100, "completed", None)
    entry = json.loads(next(tmp_path.glob("session-*.log")).read_text().strip())
    # ISO-8601 parse must succeed
    from datetime import datetime as dt
    dt.fromisoformat(entry["timestamp"])
