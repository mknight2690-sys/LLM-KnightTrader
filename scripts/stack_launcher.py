"""Desktop launcher — start/stop the full LLM KnightTrader stack.

Single-instance enforcement: port check + lock file.
No aggressive process killing — user runs 'stop' first.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DATA_DIR, DASHBOARD_HOST, DASHBOARD_PORT, PID_DIR
from trader.stack_control import _trader_python, kill_entire_stack

DASHBOARD_URL = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"
LAUNCHER_LOCK = PID_DIR / "launcher.lock"


def _write_pid(name: str, pid: int) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    (PID_DIR / f"{name}.pid").write_text(str(pid), encoding="utf-8")


def _acquire_launcher_lock() -> bool:
    """Port check + lock file. Returns True if we should proceed."""
    PID_DIR.mkdir(parents=True, exist_ok=True)

    # Primary check: is the dashboard port in use?
    if not _is_port_free(DASHBOARD_PORT):
        return False

    # Secondary: lock file (prevents double-launch from race conditions)
    if LAUNCHER_LOCK.exists():
        try:
            old_pid = int(LAUNCHER_LOCK.read_text(encoding="utf-8").strip())
            if old_pid > 0 and _pid_alive(old_pid):
                return False
            # Stale lock from crashed process
            LAUNCHER_LOCK.unlink(missing_ok=True)
        except (ValueError, OSError):
            LAUNCHER_LOCK.unlink(missing_ok=True)

    LAUNCHER_LOCK.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" -ErrorAction SilentlyContinue; "
                 f"if ($p) {{ 'alive' }} else {{ 'dead' }}"],
                stderr=subprocess.DEVNULL, text=True, timeout=5,
            )
            return out.strip() == "alive"
        except (subprocess.SubprocessError, FileNotFoundError, ValueError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _release_launcher_lock() -> None:
    try:
        if LAUNCHER_LOCK.is_file():
            pid = int(LAUNCHER_LOCK.read_text(encoding="utf-8").strip())
            if pid == os.getpid():
                LAUNCHER_LOCK.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def _popen_module(module: str, *, log_file: str | None = None) -> subprocess.Popen:
    python_bin = _trader_python()
    kwargs: dict = {"cwd": str(ROOT)}
    if log_file:
        log_path = DATA_DIR / "logs" / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "a", encoding="utf-8")
        log_fh.write(f"\n--- {module} start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log_fh.flush()
        kwargs["stdout"] = log_fh
        kwargs["stderr"] = log_fh
    else:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen([python_bin, "-m", module], **kwargs)
    if log_file:
        log_fh.write(f"  pid={proc.pid}\n")
        log_fh.flush()
        log_fh.close()
    return proc


REPAIR_AGENTS = (
    ("trader.repair_agents.watchdog", "repair_watchdog.log"),
    ("trader.repair_agents.code_fixer", "repair_code_fixer.log"),
    ("trader.repair_agents.order_guardian", "repair_order_guardian.log"),
)


def _wait_dashboard_health(timeout_sec: float = 30.0) -> bool:
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


def _ensure_desktop_shortcuts() -> None:
    script = ROOT / "scripts" / "create_desktop_shortcuts.ps1"
    if not script.is_file():
        return
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=str(ROOT),
        check=False,
    )


def _is_port_free(port: int) -> bool:
    """Check if a TCP port is free (not in use)."""
    try:
        proc = subprocess.Popen(
            ["netstat", "-ano"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            stdout, _ = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            return True
        for line in stdout.decode("utf-8", errors="ignore").splitlines():
            if f":{port}" in line and "LISTENING" in line:
                return False
        return True
    except (OSError, ValueError):
        return True


def stop_stack() -> int:
    print("Stopping all LLM KnightTrader processes...", flush=True)
    killed = kill_entire_stack()
    # Also clean up repair agent locks
    for name in ("watchdog", "code_fixer", "order_guardian"):
        lock = PID_DIR / f"repair_agent_{name}.lock"
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        LAUNCHER_LOCK.unlink(missing_ok=True)
    except OSError:
        pass
    print(f"  killed={len(killed)}", flush=True)
    print("LLM KnightTrader stopped.", flush=True)
    return 0


def start_stack(*, open_browser: bool = False) -> int:
    """Start exactly one dashboard + one trader + repair agents."""
    if not _acquire_launcher_lock():
        print(f"ERROR: Stack is already running (port {DASHBOARD_PORT} in use).")
        print("  Run 'python scripts/stack_launcher.py stop' first.")
        print(f"  If this is wrong, delete: {LAUNCHER_LOCK}")
        return 1

    try:
        return _start_stack_inner(open_browser=open_browser)
    finally:
        _release_launcher_lock()


def _start_stack_inner(*, open_browser: bool = False) -> int:
    print("Starting dashboard...", flush=True)
    try:
        dash = _popen_module("dashboard.server")
    except Exception as e:
        print(f"ERROR: Failed to start dashboard.server: {e}")
        return 1
    print(f"  dashboard pid={dash.pid}", flush=True)
    time.sleep(2.0)
    if dash.poll() is not None:
        print(f"ERROR: dashboard.server exited immediately (code {dash.returncode})")
        return 1
    _write_pid("dashboard", dash.pid)

    if not _wait_dashboard_health():
        print("ERROR: dashboard health check failed")
        return 1
    print("  health OK", flush=True)

    print("Starting trader...", flush=True)
    try:
        trader = _popen_module("trader.agent")
    except Exception as e:
        print(f"ERROR: Failed to start trader.agent: {e}")
        return 1
    print(f"  trader pid={trader.pid}", flush=True)
    time.sleep(1.5)
    if trader.poll() is not None:
        print(f"ERROR: trader.agent exited immediately (code {trader.returncode})")
        return 1
    _write_pid("trader", trader.pid)

    time.sleep(1.0)
    dash_alive = dash.poll() is None
    trader_alive = trader.poll() is None
    if not dash_alive or not trader_alive:
        print(
            f"ERROR: process exited unexpectedly — "
            f"dashboard_alive={dash_alive} trader_alive={trader_alive}"
        )
        return 1

    # Launch repair agents
    repair_pids = []
    for module, log_name in REPAIR_AGENTS:
        try:
            agent = _popen_module(module, log_file=log_name)
            repair_pids.append(agent.pid)
            print(f"  {module} pid={agent.pid}", flush=True)
        except Exception as e:
            print(f"WARNING: failed to start {module}: {e}")

    _ensure_desktop_shortcuts()
    print(f"Dashboard PID {dash.pid} | Trader PID {trader.pid}")
    if repair_pids:
        print(f"Repair agents: {', '.join(str(p) for p in repair_pids)}")
    print(f"LLM KnightTrader ready -> {DASHBOARD_URL}")
    print("Daily use: double-click 'Start LLM KnightTrader' on desktop to restart the stack.")

    if open_browser:
        webbrowser.open(DASHBOARD_URL)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM KnightTrader desktop stack control")
    sub = parser.add_subparsers(dest="command", required=True)

    start_p = sub.add_parser("start", help="Start dashboard + trader (port must be free)")
    start_p.add_argument("--open-browser", action="store_true")

    sub.add_parser("stop", help="Stop all KT processes")

    args = parser.parse_args()
    if args.command == "stop":
        return stop_stack()
    return start_stack(open_browser=bool(args.open_browser))


if __name__ == "__main__":
    raise SystemExit(main())
