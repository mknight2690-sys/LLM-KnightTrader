"""Autonomous stack watchdog — detect and repair operational issues."""

from __future__ import annotations

import json
import time
from typing import Any

from activity_log import log_event
from config import ACCOUNT_REFRESH_SEC

_DASHBOARD_BOOT_TS = 0.0
_BOOT_GRACE_SEC = 90.0
_LAST_RUN_TS = 0.0
_MIN_INTERVAL_SEC = 12.0
_LAST_REPAIR_TS = 0.0
_REPAIR_COOLDOWN_SEC = 20.0


def mark_dashboard_boot() -> None:
    global _DASHBOARD_BOOT_TS
    _DASHBOARD_BOOT_TS = time.time()


def _issue(
    code: str,
    *,
    severity: str = "warn",
    detail: str = "",
    auto: bool = True,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "detail": detail[:300],
        "auto": auto,
        "ts": time.time(),
    }


def diagnose_stack() -> list[dict[str, Any]]:
    """Return actionable issues across account, processes, and cache."""
    from blofin.account_cache import (
        _account_display_sane,
        _read_disk,
        stream_drift_issues,
        cache_age_sec,
        is_rate_limited,
        read_account_cached,
    )
    from trader.stack_control import stack_status

    from trader.stack_operator import stack_starting

    issues: list[dict[str, Any]] = []
    stack = stack_status()
    if stack_starting():
        return issues

    account = read_account_cached()
    disk = _read_disk() or {}

    trader = stack.get("trader") or {}
    if trader.get("status") == "offline":
        issues.append(_issue("trader_offline", severity="critical", detail="trader.agent not running"))
    elif trader.get("status") == "duplicate":
        issues.append(
            _issue(
                "trader_duplicate",
                severity="critical",
                detail=f"{trader.get('count', 0)} trader processes",
            )
        )

    dashboard = stack.get("dashboard") or {}
    if dashboard.get("status") == "duplicate":
        issues.append(
            _issue(
                "dashboard_duplicate",
                severity="critical",
                detail=f"{dashboard.get('count', 0)} dashboard processes",
            )
        )

    monitor_count = int((stack.get("monitor") or {}).get("count") or 0)
    watcher_count = int((stack.get("watchers") or {}).get("count") or 0)
    if monitor_count > 0 or watcher_count > 0:
        issues.append(
            _issue(
                "extra_bots_running",
                severity="warn",
                detail=f"monitor={monitor_count} watchers={watcher_count}",
            )
        )

    sane, reason = _account_display_sane(account)
    if not sane:
        issues.append(
            _issue(
                "account_display_corrupt",
                severity="critical",
                detail=f"{reason} equity={account.get('equity')} available={account.get('available')}",
            )
        )

    if account.get("display_repaired"):
        issues.append(
            _issue(
                "account_display_repaired",
                severity="warn",
                detail=str(account.get("display_warning") or "fallback applied"),
                auto=False,
            )
        )

    if is_rate_limited():
        issues.append(_issue("account_rate_limited", severity="warn", detail="BloFin API cooldown active"))

    age = cache_age_sec()
    if age > max(ACCOUNT_REFRESH_SEC * 4, 60):
        issues.append(_issue("account_cache_stale", severity="warn", detail=f"cache age {int(age)}s"))

    if float(disk.get("equity") or 0) <= 0 and disk.get("from_trades"):
        issues.append(_issue("account_cache_trade_hydration", severity="warn", detail="cache bootstrapped from trades"))

    if not account.get("ok") and not account.get("positions"):
        issues.append(_issue("account_unavailable", severity="critical", detail=str(account.get("error") or "no account")))

    drift = stream_drift_issues()
    if drift:
        issues.append(
            _issue(
                "live_stream_drift",
                severity="critical",
                detail=json.dumps(drift[:3], default=str)[:280],
            )
        )

    from trader.stack_control import desktop_shortcuts_exist

    if not desktop_shortcuts_exist():
        issues.append(
            _issue(
                "desktop_shortcuts_missing",
                severity="warn",
                detail="Desktop Start/Stop LLM KnightTrader shortcuts not found",
            )
        )

    try:
        from credentials import resolve_blofin_credentials_path

        if resolve_blofin_credentials_path() is None:
            issues.append(
                _issue(
                    "credentials_missing",
                    severity="critical",
                    detail="BloFin credentials file not found — ask user for keys or path",
                    auto=False,
                )
            )
    except Exception as exc:
        issues.append(
            _issue(
                "credentials_missing",
                severity="critical",
                detail=str(exc)[:200],
                auto=False,
            )
        )

    return issues


def _repair_account_display() -> str:
    from blofin.account_cache import bootstrap_account_cache, get_account_snapshot, read_account_cached

    bootstrap_account_cache()
    get_account_snapshot(force=True)
    acct = read_account_cached()
    return f"equity={acct.get('equity')} available={acct.get('available')}"


