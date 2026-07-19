"""Shared BloFin account snapshot cache — file-backed across processes."""

from __future__ import annotations

import json
import msvcrt
import os
import re
import time
from pathlib import Path
from typing import Any

from config import ACCOUNT_REFRESH_SEC

ROOT = Path(__file__).resolve().parents[1]
CACHE_FILE = ROOT / "data" / "account_cache.json"
LOCK_FILE = ROOT / "data" / "account_cache.lock"

_min_interval_sec: float = max(8.0, ACCOUNT_REFRESH_SEC)
_backoff_sec: float = 120.0
_live_verify_interval_sec: float = max(6.0, min(_min_interval_sec, 10.0))

_last_verified_display: dict[str, Any] | None = None
_last_live_verify_ts: float = 0.0


def _is_good_snapshot(snap: dict[str, Any] | None) -> bool:
    if not snap:
        return False
    if snap.get("ok") is True:
        return True
    if float(snap.get("equity") or 0) > 0:
        return True
    return bool(snap.get("positions"))


def response_is_rate_limited(resp: dict[str, Any]) -> bool:
    return _is_rate_limited(resp)


def is_rate_limited() -> bool:
    disk = _read_disk()
    if not disk:
        return False
    return time.time() < float(disk.get("backoff_until") or 0)


def _is_rate_limited(resp: dict[str, Any]) -> bool:
    if resp.get("error_code") == 1015 or resp.get("status") == 429:
        return True
    code = str(resp.get("code") or "")
    if code in ("429", "1015", "403"):
        return True
    if resp.get("error") is True and code == "403":
        return True
    msg = str(resp.get("msg") or resp.get("title") or "").lower()
    return "rate limit" in msg or "rate-limited" in msg or "<!doctype html>" in msg


def _map_position_row(p: dict[str, Any]) -> dict[str, Any] | None:
    sz = float(
        p.get("positions")
        or p.get("availPos")
        or p.get("availablePositions")
        or p.get("size")
        or 0
    )
    if abs(sz) <= 0:
        return None
    return {
        "instId": p.get("instId"),
        "side": p.get("positionSide"),
        "size": sz,
        "entry": float(p.get("averagePrice") or 0),
        "mark": float(p.get("markPrice") or 0),
        "upl": float(p.get("unrealizedPnl") or p.get("upl") or 0),
        "upl_ratio": float(p.get("unrealizedPnlRatio") or 0),
        "initial_margin": float(p.get("initialMargin") or 0),
        "leverage": p.get("leverage"),
        "marginMode": p.get("marginMode") or "cross",
    }


def _positions_from_account_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    parsed = data.get("positions") or []
    if parsed:
        return [p for p in parsed if abs(float(p.get("size") or 0)) > 0]
    rows = (data.get("positions_raw") or {}).get("data") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        mapped = _map_position_row(row)
        if mapped:
            out.append(mapped)
    return out


def _pick_best_snapshot(*candidates: dict[str, Any] | None) -> dict[str, Any] | None:
    good = [c for c in candidates if c and _is_good_snapshot(c)]
    if not good:
        return None
    return max(good, key=lambda c: float(c.get("fetched_at") or 0))


def _is_live_account_event(data: dict[str, Any]) -> bool:
    """True for trader cycles that logged a real BloFin API response."""
    if data.get("cached") or data.get("from_trades"):
        return False
    if "positions_raw" not in data or "balance_raw" not in data:
        return False
    bal = data.get("balance_raw") or {}
    if bal.get("error_code") in (1015,) or bal.get("status") == 429:
        return False
    if str(bal.get("code")) not in ("0", ""):
        return False
    pr = data.get("positions_raw") or {}
    if str(pr.get("code")) not in ("0", ""):
        return False
    return True


