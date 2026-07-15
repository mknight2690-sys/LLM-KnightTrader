"""Equity history tracking — snapshots for account curve chart."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from config import ACTIVITY_LOG, DATA_DIR

EQUITY_HISTORY_FILE = DATA_DIR / "equity_history.jsonl"
_lock = threading.RLock()


_PARSERS = [
    re.compile(r"Equity\s+[$]?([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
    re.compile(r"equity[:\s]+[$]?([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
]


def _parse_equity_from_title(title: str) -> float | None:
    for parser in _PARSERS:
        m = parser.search(title)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _backfill_from_activity_log(limit: int = 3000) -> list[dict[str, Any]]:
    """Read recent activity log and extract equity snapshots."""
    if not ACTIVITY_LOG.is_file():
        return []
    rows: list[dict[str, Any]] = []
    # Read last ~limit lines to avoid huge file reads
    lines = ACTIVITY_LOG.read_text(encoding="utf-8").splitlines()
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "account":
            continue
        title = str(ev.get("title", ""))
        equity = _parse_equity_from_title(title)
        if equity is not None and equity > 0:
            rows.append({"at": int(float(ev.get("ts", time.time())) * 1000), "equity": equity})
    # Deduplicate by timestamp (keep last)
    seen: dict[int, float] = {}
    for row in rows:
        seen[row["at"]] = row["equity"]
    # Sort by timestamp
    return [{"at": at, "equity": eq} for at, eq in sorted(seen.items())]


def _ensure_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not EQUITY_HISTORY_FILE.is_file():
        EQUITY_HISTORY_FILE.write_text("", encoding="utf-8")


def load_equity_history(limit: int = 5000) -> list[dict[str, Any]]:
    """Return equity history snapshots. Backfills from activity log if file is empty."""
    _ensure_file()
    with _lock:
        rows: list[dict[str, Any]] = []
        for line in EQUITY_HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row.get("at"), (int, float)) and isinstance(row.get("equity"), (int, float)):
                    rows.append({"at": int(row["at"]), "equity": float(row["equity"])})
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        if not rows:
            # Backfill from activity log on first read
            rows = _backfill_from_activity_log(limit=5000)
            if rows:
                _write_rows(rows)
        return rows


def _write_rows(rows: list[dict[str, Any]]) -> None:
    with _lock:
        _ensure_file()
        with EQUITY_HISTORY_FILE.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_equity_snapshot(equity: float, *, at_ms: int | None = None) -> None:
    """Append a new equity snapshot. Skips duplicates within 60s."""
    if not equity or equity <= 0 or equity == float("inf"):
        return
    now_ms = at_ms or int(time.time() * 1000)
    with _lock:
        rows = load_equity_history()
        # Skip if last entry is within 60s and same equity
        if rows:
            last = rows[-1]
            if abs(now_ms - last["at"]) < 60_000 and abs(last["equity"] - equity) < 0.0001:
                return
        rows.append({"at": now_ms, "equity": equity})
        # Keep last 5000 entries (plenty for 3s granularity over ~4 hours)
        rows = rows[-5000:]
        _write_rows(rows)


def get_equity_history_for_api() -> dict[str, Any]:
    """Return equity history shaped for the dashboard API."""
    history = load_equity_history()
    if not history:
        return {"history": [], "current_equity": 0, "count": 0}
    current = history[-1]["equity"] if history else 0
    return {"history": history, "current_equity": current, "count": len(history)}
