"""Repair Agent 1 — Watchdog.

Monitors the entire LLM KnightTrader stack:
- Activity log for errors and anomalies
- Process counts for duplicates (kills them)
- Dashboard health (port alive)
- Trader online/offline status
- Triggers recovery actions when things go wrong
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from activity_log import get_recent
from config import DASHBOARD_PORT
from trader.repair_agent import (
    agent_main,
    log,
    log_err,
    log_warn,
    llm_ask,
)
from trader.health import kill_duplicate_traders
from trader.stack_control import stack_status

AGENT_NAME = "watchdog"
LABEL = "Repair[Watchdog]"
LOOP_SEC = 20.0

WATCHDOG_SYSTEM = """You are the LLM KnightTrader **Watchdog** — a 24/7 operations monitor.

You watch the entire stack and respond to anything that goes wrong:
- Duplicate processes (multiple traders, crashed zombies)
- Activity log errors (API failures, LLM crashes, trade errors)
- Dashboard offline (port not responding)
- Trader offline when it should be running
- Stale or degraded account state

You are ONLY called when there is a problem. If everything is healthy, respond with {"actions":[]}.

Respond ONLY with valid JSON (no markdown):
{
  "severity": "ok|warning|critical",
  "diagnosis": "What you found (max 200 chars)",
  "actions": [
    {"type": "kill_duplicates", "params": {}},
    {"type": "restart_trader", "params": {}},
    {"type": "restart_dashboard", "params": {}},
    {"type": "notify_human", "params": {"message": "..."}},
    {"type": "hold", "params": {"reason": "..."}}
  ],
  "lesson": {"category": "watchdog", "text": "Durable lesson if worth remembering"} or null
}

You may ONLY use these action types. Max 3 actions per check.
If everything is fine: {"severity":"ok","diagnosis":"all clear","actions":[]}
"""


def _check_port() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DASHBOARD_PORT}/api/health", timeout=3.0) as r:
            return r.status == 200
    except Exception:
        return False


def _meaningful_errors(limit: int = 30) -> list[dict]:
    noise = (
        "repair llm", "repair triage", "repair complete", "repair recovered",
        "repair skipped llm", "stream guardian", "proactive repair",
        "watchdog", "duplicate trader", "[watchdog]",
    )
    out = []
    for event in reversed(get_recent(limit)):
        if event.get("type") != "error":
            continue
        blob = f"{event.get('title','')} {event.get('detail','')}".lower()
        if any(m in blob for m in noise):
            continue
        out.append(event)
    out.reverse()
    return out


def run_cycle(client, llm, state: dict) -> None:
    now = time.time()
    status = stack_status()
    trader_info = status.get("trader", {})
    trader_status = trader_info.get("status", "offline")
    trader_count = trader_info.get("count", 0)
    stack_healthy = status.get("healthy", False)

    port_alive = _check_port()
    errors = _meaningful_errors(20)

    if (
        trader_status == "online"
        and stack_healthy
        and port_alive
        and not errors
        and trader_count == 1
    ):
        state["last_activity_ts"] = now
        return

    lines = []
    actions_taken = []
    if not port_alive:
        lines.append("dashboard port not responding")
    if trader_status == "offline":
        lines.append("trader offline")
    elif trader_status == "duplicate":
        lines.append("duplicate traders detected")
    if errors:
        lines.append(f"{len(errors)} errors in activity log")
        for e in errors[:3]:
            lines.append(f"  - {e.get('title','')}: {e.get('detail','')[:100]}")

    diag = "; ".join(lines)[:200]
    log(LABEL, "Anomaly detected", diag)

    context_lines = [
        f"trader_status={trader_status}",
        f"trader_count={trader_count}",
        f"stack_healthy={stack_healthy}",
        f"port_alive={port_alive}",
        f"error_count={len(errors)}",
    ]
    if errors:
        context_lines.append(f"latest_error: {errors[-1].get('title','')} — {errors[-1].get('detail','')[:200]}")

    user_msg = "\n".join(context_lines)

    state["repairs_attempted"] += 1
    answer = llm_ask(AGENT_NAME, llm, WATCHDOG_SYSTEM, user_msg, max_tokens=400)

    if not answer:
        log_warn(LABEL, "LLM unavailable, falling back to script repair")
        killed = kill_duplicate_traders()
        if killed:
            actions_taken.append(f"killed {killed} duplicates")
        if not port_alive:
            log_warn(LABEL, "Dashboard down — notify human", "manual restart may be needed")
        state["last_action_ts"] = now
        if actions_taken:
            log(LABEL, "Script repair done", "; ".join(actions_taken))
        return

    try:
        plan = json.loads(answer)
    except (json.JSONDecodeError, ValueError):
        stripped = answer.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
            if stripped.endswith("```"):
                stripped = stripped[:-3]
            plan = json.loads(stripped.strip())
        else:
            raise

    severity = plan.get("severity", "warning")
    diagnosis = plan.get("diagnosis", "")
    actions = plan.get("actions", [])

    for action in actions:
        atype = action.get("type", "")
        params = action.get("params", {})
        if atype == "kill_duplicates":
            killed = kill_duplicate_traders()
            actions_taken.append(f"killed_dupes={killed}")
        elif atype == "hold":
            reason = params.get("reason", "")
            actions_taken.append(f"hold:{reason[:80]}")
        elif atype == "notify_human":
            msg = params.get("message", "")
            log_warn(LABEL, "HUMAN NEEDED", msg[:200])
            actions_taken.append("notified_human")

    if severity in ("ok", "warning") and not actions_taken:
        killed = kill_duplicate_traders()
        if killed:
            actions_taken.append(f"killed {killed} dupes")

    state["last_action_ts"] = now
    if actions_taken:
        state["repairs_succeeded"] += 1
        log(LABEL, f"Repair done (severity={severity})", f"actions: {'; '.join(actions_taken)} | {diagnosis}")
    else:
        log(LABEL, f"No action taken (severity={severity})", diagnosis)


if __name__ == "__main__":
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
    )