def _account_event_to_snapshot(data: dict[str, Any], ts: float) -> dict[str, Any]:
    positions = _positions_from_account_data(data)
    return {
        "equity": float(data.get("equity") or 0),
        "available": float(data.get("available") or 0),
        "positions": positions,
        "position_mode": data.get("position_mode") or "net_mode",
        "fetched_at": ts,
        "ok": True,
        "authoritative": True,
        "balance_raw": data.get("balance_raw"),
        "positions_raw": data.get("positions_raw"),
        "backoff_until": 0.0,
    }


def _parse_snapshot(client: Any, bal: dict[str, Any], pos: dict[str, Any]) -> dict[str, Any]:
    equity = 0.0
    available = 0.0
    if bal.get("code") == "0":
        data = bal.get("data") or {}
        equity = float(data.get("totalEquity") or 0)
        details = data.get("details") or [{}]
        available = float(details[0].get("available") or 0)
    positions: list[dict[str, Any]] = []
    if pos.get("code") == "0":
        for p in pos.get("data") or []:
            mapped = _map_position_row(p)
            if mapped:
                positions.append(mapped)
    return {
        "equity": equity,
        "available": available,
        "positions": positions,
        "position_mode": client._position_mode or "unknown",
        "fetched_at": time.time(),
    }


def _read_disk() -> dict[str, Any] | None:
    if not CACHE_FILE.is_file():
        return None
    for attempt in range(4):
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError):
            time.sleep(0.05 * (attempt + 1))
    return None


def _write_disk_body(snap: dict[str, Any]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    body = json.dumps(snap, indent=2)
    for attempt in range(8):
        try:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, CACHE_FILE)
            return
        except OSError:
            time.sleep(0.06 * (attempt + 1))
    raise OSError(f"failed to write {CACHE_FILE}")


def _write_disk(snap: dict[str, Any], *, locked: bool = False) -> None:
    if locked:
        _write_disk_body(snap)
        return
    lock = _FileLock()
    if not lock.acquire(timeout=4.0):
        return
    try:
        _write_disk_body(snap)
    finally:
        lock.release()


class _FileLock:
    def __init__(self) -> None:
        self._fp = None

    def acquire(self, timeout: float = 8.0) -> bool:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(LOCK_FILE, "a+b")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                time.sleep(0.15)
        return False

    def release(self) -> None:
        if not self._fp:
            return
        try:
            msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._fp.close()
        self._fp = None


def _decorate(
    snap: dict[str, Any],
    *,
    cached: bool = False,
    stale: bool = False,
    rate_limited: bool = False,
) -> dict[str, Any]:
    out = dict(snap)
    out["cached"] = cached
    if stale:
        out["stale"] = True
    if rate_limited:
        out["rate_limited"] = True
    return out


def _position_completeness(p: dict[str, Any]) -> int:
    score = 0
    if float(p.get("entry") or 0) > 0:
        score += 8
    if float(p.get("mark") or 0) > 0:
        score += 4
    lev = str(p.get("leverage") or "")
    if lev and lev not in ("?", "0", "none", "None"):
        score += 2
    if float(p.get("upl") or 0) != 0:
        score += 1
    if p.get("side"):
        score += 1
    return score


