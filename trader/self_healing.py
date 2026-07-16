"""Self-healing runtime — autonomous stack repair without human intervention."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from activity_log import get_recent, log_event
from config import DATA_DIR, PID_DIR, PROJECT_ROOT
from trader.health import _pid_alive
from trader.stack_operator import reconcile_stack
from trader.stack_watchdog import diagnose_stack

_HEAL_COOLDOWN_SEC = 20.0
_LAST_HEAL_TS = 0.0
_RECENT_LIMIT = 80
_REPAIR_LIMIT = 6


def _pid_alive_local(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        return _pid_alive(pid)
    except Exception:
        return False


def _recent_repair_incidents() -> list[dict[str, Any]]:
    try:
        return [e for e in get_recent(_RECENT_LIMIT) if str(e.get("type") or "") == "error"]
    except Exception:
        return []


def heal_once(*, max_actions: int = _REPAIR_LIMIT) -> dict[str, Any]:
    global _LAST_HEAL_TS
    now = time.time()
    if now - _LAST_HEAL_TS < _HEAL_COOLDOWN_SEC:
        return {"ok": True, "skipped": True, "reason": "cooldown"}
    _LAST_HEAL_TS = now

    incidents = _recent_repair_incidents()
    issues = []
    try:
        issues = diagnose_stack() or []
    except Exception as exc:
        log_event("error", "Self-heal diagnose failed", str(exc)[:220])
        return {"ok": False, "error": f"diagnose failed: {exc}"}

    actionable = []
    for issue in issues:
        if issue.get("auto") is False:
            continue
        actionable.append(issue)

    if not actionable and not incidents:
        return {"ok": True, "skipped": True, "reason": "nothing_to_do"}

    repair = _run_repairs(actionable[-max_actions:], incidents[-max_actions:])
    repair.update({"issues": actionable, "incident_count": len(incidents)})
    return repair


def _run_repairs(issues: list[dict[str, Any]], incidents: list[dict[str, Any]]) -> dict[str, Any]:
    actions: list[str] = []
    started: list[int] = []
    killed: list[int] = []
    errors: list[str] = []

    if not issues:
        return {"ok": True, "actions": actions, "started": started, "killed": killed, "errors": errors}

    for issue in issues:
        code = str(issue.get("code") or "")
        try:
            if code in ("trader_offline", "trader_duplicate", "dashboard_duplicate", "extra_bots_running"):
                result = reconcile_stack(allow_start_trader=True)
                actions.append("reconcile_stack")
                started.extend([int(pid) for pid in (result.get("trader", {}) or {}).get("pids", []) if _pid_alive_local(int(pid))])
                if result.get("killed_pids"):
                    killed.extend([int(pid) for pid in result.get("killed_pids", [])])
                if not result.get("ok") and result.get("error"):
                    errors.append(result["error"])
            elif code == "desktop_shortcuts_missing":
                try:
                    from trader.stack_control import ensure_desktop_shortcuts
                    ensure_desktop_shortcuts()
                    actions.append("ensure_desktop_shortcuts")
                except Exception as exc:
                    errors.append(str(exc)[:200])
            elif code in ("account_display_corrupt", "account_cache_stale", "account_unavailable", "live_stream_drift", "account_display_repaired"):
                try:
                    from blofin.account_cache import guard_account_stream
                    guard = guard_account_stream()
                    actions.append("guard_account_stream")
                    if not guard.get("ok") and guard.get("error"):
                        errors.append(str(guard["error"])[:200])
                except Exception as exc:
                    errors.append(str(exc)[:200])
        except Exception as exc:
            errors.append(f"{code}:{exc}")[:200]

    if not actions and incidents:
        try:
            _safe_wait(seconds=min(20, max(5, len(incidents) * 3)))
            actions.append("wait_cooldown")
        except Exception as exc:
            errors.append(f"wait failed:{exc}")

    ok = bool(actions) and not errors
    return {
        "ok": ok,
        "actions": actions,
        "started": list(dict.fromkeys(started)),
        "killed": list(dict.fromkeys(killed)),
        "errors": errors,
    }


def _safe_wait(*, seconds: int = 5) -> None:
    seconds = max(1, min(int(seconds), 90))
    time.sleep(seconds)


def cycle_self_heal(state: dict[str, Any] | None = None) -> dict[str, Any]:
    result = heal_once()
    try:
        log_event(
            "system",
            "Self-heal cycle",
            json.dumps(result, default=str)[:400],
            {"stack_self_heal": result},
        )
    except Exception:
        pass
    if state is not None:
        state["_last_self_heal"] = result
    return result
