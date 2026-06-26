"""Shared base for LLM-powered repair agents.

Each repair agent runs as a persistent detached process launched by the stack launcher.
They share: LLM access, activity log monitoring, stack health checks, process management,
and the ability to edit source code to fix bugs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from activity_log import get_recent, log_event
from config import DATA_DIR, PID_DIR
from llm.wrapper import LLMWrapper
from trader.stack_control import stack_status

AGENT_ERR_LOG = DATA_DIR / "logs" / "repair_agent.err"


def _ensure_log_dir() -> None:
    AGENT_ERR_LOG.parent.mkdir(parents=True, exist_ok=True)


def log(agent: str, title: str, detail: str = "", meta: dict | None = None) -> None:
    log_event("system", f"[{agent}] {title}", detail[:400] or "", meta)


def log_err(agent: str, title: str, detail: str = "") -> None:
    log_event("error", f"[{agent}] {title}", detail[:400] or "")


def log_warn(agent: str, title: str, detail: str = "") -> None:
    log_event("warning", f"[{agent}] {title}", detail[:400] or "")


def _acquire_agent_lock(name: str) -> tuple[bool, Path]:
    lock = PID_DIR / f"repair_agent_{name}.lock"
    PID_DIR.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()
    if lock.exists():
        try:
            old_pid = int(lock.read_text(encoding="utf-8").strip())
            if old_pid > 0 and _pid_alive(old_pid):
                return False, lock
            lock.unlink(missing_ok=True)
        except (ValueError, OSError):
            pass
    lock.write_text(str(my_pid), encoding="utf-8")
    return True, lock


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


def _release_agent_lock(name: str) -> None:
    lock = PID_DIR / f"repair_agent_{name}.lock"
    try:
        if lock.is_file():
            pid = int(lock.read_text(encoding="utf-8").strip())
            if pid == os.getpid():
                lock.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def setup_sigterm(handler) -> None:
    import signal
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def agent_main(
    name: str,
    label: str,
    run_cycle_fn,
    interval_sec: float = 30.0,
) -> None:
    _ensure_log_dir()
    acquired, lock_path = _acquire_agent_lock(name)
    if not acquired:
        print(f"{label}: another instance already running — exiting", flush=True)
        sys.exit(1)

    def _stop(signum, frame):
        raise SystemExit(0)

    setup_sigterm(_stop)

    client = None
    from blofin.client import BlofinClient
    client = BlofinClient()
    log(name, "Agent started", f"interval={interval_sec}s")

    llm = LLMWrapper()
    state: dict[str, Any] = {
        "last_activity_ts": 0.0,
        "seen_errors": [],
        "last_action_ts": 0.0,
        "consecutive_errors": 0,
        "repairs_attempted": 0,
        "repairs_succeeded": 0,
        "startup_ts": time.time(),
    }

    try:
        while True:
            try:
                run_cycle_fn(client, llm, state)
                state["consecutive_errors"] = 0
            except SystemExit:
                raise
            except Exception as exc:
                state["consecutive_errors"] += 1
                log_err(name, "Cycle error", str(exc)[:300])
                if state["consecutive_errors"] > 20:
                    log_err(name, "Too many errors, restarting loop", "will retry after long sleep")
                    time.sleep(120)
                    state["consecutive_errors"] = 0
            time.sleep(interval_sec)
    except SystemExit:
        log(name, "Agent stopping", "received signal")
    finally:
        _release_agent_lock(name)
        log(name, "Agent stopped", f"attempted={state['repairs_attempted']} succeeded={state['repairs_succeeded']}")


def llm_ask(agent: str, llm: LLMWrapper, system_prompt: str, user_message: str, *, max_tokens: int = 800) -> str:
    try:
        resp = llm.chat(
            messages=[{"role": "user", "content": user_message}],
            max_tokens=max_tokens,
            system=system_prompt,
        )
        return resp.text
    except Exception as exc:
        log_err(agent, "LLM call failed", str(exc)[:300])
        return ""


def read_file_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def write_file_safe(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False


def kill_agent_process(exclude_pid: int | None = None) -> int:
    exclude = exclude_pid or os.getpid()
    killed = 0
    try:
        if sys.platform == "win32":
            cmd_raw = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"(Name='python.exe' OR Name='pythonw.exe') AND CommandLine LIKE '%repair_agent%'\" | "
                 "Select-Object ProcessId | ConvertTo-Json -Compress"],
                stderr=subprocess.DEVNULL, text=True, timeout=8,
            ).strip()
            if cmd_raw and cmd_raw != "null":
                rows = json.loads(cmd_raw)
                if isinstance(rows, dict):
                    rows = [rows]
                for row in rows:
                    pid = int(row.get("ProcessId", 0))
                    if pid and pid != exclude:
                        subprocess.Popen(
                            ["taskkill", "/PID", str(pid), "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                        killed += 1
        else:
            out = subprocess.check_output(
                ["pgrep", "-f", "repair_agent"], text=True, timeout=8
            )
            for line in out.splitlines():
                pid = int(line.strip())
                if pid and pid != exclude:
                    os.kill(pid, 15)
                    killed += 1
    except (subprocess.SubprocessError, FileNotFoundError, ValueError, OSError):
        pass
    return killed