def _merge_position_row(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge historical row into live snapshot — never overwrite live mark/upl."""
    out = dict(base)
    for key, vb in extra.items():
        if key.startswith("_"):
            continue
        va = out.get(key)
        if key == "upl":
            continue
        if key in ("entry", "mark"):
            fa, fb = float(va or 0), float(vb or 0)
            if fa <= 0 and fb > 0:
                out[key] = vb
        elif key == "size":
            if va is None or va == "":
                out[key] = vb
        elif key == "leverage":
            if str(va or "") in ("", "?", "0", "None", "none") and str(vb or "") not in ("", "?", "0"):
                out[key] = vb
        elif not va and vb:
            out[key] = vb
    return out


_hist_positions_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


def _historical_positions_by_inst() -> dict[str, dict[str, Any]]:
    """Best-known position row per instrument from full activity log."""
    global _hist_positions_cache
    now = time.time()
    if _hist_positions_cache and now - _hist_positions_cache[0] < 30.0:
        return _hist_positions_cache[1]

    log_path = ROOT / "data" / "activity.jsonl"
    by_inst: dict[str, dict[str, Any]] = {}
    if log_path.is_file():
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "account":
                continue
            data = ev.get("data") or {}
            if not _is_live_account_event(data):
                continue
            rows = list(data.get("positions") or [])
            rows.extend(_positions_from_account_data(data))
            for row in rows:
                inst = str(row.get("instId") or "")
                if not inst or abs(float(row.get("size") or 0)) <= 0:
                    continue
                prev = by_inst.get(inst)
                if not prev or _position_completeness(row) >= _position_completeness(prev):
                    by_inst[inst] = dict(row)

    _hist_positions_cache = (now, by_inst)
    return by_inst


def _ticker_prices() -> dict[str, float]:
    from blofin.market_cache import get_cached_tickers

    rows = get_cached_tickers(allow_stale=True) or []
    out: dict[str, float] = {}
    for row in rows:
        inst = str(row.get("instId") or "")
        mark = float(row.get("markPrice") or row.get("markPx") or row.get("last") or 0)
        if inst and mark > 0:
            out[inst] = mark
    return out


def _contract_values() -> dict[str, float]:
    from blofin.market_cache import get_cached_instruments

    rows = get_cached_instruments(allow_stale=True) or []
    out: dict[str, float] = {}
    for row in rows:
        inst = str(row.get("instId") or "")
        ct = float(row.get("ctVal") or row.get("contractValue") or 0)
        if inst and ct > 0:
            out[inst] = ct
    return out


def _sum_upl(positions: list[dict[str, Any]]) -> float:
    return sum(float(p.get("upl") or 0) for p in positions)


def _raw_upl_by_inst(positions_raw: dict[str, Any] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    if not positions_raw:
        return out
    for row in positions_raw.get("data") or []:
        inst = str(row.get("instId") or "")
        if inst:
            out[inst] = float(row.get("unrealizedPnl") or 0)
    return out


def _estimate_upl(entry: float, mark: float, size: float, inst: str) -> float:
    """Fallback UPL when exchange value missing — not used when API reports unrealizedPnl."""
    if entry <= 0 or mark <= 0 or abs(size) <= 0:
        return 0.0
    ct = _contract_values().get(inst, 1.0)
    return (mark - entry) * float(size) * ct


def _position_upl_sane(
    pos: dict[str, Any],
    equity: float,
    raw_upl: dict[str, float] | None = None,
) -> bool:
    upl = float(pos.get("upl") or 0)
    inst = str(pos.get("instId") or "")
    if raw_upl and inst in raw_upl:
        ref = float(raw_upl[inst])
        return abs(upl - ref) <= max(abs(ref) * 0.02, 0.0005)
    entry = float(pos.get("entry") or 0)
    mark = float(pos.get("mark") or 0)
    size = abs(float(pos.get("size") or 0))
    if not size or entry <= 0 or mark <= 0:
        return True
    expected = _estimate_upl(entry, mark, float(pos.get("size") or 0), inst)
    tol = max(abs(expected) * 0.5, 0.05, abs(equity) * 0.2)
    return abs(upl - expected) <= tol


def _clamp_upl(upl: float, equity: float) -> float:
    if not float(upl) == upl:
        return 0.0
    cap = max(abs(equity) * 3.0, 50.0)
    if abs(upl) > cap:
        return 0.0
    return upl


def _account_display_sane(snap: dict[str, Any]) -> tuple[bool, str]:
    """Detect corrupted dashboard account numbers before they reach the UI."""
    equity = float(snap.get("equity") or 0)
    available = float(snap.get("available") or 0)
    positions = snap.get("positions") or []
    if equity < 0 and not snap.get("from_trades"):
        return False, "negative_equity"
    if equity > 0 and available < -max(equity * 0.25, 0.5):
        return False, "negative_available"
    cap = max(abs(equity) * 2.5, 25.0)
    upl_total = float(snap.get("upl_total") if snap.get("upl_total") is not None else _sum_upl(positions))
    if abs(upl_total) > cap:
        return False, "upl_outlier"
    for pos in positions:
        if not _position_upl_sane(pos, equity, _raw_upl_by_inst(snap.get("positions_raw"))):
            return False, f"position_upl_outlier:{pos.get('instId')}"
    if positions and equity <= 0 and snap.get("authoritative"):
        return False, "zero_equity_with_positions"
    return True, ""


def apply_mtm_account(snap: dict[str, Any]) -> dict[str, Any]:
    """Prepare positions for dashboard — exchange mark/upl are authoritative."""
    out = dict(snap)
    positions = list(out.get("positions") or [])
    if not positions:
        out["positions"] = []
        out["upl_total"] = 0.0
        out["mtm"] = False
        return out

    base_equity = float(out.get("equity") or 0)
    raw_upl = _raw_upl_by_inst(out.get("positions_raw"))
    enriched = enrich_positions(positions, positions_raw=out.get("positions_raw"))
    for row in enriched:
        row["upl"] = _clamp_upl(float(row.get("upl") or 0), base_equity)
    upl_total = _sum_upl(enriched)
    out["positions"] = enriched
    out["upl_total"] = upl_total
    out["mtm"] = bool(out.get("authoritative"))
    fetched = float(out.get("fetched_at") or 0)
    if fetched:
        out["mark_age_sec"] = max(0.0, time.time() - fetched)
    return out


def enrich_positions(
    positions: list[dict[str, Any]],
    *,
    positions_raw: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize positions for dashboard — always use BloFin markPrice and unrealizedPnl."""
    if not positions:
        return []
    hist = _historical_positions_by_inst()
    exchange_upl = _raw_upl_by_inst(positions_raw)
    exchange_mark: dict[str, float] = {}
    if positions_raw:
        for row in positions_raw.get("data") or []:
            inst = str(row.get("instId") or "")
            mark = float(row.get("markPrice") or 0)
            if inst and mark > 0:
                exchange_mark[inst] = mark

    enriched: list[dict[str, Any]] = []
    for pos in positions:
        inst = str(pos.get("instId") or "")
        if not inst:
            continue
        row = _merge_position_row(pos, hist.get(inst, {}))
        size = float(row.get("size") or 0)
        if abs(size) <= 0:
            continue

        entry = float(row.get("entry") or 0)
        mark = exchange_mark.get(inst) or float(pos.get("mark") or 0)
        upl = exchange_upl.get(inst, float(pos.get("upl") or 0))
        if upl == 0 and entry > 0 and mark > 0:
            upl = _estimate_upl(entry, mark, size, inst)

        row["mark"] = mark
        row["upl"] = upl
        side = str(row.get("side") or "net")
        if side in ("net", ""):
            row["side"] = "long" if size > 0 else "short"
        row["size"] = size
        enriched.append(row)
    return enriched


def _position_rows_by_inst(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for snap in snapshots:
        for pos in snap.get("positions") or []:
            inst = str(pos.get("instId") or "")
            if inst:
                out[inst] = pos
    return out


def hydrate_from_trade_log() -> dict[str, Any] | None:
    """Reconstruct open positions from successful trade events in activity log."""
    log_path = ROOT / "data" / "activity.jsonl"
    if not log_path.is_file():
        return None
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    ledger: dict[str, float] = {}
    last_ts = 0.0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "trade":
            continue
        title = str(ev.get("title") or "")
        detail = str(ev.get("detail") or "")
        if '"code": "0"' not in detail:
            continue
        last_ts = max(last_ts, float(ev.get("ts") or 0))
        opened = re.search(r"Opened (buy|sell) ([A-Z0-9]+-USDT)", title)
        if opened:
            side, inst = opened.groups()
            m = re.search(r"x(\d+)", title)
            sz = float(m.group(1)) if m else float((ev.get("data") or {}).get("contracts") or 0)
            if sz > 0:
                ledger[inst] = sz if side == "buy" else -sz
            continue
        closed = re.search(r"Closed ([A-Z0-9]+-USDT)", title)
        if closed:
            ledger.pop(closed.group(1), None)

    if not ledger:
        return None

    known = _historical_positions_by_inst()
    positions: list[dict[str, Any]] = []
    for inst, sz in sorted(ledger.items()):
        if abs(sz) <= 0:
            continue
        row = _merge_position_row({"instId": inst, "size": sz}, known.get(inst, {}))
        row["instId"] = inst
        row["size"] = sz
        positions.append(row)

    base = hydrate_from_activity_log() or _read_disk() or {}
    return {
        "equity": float(base.get("equity") or 0),
        "available": float(base.get("available") or 0),
        "positions": positions,
        "position_mode": base.get("position_mode") or "net_mode",
        "fetched_at": last_ts or time.time(),
        "ok": True,
        "hydrated": True,
        "from_trades": True,
        "backoff_until": float(base.get("backoff_until") or 0),
    }


def hydrate_from_activity_log() -> dict[str, Any] | None:
    """Recover the newest live BloFin account snapshot from the activity log."""
    log_path = ROOT / "data" / "activity.jsonl"
    if not log_path.is_file():
        return None
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-5000:]):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "account":
            continue
        data = ev.get("data") or {}
        if not _is_live_account_event(data):
            continue
        equity = float(data.get("equity") or 0)
        if equity <= 0:
            continue
        return _account_event_to_snapshot(data, float(ev.get("ts") or time.time()))
    return None


