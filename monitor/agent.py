"""Lightweight monitor — process watchdog only, zero BloFin API calls."""

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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from activity_log import log_event, load_history
from config import ACTIVITY_LOG, APP_NAME, DASHBOARD_HOST, DASHBOARD_PORT, PID_DIR
from monitor.dashboard_reload import hot_reload_dashboard
from trader.state import load_state, reload_chat_fields, save_state

MONITOR_INTERVAL_SEC = int(os.environ.get("KNIGHTTRADER_MONITOR_SEC", "120"))
START_COOLDOWN_SEC = int(os.environ.get("KNIGHTTRADER_START_COOLDOWN", "180"))
RELOAD_COOLDOWN_SEC = float(os.environ.get("KNIGHTTRADER_RELOAD_COOLDOWN", "60"))
CHECKPOINT_FILE = ROOT / "data" / "monitor_checkpoint.json"
PYTHON = os.environ.get(
    "KNIGHTTRADER_PYTHON",
    r"C:\Users\mknig\AppData\Local\Programs\Python\Python312\python.exe",
)


def _load_checkpoint() -> dict[str, Any]:
    if not CHECKPOINT_FILE.is_file():
        return {"last_ts": 0.0}
    try:
        return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"last_ts": 0.0}


def _save_checkpoint(cp: dict[str, Any]) -> None:
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps(cp, indent=2), encoding="utf-8")


def _read_events_since(since_ts: float) -> list[dict[str, Any]]:
    if not ACTIVITY_LOG.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in ACTIVITY_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if float(ev.get("ts") or 0) > since_ts:
            out.append(ev)
    return out


def _process_count(module_fragment: str) -> int:
    if sys.platform != "win32":
        return 0
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -match '{module_fragment}' }} | Measure-Object).Count",
            ],
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return int(out.strip() or "0")
    except (subprocess.SubprocessError, ValueError):
        return 0


def _process_running(module_fragment: str) -> bool:
    return _process_count(module_fragment) > 0


def _dedupe_agents() -> list[str]:
    """Log duplicate processes only — do not kill the whole stack."""
    fixes: list[str] = []
    for module, label in (
        ("trader.agent", "trader"),
        ("dashboard.server", "dashboard"),
        ("monitor.agent", "monitor"),
    ):
        count = _process_count(module)
        if count > 1:
            fixes.append(f"warn_dup_{label}_{count}")
    return fixes


def _recently_started(cp: dict[str, Any], name: str) -> bool:
    ts = float(cp.get(f"last_start_{name}") or 0)
    return time.time() - ts < START_COOLDOWN_SEC


def _mark_started(cp: dict[str, Any], name: str) -> None:
    cp[f"last_start_{name}"] = time.time()


def _pid_alive(name: str) -> bool:
    path = PID_DIR / f"{name}.pid"
    if not path.is_file():
        return False
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _save_pid(name: str, proc: subprocess.Popen) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    (PID_DIR / f"{name}.pid").write_text(str(proc.pid), encoding="utf-8")


def _start_if_missing(module: str, pid_name: str, cp: dict[str, Any]) -> str | None:
    if _process_count(module) > 0 or _pid_alive(pid_name):
        return None
    if _recently_started(cp, pid_name):
        return None
    proc = subprocess.Popen(
        [PYTHON, "-m", module],
        cwd=str(ROOT),
        env=_stack_env(),
        creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0,
    )
    _save_pid(pid_name, proc)
    _mark_started(cp, pid_name)
    log_event("system", f"Monitor started {pid_name}", f"pid={proc.pid}")
    return f"started_{pid_name}"


def _recently_reloaded(cp: dict[str, Any]) -> bool:
    ts = float(cp.get("last_reload_dashboard") or 0)
    return time.time() - ts < RELOAD_COOLDOWN_SEC


def _ensure_dashboard(cp: dict[str, Any]) -> str | None:
    if _dashboard_up():
        return None
    if _process_running("dashboard.server") or _pid_alive("dashboard"):
        if _recently_reloaded(cp):
            return None
        result = hot_reload_dashboard(last_reload_ts=float(cp.get("last_reload_dashboard") or 0))
        if result.get("skipped"):
            return None
        if result.get("ok"):
            cp["last_reload_dashboard"] = float(result.get("reloaded_at") or time.time())
            cp["last_start_dashboard"] = cp["last_reload_dashboard"]
            log_event(
                "system",
                "Monitor hot-reloaded dashboard",
                f"pid={result.get('pid')} killed={result.get('killed')}",
            )
            return "hot_reloaded_dashboard"
        return "hot_reload_failed"
    return _start_if_missing("dashboard.server", "dashboard", cp)


def _dashboard_up(retries: int = 3, delay_sec: float = 2.0) -> bool:
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(
                f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/api/health", timeout=5
            ) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        if attempt + 1 < retries:
            time.sleep(delay_sec)
    return False


def _summarize_issues(events: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for ev in events:
        if ev.get("type") == "error":
            title = str(ev.get("title") or "")
            if "rate limit" in title.lower() or "1015" in str(ev.get("detail") or ""):
                issues.append("rate_limited")
            elif issues.count("error") == 0:
                issues.append("error")
    return sorted(set(issues))


def run_monitor_pass() -> dict[str, Any]:
    cp = _load_checkpoint()
    since = float(cp.get("last_ts") or 0)
    events = _read_events_since(since)
    if events:
        cp["last_ts"] = float(events[-1].get("ts") or since)

    fixes: list[str] = []
    fixes.extend(_dedupe_agents())
    dup_dashboard = _process_count("dashboard.server") > 1
    if dup_dashboard and not _recently_reloaded(cp):
        result = hot_reload_dashboard(last_reload_ts=float(cp.get("last_reload_dashboard") or 0))
        if result.get("ok"):
            cp["last_reload_dashboard"] = float(result.get("reloaded_at") or time.time())
            fixes.append("hot_reloaded_dashboard_dup")
    if not _dashboard_up():
        r = _ensure_dashboard(cp)
        if r:
            fixes.append(r)
    elif not _process_running("trader.agent"):
        r = _start_if_missing("trader.agent", "trader", cp)
        if r:
            fixes.append(r)

    issues = _summarize_issues(events)
    state = reload_chat_fields(load_state())
    if "rate_limited" in issues:
        from blofin.account_cache import bootstrap_account_cache

        bootstrap_account_cache()
        fixes.append("account_cache_bootstrap")
    state["monitor_last_run"] = time.time()
    state["monitor_last_issues"] = issues
    state["monitor_last_fixes"] = fixes
    save_state(state)

    if fixes or issues:
        log_event(
            "system",
            f"{APP_NAME} monitor",
            f"events={len(events)} issues={','.join(issues) or 'none'} fixes={','.join(fixes) or 'none'}",
        )
    _save_checkpoint(cp)
    return {"issues": issues, "fixes": fixes, "events_scanned": len(events)}


def main() -> None:
    load_history()
    log_event("system", f"{APP_NAME} monitor started", f"interval={MONITOR_INTERVAL_SEC}s watchdog-only")
    while True:
        try:
            run_monitor_pass()
        except Exception as exc:
            log_event("error", "Monitor pass failed", str(exc))
        time.sleep(MONITOR_INTERVAL_SEC)


if __name__ == "__main__":
    main()
