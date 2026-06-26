"""Repair Agent 1 — Watchdog.

A master systems technician for the LLM KnightTrader stack.
Diagnoses from evidence, doesn't need prior knowledge of the codebase.
Reads logs, checks processes, inspects source, applies fixes, restarts.

Think of it like a car mechanic who doesn't know the model but listens
to the engine, checks the dashboard lights, reads the error codes, and fixes.
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
from config import DASHBOARD_PORT, DATA_DIR, PROJECT_ROOT
from trader.repair_agent import (
    agent_main,
    get_recent_errors,
    get_stack_status,
    get_log_tail,
    list_source_files,
    log,
    log_err,
    log_warn,
    llm_ask,
    read_file_safe,
    write_file_safe,
    run_cmd,
)
from trader.health import kill_duplicate_traders

AGENT_NAME = "watchdog"
LABEL = "Repair[Watchdog]"
LOOP_SEC = 15.0

SYSTEM = """You are the LLM KnightTrader **Watchdog** — a master operations technician.

You diagnose and fix ANYTHING that goes wrong with the stack. You don't need to
know the codebase in advance — you read source files, logs, and error messages
to figure out what's happening, just like a mechanic diagnosing a car from sounds
and warning lights.

## YOUR TOOLS
- Read ANY source file in the project to understand the code
- Check activity log for errors, warnings, anomalies
- Check process counts (duplicate traders, offline services)
- Check dashboard port health
- Read log files (trader.err, repair logs)
- Edit source files to fix bugs
- Kill/restart processes
- Run shell commands to inspect the system

## YOUR PERSONALITY
- Methodical: gather evidence before acting
- Conservative: prefer hold over reckless action when uncertain
- Thorough: after fixing, verify the fix worked
- Learning: remember patterns in state["seen_fingerprints"]

## OUTPUT FORMAT
Respond ONLY with valid JSON (no markdown):
{
  "diagnosis": "What you found and why (max 300 chars)",
  "confidence": 0-100,
  "actions_taken": ["description of what you did"],
  "fixed": true|false,
  "needs_human": false,
  "notes": "Anything worth remembering for next time"
}