def _best_fallback_snapshot() -> dict[str, Any] | None:
    disk = _read_disk()
    if disk and _is_good_snapshot(disk) and disk.get("authoritative") and "positions_raw" in disk:
        out = dict(disk)
        out["positions"] = enrich_positions(
            out.get("positions") or [], positions_raw=out.get("positions_raw")
        )
        out.pop("from_trades", None)
        return out

    activity = hydrate_from_activity_log()
    candidates: list[dict[str, Any]] = []
    if activity:
        candidates.append(activity)
    if disk and _is_good_snapshot(disk) and not disk.get("from_trades"):
        candidates.append(disk)
    if not candidates:
        return None
    best = max(candidates, key=lambda c: float(c.get("fetched_at") or 0))
    out = dict(best)
    out["positions"] = enrich_positions(
        out.get("positions") or [], positions_raw=out.get("positions_raw")
    )
    out.pop("from_trades", None)
    return out


def read_account_cached() -> dict[str, Any]:
    """Fast read for UI polling — never hits BloFin API (except paper ledger)."""
    from config import PAPER_TRADING

    if PAPER_TRADING:
        return get_account_snapshot(force=True)

    if _disk_needs_bootstrap(_read_disk()):
        bootstrap_account_cache()
    snap = _best_fallback_snapshot()
    now = time.time()
    if not snap:
        return {
            "equity": 0.0,
            "available": 0.0,
            "positions": [],
            "ok": False,
            "fetched_at": now,
        }
    limited = is_rate_limited()
    stale = limited or bool(snap.get("hydrated")) or (now - float(snap.get("fetched_at") or 0) > _min_interval_sec * 2)
    out = apply_mtm_account(dict(snap))
    sane, reason = _account_display_sane(out)
    if not sane:
        fallback = dict(snap)
        fallback["positions"] = list(snap.get("positions") or [])
        fallback["upl_total"] = _sum_upl(fallback["positions"])
        fallback["mtm"] = False
        fallback["display_repaired"] = True
        fallback["display_warning"] = reason
        out = fallback
    return _decorate(out, cached=True, stale=stale, rate_limited=limited)


