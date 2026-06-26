"""Shared base for LLM-powered repair agents.

Each agent runs as a persistent detached process launched by the stack launcher.
They have full access to: activity log, all log files, source code (read/edit),
process management, network checks, and the Blofin client.

They operate like automotive technicians — they don't need to know the codebase
in advance. They diagnose from evidence (logs, errors, symptoms) and fix what's broken.
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
from config import DATA_DIR, PID_DIR, PROJECT_ROOT
from llm.wrapper import LLMWrapper

AGENT_ERR_LOG = DATA_DIR / "logs" / "repair_agent.err"


def _ensure_log_dir() -> None:
    AGENT_ERR_LOG.parent.mkdir(parents=True, exist_ok=True)


def log(agent: str, title: str, detail: str = "", meta: dict | None = None) -> None:
    log_event("system", f"[{agent}] {title}", detail[:400] or "", meta)


def log_err(agent: str, title: str, detail: str = "") -> None:
    log_event("error", f"[{agent}] {title}", detail[:400] or "")


def log_warn(agent: str, title: str, detail: str = "") -> None:
    log_event("warning", f"[{agent}] {title}", detail[:400] or "")


def _acquire_agent_lock(name: str) -> bool:
    lock = PID_DIR / f"repair_agent_{name}.lock"
    PID_DIR.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()
    if lock.exists():
        try:
            old_pid = int(lock.read_text(encoding="utf-8").strip())
            if old_pid > 0 and _pid_alive(old_pid):
                return False
            lock.unlink(missing_ok=True)
        except (ValueError, OSError):
            pass
    lock.write_text(str(my_pid), encoding="utf-8")
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
    if not _acquire_agent_lock(name):
        print(f"{label}: another instance already running — exiting", flush=True)
        sys.exit(1)

    def _stop(signum, frame):
        raise SystemExit(0)

    setup_sigterm(_stop)

    from blofin.client import BlofinClient
    client = BlofinClient()
    log(name, "Agent started", f"interval={interval_sec}s")

    llm = LLMWrapper()
    state: dict[str, Any] = {
        "last_action_ts": 0.0,
        "consecutive_errors": 0,
        "repairs_attempted": 0,
        "repairs_succeeded": 0,
        "startup_ts": time.time(),
        "seen_fingerprints": [],
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
                    log_err(name, "Too many errors, will retry after long sleep", "")
                    time.sleep(120)
                    state["consecutive_errors"] = 0
            time.sleep(interval_sec)
    except SystemExit:
        log(name, "Agent stopping", "received signal")
    finally:
        _release_agent_lock(name)
        log(name, "Agent stopped", f"attempted={state['repairs_attempted']} succeeded={state['repairs_succeeded']}")


def llm_ask(agent: str, llm: LLMWrapper, system_prompt: str, user_message: str, *, max_tokens: int = 1500) -> str:
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


def run_cmd(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return -1, str(exc)


def get_stack_status() -> dict[str, Any]:
    from trader.stack_control import stack_status
    return stack_status()


def get_recent_errors(limit: int = 50) -> list[dict]:
    noise = (
        "repair llm", "repair triage", "repair complete", "repair recovered",
        "repair skipped llm", "stream guardian", "proactive repair",
        "watchdog", "duplicate trader", "[watchdog]", "[Repair",
    )
    out = []
    for event in reversed(get_recent(limit)):
        if event.get("type") != "error":
            continue
        blob = f"{event.get('title','')} {event.get('detail','')}".lower()
        if any(m in blob for m in noise):
            continue
        out.append(event)
    out.reverse()
    return out


def get_log_tail(filename: str, lines: int = 80) -> str:
    log_path = DATA_DIR / "logs" / filename
    if not log_path.is_file():
        return ""
    try:
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(all_lines[-lines:])
    except OSError:
        return ""


def list_source_files() -> list[str]:
    """List all Python source files in the project for the LLM to know about."""
    files = []
    for path in PROJECT_ROOT.rglob("*.py"):
        if "__pycache__" in str(path) or ".venv" in str(path):
            continue
        rel = path.relative_to(PROJECT_ROOT)
        files.append(str(rel))
    return sorted(files)


# --- Technician diagnostic framework (shared by all 3 repair agents) ---

TECHNICIAN_METHOD = """DIAGNOSTIC METHOD — work like an automotive technician with zero prior knowledge:
1. OBSERVE symptoms only (errors, logs, process counts, port health, account state).
2. HYPOTHESIZE 2-3 root causes from evidence — do not guess without data.
3. TEST with low-risk actions first (refresh_account, read logs, stack_status).
4. ACT using repair_action_catalog types only — never invent action types.
5. VERIFY: re-check stack_status / port / errors after each fix."""

REPAIR_AGENT_MODULES: tuple[str, ...] = (
    "trader.repair_agents.watchdog",
    "trader.repair_agents.code_fixer",
    "trader.repair_agents.order_guardian",
)


def parse_llm_json(text: str) -> dict[str, Any]:
    """Parse JSON from LLM response, stripping markdown fences if present."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    plan = json.loads(raw)
    if not isinstance(plan, dict):
        raise ValueError("LLM response is not a JSON object")
    return plan


