"""Diagnose stack / activity / wreckage for first-trade monitoring."""
from __future__ import annotations

import json
import time
from pathlib import Path

from blofin.paper_ledger import LEDGER_PATH
from config import ACTIVITY_LOG, PID_DIR, STATE_FILE
from trader.health import trader_lock_owner


def main() -> None:
    print("trader_lock", trader_lock_owner())
    for p in sorted(PID_DIR.glob("*")):
        try:
            print("pidfile", p.name, p.read_text(encoding="utf-8", errors="replace").strip()[:60])
        except OSError as exc:
            print("pidfile", p.name, exc)

    st = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.is_file() else {}
    bp = st.get("best_params") or {}
    print("cycles", st.get("cycles"), "exec_ready", st.get("execution_ready"))
    print("best", bp.get("STRATEGY"), bp.get("MARGIN_USE_RATIO"), "peak", st.get("peak_equity"))
    ld = st.get("last_decision")
    if ld:
        print(
            "last_decision",
            ld.get("action"),
            ld.get("instId"),
            ld.get("side"),
            str(ld.get("reasoning") or "")[:140],
        )
    else:
        print("last_decision", None)
    trades = st.get("trades") or []
    print("trades_n", len(trades), "guard_hist", len((st.get("order_guard") or {}).get("history") or []))
    for t in trades[-8:]:
        if isinstance(t, dict):
            print(" trade", t.get("action"), t.get("instId"), "ok=", t.get("ok"))

    led = json.loads(LEDGER_PATH.read_text(encoding="utf-8")) if LEDGER_PATH.is_file() else {}
    print("ledger", led.get("cash"), "pos", list((led.get("positions") or {}).keys()), "fills", len(led.get("fills") or []))

    if ACTIVITY_LOG.is_file():
        raw = ACTIVITY_LOG.read_bytes()
        text = raw[-500000:].decode("utf-8", "replace")
        now = time.time()
        recent = []
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            age = now - float(e.get("ts") or 0)
            if age < 7200:
                recent.append(e)
        print("activity_2h", len(recent))
        for e in recent[-25:]:
            age = now - float(e.get("ts") or 0)
            detail = str(e.get("detail") or "").replace("\n", " ")[:120]
            title = str(e.get("title") or "")
            print(f"  {int(age):5d}s {e.get('type')}|{title}|{detail}")


if __name__ == "__main__":
    main()