def _disk_needs_bootstrap(disk: dict[str, Any] | None) -> bool:
    if not disk:
        return True
    if disk.get("from_trades"):
        return True
    equity = float(disk.get("equity") or 0)
    cap = max(abs(equity) * 2.0, 25.0)
    for pos in disk.get("positions") or []:
        if abs(float(pos.get("upl") or 0)) > cap:
            return True
    # Fresh authoritative API snapshot on disk — do not replace from older activity rows.
    if disk.get("authoritative") and "positions_raw" in disk:
        return False
    auth = hydrate_from_activity_log()
    if not auth:
        return False
    if len(disk.get("positions") or []) != len(auth.get("positions") or []):
        return True
    if auth.get("authoritative") and not disk.get("authoritative"):
        return True
    return False


def get_account_snapshot(*, force: bool = False) -> dict[str, Any]:
    """Return account data from shared file cache; one process refreshes per interval."""
    from config import PAPER_TRADING

    if PAPER_TRADING:
        from blofin.paper_ledger import apply_tpsl_triggers, snapshot

        try:
            apply_tpsl_triggers()
        except Exception:
            pass
        snap = snapshot(force_marks=True)
        _write_disk(snap)
        return _decorate(snap, cached=False)

    disk0 = _read_disk()
    if not _is_good_snapshot(disk0) or _disk_needs_bootstrap(disk0):
        bootstrap_account_cache()

    now = time.time()
    disk = _read_disk()
    backoff_until = float((disk or {}).get("backoff_until") or 0)

    if now < backoff_until:
        fallback = _best_fallback_snapshot()
        if fallback:
            return _decorate(fallback, cached=True, stale=True, rate_limited=True)
        return _decorate(
            {
                "equity": 0.0,
                "available": 0.0,
                "positions": [],
                "ok": False,
                "error": "rate limited, no cached account",
                "fetched_at": now,
            },
            cached=True,
            stale=True,
            rate_limited=True,
        )

    if disk and not force:
        age = now - float(disk.get("fetched_at") or 0)
        if age < _min_interval_sec and _is_good_snapshot(disk):
            return _decorate(disk, cached=True)

    lock = _FileLock()
    if not lock.acquire():
        fallback = _best_fallback_snapshot()
        if fallback:
            return _decorate(fallback, cached=True, stale=True)
        return {
            "equity": 0.0,
            "available": 0.0,
            "positions": [],
            "ok": False,
            "error": "cache lock busy",
            "fetched_at": now,
        }

    try:
        disk = _read_disk()
        if disk and not force:
            age = now - float(disk.get("fetched_at") or 0)
            if age < _min_interval_sec and _is_good_snapshot(disk):
                return _decorate(disk, cached=True)

        from blofin.client import BlofinClient

        client = BlofinClient()
        try:
            mode = client.ensure_net_position_mode()
            bal = client.get_balance()
            if _is_rate_limited(bal):
                raise RuntimeError("balance rate limited")
            pos = client.get_positions()
            if _is_rate_limited(pos):
                raise RuntimeError("positions rate limited")
            snap = _parse_snapshot(client, bal, pos)
            snap["position_mode"] = mode
            snap["balance_raw"] = bal
            snap["positions_raw"] = pos
            snap["ok"] = bal.get("code") == "0" and pos.get("code") == "0"
            snap["authoritative"] = True
            snap["backoff_until"] = 0.0
            snap.pop("rate_limited", None)
            snap.pop("stale", None)
            if not snap["ok"]:
                snap["error"] = json.dumps(
                    {"balance": bal.get("code"), "positions": pos.get("code")}
                )[:200]
                if _is_rate_limited(bal) or _is_rate_limited(pos):
                    raise RuntimeError("account API rate limited")
        except Exception as exc:
            msg = str(exc).lower()
            if "rate limit" in msg or "403" in msg:
                note_rate_limit(_backoff_sec)
            fallback = _best_fallback_snapshot()
            if fallback:
                return _decorate(
                    fallback,
                    cached=True,
                    stale=True,
                    rate_limited="rate limit" in msg,
                )
            return {
                "equity": 0.0,
                "available": 0.0,
                "positions": [],
                "ok": False,
                "error": str(exc),
                "fetched_at": now,
            }

        if _is_good_snapshot(snap):
            snap["positions"] = enrich_positions(
                snap.get("positions") or [], positions_raw=snap.get("positions_raw")
            )
            _write_disk(snap, locked=True)
        else:
            snap["positions"] = enrich_positions(
                snap.get("positions") or [], positions_raw=snap.get("positions_raw")
            )
        return _decorate(snap, cached=False)
    finally:
        lock.release()