def triage_with_repair_engine(
    client: Any,
    llm: LLMWrapper,
    state: dict[str, Any],
    incident: dict[str, Any],
    *,
    label: str = "repair_agent",
) -> Any:
    """Run full deterministic + LLM repair pipeline (same as trader/repair.py)."""
    from blofin.account_cache import read_account_cached
    from trader.repair import llm_triage_and_repair
    from trader.state import save_state

    account = read_account_cached() or {}
    try:
        result = llm_triage_and_repair(state, client, llm, account, None, incident=incident)
        save_state(state)
        log(label, "Repair engine result", f"recovered={result.recovered} | {result.diagnosis[:180]}")
        return result
    except Exception as exc:
        log_err(label, "Repair engine failed", str(exc)[:300])
        return None


def execute_catalog_actions(
    client: Any,
    llm: LLMWrapper,
    state: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    label: str = "repair_agent",
) -> list[str]:
    """Execute a list of repair catalog actions; returns labels of what ran."""
    from blofin.account_cache import read_account_cached
    from trader.repair import execute_repair_action

    account = read_account_cached() or {}
    taken: list[str] = []
    for action in actions[:5]:
        if not isinstance(action, dict):
            continue
        atype = str(action.get("type") or "")
        label_out, ok, _ = execute_repair_action(state, client, llm, account, None, action)
        taken.append(f"{label_out}:{'ok' if ok else 'fail'}")
        log(label, f"Action {atype}", f"{'ok' if ok else 'fail'}")
    return taken


def source_context_around_line(rel_path: str, line_no: int, *, radius: int = 12) -> str:
    """Return source lines around an error line for code-fixer context."""
    path = PROJECT_ROOT / rel_path
    source = read_file_safe(path)
    if not source:
        return ""
    lines = source.splitlines()
    idx = max(0, min(line_no - 1, len(lines) - 1))
    start = max(0, idx - radius)
    end = min(len(lines), idx + radius + 1)
    out = []
    for i in range(start, end):
        marker = ">>>" if i == idx else "   "
        out.append(f"{marker} {i + 1:4d}| {lines[i]}")
    return "\n".join(out)


def kill_agent_process() -> int:
    killed = 0
    try:
        if sys.platform == "win32":
            cmd_raw = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"(Name='python.exe' OR Name='pythonw.exe') AND CommandLine LIKE '%repair_agents%'\" | "
                 "Select-Object ProcessId | ConvertTo-Json -Compress"],
                stderr=subprocess.DEVNULL, text=True, timeout=8,
            ).strip()
            if cmd_raw and cmd_raw != "null":
                rows = json.loads(cmd_raw)
                if isinstance(rows, dict):
                    rows = [rows]
                for row in rows:
                    pid = int(row.get("ProcessId", 0))
                    if pid and pid != os.getpid():
                        subprocess.Popen(
                            ["taskkill", "/PID", str(pid), "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                        killed += 1
        else:
            out = subprocess.check_output(
                ["pgrep", "-f", "repair_agents"], text=True, timeout=8
            )
            for line in out.splitlines():
                pid = int(line.strip())
                if pid and pid != os.getpid():
                    os.kill(pid, 15)
                    killed += 1
    except (subprocess.SubprocessError, FileNotFoundError, ValueError, OSError):
        pass
    return killed