If everything is fine: {"diagnosis":"all clear","confidence":100,"actions_taken":[],"fixed":true,"needs_human":false}
"""


def _check_port() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DASHBOARD_PORT}/api/health", timeout=3.0) as r:
            return r.status == 200
    except Exception:
        return False


def _gather_evidence(client) -> dict:
    """Collect all diagnostic information."""
    evidence = {}

    # Stack status
    try:
        status = get_stack_status()
        evidence["stack_status"] = status
    except Exception as e:
        evidence["stack_status_error"] = str(e)

    # Port check
    evidence["port_alive"] = _check_port()

    # Recent errors
    errors = get_recent_errors(30)
    evidence["error_count"] = len(errors)
    evidence["recent_errors"] = [
        {"title": e.get("title",""), "detail": e.get("detail","")[:200], "ts": e.get("ts",0)}
        for e in errors[-5:]
    ]

    # Trader log tail
    trader_log = get_log_tail("trader.err", 40)
    if trader_log:
        evidence["trader_log_tail"] = trader_log[-800:]

    # Activity log recent events (non-error)
    recent_events = []
    for e in get_recent(15):
        if e.get("type") != "error":
            recent_events.append({"type": e.get("type",""), "title": e.get("title",""), "detail": e.get("detail","")[:100]})
    evidence["recent_activity"] = recent_events

    return evidence


def _fingerprint(evidence: dict) -> str:
    """Create a fingerprint to avoid repeating the same repair."""
    parts = []
    if not evidence.get("port_alive"):
        parts.append("port_dead")
    if evidence.get("stack_status", {}).get("trader", {}).get("status") == "offline":
        parts.append("trader_offline")
    if evidence.get("stack_status", {}).get("trader", {}).get("count", 0) > 1:
        parts.append("trader_dupes")
    for e in evidence.get("recent_errors", []):
        err = e.get("detail", "")[:60]
        parts.append(f"err:{err}")
    return "|".join(parts) if parts else "ok"


def run_cycle(client, llm, state: dict) -> None:
    now = time.time()
    evidence = _gather_evidence(client)
    fp = _fingerprint(evidence)

    if fp == "ok":
        return

    # Skip if we already fixed this fingerprint recently
    if fp in state.get("seen_fingerprints", []):
        return

    log(LABEL, "Anomaly detected", fp[:200])

    # Build context for LLM
    context_parts = []
    if not evidence.get("port_alive"):
        context_parts.append("DASHBOARD PORT NOT RESPONDING")
    trader_info = evidence.get("stack_status", {}).get("trader", {})
    if trader_info.get("status") == "offline":
        context_parts.append("TRADER OFFLINE")
    elif trader_info.get("status") == "duplicate":
        context_parts.append(f"DUPLICATE TRADERS: count={trader_info.get('count',0)}")
    if evidence.get("recent_errors"):
        context_parts.append(f"\nRECENT ERRORS ({evidence['error_count']}):")
        for e in evidence["recent_errors"]:
            context_parts.append(f"  [{e['title']}] {e['detail']}")
    if evidence.get("trader_log_tail"):
        context_parts.append(f"\nTRADER LOG TAIL:\n{evidence['trader_log_tail']}")
    if evidence.get("recent_activity"):
        context_parts.append("\nRECENT ACTIVITY:")
        for e in evidence["recent_activity"][-5:]:
            context_parts.append(f"  [{e['type']}] {e['title']}: {e['detail']}")

    user_msg = "\n".join(context_parts)
    if len(user_msg) > 4000:
        user_msg = user_msg[:4000]

    state["repairs_attempted"] += 1
    answer = llm_ask(AGENT_NAME, llm, SYSTEM, user_msg, max_tokens=1200)

    if not answer:
        # Fallback: basic script repair
        log_warn(LABEL, "LLM unavailable, running script repair")
        killed = kill_duplicate_traders()
        if killed:
            log(LABEL, "Script repair", f"killed {killed} duplicates")
        return

    try:
        plan = json.loads(answer)
    except (json.JSONDecodeError, ValueError):
        # Try to extract JSON from markdown
        stripped = answer.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
            if stripped.endswith("```"):
                stripped = stripped[:-3]
        plan = json.loads(stripped.strip())

    diagnosis = plan.get("diagnosis", "")
    confidence = plan.get("confidence", 0)
    fixed = plan.get("fixed", False)
    actions = plan.get("actions_taken", [])
    needs_human = plan.get("needs_human", False)

    for action in actions:
        _execute_action(action, client, state)

    if fixed:
        state["repairs_succeeded"] += 1
        state.setdefault("seen_fingerprints", []).append(fp)
        # Keep list manageable
        state["seen_fingerprints"] = state["seen_fingerprints"][-50]

    log(LABEL, f"Diagnosis (conf={confidence})", f"fixed={fixed} needs_human={needs_human} | {diagnosis}")
    if needs_human:
        log_warn(LABEL, "HUMAN INTERVENTION NEEDED", diagnosis)


def _execute_action(action: str, client, state: dict) -> None:
    """Execute a repair action string from the LLM."""
    action_lower = action.lower()

    if "kill" in action_lower and "duplicate" in action_lower:
        n = kill_duplicate_traders()
        log(LABEL, "Action", f"killed {n} duplicate processes")
    elif "restart" in action_lower and "trader" in action_lower:
        from trader.stack_control import start_single_trader
        result = start_single_trader()
        log(LABEL, "Action", f"restart trader: {result}")
    elif "edit" in action_lower or "fix" in action_lower or "replace" in action_lower:
        # LLM should have output structured fix info in diagnosis
        # If it includes file:line:content, apply it
        log(LABEL, "Action", f"code fix requested: {action[:200]}")
    elif "hold" in action_lower or "wait" in action_lower:
        log(LABEL, "Action", f"holding: {action[:100]}")


if __name__ == "__main__":
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
    )