def note_rate_limit(retry_after: float = 90.0) -> None:
    until = time.time() + retry_after
    base = hydrate_from_activity_log() or _read_disk() or {
        "equity": 0.0,
        "available": 0.0,
        "positions": [],
        "fetched_at": 0.0,
    }
    disk = dict(base)
    disk["backoff_until"] = until
    disk["rate_limited"] = True
    disk["stale"] = True
    _write_disk(disk)


def bootstrap_account_cache() -> None:
    """Seed shared cache from the newest live API account event in the activity log."""
    auth = hydrate_from_activity_log()
    if not auth:
        return
    disk = _read_disk() or {}
    if (
        disk
        and not _disk_needs_bootstrap(disk)
        and float(disk.get("fetched_at") or 0) >= float(auth.get("fetched_at") or 0)
    ):
        return
    merged = dict(auth)
    if float(disk.get("backoff_until") or 0) > time.time():
        merged["backoff_until"] = float(disk["backoff_until"])
        merged["rate_limited"] = True
        merged["stale"] = True
    _write_disk(merged)


def cache_age_sec() -> float:
    disk = _read_disk()
    if not disk:
        return 9999.0
    return time.time() - float(disk.get("fetched_at") or 0)


def _positions_by_inst(account: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(p.get("instId")): p
        for p in account.get("positions") or []
        if p.get("instId")
    }


