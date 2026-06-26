"""Trader process health: single-instance lock and decision validation."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from activity_log import log_event
from config import APP_NAME, PID_DIR

TRADER_PID_FILE = PID_DIR / "trader.pid"
VALID_ACTIONS = frozenset({"hold", "open", "close", "close_all"})


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            line = out.strip().lower()
            return str(pid) in line and "no tasks" not in line
        except (subprocess.SubprocessError, FileNotFoundError, ValueError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_trader_process_cmd(cmd: str) -> bool:
    if any(marker in cmd for marker in ("stack_control", "stack_launcher", "stack_status", "_trader_pids")):
        return False
    compact = " ".join(cmd.split())
    if "-m trader.agent" in compact:
        return True
    if "trader\\agent.py" in compact or "trader/agent.py" in compact:
        return True
    return "from trader.agent import main; main()" in compact


def _trader_pids(exclude: int | None = None) -> list[int]:
    """Find running trader.agent PIDs (Windows + Unix)."""
    mine = exclude or os.getpid()
    found: list[int] = []
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -match 'python' } | "
                    "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress",
                ],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=20,
            ).strip()
            if not out:
                return []
            rows = __import__("json").loads(out)
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                pid = int(row["ProcessId"])
                if pid == mine:
                    continue
                cmd = row.get("CommandLine") or ""
                if _is_trader_process_cmd(cmd):
                    found.append(pid)
        else:
            out = subprocess.check_output(["pgrep", "-f", "-m trader.agent"], text=True, timeout=10)
            for line in out.splitlines():
                if line.strip().isdigit():
                    pid = int(line.strip())
                    if pid != mine:
                        found.append(pid)
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        pass
    return found


def kill_duplicate_traders(exclude_pid: int | None = None) -> int:
    """Terminate extra trader.agent processes. Returns count killed."""
    killed = 0
    for pid in _trader_pids(exclude_pid):
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
            else:
                os.kill(pid, 15)
            killed += 1
            log_event("system", "Duplicate trader stopped", f"pid {pid}")
        except OSError:
            pass
    return killed


def acquire_trader_lock() -> bool:
    """Ensure only one trader.agent runs (atomic lock file; Windows-safe)."""
    PID_DIR.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()
    lock_path = PID_DIR / "trader.lock"

    for _ in range(3):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(my_pid).encode("ascii"))
            os.close(fd)
            TRADER_PID_FILE.write_text(str(my_pid), encoding="utf-8")
            return True
        except FileExistsError:
            if not _lock_is_stale(lock_path):
                live = [pid for pid in _trader_pids() if pid != my_pid]
                print(
                    f"{APP_NAME}: another trader.agent is already running"
                    + (f" (pids {live})" if live else "")
                    + " — exiting",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                time.sleep(0.3)
    return False


def trader_lock_owner() -> int | None:
    """Return alive PID holding trader.lock, or None."""
    lock_path = PID_DIR / "trader.lock"
    if not lock_path.is_file():
        return None
    try:
        owner = int(lock_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    if owner > 0 and _pid_alive(owner):
        return owner
    return None


def _lock_is_stale(lock_path: Path) -> bool:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        owner = int(raw) if raw.isdigit() else 0
    except (OSError, ValueError):
        return True
    if owner <= 0 or owner == os.getpid():
        return True
    if not _pid_alive(owner):
        return True
    # WMI cmdline scan can lag; alive PID with lock is authoritative.
    return False


def release_trader_lock() -> None:
    my_pid = os.getpid()
    lock_path = PID_DIR / "trader.lock"
    try:
        if lock_path.is_file():
            raw = lock_path.read_text(encoding="utf-8").strip()
            if raw == str(my_pid):
                lock_path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    try:
        if TRADER_PID_FILE.is_file() and int(TRADER_PID_FILE.read_text(encoding="utf-8").strip()) == my_pid:
            TRADER_PID_FILE.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def normalize_decision(raw: dict[str, Any]) -> dict[str, Any]:
    """Reject API error blobs and invalid actions before execution."""
    if not isinstance(raw, dict):
        raise ValueError("decision is not a dict")
    if raw.get("error"):
        raise ValueError(f"API error in decision: {raw.get('error')}")
    action = str(raw.get("action") or "hold").lower().strip()
    if action not in VALID_ACTIONS:
        raise ValueError(f"invalid action: {action!r}")
    raw["action"] = action
    if action == "open" and not raw.get("instId"):
        raise ValueError("open requires instId")
    if action == "close" and not raw.get("instId"):
        raise ValueError("close requires instId")
    return raw
