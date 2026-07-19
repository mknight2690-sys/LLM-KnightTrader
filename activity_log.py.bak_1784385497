"""Thread-safe activity log for dashboard + trader."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from config import ACTIVITY_LOG, DATA_DIR

EventType = Literal[
    "system",
    "research",
    "trade",
    "account",
    "llm",
    "chat",
    "error",
]

_lock = threading.Lock()
_memory: deque[dict[str, Any]] = deque(maxlen=2000)
_subscribers: list[Any] = []


@dataclass
class ActivityEvent:
    ts: float
    type: EventType
    title: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def subscribe(callback) -> None:
    with _lock:
        _subscribers.append(callback)


def unsubscribe(callback) -> None:
    with _lock:
        if callback in _subscribers:
            _subscribers.remove(callback)


def log_event(
    event_type: EventType,
    title: str,
    detail: str = "",
    data: dict[str, Any] | None = None,
) -> ActivityEvent:
    event = ActivityEvent(
        ts=time.time(),
        type=event_type,
        title=title,
        detail=detail,
        data=data or {},
    )
    row = event.to_dict()
    with _lock:
        _memory.append(row)
        _ensure_data_dir()
        with ACTIVITY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        for cb in list(_subscribers):
            try:
                cb(row)
            except Exception:
                pass
    return event


def get_recent(limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        return list(_memory)[-limit:]


def load_history(limit: int = 500) -> None:
    """Hydrate in-memory buffer from disk on startup."""
    if not ACTIVITY_LOG.is_file():
        return
    with _lock:
        rows: list[dict[str, Any]] = []
        for line in ACTIVITY_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        _memory.clear()
        for row in rows[-limit:]:
            _memory.append(row)


def tail_new_events(since_ts: float = 0.0) -> list[dict[str, Any]]:
    """Read events from disk newer than since_ts (for cross-process sync)."""
    if not ACTIVITY_LOG.is_file():
        return []
    new_rows: list[dict[str, Any]] = []
    for line in ACTIVITY_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if float(row.get("ts") or 0) > since_ts:
            new_rows.append(row)
    if not new_rows:
        return []
    with _lock:
        existing_ts = {r.get("ts") for r in _memory}
        for row in new_rows:
            if row.get("ts") not in existing_ts:
                _memory.append(row)
                for cb in list(_subscribers):
                    try:
                        cb(row)
                    except Exception:
                        pass
    return new_rows