def _display_from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    out = apply_mtm_account(dict(snap))
    sane, reason = _account_display_sane(out)
    if not sane:
        fallback = dict(snap)
        fallback["positions"] = list(snap.get("positions") or [])
        fallback["upl_total"] = _sum_upl(fallback["positions"])
        fallback["mtm"] = False
        fallback["display_repaired"] = True
        fallback["display_warning"] = reason
        return fallback
    return out


def fetch_live_account_snapshot() -> dict[str, Any] | None:
    """Fetch balance + positions directly from BloFin (watchdog verification)."""
    try:
        from blofin.client import BlofinClient

        client = BlofinClient()
        mode = client.ensure_net_position_mode()
        bal = client.get_balance()
        if _is_rate_limited(bal):
            return None
        pos = client.get_positions()
        if _is_rate_limited(pos):
            return None
        snap = _parse_snapshot(client, bal, pos)
        snap["position_mode"] = mode
        snap["balance_raw"] = bal
        snap["positions_raw"] = pos
        snap["ok"] = bal.get("code") == "0" and pos.get("code") == "0"
        snap["authoritative"] = True
        snap["backoff_until"] = 0.0
        snap["fetched_at"] = time.time()
        if not snap["ok"]:
            return None
        return snap
    except Exception:
        return None


