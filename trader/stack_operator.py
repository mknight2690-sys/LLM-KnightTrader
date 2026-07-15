"""Full-time stack operator — cold start/stop, reconcile, LLM escalation.

Single entry point for desktop launcher, Stop shortcut, dashboard Restart traders,
and the dashboard watchdog loop. Deterministic repairs run first; repair LLM when stuck.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from activity_log import log_event
from config import DASHBOARD_HOST, DASHBOARD_PORT, PID_DIR, PROJECT_ROOT

_FAIL_STREAK = 0
_LAST_RECONCILE_TS = 0.0
_RECONCILE_MIN_SEC = 10.0
_LLM_ESCALATE_AFTER = 2
_BOOT_SETTLE_SEC = 4.0

DASHBOARD_URL = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"


def _write_pid(name: str, pid: int) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    (PID_DIR / f"{name}.pid").write_text(str(pid), encoding="utf-8")


def _wait_dashboard_health(timeout_sec: float = 45.0) -> bool:
    deadline = time.time() + timeout_sec
    url = f"{DASHBOARD_URL}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(1.0)
    return False


_STACK_STARTING_FILE = PID_DIR / "stack.starting"
_STACK_STARTING_MAX_SEC = 120.0


def mark_stack_starting() -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    _STACK_STARTING_FILE.write_text(str(time.time()), encoding="utf-8")


def clear_stack_starting() -> None:
    try:
        _STACK_STARTING_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def stack_starting() -> bool:
    if not _STACK_STARTING_FILE.is_file():
        return False
    try:
        ts = float(_STACK_STARTING_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        clear_stack_starting()
        return False
    if time.time() - ts > _STACK_STARTING_MAX_SEC:
        clear_stack_starting()
        return False
    return True


def cold_stop_stack() -> dict[str, Any]:
    """Stop every KnightTrader process (dashboard, traders, monitor, watchers)."""
    from trader.stack_control import is_entire_stack_stopped, running_process_counts, stop_all_stack_processes

    killed = stop_all_stack_processes()
    counts = running_process_counts()
    ok = is_entire_stack_stopped()
    if ok:
        log_event("system", "Stack stopped", f"killed {len(killed)} process(es)")
    else:
        log_event(
            "error",
            "Stack stop incomplete",
            json.dumps(counts)[:200],
        )
    return {"ok": ok, "killed_pids": killed, "counts": counts}


def cold_start_stack(*, open_browser: bool = False) -> dict[str, Any]:
    """Stop everything, then start exactly one dashboard + one trader."""
    import webbrowser

    from trader.health import trader_lock_owner
    from trader.stack_control import (
        _module_pids,
        _subprocess_kwargs,
        _trader_python,
        ensure_desktop_shortcuts,
        running_process_counts,
        stack_status,
        start_single_trader,
    )

    mark_stack_starting()
    try:
        stop = cold_stop_stack()
        if not stop["ok"]:
            log_event("system", "Stack cold start", "stop incomplete — continuing anyway")

        python_bin = _trader_python()
        wrapper = PROJECT_ROOT / "scripts" / "_launch_dashboard.py"
        kwargs = _subprocess_kwargs()
        dash = subprocess.Popen([python_bin, "-u", str(wrapper)], **kwargs)
        time.sleep(2.0)
        if dash.poll() is not None:
            return {
                "ok": False,
                "error": f"dashboard exited immediately (code {dash.returncode})",
                "phase": "dashboard",
            }

        if not _wait_dashboard_health():
            return {"ok": False, "error": "dashboard health check failed", "phase": "dashboard"}

        dash_pid = dash.pid if dash.poll() is None else None
        if dash_pid is None:
            pids = _module_pids("dashboard.server")
            dash_pid = pids[-1] if pids else None
        if dash_pid is None:
            return {"ok": False, "error": "dashboard pid not found", "phase": "dashboard"}

        _write_pid("dashboard", dash_pid)
        time.sleep(_BOOT_SETTLE_SEC)

        trader = start_single_trader()
        if not trader.get("ok"):
            return {
                "ok": False,
                "error": str(trader.get("error") or "trader failed"),
                "phase": "trader",
                "trader": trader,
            }

        lock_pid = trader_lock_owner()
        if not lock_pid:
            time.sleep(5.0)
            lock_pid = trader_lock_owner()
        if not lock_pid:
            return {
                "ok": False,
                "error": "trader lock not held after start",
                "phase": "verify",
                "trader": trader,
            }

        trader_pid = lock_pid
        _write_pid("trader", trader_pid)

        shortcuts = ensure_desktop_shortcuts()
        if open_browser:
            webbrowser.open(DASHBOARD_URL)

        counts = running_process_counts()
        stack = stack_status()
        log_event(
            "system",
            "Stack cold start complete",
            f"dashboard={dash_pid} trader={trader_pid} counts={counts}",
        )
        return {
            "ok": True,
            "dashboard_pid": dash_pid,
            "trader_pid": trader_pid,
            "counts": counts,
            "stack": stack,
            "shortcuts": shortcuts,
            "url": DASHBOARD_URL,
        }
    finally:
        clear_stack_starting()


def restart_traders() -> dict[str, Any]:
    """Detached full cold restart — same as desktop Start shortcut."""
    from trader.stack_control import spawn_full_stack_restart

    result = spawn_full_stack_restart()
    if result.get("ok"):
        log_event("system", "Restart traders requested", f"spawned pid {result.get('spawned_pid')}")
    return result


def reconcile_stack(*, allow_start_trader: bool = True) -> dict[str, Any]:
    """While dashboard is up: kill extras, dedupe, start trader if missing."""
    from trader.stack_control import (
        _kill_monitor_and_watchers,
        dedupe_preferred_dashboard,
        dedupe_preferred_trader,
        ensure_single_stack,
        running_process_counts,
        stack_status,
        start_single_trader,
    )
    from trader.stack_watchdog import _DASHBOARD_BOOT_TS, _BOOT_GRACE_SEC

    if stack_starting():
        return {
            "ok": True,
            "skipped": True,
            "reason": "stack cold start in progress",
            "counts": running_process_counts(),
            "stack": stack_status(),
            "actions": [],
        }

    killed = _kill_monitor_and_watchers()
    dedupe_preferred_dashboard()
    dedupe_preferred_trader()

    counts = running_process_counts()
    stack = stack_status()
    actions: list[str] = []

    in_boot_grace = (
        stack_starting()
        or (_DASHBOARD_BOOT_TS and (time.time() - _DASHBOARD_BOOT_TS) < _BOOT_GRACE_SEC)
    )

    # Reconcile Owl Swarm agents
    try:
        from trader.orchestrator import all_agent_statuses, start_all_agents
        agent_statuses = all_agent_statuses()
        offline = [a for a in agent_statuses if a.get("status") == "offline"]
        if offline and not in_boot_grace and allow_start_trader:
            start_all_agents()
            actions.append(f"start_agents:{len(offline)}")
    except Exception:
        pass

    if allow_start_trader and not in_boot_grace:
        trader = stack.get("trader") or {}
        if trader.get("status") == "offline" and counts.get("trader", 0) == 0:
            result = start_single_trader()
            actions.append(f"start_trader:{result.get('pid') or result.get('error')}")
        elif trader.get("status") == "duplicate" or counts.get("trader", 0) > 1:
            keep = dedupe_preferred_trader()
            actions.append(f"dedupe_trader:{keep}")
            stack = stack_status()
            if (stack.get("trader") or {}).get("status") == "offline":
                result = start_single_trader()
                actions.append(f"start_trader:{result.get('pid') or result.get('error')}")

    dash = stack.get("dashboard") or {}
    if dash.get("status") == "duplicate" or counts.get("dashboard", 0) > 1:
        keep = dedupe_preferred_dashboard()
        actions.append(f"dedupe_dashboard:{keep}")

    if killed:
        actions.append(f"kill_extras:{len(killed)}")

    single = ensure_single_stack(require_trader=allow_start_trader and not in_boot_grace, settle_sec=8.0)
    return {
        "ok": single["ok"],
        "issues": single.get("issues", []),
        "counts": single.get("counts", running_process_counts()),
        "stack": single.get("stack", stack_status()),
        "actions": actions,
        "boot_grace": in_boot_grace,
    }


def _escalate_to_repair_llm(issues: list[str]) -> tuple[bool, str]:
    try:
        from blofin.client import BlofinClient
        from llm.wrapper import LLMWrapper
        from trader.repair import llm_triage_and_repair
        from trader.stack_fix import stack_fix_context
        from trader.state import load_state, save_state
        from blofin.account_cache import read_account_cached

        state = load_state()
        client = BlofinClient()
        llm = LLMWrapper(provider_priority=("openrouter",), openrouter_models=["openai/gpt-oss-20b:free"])
        account = read_account_cached()
        incident = {
            "phase": "stack_operator",
            "error": "; ".join(issues[:6]),
            "stack_fix": stack_fix_context(),
        }
        result = llm_triage_and_repair(state, client, llm, account, None, incident=incident)
        save_state(state)
        detail = result.diagnosis or "; ".join(result.actions_taken)
        return bool(result.recovered or result.actions_taken), detail[:300]
    except Exception as exc:
        return False, str(exc)[:200]


def run_operator_cycle(*, allow_llm: bool = True) -> dict[str, Any]:
    """One watchdog tick: reconcile processes; LLM if repeatedly stuck."""
    global _FAIL_STREAK, _LAST_RECONCILE_TS

    now = time.time()
    if now - _LAST_RECONCILE_TS < _RECONCILE_MIN_SEC:
        return {"skipped": True, "reason": "interval"}
    _LAST_RECONCILE_TS = now

    from trader.stack_watchdog import diagnose_stack

    issues = diagnose_stack()
    reconcile = reconcile_stack(allow_start_trader=True)
    process_issues = reconcile.get("issues") or []

    healthy = reconcile["ok"] and not [
        i
        for i in issues
        if i.get("severity") == "critical" and i.get("auto", True)
    ]

    # Account issues handled by existing watchdog account repairs
    account_critical = [
        i for i in issues
        if i.get("severity") == "critical"
        and i.get("code") not in (
            "trader_offline",
            "trader_duplicate",
            "dashboard_duplicate",
            "extra_bots_running",
            "credentials_missing",
        )
    ]

    if reconcile["ok"] and not account_critical:
        _FAIL_STREAK = 0
        return {
            "ok": True,
            "healthy": True,
            "reconcile": reconcile,
            "issues": issues,
        }

    if process_issues:
        _FAIL_STREAK += 1
        log_event(
            "system",
            "Stack operator reconcile",
            json.dumps({"issues": process_issues, "actions": reconcile.get("actions")})[:400],
        )
    else:
        _FAIL_STREAK = max(0, _FAIL_STREAK - 1)

    llm_used = False
    llm_detail = ""
    if allow_llm and _FAIL_STREAK >= _LLM_ESCALATE_AFTER and process_issues:
        llm_ok, llm_detail = _escalate_to_repair_llm(process_issues)
        llm_used = True
        if llm_ok:
            _FAIL_STREAK = 0
            reconcile = reconcile_stack(allow_start_trader=True)
            log_event("system", "Stack operator LLM repair", llm_detail[:300])

    return {
        "ok": reconcile["ok"] and not account_critical,
        "healthy": reconcile["ok"] and not account_critical,
        "reconcile": reconcile,
        "issues": issues,
        "fail_streak": _FAIL_STREAK,
        "llm_used": llm_used,
        "llm_detail": llm_detail,
    }
