"""Bot stack control — status and restart without killing the dashboard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from config import DATA_DIR, PID_DIR, PROJECT_ROOT
from trader.health import TRADER_PID_FILE, _pid_alive, _trader_pids

TRADER_ERR_LOG = DATA_DIR / "logs" / "trader.err"

BOT_MODULES = (
    "trader.agent",
    "monitor.agent",
    "babysit_12m",
    "babysit_perpetual",
    "watch_and_fix",
    "watch_logs",
)

MONITOR_PID_FILE = PID_DIR / "monitor.pid"
DASHBOARD_PID_FILE = PID_DIR / "dashboard.pid"
DEFAULT_TRADER_PYTHON = Path(
    os.environ.get(
        "KNIGHTTRADER_PYTHON",
        sys.executable,
    )
)


def _trader_python() -> str:
    env_py = os.environ.get("KNIGHTTRADER_PYTHON", "").strip()
    if env_py and Path(env_py).is_file():
        return env_py
    if DEFAULT_TRADER_PYTHON and Path(str(DEFAULT_TRADER_PYTHON)).is_file():
        return str(DEFAULT_TRADER_PYTHON)
    return sys.executable


def _module_pids(module: str, exclude: int | None = None) -> list[int]:
    """Find running module PIDs via command-line match."""
    mine = exclude or os.getpid()
    pattern = module.replace(".", r"\.")
    found: list[int] = []
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                    f"Where-Object {{ $_.CommandLine -match '-m {pattern}' }} | "
                    "Select-Object -ExpandProperty ProcessId",
                ],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=15,
            )
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    pid = int(line)
                    if pid != mine:
                        found.append(pid)
        else:
            out = subprocess.check_output(["pgrep", "-f", f"-m {module}"], text=True, timeout=10)
            for line in out.splitlines():
                if line.strip().isdigit():
                    pid = int(line.strip())
                    if pid != mine:
                        found.append(pid)
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        pass
    return found


def _cmd_matches_module(cmd: str, module: str) -> bool:
    if f"-m {module}" in cmd:
        return True
    if module in cmd and "-m" in cmd:
        return True
    return False


def _matches_bot(cmd: str) -> bool:
    if "dashboard.server" in cmd:
        return False
    if any(f"-m {module}" in cmd for module in BOT_MODULES):
        return True
    root = str(PROJECT_ROOT).lower()
    if root in cmd.lower() or "hermes-llm-trader" in cmd.lower():
        return any(module in cmd for module in BOT_MODULES)
    return False


def _enumerate_python_processes() -> list[dict[str, Any]]:
    if sys.platform == "win32":
        script = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -eq 'python.exe' } | "
            "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
        )
        try:
            raw = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", script],
                text=True,
                timeout=20,
            ).strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            return []
        if not raw:
            return []
        rows = json.loads(raw)
        if isinstance(rows, dict):
            rows = [rows]
        return [
            {"pid": int(row["ProcessId"]), "cmd": row.get("CommandLine") or ""}
            for row in rows
            if row.get("ProcessId")
        ]

    found: list[dict[str, Any]] = []
    try:
        out = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True, timeout=15)
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            pid_s, cmd = parts
            if "python" not in cmd.lower():
                continue
            found.append({"pid": int(pid_s), "cmd": cmd})
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        pass
    return found


def _kill_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            os.kill(pid, 15)
            time.sleep(0.2)
            if _pid_alive(pid):
                os.kill(pid, 9)
        return True
    except OSError:
        return False


def _clear_bot_pid_files() -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    for path in (TRADER_PID_FILE, MONITOR_PID_FILE, DASHBOARD_PID_FILE):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _clear_stack_pid_files() -> None:
    _clear_bot_pid_files()


def running_process_counts() -> dict[str, int]:
    """Live python process counts for the full KnightTrader stack."""
    watcher_modules = ("babysit_12m", "babysit_perpetual", "watch_and_fix", "watch_logs")
    watchers = 0
    for module in watcher_modules:
        watchers += len(_pids_for_module(module))
    return {
        "dashboard": len(_module_pids("dashboard.server")),
        "trader": len(_trader_pids()),
        "monitor": len(_pids_for_module("monitor.agent")),
        "watchers": watchers,
    }


def is_entire_stack_stopped() -> bool:
    counts = running_process_counts()
    return all(v == 0 for v in counts.values())


def kill_entire_stack(exclude_pids: set[int] | None = None) -> list[int]:
    """Kill dashboard, trader, monitor, and watcher processes (full desktop stack)."""
    exclude = set(exclude_pids or ())
    exclude.add(os.getpid())
    killed: list[int] = list(kill_all_bots(exclude_pids=exclude))

    for pid in _module_pids("dashboard.server"):
        if pid in exclude or pid in killed:
            continue
        if _kill_pid(pid):
            killed.append(pid)

    for row in _enumerate_python_processes():
        pid = row["pid"]
        if pid in exclude or pid in killed:
            continue
        cmd = row["cmd"]
        if "dashboard.server" not in cmd:
            continue
        root = str(PROJECT_ROOT).lower()
        if f"-m dashboard.server" in cmd or root in cmd.lower() or "hermes-llm-trader" in cmd.lower():
            if _kill_pid(pid):
                killed.append(pid)

    _clear_stack_pid_files()
    return killed


def _pids_for_module(module: str) -> list[int]:
    if module == "trader.agent":
        return _trader_pids()
    return _module_pids(module)


def _classify_processes() -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for module in BOT_MODULES:
        grouped[module] = sorted(set(_pids_for_module(module)))

    for row in _enumerate_python_processes():
        cmd = row["cmd"]
        if not _matches_bot(cmd):
            continue
        for module in BOT_MODULES:
            if _cmd_matches_module(cmd, module):
                grouped[module].append(row["pid"])
        for module in BOT_MODULES:
            grouped[module] = sorted(set(grouped[module]))
    return grouped


def _component_status(count: int) -> str:
    if count == 1:
        return "online"
    if count > 1:
        return "duplicate"
    return "offline"


def stack_status() -> dict[str, Any]:
    grouped = _classify_processes()
    trader_pids = grouped["trader.agent"]
    monitor_pids = grouped["monitor.agent"]
    watcher_pids: list[int] = []
    for module in ("babysit_12m", "babysit_perpetual", "watch_and_fix", "watch_logs"):
        watcher_pids.extend(grouped[module])
    watcher_pids = sorted(set(watcher_pids))

    trader_pid_file: int | None = None
    if TRADER_PID_FILE.is_file():
        try:
            trader_pid_file = int(TRADER_PID_FILE.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            trader_pid_file = None

    trader_online = len(trader_pids) == 1
    extras_running = len(monitor_pids) > 0 or len(watcher_pids) > 0

    return {
        "trader": {
            "status": _component_status(len(trader_pids)),
            "pid": trader_pids[0] if trader_online else None,
            "count": len(trader_pids),
            "pid_file": trader_pid_file,
            "pid_file_alive": bool(trader_pid_file and _pid_alive(trader_pid_file)),
        },
        "monitor": {
            "status": _component_status(len(monitor_pids)),
            "pid": monitor_pids[0] if len(monitor_pids) == 1 else None,
            "count": len(monitor_pids),
        },
        "watchers": {
            "count": len(watcher_pids),
            "pids": watcher_pids,
        },
        "healthy": trader_online and not extras_running,
    }


def kill_all_bots(exclude_pids: set[int] | None = None) -> list[int]:
    """Kill trader, monitor, and watcher bots. Never kills dashboard.server."""
    exclude = set(exclude_pids or ())
    exclude.add(os.getpid())
    killed: list[int] = []

    for module in BOT_MODULES:
        for pid in _pids_for_module(module):
            if pid in exclude or pid in killed:
                continue
            if _kill_pid(pid):
                killed.append(pid)

    for row in _enumerate_python_processes():
        pid = row["pid"]
        if pid in exclude or pid in killed:
            continue
        if not _matches_bot(row["cmd"]):
            continue
        if _kill_pid(pid):
            killed.append(pid)
    return killed


def start_single_trader() -> dict[str, Any]:
    """Start exactly one trader.agent process."""
    for pid in _trader_pids():
        _kill_pid(pid)
    _clear_bot_pid_files()
    time.sleep(0.5)

    if _trader_pids():
        return {"ok": False, "error": "could not stop existing trader processes", "pids": _trader_pids()}

    kwargs: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    python_bin = _trader_python()
    TRADER_ERR_LOG.parent.mkdir(parents=True, exist_ok=True)
    err_log = open(TRADER_ERR_LOG, "a", encoding="utf-8")
    err_log.write(f"\n--- trader start {time.strftime('%Y-%m-%d %H:%M:%S')} pid-pending ---\n")
    err_log.flush()
    kwargs["stderr"] = err_log
    proc = subprocess.Popen([python_bin, "-m", "trader.agent"], **kwargs)
    PID_DIR.mkdir(parents=True, exist_ok=True)
    TRADER_PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    time.sleep(2.0)
    alive_pids = _trader_pids()
    if proc.poll() is not None and not alive_pids:
        try:
            TRADER_PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "error": f"trader exited immediately (code {proc.returncode})"}

    pid = alive_pids[0] if len(alive_pids) == 1 else proc.pid
    if len(alive_pids) > 1:
        return {"ok": False, "error": "multiple traders started", "pids": alive_pids}

    TRADER_PID_FILE.write_text(str(pid), encoding="utf-8")
    return {"ok": True, "pid": pid, "python": python_bin}


def restart_bots() -> dict[str, Any]:
    """Kill all bots, then start a single trader instance."""
    killed = kill_all_bots()
    _clear_bot_pid_files()
    time.sleep(1.5)
    for pid in kill_all_bots():
        if pid not in killed:
            killed.append(pid)

    trader = start_single_trader()
    return {
        "killed_pids": killed,
        "trader": trader,
        "stack": stack_status(),
    }
