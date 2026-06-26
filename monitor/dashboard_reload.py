"""Hot-reload dashboard.server only — trader and monitor stay running."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DASHBOARD_HOST, DASHBOARD_PORT, PID_DIR, PROJECT_ROOT

def _launcher_python() -> str:
    env_py = os.environ.get("KNIGHTTRADER_PYTHON", "").strip()
    if env_py and Path(env_py).is_file():
        return env_py
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        for ver in ("Python312", "Python313", "Python311"):
            candidate = local / "Programs" / ver / "python.exe"
            if candidate.is_file():
                return str(candidate)
    return sys.executable


PYTHON = _launcher_python()
RELOAD_COOLDOWN_SEC = float(os.environ.get("KNIGHTTRADER_RELOAD_COOLDOWN", "60"))


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


def _dashboard_pids() -> list[int]:
    if sys.platform != "win32":
        return []
    try:
        raw = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'dashboard.server' } | Select-Object ProcessId | ConvertTo-Json -Compress",
            ],
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return [int(row["ProcessId"]) for row in data if row.get("ProcessId")]
    except (subprocess.SubprocessError, ValueError, json.JSONDecodeError, KeyError):
        return []


def _kill_dashboard() -> int:
    killed = 0
    me = os.getpid()
    for pid in _dashboard_pids():
        if pid == me:
            continue
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        killed += 1
    pid_file = PID_DIR / "dashboard.pid"
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().strip())
            if pid != me and pid not in _dashboard_pids():
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                killed += 1
        except (OSError, ValueError):
            pass
    return killed


def _spawn_dashboard() -> subprocess.Popen:
    from trader.stack_control import _stack_env

    PID_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [PYTHON, "-m", "dashboard.server"],
        cwd=str(ROOT),
        env=_stack_env(),
        creationflags=subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0,
    )
    (PID_DIR / "dashboard.pid").write_text(str(proc.pid), encoding="utf-8")
    return proc


def hot_reload_dashboard(*, last_reload_ts: float = 0.0) -> dict[str, object]:
    """Stop dashboard.server and start a fresh one. Does not touch trader/monitor."""
    if last_reload_ts and time.time() - last_reload_ts < RELOAD_COOLDOWN_SEC:
        return {"ok": False, "skipped": True, "reason": "reload_cooldown"}

    killed = _kill_dashboard()
    if killed:
        time.sleep(1.5)

    proc = _spawn_dashboard()
    healthy = _dashboard_up(retries=20, delay_sec=1.5)
    return {
        "ok": healthy,
        "pid": proc.pid,
        "killed": killed,
        "reloaded_at": time.time(),
    }


if __name__ == "__main__":
    from activity_log import log_event, load_history

    load_history()
    result = hot_reload_dashboard()
    if result.get("ok"):
        log_event(
            "system",
            "Dashboard hot-reloaded",
            f"pid={result.get('pid')} killed={result.get('killed')}",
        )
        print(json.dumps(result))
        raise SystemExit(0)
    if result.get("skipped"):
        print(json.dumps(result))
        raise SystemExit(0)
    log_event("error", "Dashboard hot-reload failed", json.dumps(result))
    print(json.dumps(result))
    raise SystemExit(1)
