"""Connect learning + state to execution — block repeat / churn orders."""

from __future__ import annotations

import re
import time
from typing import Any

from activity_log import log_event
from trader.learning import append_lesson
from trader.pnl_tracker import drawdown_state, performance_summary
from trader.sizing import ABSOLUTE_MIN_MARGIN, budget_for_decision, margin_budget_for_setup

_INST_RE = re.compile(r"\b([A-Z0-9]{2,15}-USDT)\b", re.I)
_SIDE_BUY_RE = re.compile(r"\b(buy|long|bullish)\b", re.I)
_SIDE_SELL_RE = re.compile(r"\b(sell|short|bearish)\b", re.I)


def _scan_inst_ids(scan: list[dict[str, Any]] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for row in scan or []:
        inst = str(row.get("instId") or "").strip().upper()
        if inst and inst not in seen:
            seen.add(inst)
            out.append(inst)
    return out


def _complete_inst_id(raw: str, scan: list[dict[str, Any]] | None, blob: str) -> str | None:
    """Resolve truncated or partial instId using scan + research text."""
    raw_u = (raw or "").strip().upper()
    ids = _scan_inst_ids(scan)
    if raw_u in ids:
        return raw_u
    if raw_u and "-" in raw_u and raw_u.endswith("USDT"):
        return raw_u
    # Exact mention in research/reasoning takes priority.
    for m in _INST_RE.finditer(blob or ""):
        cand = m.group(1).upper()
        if not ids or cand in ids:
            return cand
    if not raw_u or not ids:
        return None
    # Unique prefix among live scan (e.g. BI → BILL-USDT).
    matches = [i for i in ids if i.startswith(raw_u) or i.split("-", 1)[0].startswith(raw_u)]
    if len(matches) == 1:
        return matches[0]
    # Prefer affordable/high-score row when multiple prefix matches.
    if matches and scan:
        ranked = sorted(
            (r for r in scan if str(r.get("instId") or "").upper() in matches),
            key=lambda r: abs(float(r.get("score") or 0)),
            reverse=True,
        )
        if ranked:
            return str(ranked[0].get("instId") or "").upper()
    return None


def _infer_side(raw_side: Any, blob: str, scan_row: dict[str, Any] | None) -> str | None:
    side = str(raw_side or "").strip().lower()
    if side in ("buy", "sell"):
        return side
    if scan_row:
        s = str(scan_row.get("side") or "").strip().lower()
        if s in ("buy", "sell"):
            return s
    buy = bool(_SIDE_BUY_RE.search(blob or ""))
    sell = bool(_SIDE_SELL_RE.search(blob or ""))
    if buy and not sell:
        return "buy"
    if sell and not buy:
        return "sell"
    return None


def repair_open_decision(
    decision: dict[str, Any],
    scan: list[dict[str, Any]] | None,
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Auto-complete truncated instId / missing side from scan + research.
    Unrecoverable opens become hold (with a lesson), not silent spam.
    """
    action = str(decision.get("action") or "hold").lower()
    if action != "open":
        return decision

    blob = " ".join(
        str(decision.get(k) or "")
        for k in ("research", "reasoning", "strategy_update", "instId", "side")
    )
    fixed = dict(decision)
    raw_inst = str(fixed.get("instId") or "").strip()
    completed = _complete_inst_id(raw_inst, scan, blob)
    if completed:
        fixed["instId"] = completed

    scan_row = next(
        (r for r in (scan or []) if str(r.get("instId") or "").upper() == str(fixed.get("instId") or "").upper()),
        None,
    )
    side = _infer_side(fixed.get("side"), blob, scan_row)
    if side:
        fixed["side"] = side

    inst_ok = bool(fixed.get("instId")) and "-" in str(fixed.get("instId"))
    side_ok = str(fixed.get("side") or "").lower() in ("buy", "sell")
    if inst_ok and side_ok:
        if completed and completed != raw_inst.upper():
            note = f"Completed truncated instId {raw_inst!r} → {completed}"
            log_event("trade", "Open decision repaired", note, {"from": raw_inst, "to": completed, "side": side})
            if state is not None:
                append_lesson(state, category="open", lesson=note, source="open_repair")
        return fixed

    held = dict(fixed)
    held["action"] = "hold"
    held["_malformed_held"] = True
    held["reasoning"] = (
        f"malformed open unrepaired (instId={fixed.get('instId')!r} side={fixed.get('side')!r}) — held"
    )
    log_event(
        "trade",
        "Malformed open held",
        held["reasoning"],
        {"instId": fixed.get("instId"), "side": fixed.get("side")},
    )
    if state is not None:
        append_lesson(
            state,
            category="open",
            lesson="Emit full COIN-USDT instId and buy|sell; stack auto-completes truncations from scan when unique.",
            source="open_repair",
        )
    return held

# Base cooldowns (seconds) — scaled up in sub-peak / recovery mode.
OPEN_SAME_INST_COOLDOWN_SEC = 10 * 60
FAILED_OPEN_COOLDOWN_SEC = 15 * 60
DUPLICATE_DECISION_CYCLES = 2
CHURN_AFTER_CLOSE_SEC = 5 * 60
SYMBOL_DAILY_TOUCH_LIMIT_RECOVERY = 2
SYMBOL_DAILY_TOUCH_LIMIT_SUBPEAK = 4
SYMBOL_LOOKBACK_SEC = 24 * 3600


def _pos_size(pos: dict[str, Any]) -> float:
    return abs(float(pos.get("size") or pos.get("positions") or 0))


def _open_positions(account: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pos in account.get("positions") or []:
        if _pos_size(pos) > 0:
            out.append(pos)
    return out


def _trade_ok(tr: dict[str, Any]) -> bool:
    if tr.get("ok") is False:
        return False
    action = str(tr.get("action") or "")
    if action.endswith("_failed") or "failed" in action:
        return False
    resp = tr.get("response") or {}
    if resp.get("code") not in ("0", 0, None, ""):
        rows = resp.get("data") or []
        if isinstance(rows, list) and rows:
            code = str(rows[0].get("code") or "")
            if code and code not in ("0", ""):
                return False
    return action in ("open", "close", "tpsl_ok")


def _history(state: dict[str, Any]) -> list[dict[str, Any]]:
    guard = state.setdefault("order_guard", {})
    rows = list(guard.get("history") or [])
    guard["history"] = rows[-300:]
    return guard["history"]


def _cooldown_multiplier(state: dict[str, Any], account: dict[str, Any]) -> float:
    dd = drawdown_state(account, state)
    if dd["recovery_mode"]:
        return 2.0
    if dd["sub_peak"]:
        return 1.5
    return 1.0


def _symbol_open_count(state: dict[str, Any], inst: str, *, window_sec: float = SYMBOL_LOOKBACK_SEC) -> int:
    cutoff = time.time() - window_sec
    return sum(
        1
        for h in _history(state)
        if h.get("instId") == inst
        and h.get("action") == "open"
        and h.get("ok")
        and not h.get("blocked")
        and float(h.get("ts") or 0) >= cutoff
    )


def record_execution(
    state: dict[str, Any],
    decision: dict[str, Any],
    trade_results: list[dict[str, Any]],
) -> None:
    now = time.time()
    action = str(decision.get("action") or "hold").lower()
    inst = decision.get("instId")
    side = decision.get("side")
    ok = any(_trade_ok(tr) for tr in trade_results)
    blocked = bool(decision.get("_guard_blocked"))
    row = {
        "ts": now,
        "cycle": state.get("cycles", 0),
        "action": action,
        "instId": inst,
        "side": side,
        "ok": ok,
        "blocked": blocked,
        "reason": decision.get("_guard_reason") or decision.get("reasoning", "")[:160],
        "results": [
            {"action": tr.get("action"), "instId": tr.get("instId"), "ok": _trade_ok(tr)}
            for tr in trade_results[:6]
        ],
    }
    hist = _history(state)
    hist.append(row)
    state["order_guard"]["history"] = hist[-300:]
    state["last_executed_decision"] = {"ts": now, "action": action, "instId": inst, "side": side, "ok": ok}


def _recent_history(state: dict[str, Any], *, window_sec: float) -> list[dict[str, Any]]:
    cutoff = time.time() - window_sec
    return [h for h in _history(state) if float(h.get("ts") or 0) >= cutoff]


def validate_open(
    state: dict[str, Any],
    account: dict[str, Any],
    decision: dict[str, Any],
) -> tuple[bool, str]:
    inst = str(decision.get("instId") or "").strip()
    side = str(decision.get("side") or "").strip().lower()
    if not inst or side not in ("buy", "sell"):
        return False, "missing instId or side"

    dd = drawdown_state(account, state)
    mult = _cooldown_multiplier(state, account)
    open_cd = OPEN_SAME_INST_COOLDOWN_SEC * mult
    fail_cd = FAILED_OPEN_COOLDOWN_SEC * mult
    churn_cd = CHURN_AFTER_CLOSE_SEC * mult

    positions = _open_positions(account)
    open_inst_ids = {str(p.get("instId") or "") for p in positions}

    if inst in open_inst_ids:
        return False, f"already have open position on {inst} — manage or close first"

    sizing = budget_for_decision(account, decision)
    budget = float(sizing.get("margin_budget") or 0)
    if budget < ABSOLUTE_MIN_MARGIN:
        conf = decision.get("confidence", "?")
        return False, (
            f"insufficient margin for conviction {conf} setup "
            f"(budget ${budget:.2f} < ${ABSOLUTE_MIN_MARGIN:.2f} floor)"
        )

    # Sub-peak: limit how many times each symbol can be traded per day.
    touch_limit = SYMBOL_DAILY_TOUCH_LIMIT_RECOVERY if dd["recovery_mode"] else (
        SYMBOL_DAILY_TOUCH_LIMIT_SUBPEAK if dd["sub_peak"] else 99
    )
    touches = _symbol_open_count(state, inst)
    if touches >= touch_limit:
        mode = "recovery" if dd["recovery_mode"] else "sub-peak"
        return False, f"{mode} anti-churn: {inst} already opened {touches}x in 24h — pick a different symbol"

    # Block re-opening symbols with very recent realized losses.
    for entry in reversed(list(state.get("pnl_ledger", {}).get("entries") or [])[-20:]):
        if entry.get("instId") != inst:
            continue
        pnl = float(entry.get("realized_pnl") or 0)
        age = time.time() - float(entry.get("ts") or 0)
        if pnl < -0.10 and age < 6 * 60 * 60:
            return False, f"recent loss on {inst} (${pnl:.4f}) — no re-open for 6h"

    now = time.time()
    recent = _recent_history(state, window_sec=open_cd)

    for row in reversed(recent):
        if row.get("instId") != inst:
            continue
        if row.get("action") == "open" and row.get("ok") and not row.get("blocked"):
            age = now - float(row.get("ts") or 0)
            return False, f"opened {inst} {int(age // 60)}m ago — no repeat add"

    fail_recent = _recent_history(state, window_sec=fail_cd)
    for row in reversed(fail_recent):
        if row.get("instId") != inst:
            continue
        if row.get("action") == "open" and not row.get("ok") and not row.get("blocked"):
            age = now - float(row.get("ts") or 0)
            return False, f"recent failed open on {inst} ({int(age // 60)}m ago) — pick another setup"

    for row in reversed(_recent_history(state, window_sec=churn_cd)):
        if row.get("instId") != inst:
            continue
        if row.get("action") == "close" and row.get("ok"):
            age = now - float(row.get("ts") or 0)
            return False, f"closed {inst} {int(age // 60)}m ago — anti-churn cooldown"

    last_decisions = [
        h
        for h in reversed(_history(state)[-20:])
        if h.get("action") == "open" and h.get("instId") == inst and h.get("side") == side
    ]
    if len(last_decisions) >= DUPLICATE_DECISION_CYCLES:
        if all(not h.get("blocked") for h in last_decisions[:DUPLICATE_DECISION_CYCLES]):
            return False, f"duplicate open {side} {inst} — already attempted {DUPLICATE_DECISION_CYCLES}x"

    return True, ""


def apply_open_guard(
    state: dict[str, Any],
    account: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    action = str(decision.get("action") or "hold").lower()
    if action != "open":
        return decision

    allowed, reason = validate_open(state, account, decision)
    if allowed:
        return decision

    inst = decision.get("instId")
    append_lesson(state, category="churn", lesson=f"Blocked repeat/churn {inst}: {reason}", source="guard")
    log_event("trade", "Repeat open blocked", reason, {"instId": inst, "side": decision.get("side"), "guard": True})
    blocked = dict(decision)
    blocked["action"] = "hold"
    blocked["_guard_blocked"] = True
    blocked["_guard_reason"] = reason
    blocked["reasoning"] = f"Execution guard: {reason}"
    blocked["strategy_update"] = f"No churn: {reason[:80]}"
    return blocked


def _blocked_list(state: dict[str, Any], account: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for pos in _open_positions(account):
        inst = str(pos.get("instId") or "")
        if inst:
            out.append(f"{inst}: position already open")
    dd = drawdown_state(account, state)
    if dd["recovery_mode"]:
        out.append("RECOVERY: anti-churn on same symbol; still fill book with NEW instIds while margin allows")
    elif dd["sub_peak"]:
        out.append(f"SUB-PEAK ({dd['drawdown_pct']}% below peak): prefer new symbols, no same-instrument churn")
    seen: set[str] = set()
    for side in ("buy", "sell"):
        for row in _recent_history(state, window_sec=SYMBOL_LOOKBACK_SEC):
            inst = str(row.get("instId") or "")
            if not inst or inst in seen:
                continue
            allowed, reason = validate_open(state, account, {"action": "open", "instId": inst, "side": side, "confidence": 80})
            if not allowed and reason not in seen:
                seen.add(reason)
                out.append(reason if inst in reason else f"{inst}: {reason}")
    return out[:14]


def execution_context(state: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    from trader.harvest import harvest_context

    positions = _open_positions(account)
    dd = drawdown_state(account, state)
    perf = performance_summary(state)
    example = margin_budget_for_setup(account, confidence=75, score=5)
    return {
        "open_position_count": len(positions),
        "open_instruments": [p.get("instId") for p in positions],
        "blocked_repeat_opens": _blocked_list(state, account),
        "drawdown": dd,
        "realized_pnl_summary": perf,
        "blohunter_harvest": harvest_context(positions, account.get("positions_raw")),
        "sizing": {
            "policy": (
                "No max position count. Each open sized from conviction × sentiment × "
                "available margin × portfolio heat."
            ),
            "available_margin": round(float(account.get("available") or 0), 4),
            "portfolio_heat": example.get("portfolio_heat"),
            "example_at_conf_75_score_5": example,
        },
        "recent_executions": [
            {
                "action": h.get("action"),
                "instId": h.get("instId"),
                "ok": h.get("ok"),
                "blocked": h.get("blocked"),
                "mins_ago": int((time.time() - float(h.get("ts") or 0)) // 60),
            }
            for h in reversed(_history(state)[-8:])
        ],
        "rules": [
            "Size each open from confidence + scan score + available margin (see sizing).",
            "Only CLOSE to harvest winners at +NTP% (see blohunter_harvest). Never close losers or sub-floor greens.",
            "Success = realized PnL on harvest close, not open fills.",
            "Do NOT open an instrument already in open_instruments.",
            "Do NOT repeat open on inst in blocked_repeat_opens.",
            "Higher conviction + stronger sentiment → larger margin slice; more open positions → softer heat dilution.",
        ],
    }


def bootstrap_order_guard_from_trades(state: dict[str, Any]) -> int:
    guard = state.setdefault("order_guard", {"history": []})
    if guard.get("history"):
        return 0
    added = 0
    for tr in state.get("trades") or []:
        action = str(tr.get("action") or "")
        if action not in ("open", "close", "open_blocked", "close_failed"):
            continue
        resp = tr.get("response") or {}
        ok = tr.get("ok")
        if ok is None:
            ok = action in ("open", "close") and (not resp or str(resp.get("code", "0")) in ("0", ""))
        guard["history"].append(
            {
                "ts": float(tr.get("ts") or time.time()),
                "cycle": 0,
                "action": action.replace("_blocked", "").replace("_failed", ""),
                "instId": tr.get("instId"),
                "side": tr.get("side"),
                "ok": bool(ok) and "failed" not in action and "blocked" not in action,
                "blocked": "blocked" in action,
                "reason": tr.get("reason", ""),
                "results": [],
            }
        )
        added += 1
    guard["history"] = guard["history"][-300:]
    return added