def compare_accounts(display: dict[str, Any], reference: dict[str, Any]) -> list[dict[str, Any]]:
    """Return drift entries when dashboard stream diverges from reference (live or last verified)."""
    drift: list[dict[str, Any]] = []
    d_eq = float(display.get("equity") or 0)
    r_eq = float(reference.get("equity") or 0)
    if abs(d_eq - r_eq) > max(0.02, abs(r_eq) * 0.012):
        drift.append({"field": "equity", "display": d_eq, "reference": r_eq})

    d_av = float(display.get("available") or 0)
    r_av = float(reference.get("available") or 0)
    if abs(d_av - r_av) > max(0.02, abs(r_av) * 0.02):
        drift.append({"field": "available", "display": d_av, "reference": r_av})

    d_pos = _positions_by_inst(display)
    r_pos = _positions_by_inst(reference)
    if set(d_pos) != set(r_pos):
        drift.append(
            {
                "field": "positions",
                "display": sorted(d_pos),
                "reference": sorted(r_pos),
            }
        )

    raw_upl = _raw_upl_by_inst(reference.get("positions_raw"))
    for inst, ref_row in r_pos.items():
        disp_row = d_pos.get(inst)
        if not disp_row:
            continue
        ref_upl = float(raw_upl.get(inst, ref_row.get("upl") or 0))
        disp_upl = float(disp_row.get("upl") or 0)
        tol = max(0.0005, abs(ref_upl) * 0.02)
        if abs(disp_upl - ref_upl) > tol:
            drift.append(
                {
                    "field": "upl",
                    "instId": inst,
                    "display": disp_upl,
                    "reference": ref_upl,
                }
            )

    d_upl_total = float(display.get("upl_total") if display.get("upl_total") is not None else _sum_upl(d_pos.values()))
    r_upl_total = float(
        reference.get("upl_total") if reference.get("upl_total") is not None else _sum_upl(r_pos.values())
    )
    if abs(d_upl_total - r_upl_total) > max(0.02, abs(r_upl_total) * 0.03):
        drift.append(
            {
                "field": "upl_total",
                "display": d_upl_total,
                "reference": r_upl_total,
            }
        )
    return drift


def stream_drift_issues() -> list[dict[str, Any]]:
    """Fast drift check vs last live-verified snapshot (no API call)."""
    if not _last_verified_display:
        return []
    display = read_account_cached()
    drift = compare_accounts(display, _last_verified_display)
    if not drift:
        return []
    return drift


def guard_account_stream() -> dict[str, Any]:
    """
    Keep the dashboard stream aligned with live BloFin data.
    Fast path: compare cache to last verified snapshot every call.
    Slow path: hit BloFin API on interval or when drift is detected.
    """
    global _last_verified_display, _last_live_verify_ts

    display = read_account_cached()
    now = time.time()
    result: dict[str, Any] = {
        "ok": True,
        "refreshed": False,
        "drift": [],
        "account": display,
    }

    if is_rate_limited():
        if _last_verified_display:
            drift = compare_accounts(display, _last_verified_display)
            if drift:
                result["ok"] = False
                result["drift"] = drift
                result["skipped_live"] = "rate_limited"
        else:
            result["skipped_live"] = "rate_limited"
        return result

    if _last_verified_display:
        drift = compare_accounts(display, _last_verified_display)
        if drift:
            result["drift"] = drift
            result["ok"] = False

    need_live = (
        _last_verified_display is None
        or (now - _last_live_verify_ts) >= _live_verify_interval_sec
        or not result["ok"]
    )
    if not need_live:
        result["live_verified"] = True
        result["live_age_sec"] = now - _last_live_verify_ts
        return result

    live_snap = fetch_live_account_snapshot()
    if not live_snap:
        result["skipped_live"] = "fetch_failed"
        return result

    _last_live_verify_ts = now
    live_display = _display_from_snapshot(live_snap)
    live_display["positions_raw"] = live_snap.get("positions_raw")
    live_display["authoritative"] = True
    live_display["fetched_at"] = live_snap.get("fetched_at")

    drift = compare_accounts(display, live_display)
    if drift:
        from activity_log import log_event

        get_account_snapshot(force=True)
        display = read_account_cached()
        drift_after = compare_accounts(display, live_display)
        result["refreshed"] = True
        result["drift"] = drift
        result["ok"] = not drift_after
        if drift_after:
            result["drift_after_refresh"] = drift_after
        log_event(
            "system",
            "Stream guardian corrected drift",
            json.dumps(drift[:4], default=str)[:280],
        )
    else:
        result["ok"] = True

    _last_verified_display = live_display
    result["account"] = display
    result["live_verified"] = True
    result["live_age_sec"] = 0.0
    return result


def refresh_account_if_stale(*, force: bool = False) -> None:
    """Refresh shared cache from BloFin when older than the refresh interval."""
    if force or cache_age_sec() >= _min_interval_sec:
        get_account_snapshot(force=force)
