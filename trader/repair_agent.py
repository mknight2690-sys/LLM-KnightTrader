"""Shared base for LLM-powered repair agents.

Each agent runs as a persistent detached process launched by the stack launcher.
They have full access to: activity log, all log files, source code (read/edit),
process management, network checks, and the Blofin client.

They operate like automotive technicians — they don't need to know the codebase
in advance. They diagnose from evidence (logs, errors, symptoms) and fix what's broken.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from activity_log import get_recent, load_history, log_event
from config import ACCOUNT_REFRESH_SEC, DATA_DIR, PID_DIR, PROJECT_ROOT
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
                creationflags=subprocess.CREATE_NO_WINDOW,
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

    # Important: agents run as separate processes, so seed their local in-memory
    # activity buffer from disk. This makes them "tuned into activity logs"
    # immediately after Start.
    try:
        load_history(limit=1500)
    except Exception:
        pass

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
                # If the dashboard queued a manual repair request, run the repair engine now.
                maybe_handle_manual_repair_requests(client, llm, state, label=label)
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


def maybe_handle_manual_repair_requests(
    client: Any,
    llm: LLMWrapper,
    state: dict[str, Any],
    *,
    label: str = "repair_agent",
    max_requests_per_cycle: int = 1,
) -> bool:
    """Process any dashboard-queued 'Manual repair request' events."""
    try:
        recent = get_recent(limit=250)
    except Exception:
        return False

    startup_ts = float(state.get("startup_ts") or 0.0)
    done_list: list[str] = list(state.get("_manual_repair_done") or [])
    done = set(done_list)

    processed = 0
    for ev in reversed(recent):
        try:
            if processed >= max_requests_per_cycle:
                break

            if str(ev.get("title") or "") != "Manual repair request":
                continue

            ev_ts = float(ev.get("ts") or 0.0)
            if ev_ts and ev_ts < startup_ts:
                continue

            detail = str(ev.get("detail") or "")
            if not detail.strip():
                continue

            fp_src = f"manual_repair:{ev.get('title')}:{detail}"
            fp = hashlib.sha1(fp_src.encode("utf-8", "ignore")).hexdigest()[:16]
            if fp in done:
                continue

            done.add(fp)
            done_list.append(fp)
            state["_manual_repair_done"] = done_list

            incident = {
                "phase": "manual_repair",
                "title": "Manual repair request",
                "error": detail[:4000],
            }

            triage_with_repair_engine(
                client=client,
                llm=llm,
                state=state,
                incident=incident,
                label=label,
            )
            processed += 1
        except Exception:
            continue

    return processed > 0


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


# --- Novel-issue detection (logs, locks, multi-turn investigation) ---

_TRACEBACK_FILE_RE = re.compile(r'File "([^"]+)", line (\d+), in (\w+)')
_ERROR_TYPE_RE = re.compile(
    r"(\w+(?:Error|Exception)|Traceback|CRITICAL|FATAL|failed|rejected|timeout|refused)[:\s]",
    re.IGNORECASE,
)
_LOG_ISSUE_MARKERS = (
    "traceback",
    "error",
    "exception",
    "failed",
    "critical",
    "fatal",
    "modulenotfound",
    "importerror",
    "attributeerror",
    "typeerror",
    "keyerror",
    "syntaxerror",
    "connection refused",
    "timed out",
    "103003",
    "102089",
)

RESTARTABLE_MODULES = (
    "trader.agent",
    "dashboard.server",
    "monitor.agent",
)

NOVEL_INVESTIGATOR_PROMPT = (
    "You are an expert debugger fixing NOVEL failures in LLM KnightTrader.\n"
    + TECHNICIAN_METHOD
    + "\n\n"
    "You have never seen this codebase. Work only from evidence in logs and source.\n"
    "Multi-turn workflow:\n"
    "1. INVESTIGATE: request read_files[] to inspect source (max 3 paths per turn)\n"
    "2. DIAGNOSE: state root cause from evidence\n"
    "3. FIX: output exact search_text/replace_text (max 15 lines changed)\n"
    "4. RESTART: restart_module if runtime code changed\n\n"
    "Respond ONLY with JSON:\n"
    "{\n"
    '  "phase": "investigate"|"fix"|"done",\n'
    '  "read_files": ["trader/foo.py"],\n'
    '  "diagnosis": "...",\n'
    '  "file": "relative/path.py",\n'
    '  "search_text": "exact match",\n'
    '  "replace_text": "replacement",\n'
    '  "restart_module": "trader.agent"|"dashboard.server"|null,\n'
    '  "confidence": 0-100\n'
    "}\n"
    "phase=done when fixed or unfixable. confidence<50 means do not patch.\n"
)


def list_log_files() -> list[Path]:
    log_dir = DATA_DIR / "logs"
    if not log_dir.is_dir():
        return []
    files = sorted(
        [p for p in log_dir.iterdir() if p.is_file() and p.suffix in (".log", ".err")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[:20]


def scan_logs_for_issues(*, tail_lines: int = 80) -> list[dict[str, Any]]:
    """Scan all log files for tracebacks and error signatures — catches issues activity log missed."""
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for log_path in list_log_files():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        blob = "\n".join(lines[-tail_lines:])
        if not blob.strip():
            continue
        lower = blob.lower()
        if not any(m in lower for m in _LOG_ISSUE_MARKERS):
            continue

        tb_match = None
        for m in _TRACEBACK_FILE_RE.finditer(blob):
            tb_match = m
        if tb_match:
            abs_file = tb_match.group(1)
            rel = _rel_project_path(abs_file)
            err_m = _ERROR_TYPE_RE.search(blob[tb_match.end() :])
            fp = f"tb:{rel}:{tb_match.group(2)}:{err_m.group(1) if err_m else 'err'}"
            if fp not in seen:
                seen.add(fp)
                issues.append(
                    {
                        "type": "traceback",
                        "source": log_path.name,
                        "file": rel,
                        "line": int(tb_match.group(2)),
                        "function": tb_match.group(3),
                        "error": (err_m.group(1) if err_m else "Traceback")[:200],
                        "snippet": blob[-1800:],
                        "fingerprint": fp,
                    }
                )
            continue

        for line in lines[-tail_lines:]:
            stripped = line.strip()
            if not stripped or len(stripped) < 8:
                continue
            low = stripped.lower()
            if not any(m in low for m in _LOG_ISSUE_MARKERS):
                continue
            fp = f"log:{log_path.name}:{hashlib.sha1(stripped.encode()).hexdigest()[:12]}"
            if fp in seen:
                continue
            seen.add(fp)
            issues.append(
                {
                    "type": "log_error",
                    "source": log_path.name,
                    "error": stripped[:300],
                    "snippet": stripped,
                    "fingerprint": fp,
                }
            )
    return issues[-15:]


def _rel_project_path(abs_file: str) -> str:
    try:
        return str(Path(abs_file).resolve().relative_to(PROJECT_ROOT.resolve()))
    except (ValueError, OSError):
        norm = abs_file.replace("\\", "/")
        for marker in ("hermes-llm-trader/", "llm-knighttrader/"):
            if marker in norm:
                return norm.split(marker, 1)[1]
        return Path(abs_file).name


def try_acquire_incident_lock(fingerprint: str, *, ttl_sec: float = 120.0) -> bool:
    """One repair agent owns a fingerprint at a time."""
    if not fingerprint or fingerprint == "ok":
        return True
    lock = PID_DIR / f"repair_incident_{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}.lock"
    PID_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if lock.is_file():
        try:
            raw = lock.read_text(encoding="utf-8").strip().split("|")
            ts = float(raw[0])
            owner = int(raw[1]) if len(raw) > 1 else 0
            if now - ts < ttl_sec and owner != os.getpid() and _pid_alive(owner):
                return False
        except (ValueError, OSError):
            pass
    lock.write_text(f"{now}|{os.getpid()}", encoding="utf-8")
    return True


def release_incident_lock(fingerprint: str) -> None:
    if not fingerprint:
        return
    lock = PID_DIR / f"repair_incident_{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}.lock"
    try:
        if lock.is_file():
            raw = lock.read_text(encoding="utf-8").strip().split("|")
            owner = int(raw[1]) if len(raw) > 1 else 0
            if owner == os.getpid():
                lock.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def restart_stack_module(module: str) -> dict[str, Any]:
    """Restart a stack module after a code or runtime fix."""
    from trader.stack_control import restart_module

    return restart_module(module)


def apply_source_patch(
    rel_path: str,
    search_text: str,
    replace_text: str,
    *,
    label: str = "repair_agent",
) -> bool:
    """Apply a surgical source patch with timestamped backup."""
    file_path = PROJECT_ROOT / rel_path
    if not file_path.is_file():
        log_err(label, "Patch file missing", rel_path)
        return False
    source = read_file_safe(file_path)
    if source is None:
        return False
    if search_text not in source:
        log_warn(label, "Patch search_text missing", rel_path)
        return False
    new_source = source.replace(search_text, replace_text, 1)
    if new_source == source:
        return False
    try:
        backup = file_path.with_suffix(file_path.suffix + f".bak_{int(time.time())}")
        backup.write_text(source, encoding="utf-8")
    except OSError:
        pass
    if not write_file_safe(file_path, new_source):
        return False
    rc, out = run_cmd(
        [sys.executable, "-m", "py_compile", str(file_path)],
        timeout=25,
    )
    if rc != 0:
        log_warn(label, "Patch syntax check failed", out[:200])
        try:
            write_file_safe(file_path, source)
        except OSError:
            pass
        return False
    log(label, "Source patch applied", rel_path)
    return True


def _run_safe_diagnostic(cmd: list[str]) -> tuple[int, str]:
    if not cmd or len(cmd) > 10:
        return -1, "diagnostic rejected"
    exe = str(cmd[0]).lower()
    if not (exe.endswith("python.exe") or exe.endswith("python") or cmd[0] == sys.executable):
        return -1, "only python diagnostics allowed"
    joined = " ".join(cmd).lower()
    for blocked in ("pip install", "git push", "rm -", "del /", "format ", "shutdown"):
        if blocked in joined:
            return -1, "blocked diagnostic"
    if "-m" in cmd and "py_compile" not in joined and "import" not in joined:
        return -1, "only py_compile or import checks"
    return run_cmd(cmd, timeout=30)


def run_novel_investigation(
    agent_name: str,
    label: str,
    llm: LLMWrapper,
    state: dict[str, Any],
    issue: dict[str, Any],
    *,
    max_turns: int = 4,
) -> bool:
    """Multi-turn LLM investigation for novel bugs — read files, patch, restart, verify."""
    fp = str(issue.get("fingerprint") or issue.get("error") or "")[:200]
    if not try_acquire_incident_lock(fp):
        return False

    try:
        context_parts = [
            f"ISSUE TYPE: {issue.get('type')}",
            f"SOURCE LOG: {issue.get('source', '')}",
            f"ERROR: {issue.get('error', '')}",
        ]
        if issue.get("file"):
            ctx = source_context_around_line(str(issue["file"]), int(issue.get("line") or 1))
            if ctx:
                context_parts.append(f"SOURCE CONTEXT:\n{ctx}")
        context_parts.append(f"LOG SNIPPET:\n{(issue.get('snippet') or '')[-2500:]}")
        context_parts.append(f"\nProject .py files ({len(list_source_files())} total) — request read_files to inspect.")

        transcript = "\n".join(context_parts)
        fixed = False

        for turn in range(max_turns):
            user_msg = transcript
            if turn > 0:
                user_msg += f"\n\n--- TURN {turn + 1}/{max_turns} ---\nContinue investigation or apply fix."
            answer = llm_ask(agent_name, llm, NOVEL_INVESTIGATOR_PROMPT, user_msg[:8000], max_tokens=1800)
            if not answer:
                break
            try:
                plan = parse_llm_json(answer)
            except (json.JSONDecodeError, ValueError) as exc:
                log_warn(label, "Investigation parse failed", str(exc)[:100])
                break

            phase = str(plan.get("phase") or "investigate")
            diagnosis = str(plan.get("diagnosis") or "")[:300]
            if diagnosis:
                log(label, f"Investigation t{turn + 1}", diagnosis[:200])

            read_files = plan.get("read_files") or []
            if isinstance(read_files, list) and read_files:
                reads = []
                for rel in read_files[:3]:
                    rel_s = str(rel).strip()
                    content = read_file_safe(PROJECT_ROOT / rel_s)
                    if content:
                        reads.append(f"=== {rel_s} ===\n{content[:4000]}")
                    else:
                        reads.append(f"=== {rel_s} === (not found)")
                transcript += "\n\nFILE CONTENTS:\n" + "\n\n".join(reads)

            run_cmd_plan = plan.get("run_cmd")
            if isinstance(run_cmd_plan, list) and run_cmd_plan:
                rc, out = _run_safe_diagnostic([str(x) for x in run_cmd_plan])
                transcript += f"\n\nDIAGNOSTIC rc={rc}:\n{out[:800]}"

            confidence = int(plan.get("confidence") or 0)
            if phase == "fix" and plan.get("file") and plan.get("search_text") and confidence >= 50:
                ok = apply_source_patch(
                    str(plan["file"]),
                    str(plan["search_text"]),
                    str(plan.get("replace_text") or ""),
                    label=label,
                )
                if ok:
                    state["repairs_succeeded"] += 1
                    fixed = True
                    restart = plan.get("restart_module")
                    if restart and str(restart) in RESTARTABLE_MODULES:
                        restart_stack_module(str(restart))
                        log(label, "Restarted after patch", str(restart))
                    if phase == "done" or confidence >= 70:
                        return True

            if phase == "done":
                return fixed

        return fixed
    finally:
        release_incident_lock(fp)


def gather_novel_incidents() -> list[dict[str, Any]]:
    """Combine log scan + activity errors into deduped novel incidents."""
    incidents: list[dict[str, Any]] = []
    seen: set[str] = set()

    for issue in scan_logs_for_issues():
        fp = str(issue.get("fingerprint") or "")
        if fp and fp not in seen:
            seen.add(fp)
            incidents.append(issue)

    for event in get_recent_errors(30):
        detail = event.get("detail", "")
        title = event.get("title", "")
        blob = f"{title} {detail}"
        if not any(m in blob.lower() for m in _LOG_ISSUE_MARKERS):
            continue
        fp = f"act:{hashlib.sha1(blob.encode()).hexdigest()[:16]}"
        if fp in seen:
            continue
        seen.add(fp)
        if "Traceback" in detail:
            files = _TRACEBACK_FILE_RE.findall(detail)
            if files:
                target = files[-1]
                incidents.append(
                    {
                        "type": "traceback",
                        "source": "activity_log",
                        "file": _rel_project_path(target[0]),
                        "line": int(target[1]),
                        "function": target[2],
                        "error": title[:200],
                        "snippet": detail[-2000:],
                        "fingerprint": fp,
                    }
                )
                continue
        incidents.append(
            {
                "type": "activity_error",
                "source": "activity_log",
                "error": f"{title}: {detail[:200]}",
                "snippet": detail[:1500],
                "fingerprint": fp,
            }
        )
    return incidents[-12:]


def _infer_repair_phase_from_error(err: str, title: str) -> str:
    e = (err or "").lower()
    t = (title or "").lower()
    if any(k in e for k in ("tpsl", "tp/sl", "tp/sl", "tp ", "sl ", "tpsl_failed")):
        return "tpsl_failed"
    if "close" in e and ("fail" in e or "reject" in e):
        return "close_failed"
    if "open" in e and "reject" in e:
        return "open_rejected"
    if "open" in e and ("fail" in e or "reject" in e or "error" in e):
        return "open_failed"
    if "cycle_crash" in e or "crash" in e or "exception" in e:
        return "cycle_crash"
    if any(k in e for k in ("proactive", "stale", "anomaly", "mismatch", "cooldown")):
        return "proactive_anomaly"
    # Default: let repair.py decide based on error codes (102089, etc.).
    return "unknown_incident"


def _extract_inst_id(text: str) -> str | None:
    if not text:
        return None
    # Try common patterns in error blobs.
    m = re.search(r"instId[\\s:=\\\"]+([A-Za-z0-9_-]+)", text)
    if m:
        return m.group(1)
    m2 = re.search(r"\\b(inst[\\-]?[A-Za-z0-9_-]{3,})\\b", text)
    if m2:
        return m2.group(1)
    return None


def maybe_autorepair_global(
    client: Any,
    llm: LLMWrapper,
    state: dict[str, Any],
    *,
    label: str,
    max_incidents: int = 2,
    cooldown_sec: float = 180.0,
) -> bool:
    """
    Extra safety net:
    - Watches for novel failures in BOTH activity log + log files
    - Feeds them into trader.repair.llm_triage_and_repair
    - Does NOT replace role-specific behavior; it only triggers when we see
      new, non-repaired failures.
    """
    try:
        incidents = gather_novel_incidents()
    except Exception as exc:
        log_warn(label, "Global autorepair incident scan failed", str(exc)[:200])
        return False

    seen = state.setdefault("global_autorepair_seen", {})
    ok_any = False
    for issue in incidents[-max_incidents:]:
        fp = str(issue.get("fingerprint") or issue.get("error") or "")[:200]
        if not fp:
            continue
        last = float(seen.get(fp) or 0)
        if time.time() - last < cooldown_sec:
            continue

        err = str(issue.get("error") or issue.get("snippet") or "")
        title = str(issue.get("file") or issue.get("source") or "")
        phase = _infer_repair_phase_from_error(err, title)
        inst_id = _extract_inst_id(err)

        incident = {
            "phase": phase,
            "error": err[:1200],
            "title": title[:120],
            "instId": inst_id,
        }

        result = triage_with_repair_engine(
            client,
            llm,
            state,
            incident,
            label=label,
        )
        if result and (result.recovered or result.actions_taken):
            seen[fp] = time.time()
            ok_any = True
            log(label, "Global autorepair recovered", f"phase={phase} fp={fp[:16]}")
        else:
            # Still mark it so we don't loop.
            seen.setdefault(fp, time.time())

    return ok_any


def check_account_early_and_repair(
    client: Any,
    llm: LLMWrapper,
    state: dict[str, Any],
    *,
    label: str,
    max_age_mult: float = 4.0,
    cooldown_sec: float = 300.0,
) -> bool:
    """
    Early warning net: if equity/available/margin-like numbers or drift look corrupted,
    correct the dashboard stream immediately (deterministic first), and only then
    escalate to the full repair engine if still unresolved.
    """
    try:
        from blofin.account_cache import (
            _account_display_sane,  # internal but stable in this repo
            cache_age_sec,
            guard_account_stream,
            is_rate_limited,
            read_account_cached,
            stream_drift_issues,
        )
    except Exception as exc:
        log_warn(label, "Early account check import failed", str(exc)[:200])
        return False


def _trade_failed_fingerprint(trade: dict[str, Any]) -> str:
    action = str(trade.get("action") or "")
    inst = str(trade.get("instId") or "")
    ts = float(trade.get("ts") or 0)
    return f"trade:{action}:{inst}:{int(ts)}"


def _trade_error_to_incident(trade: dict[str, Any]) -> dict[str, Any] | None:
    action = str(trade.get("action") or "")
    ok = trade.get("ok")
    resp = trade.get("response") or {}
    reason = str(trade.get("reason") or trade.get("error") or "")

    # Determine failure.
    failed = (
        ok is False
        or "failed" in action.lower()
        or str(action).lower() in ("open_failed", "close_failed", "open_rejected")
    )
    # Some trades may have ok missing but response code indicates failure.
    if not failed and resp:
        code = resp.get("code")
        if code is not None and str(code) not in ("0", "0.0", ""):
            failed = True
        if resp.get("error") or resp.get("msg") or resp.get("title"):
            # If response has an obvious error field, treat as failure.
            msg_blob = str(resp.get("error") or resp.get("msg") or resp.get("title") or "")
            if msg_blob and ("error" in msg_blob.lower() or "reject" in msg_blob.lower()):
                failed = True

    if not failed:
        return None

    phase = "open_failed"
    a = action.lower()
    if "close" in a:
        phase = "close_failed"
    elif "tpsl" in a or "tp/sl" in a:
        phase = "tpsl_failed"
    elif "open" in a:
        phase = "open_failed"

    inst_id = trade.get("instId")
    side = trade.get("side")
    contracts = trade.get("contracts") or trade.get("size_contracts") or trade.get("size")
    price = resp.get("markPrice") or resp.get("mark") or trade.get("price") or trade.get("mark")
    tp_pct = trade.get("tp_pct") or resp.get("tp_pct") if isinstance(resp, dict) else None
    sl_pct = trade.get("sl_pct") or resp.get("sl_pct") if isinstance(resp, dict) else None

    err_blob = reason or str(resp.get("msg") or resp.get("error") or resp.get("title") or "")
    if not err_blob:
        err_blob = f"trade failed: action={action}"

    return {
        "phase": phase,
        "title": "dashboard recent trade failed",
        "error": err_blob[:900],
        "instId": inst_id,
        "side": side,
        "contracts": contracts,
        "price": price,
        "tp_pct": tp_pct if tp_pct is not None else 2.0,
        "sl_pct": sl_pct if sl_pct is not None else 1.0,
        "leverage": trade.get("leverage") or 3,
    }


def check_dashboard_sections_and_repair(
    client: Any,
    llm: LLMWrapper,
    state: dict[str, Any],
    *,
    label: str,
    cooldown_sec: float = 180.0,
) -> bool:
    """
    Monitor dashboard sections (activity log already handled elsewhere) by scanning:
    - Recent Trades: state['trades'] for failed/open/close/tpsl problems
    - Research & Strategy: state['research_notes'] for explicit error markers
    - Last LLM Decision: state['last_decision'] for fallback/invalid markers

    If found, run full repair triage for the incident.
    """
    try:
        from trader.state import load_state
    except Exception as exc:
        log_warn(label, "Dashboard state import failed", str(exc)[:200])
        return False

    try:
        s = load_state()
    except Exception as exc:
        log_warn(label, "Dashboard state load failed", str(exc)[:200])
        return False

    seen = state.setdefault("dashboard_incident_seen", {})
    if not isinstance(seen, dict):
        seen = {}
        state["dashboard_incident_seen"] = seen

    def _cooldown(fp: str) -> bool:
        last = float(seen.get(fp) or 0)
        return fp and time.time() - last < cooldown_sec

    incidents: list[dict[str, Any]] = []

    # Recent trades
    trades = list(s.get("trades") or [])
    for tr in trades[-25:]:
        if not isinstance(tr, dict):
            continue
        incident = _trade_error_to_incident(tr)
        if not incident:
            continue
        fp = _trade_failed_fingerprint(tr)
        if _cooldown(fp):
            continue
        seen[fp] = time.time()
        incidents.append(incident)

    # Research notes: if agent explicitly recorded an error/failure note.
    research = list(s.get("research_notes") or [])
    for n in research[-30:]:
        if not isinstance(n, dict):
            continue
        note = str(n.get("note") or "")
        if any(k in note.lower() for k in ("error", "fallback", "unsafe", "failed", "rejected")):
            fp = f"research:{int(float(n.get('ts') or time.time()))}:{note[:40]}"
            if _cooldown(fp):
                continue
            seen[fp] = time.time()
            incidents.append(
                {
                    "phase": "proactive_anomaly",
                    "title": "dashboard research flagged failure",
                    "error": note[:900],
                }
            )
            break

    # Last decision: invalid response / fallback after error
    decision = s.get("last_decision")
    if isinstance(decision, dict):
        reasoning = str(decision.get("reasoning") or "")
        blob = json.dumps(decision, default=str)[:900]
        if any(k in reasoning.lower() for k in ("fallback", "invalid", "unsafe", "rate limited", "error")):
            fp = f"decision:{int(time.time())}:{blob[:40]}"
            if not _cooldown(fp):
                seen[fp] = time.time()
                incidents.append(
                    {
                        "phase": "proactive_anomaly",
                        "title": "dashboard last decision indicates failure",
                        "error": reasoning[:900] or blob,
                    }
                )

    if not incidents:
        return False

    # Run one incident per cycle per agent to avoid stacking multiple LLM repairs.
    incident = incidents[0]
    try:
        result = triage_with_repair_engine(client, llm, state, incident, label=label)
        if result and (result.recovered or result.actions_taken):
            return True
    except Exception as exc:
        log_warn(label, "Dashboard section repair failed", str(exc)[:200])
        return False

    return False

    # Cooldown per-process to avoid hammering API/cache operations.
    meta = state.setdefault("_early_account_meta", {})
    last_ts = float(meta.get("last_ts") or 0)
    if time.time() - last_ts < cooldown_sec:
        return False

    snap = read_account_cached() or {}
    equity = float(snap.get("equity") or 0)
    available = float(snap.get("available") or 0)
    issues: list[dict[str, Any]] = []

    try:
        sane, reason = _account_display_sane(snap)
        if not sane and reason:
            issues.append({"code": reason, "detail": f"equity={equity} available={available}"})
    except Exception as exc:
        issues.append({"code": "account_display_sane_error", "detail": str(exc)[:200]})

    try:
        if is_rate_limited():
            issues.append(
                {"code": "account_rate_limited", "detail": "BloFin cooldown / rate limit active"}
            )
    except Exception:
        pass

    try:
        age = float(cache_age_sec())
        if age > max(ACCOUNT_REFRESH_SEC * max_age_mult, 60):
            issues.append({"code": "account_cache_stale", "detail": f"cache age {int(age)}s"})
    except Exception:
        pass

    try:
        drift = stream_drift_issues()
        if drift:
            issues.append({"code": "live_stream_drift", "detail": str(drift[:3])[:200]})
    except Exception:
        pass

    if not issues:
        return False

    meta["last_ts"] = time.time()
    log_warn(label, "Early account anomaly detected", "; ".join(i["code"] for i in issues)[:200])

    # Deterministic fix first: align stream with live BloFin if needed.
    try:
        guard = guard_account_stream()
        if guard.get("ok") and (guard.get("refreshed") or guard.get("live_verified")):
            state["repairs_succeeded"] += 1
            return True
    except Exception as exc:
        log_warn(label, "Early deterministic stream repair failed", str(exc)[:200])

    # If guard didn't resolve it, escalate to full repair triage once.
    try:
        incident = {
            "phase": "proactive_anomaly",
            "error": f"account_anomaly: equity={equity} available={available}",
            "issues": issues,
        }
        result = triage_with_repair_engine(client, llm, state, incident, label=label)
        return bool(result and (result.recovered or result.actions_taken))
    except Exception as exc:
        log_warn(label, "Early repair escalation failed", str(exc)[:200])
        return False


def kill_agent_process() -> int:
    killed = 0
    try:
        if sys.platform == "win32":
            cmd_raw = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"(Name='python.exe' OR Name='pythonw.exe') AND CommandLine LIKE '%repair_agents%'\" | "
                 "Select-Object ProcessId | ConvertTo-Json -Compress"],
                stderr=subprocess.DEVNULL, text=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW,
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