def _repair_known_issue(issue: dict[str, Any]) -> tuple[bool, str]:
    code = str(issue.get("code") or "")

    if code in ("trader_duplicate", "extra_bots_running", "dashboard_duplicate", "trader_offline"):
        if code == "trader_offline" and _DASHBOARD_BOOT_TS and time.time() - _DASHBOARD_BOOT_TS < _BOOT_GRACE_SEC:
            return False, "startup grace — launcher owns trader start"
        from trader.stack_operator import reconcile_stack

        result = reconcile_stack(allow_start_trader=True)
        return bool(result.get("ok")), json.dumps(result.get("actions") or result.get("issues"))[:300]

    if code == "desktop_shortcuts_missing":
        from trader.stack_control import ensure_desktop_shortcuts

        result = ensure_desktop_shortcuts()
        detail = str(result.get("paths") or result.get("error") or result)
        return bool(result.get("ok")), detail[:300]

    if code in (
        "account_display_corrupt",
        "account_rate_limited",
        "account_cache_stale",
        "account_cache_trade_hydration",
        "account_unavailable",
        "account_mtm_probe_failed",
        "live_stream_drift",
    ):
        from blofin.account_cache import guard_account_stream, read_account_cached

        guard = guard_account_stream()
        if guard.get("refreshed") or guard.get("ok"):
            acct = guard.get("account") or read_account_cached()
            return True, f"equity={acct.get('equity')} drift={len(guard.get('drift') or [])}"
        detail = _repair_account_display()
        return True, detail

    return False, "no deterministic repair"


def _repair_with_llm(issues: list[dict[str, Any]]) -> tuple[bool, str]:
    """LLM triage for novel stack incidents when deterministic repair is not enough."""
    try:
        from blofin.client import BlofinClient
        from llm.wrapper import LLMWrapper
        from trader.repair import llm_triage_and_repair
        from trader.state import load_state

        state = load_state()
        client = BlofinClient()
        llm = LLMWrapper(provider_priority=("openrouter",), openrouter_models=["openai/gpt-oss-20b:free"])
        from blofin.account_cache import read_account_cached

        account = read_account_cached()
        incident = {
            "phase": "stack_watchdog",
            "error": "; ".join(f"{i.get('code')}: {i.get('detail', '')[:80]}" for i in issues[:6]),
            "issues": issues[:8],
        }
        result = llm_triage_and_repair(state, client, llm, account, None, incident=incident)
        from trader.state import save_state

        save_state(state)
        return result.recovered or bool(result.actions_taken), result.diagnosis or "; ".join(result.actions_taken)
    except Exception as exc:
        return False, str(exc)[:200]


_REPAIR_PROCESS_CODES = frozenset(
    {"trader_offline", "trader_duplicate", "dashboard_duplicate", "extra_bots_running"}
)


def run_stack_watchdog(*, allow_llm: bool = True) -> dict[str, Any]:
    """Diagnose stack health and apply repairs. Safe to call from dashboard loop."""
    global _LAST_RUN_TS, _LAST_REPAIR_TS

    now = time.time()
    if now - _LAST_RUN_TS < _MIN_INTERVAL_SEC:
        return {"skipped": True, "reason": "interval"}
    _LAST_RUN_TS = now

    from trader.stack_operator import run_operator_cycle

    operator = run_operator_cycle(allow_llm=allow_llm)
    if operator.get("skipped"):
        return operator

    issues = diagnose_stack()
    if not issues and operator.get("healthy"):
        return {
            "ok": True,
            "issues": [],
            "repaired": [],
            "healthy": True,
            "operator": operator,
        }

    repaired: list[dict[str, Any]] = []
    for action in operator.get("reconcile", {}).get("actions") or []:
        repaired.append({"code": "stack_operator", "ok": True, "detail": str(action)[:300]})
    if operator.get("llm_used"):
        repaired.append({"code": "stack_operator_llm", "ok": True, "detail": operator.get("llm_detail", "")})

    unresolved: list[dict[str, Any]] = []
    can_repair = now - _LAST_REPAIR_TS >= _REPAIR_COOLDOWN_SEC
    operator_handled_process = bool(operator.get("reconcile", {}).get("ok"))

    for issue in issues:
        if not issue.get("auto", True):
            unresolved.append(issue)
            continue
        if issue.get("code") in _REPAIR_PROCESS_CODES and operator_handled_process:
            continue
        if not can_repair:
            unresolved.append(issue)
            continue
        ok, detail = _repair_known_issue(issue)
        entry = {"code": issue.get("code"), "ok": ok, "detail": detail}
        repaired.append(entry)
        if ok:
            log_event("system", f"Watchdog repaired: {issue.get('code')}", detail[:300])
        else:
            unresolved.append(issue)
            log_event(
                "error",
                f"Watchdog repair failed: {issue.get('code')}",
                detail[:300],
            )

    llm_used = bool(operator.get("llm_used"))
    llm_detail = operator.get("llm_detail", "")
    critical_unresolved = [i for i in unresolved if i.get("severity") == "critical"]
    if allow_llm and critical_unresolved and can_repair:
        llm_ok, llm_detail = _repair_with_llm(critical_unresolved)
        llm_used = True
        if llm_ok:
            _LAST_REPAIR_TS = now
            log_event("system", "Watchdog LLM repair", llm_detail[:300])
            issues = diagnose_stack()
            unresolved = issues

    if any(r.get("ok") for r in repaired):
        _LAST_REPAIR_TS = now

    healthy = not unresolved
    if unresolved:
        log_event(
            "system",
            "Watchdog issues remain",
            json.dumps([{"code": i.get("code"), "detail": i.get("detail", "")[:120]} for i in unresolved[:6]])[:500],
        )

    return {
        "ok": healthy,
        "healthy": healthy,
        "issues": issues,
        "repaired": repaired,
        "unresolved": [{"code": i.get("code"), "detail": i.get("detail", "")[:160]} for i in unresolved],
        "llm_used": llm_used,
        "llm_detail": llm_detail[:200],
        "operator": operator,
    }
