"""Bot stack control — status, dedupe, and full-stack restart."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from config import DATA_DIR, PID_DIR, PROJECT_ROOT
from trader.health import TRADER_PID_FILE, _is_trader_process_cmd, _pid_alive, _trader_pids, trader_lock_owner

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
def _discover_python() -> Path:
    env_py = os.environ.get("KNIGHTTRADER_PYTHON", "").strip()
    if env_py and Path(env_py).is_file():
        return Path(env_py)
    candidates: list[Path] = []
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates.append(local / "hermes" / "hermes-agent" / "venv" / "Scripts" / "python.exe")
        for ver in ("Python312", "Python313", "Python311"):
            candidates.append(local / "Programs" / ver / "python.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


DEFAULT_TRADER_PYTHON = _discover_python()


def _trader_python() -> str:
    env_py = os.environ.get("KNIGHTTRADER_PYTHON", "").strip()
    if env_py and Path(env_py).is_file():
        return env_py
    if DEFAULT_TRADER_PYTHON and Path(str(DEFAULT_TRADER_PYTHON)).is_file():
        return str(DEFAULT_TRADER_PYTHON)
    return sys.executable




def _cmdline_has_module(cmd: str, module: str) -> bool:
    if f"-m {module}" in cmd:
        return True
    if module == "trader.agent":
        if _is_trader_process_cmd(cmd):
            return True
        if "_launch_trader.py" in cmd:
            return True
        return False
    if module == "dashboard.server":
        if "-m dashboard.server" in cmd:
            return True
        if "_launch_dashboard.py" in cmd:
            return True
        if "from dashboard.server" in cmd:
            return True
        return False
    return False


def _module_pids(module: str, *, exclude_self: bool = False) -> list[int]:
    """Find running module PIDs via command-line match."""
    mine = os.getpid() if exclude_self else None
    found: list[int] = []
    for row in _enumerate_python_processes():
        pid = row["pid"]
        if mine is not None and pid == mine:
            continue
        if _cmdline_has_module(row["cmd"], module):
            found.append(pid)
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
            "Where-Object { $_.Name -match 'python' } | "
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


def _stack_env() -> dict[str, str]:
    """Pinned environment for child stack processes."""
    env = os.environ.copy()
    python_bin = _trader_python()
    env["KNIGHTTRADER_PYTHON"] = python_bin
    root = str(PROJECT_ROOT)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _subprocess_kwargs(*, stderr: Any = subprocess.DEVNULL) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdout": subprocess.DEVNULL,
        "stderr": stderr,
        "env": _stack_env(),
    }
    return kwargs


def _kill_pid(pid: int, *, tree: bool = False) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            args = ["taskkill", "/F", "/PID", str(pid)]
            if tree:
                args.insert(2, "/T")
            subprocess.run(
                args,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
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
    for path in (TRADER_PID_FILE, MONITOR_PID_FILE, DASHBOARD_PID_FILE, PID_DIR / "trader.lock"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _clear_stack_pid_files() -> None:
    _clear_bot_pid_files()


def _parent_pid_map() -> dict[int, int]:
    """Map pid -> parent pid for python processes (Windows WMI)."""
    if sys.platform != "win32":
        return {}
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'python' } | "
        "Select-Object ProcessId, ParentProcessId | ConvertTo-Json -Compress"
    )
    try:
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", script],
            text=True,
            timeout=20,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}
    if not raw:
        return {}
    rows = json.loads(raw)
    if isinstance(rows, dict):
        rows = [rows]
    out: dict[int, int] = {}
    for row in rows:
        try:
            out[int(row["ProcessId"])] = int(row.get("ParentProcessId") or 0)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _collapse_parent_child_pids(pids: list[int]) -> list[int]:
    """Uvicorn / Windows can show parent+child as two PIDs for one service."""
    unique = sorted(set(pids))
    if len(unique) <= 1:
        return unique
    parent_map = _parent_pid_map()
    drop: set[int] = set()
    for pid in unique:
        parent = parent_map.get(pid, 0)
        if parent in unique:
            drop.add(parent)
    collapsed = [pid for pid in unique if pid not in drop]
    return collapsed if collapsed else [unique[-1]]


def _dashboard_pids() -> list[int]:
    raw = sorted(set(_module_pids("dashboard.server")))
    collapsed = _collapse_parent_child_pids(raw)
    if len(collapsed) <= 1:
        return collapsed
    if DASHBOARD_PID_FILE.is_file():
        try:
            pid = int(DASHBOARD_PID_FILE.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = 0
        if pid and _pid_alive(pid):
            parent_map = _parent_pid_map()
            related = [p for p in collapsed if p == pid or parent_map.get(p) == pid]
            if related:
                return [related[-1]]
    return collapsed


def _stack_trader_pids() -> list[int]:
    owner = trader_lock_owner()
    collapsed = _collapse_parent_child_pids(sorted(set(_trader_pids())))
    if owner and owner not in collapsed:
        if _pid_alive(owner):
            return _collapse_parent_child_pids(sorted(set(collapsed + [owner])))
    return collapsed


def _active_trader_count() -> int:
    """Authoritative trader count — lock file beats slow WMI cmdline scans."""
    if trader_lock_owner():
        return 1
    return len(_stack_trader_pids())


def running_process_counts() -> dict[str, int]:
    """Live python process counts for the full KnightTrader stack."""
    watcher_modules = ("babysit_12m", "babysit_perpetual", "watch_and_fix", "watch_logs")
    watchers = 0
    for module in watcher_modules:
        watchers += len(_pids_for_module(module))
    return {
        "dashboard": len(_dashboard_pids()),
        "trader": _active_trader_count(),
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

    for pid in _module_pids("dashboard.server", exclude_self=True):
        if pid in exclude or pid in killed:
            continue
        if _kill_pid(pid, tree=True):
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
            if _kill_pid(pid, tree=True):
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
    dashboard_pids = _dashboard_pids()
    trader_pids = _stack_trader_pids()
    if trader_lock_owner() and not trader_pids:
        trader_pids = [trader_lock_owner()]  # type: ignore[list-item]
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
    dashboard_online = len(dashboard_pids) == 1
    extras_running = len(monitor_pids) > 0 or len(watcher_pids) > 0
    no_duplicates = (
        len(dashboard_pids) <= 1
        and len(trader_pids) <= 1
        and len(monitor_pids) <= 1
    )

    return {
        "dashboard": {
            "status": _component_status(len(dashboard_pids)),
            "pid": dashboard_pids[0] if dashboard_online else None,
            "count": len(dashboard_pids),
        },
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
        "healthy": dashboard_online and trader_online and not extras_running and no_duplicates,
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


def dedupe_preferred_module(module: str, *, keep: int | None = None) -> int | None:
    """Kill duplicate processes for module; prefer KNIGHTTRADER_PYTHON interpreter."""
    preferred = str(Path(_trader_python()).resolve()).lower()
    if module == "trader.agent":
        raw = sorted(set(_trader_pids()))
        collapsed = _collapse_parent_child_pids(raw)
        if len(collapsed) <= 1:
            return collapsed[0] if collapsed else keep
        pids = collapsed
    elif module == "dashboard.server":
        raw = sorted(set(_module_pids(module)))
        collapsed = _collapse_parent_child_pids(raw)
        if len(collapsed) <= 1:
            return collapsed[0] if collapsed else keep
        pids = collapsed
    else:
        pids = sorted(set(_module_pids(module)))
        if not pids:
            return None
    if not pids:
        return keep
    keep_pid = pids[-1]
    for row in _enumerate_python_processes():
        pid = row["pid"]
        if pid not in pids:
            continue
        if preferred in (row.get("cmd") or "").lower():
            keep_pid = pid
            break
    if keep is not None and keep in pids:
        keep_pid = keep
    for pid in pids:
        if pid != keep_pid:
            _kill_pid(pid)
    time.sleep(0.3)
    return keep_pid


def dedupe_preferred_dashboard(keep: int | None = None) -> int | None:
    """Kill duplicate dashboard.server processes."""
    return dedupe_preferred_module("dashboard.server", keep=keep)


def dedupe_preferred_trader() -> int | None:
    """Kill duplicate trader processes; keep KNIGHTTRADER_PYTHON instance."""
    return dedupe_preferred_module("trader.agent")


def start_single_trader() -> dict[str, Any]:
    """Start exactly one trader.agent process."""
    for _ in range(2):
        for pid in _trader_pids():
            _kill_pid(pid, tree=True)
        time.sleep(0.5)
    _clear_bot_pid_files()
    time.sleep(0.5)

    if _stack_trader_pids():
        dedupe_preferred_trader()
    if _stack_trader_pids():
        return {
            "ok": False,
            "error": "could not stop existing trader processes",
            "pids": _stack_trader_pids(),
        }

    python_bin = _trader_python()
    TRADER_ERR_LOG.parent.mkdir(parents=True, exist_ok=True)
    err_log = open(TRADER_ERR_LOG, "a", encoding="utf-8")
    err_log.write(f"\n--- trader start {time.strftime('%Y-%m-%d %H:%M:%S')} pid-pending ---\n")
    err_log.flush()

    wrapper = PROJECT_ROOT / "scripts" / "_launch_trader.py"
    kwargs = _subprocess_kwargs(stderr=err_log)
    cmd = [python_bin, "-u", str(wrapper)]
    proc = subprocess.Popen(cmd, **kwargs)
    PID_DIR.mkdir(parents=True, exist_ok=True)
    TRADER_PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    deadline = time.time() + 35.0
    stable_since: float | None = None
    stable_pid: int | None = None
    while time.time() < deadline:
        owner = trader_lock_owner()
        collapsed = _stack_trader_pids()
        if len(collapsed) > 1:
            dedupe_preferred_trader()
            collapsed = _stack_trader_pids()
            owner = trader_lock_owner()

        candidate = owner or (collapsed[0] if len(collapsed) == 1 else None)
        if candidate and _pid_alive(candidate):
            if stable_pid != candidate:
                stable_pid = candidate
                stable_since = time.time()
            elif stable_since and time.time() - stable_since >= 4.0:
                TRADER_PID_FILE.write_text(str(candidate), encoding="utf-8")
                return {"ok": True, "pid": candidate, "python": python_bin}
        else:
            stable_since = None
            stable_pid = None

        if proc.poll() is not None and not trader_lock_owner() and not collapsed:
            try:
                TRADER_PID_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            tail = ""
            try:
                err_log.flush()
                err_log.seek(0, 2)
            except OSError:
                pass
            return {"ok": False, "error": f"trader exited immediately (code {proc.returncode})"}
        time.sleep(0.5)

    collapsed = _stack_trader_pids()
    owner = trader_lock_owner()
    candidate = owner or (collapsed[0] if len(collapsed) == 1 else None)
    if candidate and _pid_alive(candidate):
        TRADER_PID_FILE.write_text(str(candidate), encoding="utf-8")
        return {"ok": True, "pid": candidate, "python": python_bin}

    try:
        TRADER_PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return {
        "ok": False,
        "error": "trader did not reach a stable single instance",
        "pids": collapsed,
        "spawn_exit": proc.poll(),
    }


def stop_all_stack_processes(*, exclude_pids: set[int] | None = None) -> list[int]:
    """Two-pass kill of the full stack (same as desktop Stop / launcher preamble)."""
    killed = kill_entire_stack(exclude_pids=exclude_pids)
    time.sleep(1.0)
    for pid in kill_entire_stack(exclude_pids=exclude_pids):
        if pid not in killed:
            killed.append(pid)
    return killed


def _kill_monitor_and_watchers(exclude_pids: set[int] | None = None) -> list[int]:
    """Kill monitor and watcher bots only (never dashboard or trader)."""
    exclude = set(exclude_pids or ())
    exclude.add(os.getpid())
    killed: list[int] = []
    extra_modules = ("monitor.agent", "babysit_12m", "babysit_perpetual", "watch_and_fix", "watch_logs")
    for module in extra_modules:
        for pid in _pids_for_module(module):
            if pid in exclude or pid in killed:
                continue
            if _kill_pid(pid):
                killed.append(pid)
    return killed


def ensure_single_stack(
    *,
    keep_dashboard_pid: int | None = None,
    require_trader: bool = True,
    settle_sec: float = 12.0,
    dedupe: bool = True,
) -> dict[str, Any]:
    """Dedupe dashboard/trader; kill monitor/watchers; verify exactly one of each."""
    exclude = {os.getpid()}
    if keep_dashboard_pid:
        exclude.add(keep_dashboard_pid)

    killed: list[int] = list(_kill_monitor_and_watchers(exclude_pids=exclude))
    deadline = time.time() + settle_sec
    last: dict[str, Any] = {}

    while time.time() < deadline:
        if dedupe:
            dedupe_preferred_dashboard(keep=keep_dashboard_pid)
            dedupe_preferred_trader()
        for pid in _kill_monitor_and_watchers(exclude_pids=exclude):
            if pid not in killed:
                killed.append(pid)

        counts = running_process_counts()
        stack = stack_status()
        issues: list[str] = []
        if counts["dashboard"] != 1:
            issues.append(f"dashboard count {counts['dashboard']}")
        if require_trader and _active_trader_count() != 1:
            issues.append(f"trader count {_active_trader_count()}")
        if counts["monitor"] > 0:
            issues.append(f"monitor count {counts['monitor']}")
        if counts["watchers"] > 0:
            issues.append(f"watchers count {counts['watchers']}")
        if stack.get("trader", {}).get("status") == "duplicate":
            issues.append("trader duplicate")
        if stack.get("dashboard", {}).get("status") == "duplicate":
            issues.append("dashboard duplicate")

        last = {
            "ok": not issues,
            "issues": issues,
            "counts": counts,
            "stack": stack,
            "killed_pids": killed,
        }
        if not issues:
            return last
        time.sleep(1.0)

    if require_trader and trader_lock_owner():
        counts = running_process_counts()
        stack = stack_status()
        last = {
            "ok": True,
            "issues": [],
            "counts": counts,
            "stack": stack,
            "killed_pids": killed,
        }
    return last


def spawn_full_stack_restart() -> dict[str, Any]:
    """Detached cold restart — same behavior as desktop Start shortcut."""
    script = PROJECT_ROOT / "scripts" / "stack_launcher.py"
    python_bin = _trader_python()
    if not script.is_file():
        return {"ok": False, "error": f"missing launcher: {script}"}

    kwargs: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    try:
        proc = subprocess.Popen([python_bin, str(script), "start"], **kwargs)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "mode": "full_stack_restart",
        "spawned_pid": proc.pid,
        "message": "Full stack restart spawned (stop all, then one dashboard + one trader)",
    }


def restart_traders() -> dict[str, Any]:
    """Cold restart entire stack — stop all KT processes, start one dashboard + one trader."""
    return spawn_full_stack_restart()


def restart_bots() -> dict[str, Any]:
    """Alias for restart_traders (legacy API)."""
    return restart_traders()


DESKTOP_START_LNK = "Start LLM KnightTrader.lnk"
DESKTOP_STOP_LNK = "Stop LLM KnightTrader.lnk"


def _windows_desktop_dir() -> Path:
    if sys.platform != "win32":
        return Path.home() / "Desktop"
    try:
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0, None, 0, buf) == 0:
            return Path(buf.value)
    except (AttributeError, OSError, ValueError):
        pass
    return Path.home() / "Desktop"


def desktop_shortcut_paths() -> dict[str, Path]:
    desktop = _windows_desktop_dir()
    return {"start": desktop / DESKTOP_START_LNK, "stop": desktop / DESKTOP_STOP_LNK}


def desktop_shortcuts_exist() -> bool:
    paths = desktop_shortcut_paths()
    return paths["start"].is_file() and paths["stop"].is_file()


def ensure_desktop_shortcuts() -> dict[str, Any]:
    """Create Start + Stop LLM KnightTrader shortcuts on the user Desktop."""
    script = PROJECT_ROOT / "scripts" / "create_desktop_shortcuts.ps1"
    if not script.is_file():
        return {"ok": False, "error": f"missing script: {script}"}
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=str(PROJECT_ROOT),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc))[:300]
        return {"ok": False, "error": detail}
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "error": str(exc)[:300]}
    paths = desktop_shortcut_paths()
    ok = desktop_shortcuts_exist()
    return {
        "ok": ok,
        "paths": {k: str(v) for k, v in paths.items()},
        "error": None if ok else "shortcuts script ran but .lnk files not found",
    }
