"""JSONL event logger. One file per session, one JSON object per line.

Schema (D-16, extended by D-18 for Phase 02.1):
  timestamp        ISO-8601 UTC
  class            "A" | "B" | "C" | "unknown"
  destination_bin  int | null
  cycle_time_ms    int
  status           "completed" | "unknown_package" | "error"
  error            string | null

Phase 02.1 optional fields (D-18) — only present when vision confirmation ran:
  vision_confirmed  bool | null
  drift_px          int | null
  vision_reason     string | null
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Literal
import json
import logging

log = logging.getLogger(__name__)

Status = Literal["completed", "unknown_package", "error"]


@dataclass
class LogEntry:
    timestamp: str
    cls: Optional[str]                    # "A" | "B" | "C" | "unknown"
    destination_bin: Optional[int]
    cycle_time_ms: int
    status: str                            # completed | unknown_package | error
    error: Optional[str]
    # Phase 02.1 optional vision-confirm fields (D-18); default None = not run
    vision_confirmed: Optional[bool] = None
    drift_px: Optional[int] = None
    vision_reason: Optional[str] = None

    def to_dict(self) -> dict:
        # Rename cls -> class on the wire (D-16 says "class")
        d = asdict(self)
        d["class"] = d.pop("cls")
        # D-18: omit vision fields entirely when all three are None so Phase 1
        # log consumers that validate strict schemas do not break.
        if d.get("vision_confirmed") is None and d.get("drift_px") is None and d.get("vision_reason") is None:
            d.pop("vision_confirmed", None)
            d.pop("drift_px", None)
            d.pop("vision_reason", None)
        return d


class EventLogger:
    """Append-only JSONL logger. File per session (D-15, LOG-02)."""

    def __init__(self, log_dir: Path, session_ts: Optional[datetime] = None):
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = (session_ts or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
        self.path = log_dir / f"session-{ts}.log"
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)  # line-buffered
        self._closed = False
        log.info("EventLogger opened %s", self.path)

    def write(
        self,
        cls: Optional[str],
        destination_bin: Optional[int],
        cycle_time_ms: int,
        status: str,
        error: Optional[str] = None,
        *,
        vision_confirmed: Optional[bool] = None,
        drift_px: Optional[int] = None,
        vision_reason: Optional[str] = None,
    ) -> LogEntry:
        if status not in ("completed", "unknown_package", "error"):
            raise ValueError(f"invalid status {status!r}")
        entry = LogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            cls=cls,
            destination_bin=destination_bin,
            cycle_time_ms=int(cycle_time_ms),
            status=status,
            error=error,
            vision_confirmed=vision_confirmed,
            drift_px=drift_px,
            vision_reason=vision_reason,
        )
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()
        return entry

    def close(self) -> None:
        if not self._closed:
            try:
                self._fh.close()
            finally:
                self._closed = True

    def __enter__(self) -> "EventLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
