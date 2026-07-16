"""Point-of-view / cycle closure helpers.

Keeps the live loop consistent after closes:
- refreshes scan morphologies after state changes
- repairs closed or missing positions from the recent last-scan snapshot
- deduplicates position activity feed entries
- exposes concise edge/health signals for the self-heal and edge loops.
"""

from __future__ import annotations

import collections
import json
import time
from typing import Any

from activity_log import get_recent, log_event
from config import DATA_DIR
from trader.state import load_state, save_state

_LAST_SCAN_FILE = DATA_DIR / "last_scan.json"
_CYCLE_REFRESH_EVENTS = (
    "Harvested winner",
    "Closed ",
    "Opened ",
    "Open retry ok",
    "TP/SL attached",
    "TP/SL sync complete",
)


class CycleSnapshot:
    def __init__(self, state: dict[str, Any], account: dict[str, Any], scan: list[dict[str, Any]]) -> None:
        self.state = dict(state or {})
        self.account = dict(account or {})
        self.scan = [dict(row) for row in (scan or [])]
        self.ts = time.time()


def _load_last_scan_set() -> dict[str, dict[str, Any]]:
    if not _LAST_SCAN_FILE.is_file():
        return {}
    try:
        data = json.loads(_LAST_SCAN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    setups = data.get("setups") or []
    out: dict[str, dict[str, Any]] = {}
    for idx, row in enumerate(setups):
        inst = str(row.get("instId") or "").strip()
        if not inst:
            continue
        out[inst] = dict(row)
        out[inst]["_scan_index"] = idx
    return out


def _morphology_changed(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if not a or not b:
        return True
    keys = ("instId", "side", "score", "c1_pct", "c5_pct", "min_leverage", "est_margin", "price")
    for key in keys:
        av = a.get(key)
        bv = b.get(key)
        try:
            if av != bv:
                return True
        except Exception:
            return True
    return False


def _recent_major_events(limit: int = 24) -> list[dict[str, Any]]:
    try:
        return [e for e in get_recent(limit) if e.get("type") in ("trade", "error", "system")]
    except Exception:
        return []


def _recent_activity_hints(limit: int = 80) -> list[str]:
    hints: list[str] = []
    for event in _recent_major_events(limit):
        title = str(event.get("title") or "")
        detail = str(event.get("detail") or "")
        payload = json.dumps(event.get("data") or {}, default=str)
        if any(token in title or token in detail or token in payload for token in _CYCLE_REFRESH_EVENTS):
            hints.append(f"{title}|{detail[:80]}")
    return hints[-20:]


def _recent_inst_activity(limit: int = 120) -> dict[str, list[str]]:
    out: dict[str, list[str]] = collections.defaultdict(list)
    for event in get_recent(limit):
        payload = event.get("data") or {}
        inst = str(payload.get("instId") or "").strip()
        if not inst:
            continue
        out[inst].append(str(event.get("title") or ""))
    return out


def repair_missing_positions(account: dict[str, Any]) -> tuple[list[str], list[str]]:
    repaired: list[str] = []
    missing: list[str] = []
    last_scan = _load_last_scan_set()
    positions = {str(p.get("instId") or ""): p for p in (account.get("positions") or []) if p.get("instId")}
    position_raws = account.get("positions_raw") or {}
    for prev_inst, morph in list(last_scan.items()):
        if prev_inst in positions:
            continue
        raw = position_raws.get(prev_inst) if isinstance(position_raws, dict) else {}
        if raw.get("instId"):
            repaired.append(prev_inst)
            continue
        missing.append(prev_inst)
    return missing, repaired


def cycle_refresh(
    snapshot: CycleSnapshot,
    *,
    refresh_scan_on_changed: bool = True,
    repair_closed_positions: bool = True,
) -> CycleSnapshot:
    if repair_closed_positions:
        missing, repaired = repair_missing_positions(snapshot.account)
        if missing or repaired:
            log_event(
                "system",
                "POS ouvrir refresh",
                json.dumps({"missing": missing[:20], "repaired": repaired[:20]}, default=str)[:500],
                {"inst_missing": missing[:20], "inst_repaired": repaired[:20]},
            )
    hints = _recent_activity_hints()
    if hints and refresh_scan_on_changed:
        try:
            from trader.agent import _scan_universe
            from blofin.client import BlofinClient

            client = BlofinClient()
            new_scan = _scan_universe(client, snapshot.state) or []
            if new_scan:
                new_index = _load_last_scan_set()
                merged = []
                changed = 0
                for row in snapshot.scan:
                    inst = str(row.get("instId") or "")
                    base = dict(row)
                    latest = new_index.get(inst) or new_and_lookup.get(inst)
                    if latest is not None:
                        new_and_lookup = {str(r.get("instId") or ""): r for r in new_scan}
                        latest = new_and_lookup.get(inst)
                    if latest is not None and _morphology_changed(base, latest):
                        changed += 1
                        base.update({
                            "score": latest.get("score", base.get("score")),
                            "c1_pct": latest.get("c1_pct", base.get("c1_pct")),
                            "c5_pct": latest.get("c5_pct", base.get("c5_pct")),
                            "min_leverage": latest.get("min_leverage", base.get("min_leverage")),
                            "est_margin": latest.get("est_margin", base.get("est_margin")),
                            "price": latest.get("price", base.get("price")),
                            "side": latest.get("side", base.get("side")),
                            "refreshed_ts": snapshot.ts,
                        })
                    merged.append(base)
                if changed:
                    log_event("research", "Scan morphology updated", f"changed={changed}")
                else:
                    scan_lookup = {str(r.get("instId") or ""): r for r in new_scan}
                    fresh = []
                    changed = 0
                    for row in snapshot.scan:
                        inst = str(row.get("instId") or "")
                        latest = scan_lookup.get(inst)
                        if latest and _morphology_changed(row, latest):
                            changed += 1
                            row = dict(row)
                            row.update({
                                "score": latest.get("score", row.get("score")),
                                "c1_pct": latest.get("c1_pct", row.get("c1_pct")),
                                "c5_pct": latest.get("c5_pct", row.get("c5_pct")),
                                "min_leverage": latest.get("min_leverage", row.get("min_leverage")),
                                "est_margin": latest.get("est_margin", row.get("est_margin")),
                                "price": latest.get("price", row.get("price")),
                                "side": latest.get("side", row.get("side")),
                                "refreshed_ts": snapshot.ts,
                            })
                        fresh.append(row)
                    if changed:
                        log_event("research", "Scan morphology updated", f"changed={changed}")
                    merged = fresh
                snapshot.scan = merged or snapshot.scan
                snapshot.account["_scan"] = snapshot.scan
        except Exception as exc:
            log_event("error", "Scan morphology refresh failed", str(exc)[:220])
    return snapshot


def dedupe_trade_feed(state: dict[str, Any], *, max_keep: int = 180) -> None:
    trades = list(state.get("trades") or [])
    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    for trade in trades:
        inst = str(trade.get("instId") or "").strip()
        if not inst:
            continue
        key = (
            inst,
            str(trade.get("action") or "").strip().lower(),
            str(float(trade.get("ts") or 0.0)),
        )
        if key in seen:
            continue
        seen.add(key)
        clean.append(trade)
    state["trades"] = clean[-max_keep:]


def edge_signal_from_account(account: dict[str, Any]) -> dict[str, Any]:
    positions = account.get("positions") or []
    if not positions:
        return {"ok": True, "signal": "clear", "reason": "no_open_positions"}
    stress = market_stress(account)
    if stress:
        return {"ok": True, "signal": "reduce_risk", "stress": stress}
    return {"ok": True, "signal": "manage_existing", "positions": len(positions)}


def market_stress(account: dict[str, Any]) -> dict[str, Any] | None:
    positions = account.get("positions") or []
    if len(positions) >= 18:
        return {"type": "over_exposure", "positions": len(positions)}
    margin_use = market_margin_use_ratio(account)
    if margin_use >= 0.92:
        return {"type": "margin_stress", "ratio": round(margin_use, 4)}
    return None


def market_margin_use_ratio(account: dict[str, Any]) -> float:
    equity = float(account.get("equity") or 0)
    used = float(account.get("used_margin") or account.get("margin_used") or 0)
    if equity <= 0:
        return 1.0
    return max(0.0, min(1.0, used / equity))
