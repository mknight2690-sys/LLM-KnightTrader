"""Repair Agent 1 — Watchdog.

Master systems technician for the LLM KnightTrader stack.
Diagnoses from evidence only — like a mechanic who has never seen this car before
but reads the dash lights, listens to the engine, pulls error codes, and fixes it.
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
    TECHNICIAN_METHOD,
    agent_main,
    gather_novel_incidents,
    get_log_tail,
    get_recent_errors,
    get_stack_status,
    list_source_files,
    log,
    log_warn,
    maybe_autorepair_global,
    run_novel_investigation,
    triage_with_repair_engine,
)

AGENT_NAME = "watchdog"
LABEL = "Repair[Watchdog]"
LOOP_SEC = 15.0


def _check_port() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{DASHBOARD_PORT}/api/health", timeout=3.0
        ) as r:
            return r.status == 200
    except Exception:
        return False


def _gather_evidence() -> dict:
    evidence: dict = {}
    try:
        evidence["stack_status"] = get_stack_status()
    except Exception as exc:
        evidence["stack_status_error"] = str(exc)
    evidence["port_alive"] = _check_port()
    errors = get_recent_errors(30)
    evidence["error_count"] = len(errors)
    evidence["recent_errors"] = [
        {
            "title": e.get("title", ""),
            "detail": e.get("detail", "")[:200],
            "ts": e.get("ts", 0),
        }
        for e in errors[-8:]
    ]
    trader_log = get_log_tail("trader.err", 50)
    if trader_log:
        evidence["trader_log_tail"] = trader_log[-1200:]
    dash_log = get_log_tail("dashboard.err", 30)
    if dash_log:
        evidence["dashboard_log_tail"] = dash_log[-600:]
    recent_events = []
    for e in get_recent(20):
        if e.get("type") != "error":
            recent_events.append(
                {
                    "type": e.get("type", ""),
                    "title": e.get("title", ""),
                    "detail": e.get("detail", "")[:120],
                }
            )
    evidence["recent_activity"] = recent_events[-10:]
    evidence["source_file_count"] = len(list_source_files())
    return evidence


def _fingerprint(evidence: dict) -> str:
    parts: list[str] = []
    if not evidence.get("port_alive"):
        parts.append("port_dead")
    trader_info = (evidence.get("stack_status") or {}).get("trader", {})
    dash_info = (evidence.get("stack_status") or {}).get("dashboard", {})
    if trader_info.get("status") == "offline":
        parts.append("trader_offline")
    if trader_info.get("count", 0) > 1 or trader_info.get("status") == "duplicate":
        parts.append("trader_dupes")
    if dash_info.get("status") == "offline":
        parts.append("dashboard_offline")
    if dash_info.get("count", 0) > 1:
        parts.append("dashboard_dupes")
    for e in evidence.get("recent_errors", []):
        parts.append(f"err:{e.get('title', '')[:40]}")
    return "|".join(parts) if parts else "ok"


def _build_incident(evidence: dict, reconcile: dict | None) -> dict:
    issues = (reconcile or {}).get("issues") or []
    issue_codes = [
        str(i.get("code") or i) if isinstance(i, dict) else str(i) for i in issues
    ]
    err_blob = "; ".join(
        f"{e.get('title')}: {e.get('detail', '')[:80]}"
        for e in evidence.get("recent_errors", [])[-3:]
    )
    if not err_blob and issue_codes:
        err_blob = "; ".join(issue_codes[:6])
    if not err_blob:
        if not evidence.get("port_alive"):
            err_blob = "dashboard port not responding"
        elif (evidence.get("stack_status") or {}).get("trader", {}).get("status") == "offline":
            err_blob = "trader_offline"
        else:
            err_blob = "stack anomaly"

    return {
        "phase": "repair_watchdog",
        "error": err_blob[:400],
        "title": "Watchdog anomaly",
        "issues": issues,
        "evidence": {
            "port_alive": evidence.get("port_alive"),
            "stack": evidence.get("stack_status"),
            "reconcile_actions": (reconcile or {}).get("actions"),
        },
    }


def run_cycle(client, llm, state: dict) -> None:
    from trader.stack_operator import reconcile_stack

    # Step 1: deterministic reconcile (kill dupes, start offline trader)
    reconcile = reconcile_stack(allow_start_trader=True)

    evidence = _gather_evidence()
    fp = _fingerprint(evidence)

    if fp == "ok" and reconcile.get("ok"):
        return

    # Cooldown: don't hammer same fingerprint every 15s
    seen = state.setdefault("seen_fingerprints", {})
    if isinstance(seen, list):
        seen = {k: 0.0 for k in seen}
        state["seen_fingerprints"] = seen
    last = float(seen.get(fp) or 0)
    if fp != "ok" and time.time() - last < 90.0:
        return

    log(LABEL, "Anomaly detected", fp[:200])
    state["repairs_attempted"] += 1

    # Step 2: if reconcile alone fixed it, verify and exit
    evidence_after = _gather_evidence()
    if _fingerprint(evidence_after) == "ok" and reconcile.get("ok"):
        state["repairs_succeeded"] += 1
        seen[fp] = time.time()
        log(LABEL, "Fixed by reconcile", "; ".join(reconcile.get("actions") or [])[:200])
        return

    # Step 3: novel failure investigation (multi-turn diagnose + source patch).
    # This is how the agents "learn" new breakage that isn't covered by the action catalog.
    try:
        novel_seen = state.setdefault("novel_fingerprints", {})
        incidents = gather_novel_incidents()
        for issue in incidents:
            issue_fp = str(issue.get("fingerprint") or "")
            if not issue_fp or issue_fp == "ok":
                continue
            last = float(novel_seen.get(issue_fp) or 0)
            if time.time() - last < 180.0:
                continue
            log(LABEL, "Novel incident investigate", str(issue.get("error") or issue.get("file") or "")[:180])
            novel_seen[issue_fp] = time.time()
            ok = run_novel_investigation(
                AGENT_NAME,
                LABEL,
                llm,
                state,
                issue,
                max_turns=4,
            )
            if ok:
                state["repairs_succeeded"] += 1
                return
    except Exception as exc:
        log_warn(LABEL, "Novel investigation failed", str(exc)[:200])

    # Step 4: full repair engine (deterministic playbook + LLM triage)
    incident = _build_incident(evidence, reconcile)
    incident["technician_method"] = TECHNICIAN_METHOD
    result = triage_with_repair_engine(client, llm, state, incident, label=LABEL)

    if result and (result.recovered or result.actions_taken):
        state["repairs_succeeded"] += 1
        seen[fp] = time.time()
        log(
            LABEL,
            "Repair complete",
            f"recovered={result.recovered} actions={'; '.join(result.actions_taken)[:200]}",
        )
    else:
        log_warn(LABEL, "Repair incomplete", fp[:200])
        # Extra net: if something novel slipped through, try global autorepair once.
        try:
            maybe_autorepair_global(
                client,
                llm,
                state,
                label=LABEL,
                max_incidents=1,
                cooldown_sec=240.0,
            )
        except Exception as exc:
            log_warn(LABEL, "Global autorepair failed", str(exc)[:200])


if __name__ == "__main__":
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
    )
