"""Hermes-style persistent learning — realized PnL wins only, throttled noise."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from activity_log import log_event
from config import DATA_DIR
from trader.pnl_tracker import MIN_REALIZED_LOSS_USD, MIN_REALIZED_WIN_USD, performance_summary, record_close_entry

MAX_LESSONS = 80
MAX_LESSON_CHARS = 220
RISK_LESSON_COOLDOWN_SEC = 1800

_ERROR_LESSONS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"103003|insufficient margin", re.I), "margin", "Raise leverage ladder or pick lower-notional perp before open; never repeat raw 103003."),
    (re.compile(r"102040|trigger price should be", re.I), "tpsl", "Short SL must be above mark; long SL below. Use positive tp_pct/sl_pct only."),
    (re.compile(r"102089|positionSide", re.I), "order", "Account is net_mode — orders must use positionSide=net."),
    (re.compile(r"rate limit|403|<!doctype", re.I), "api", "BloFin rate limited — hold opens/closes until cooldown clears."),
    (re.compile(r"LLM cycle failed|JSON parse|empty LLM", re.I), "llm", "Return compact valid JSON only; avoid long reasoning strings."),
    (re.compile(r"Close failed|close_failed", re.I), "close", "Verify live position exists before close; check net side and size."),
    (re.compile(r"Open failed|open_failed", re.I), "open", "Confirm affordable setup (est_margin) before open; redirect if too expensive."),
    (re.compile(r"phantom|hydrated|from_trades", re.I), "account", "Trust live API positions_raw only — ignore trade-log phantom positions."),
    (re.compile(r"churn|repeat open|recovery mode", re.I), "churn", "Sub-peak equity: do not re-trade same symbol — pick a new instId or hold."),
    (re.compile(r"malformed open|missing instId|truncated inst", re.I), "open", "Always emit full instId (e.g. BILL-USDT) and side buy|sell; stack will auto-complete truncations from scan when possible."),
    (re.compile(r"UnicodeDecodeError|invalid start byte|utf-8", re.I), "log", "Activity log must be read as utf-8 with errors=replace / tail-only — never full strict decode of activity.jsonl."),
    (re.compile(r"ModuleNotFoundError: No module named 'trader'|No module named 'llm'", re.I), "env", "Child processes need PYTHONPATH=project root; restart with stack_launcher env, do not spin repair LLM."),
    (re.compile(r"proactive_anomaly|Hold if errors persist after auto refresh", re.I), "repair", "Skip proactive_anomaly when no position/harvest gaps; refresh only, resume trading."),
]


def _lesson_key(category: str, text: str) -> str:
    return f"{category}:{text.strip().lower()[:120]}"


def append_lesson(
    state: dict[str, Any],
    *,
    category: str,
    lesson: str,
    source: str = "cycle",
    severity: str = "warn",
) -> bool:
    lesson = lesson.strip()[:MAX_LESSON_CHARS]
    if not lesson:
        return False
    lessons: list[dict[str, Any]] = list(state.get("lessons") or [])
    key = _lesson_key(category, lesson)
    now = time.time()
    for row in lessons:
        if row.get("_key") == key:
            row["count"] = int(row.get("count") or 1) + 1
            row["last_ts"] = now
            row["source"] = source
            state["lessons"] = lessons[-MAX_LESSONS:]
            return False
    lessons.append(
        {
            "_key": key,
            "ts": now,
            "last_ts": now,
            "category": category,
            "lesson": lesson,
            "source": source,
            "severity": severity,
            "count": 1,
        }
    )
    state["lessons"] = lessons[-MAX_LESSONS:]
    log_event("research", "Lesson learned", f"[{category}] {lesson}", {"category": category, "source": source})
    return True


def append_lesson_once_per_interval(
    state: dict[str, Any],
    *,
    category: str,
    lesson: str,
    interval_key: str,
    interval_sec: float = RISK_LESSON_COOLDOWN_SEC,
    source: str = "equity",
) -> bool:
    throttle = state.setdefault("_lesson_throttle", {})
    now = time.time()
    last = float(throttle.get(interval_key) or 0)
    if now - last < interval_sec:
        return False
    if append_lesson(state, category=category, lesson=lesson, source=source):
        throttle[interval_key] = now
        return True
    throttle[interval_key] = now
    return False


def lessons_digest(state: dict[str, Any], *, limit: int = 15) -> str:
    rows = list(state.get("lessons") or [])
    if not rows:
        return "No lessons yet — wins are recorded only after realized PnL on close."
    perf = performance_summary(state)
    header = (
        f"Realized PnL: ${perf['total_realized_pnl']:.4f} "
        f"({perf['realized_wins']}W / {perf['realized_losses']}L over {perf['closed_trades']} closes). "
        "Repeat patterns from realized_win only — fills alone are not wins."
    )
    rows.sort(key=lambda r: (int(r.get("count") or 1), float(r.get("last_ts") or r.get("ts") or 0)), reverse=True)
    win_cats = {"realized_win", "success"}
    wins = [r for r in rows if r.get("category") in win_cats][: max(4, limit // 3)]
    rest = [r for r in rows if r.get("category") not in win_cats]
    ordered = wins + [r for r in rest if r not in wins]
    lines: list[str] = [header]
    if wins:
        lines.append("What worked (realized profit — repeat these):")
    for row in ordered[:limit]:
        cat = row.get("category") or "general"
        txt = row.get("lesson") or ""
        cnt = int(row.get("count") or 1)
        suffix = f" (×{cnt})" if cnt > 1 else ""
        prefix = "  " if row in wins else "- "
        lines.append(f"{prefix}[{cat}] {txt}{suffix}")
    return "\n".join(lines)


def _match_error_lessons(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for pat, cat, lesson in _ERROR_LESSONS:
        if pat.search(text):
            out.append((cat, lesson))
    return out


def _lesson_from_realized_close(state: dict[str, Any], tr: dict[str, Any]) -> bool:
    inst = str(tr.get("instId") or "")
    pnl = float(tr.get("realized_pnl") if tr.get("realized_pnl") is not None else 0)
    if not inst:
        return False
    entry = record_close_entry(state, inst_id=inst, side=tr.get("side"), realized_pnl=pnl)
    lev = entry.get("leverage") or "?"
    hold_min = int(float(entry.get("hold_sec") or 0) // 60)

    if pnl >= MIN_REALIZED_WIN_USD:
        append_lesson(
            state,
            category="realized_win",
            lesson=f"Closed {inst} +${pnl:.4f} ({hold_min}m hold, {lev}x) — repeat this entry/exit pattern.",
            source="pnl",
            severity="info",
        )
        log_event("research", "Realized win", f"{inst} +${pnl:.4f}", {"instId": inst, "pnl": pnl})
        return True
    if pnl <= -MIN_REALIZED_LOSS_USD:
        append_lesson(
            state,
            category="realized_loss",
            lesson=f"Closed {inst} ${pnl:.4f} — avoid re-opening {inst} until setup changes materially.",
            source="pnl",
        )
        return True
    return False


def learn_from_trade_results(state: dict[str, Any], trade_results: list[dict[str, Any]]) -> int:
    added = 0
    for tr in trade_results:
        action = str(tr.get("action") or "")
        inst = str(tr.get("instId") or "")
        resp = tr.get("response") or {}
        blob = json.dumps(resp)[:400]
        detail = f"{action} {inst} {blob}"

        if tr.get("ok") is False or action.endswith("_failed") or "failed" in action:
            for cat, lesson in _match_error_lessons(detail):
                if append_lesson(state, category=cat, lesson=f"{inst}: {lesson}" if inst else lesson, source="trade"):
                    added += 1
            err_rows = (resp.get("data") or []) if isinstance(resp.get("data"), list) else []
            for row in err_rows:
                code = str(row.get("code") or "")
                msg = str(row.get("msg") or "")
                if code and code not in ("0", ""):
                    for cat, lesson in _match_error_lessons(f"{code}: {msg}"):
                        if append_lesson(
                            state,
                            category=cat,
                            lesson=f"{inst} {code}: {msg[:80]}" if inst else f"{code}: {msg[:80]}",
                            source="trade",
                        ):
                            added += 1
        elif action == "close" and tr.get("ok") is not False:
            if _lesson_from_realized_close(state, tr):
                added += 1
        elif action == "open_blocked":
            reason = tr.get("reason") or "repeat order blocked"
            append_lesson(state, category="churn", lesson=f"{inst}: {reason}", source="guard")
            added += 1
    return added


def learn_from_activity_tail(state: dict[str, Any], *, tail_lines: int = 400) -> int:
    log_path = DATA_DIR / "activity.jsonl"
    if not log_path.is_file():
        return 0
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    added = 0
    for line in lines[-tail_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "error":
            continue
        text = f"{ev.get('title') or ''} {ev.get('detail') or ''}"
        for cat, lesson in _match_error_lessons(text):
            if append_lesson(state, category=cat, lesson=lesson, source="activity"):
                added += 1
    return added


def learn_from_cycle(
    state: dict[str, Any],
    *,
    account: dict[str, Any],
    decision: dict[str, Any],
    trade_results: list[dict[str, Any]],
    peak_equity: float,
) -> None:
    learn_from_trade_results(state, trade_results)

    equity = float(account.get("equity") or 0)
    peak = float(state.get("peak_equity") or 0)
    prev_lesson_peak = float(state.get("_last_lesson_peak") or 0)
    if peak > prev_lesson_peak + 0.05 and equity >= peak * 0.995:
        append_lesson(
            state,
            category="realized_win",
            lesson=f"New equity peak ${peak:.2f} — repeat recent profitable close patterns only.",
            source="equity",
            severity="info",
        )
        state["_last_lesson_peak"] = peak

    if peak_equity > 0 and equity < peak_equity * 0.85:
        append_lesson_once_per_interval(
            state,
            category="risk",
            lesson=f"Recovery mode: equity ${equity:.2f} vs peak ${peak_equity:.2f} — no symbol churn; new instId only or hold.",
            interval_key="drawdown_recovery",
            source="equity",
        )

    if decision.get("strategy_update"):
        note = str(decision["strategy_update"]).strip()
        low = note.lower()
        if any(w in low for w in ("never", "avoid", "stop", "don't", "do not", "mistake", "error", "churn")):
            append_lesson(state, category="strategy", lesson=note[:MAX_LESSON_CHARS], source="llm")
