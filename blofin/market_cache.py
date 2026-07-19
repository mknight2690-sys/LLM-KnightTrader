"""Short-lived cache for public market REST responses."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TICKER_DISK = ROOT / "data" / "ticker_cache.json"
INSTRUMENTS_DISK = ROOT / "data" / "instruments_cache.json"

_lock = threading.Lock()
_candles: dict[str, tuple[float, list[list[str]]]] = {}
_tickers: tuple[float, list[dict[str, Any]]] | None = None
_instruments: tuple[float, list[dict[str, Any]]] | None = None

CANDLE_TTL_SEC = 55.0
TICKER_TTL_SEC = 8.0
INSTRUMENTS_TTL_SEC = 3600.0


def fetch_public_tickers(*, inst_type: str = "SWAP", timeout: float = 12.0) -> list[dict[str, Any]]:
    """Unsigned public market tickers — works during account API cooldown."""
    from config import BLOFIN_MARKET_BASE

    url = f"{BLOFIN_MARKET_BASE}/api/v1/market/tickers?instType={urllib.parse.quote(inst_type)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    rows = list(payload.get("data") or [])
    if rows:
        set_cached_tickers(rows)
    return rows


def get_cached_candles(inst_id: str) -> list[list[str]] | None:
    with _lock:
        row = _candles.get(inst_id)
        if not row:
            return None
        ts, data = row
        if time.time() - ts > CANDLE_TTL_SEC:
            return None
        return data


def set_cached_candles(inst_id: str, rows: list[list[str]]) -> None:
    with _lock:
        _candles[inst_id] = (time.time(), rows)


def get_cached_tickers(*, allow_stale: bool = False) -> list[dict[str, Any]] | None:
    with _lock:
        global _tickers
        if _tickers:
            ts, data = _tickers
            if allow_stale or time.time() - ts <= TICKER_TTL_SEC:
                return data
        if allow_stale:
            disk = _read_ticker_disk()
            if disk:
                return disk.get("rows")
        return None


def set_cached_tickers(rows: list[dict[str, Any]]) -> None:
    with _lock:
        global _tickers
        _tickers = (time.time(), rows)
    _write_ticker_disk(rows)


def _read_ticker_disk() -> dict[str, Any] | None:
    if not TICKER_DISK.is_file():
        return None
    try:
        return json.loads(TICKER_DISK.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_ticker_disk(rows: list[dict[str, Any]]) -> None:
    TICKER_DISK.parent.mkdir(parents=True, exist_ok=True)
    TICKER_DISK.write_text(
        json.dumps({"fetched_at": time.time(), "rows": rows}, indent=2),
        encoding="utf-8",
    )


def ticker_fetched_at() -> float:
    with _lock:
        if _tickers:
            return float(_tickers[0])
    disk = _read_ticker_disk()
    if disk:
        return float(disk.get("fetched_at") or 0)
    return 0.0


def get_cached_instruments(*, allow_stale: bool = False) -> list[dict[str, Any]] | None:
    with _lock:
        global _instruments
        if _instruments:
            ts, data = _instruments
            if allow_stale or time.time() - ts <= INSTRUMENTS_TTL_SEC:
                return data
        if allow_stale:
            disk = _read_instruments_disk()
            if disk:
                return disk.get("rows")
        return None


def set_cached_instruments(rows: list[dict[str, Any]]) -> None:
    with _lock:
        global _instruments
        _instruments = (time.time(), rows)
    _write_instruments_disk(rows)


def _read_instruments_disk() -> dict[str, Any] | None:
    if not INSTRUMENTS_DISK.is_file():
        return None
    try:
        return json.loads(INSTRUMENTS_DISK.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_instruments_disk(rows: list[dict[str, Any]]) -> None:
    INSTRUMENTS_DISK.parent.mkdir(parents=True, exist_ok=True)
    INSTRUMENTS_DISK.write_text(
        json.dumps({"fetched_at": time.time(), "rows": rows}, indent=2),
        encoding="utf-8",
    )
