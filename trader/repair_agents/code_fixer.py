"""Repair Agent 2 — Code Fixer.

A master software engineer that diagnoses code bugs from tracebacks and error logs.
Reads source, understands context, applies surgical fixes, restarts affected modules.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from activity_log import get_recent
from config import DATA_DIR, PROJECT_ROOT
from trader.repair_agent import (
    TECHNICIAN_METHOD,
    agent_main,
    get_log_tail,
    get_recent_errors,
    list_source_files,
    log,
    log_err,
    log_warn,
    llm_ask,
    parse_llm_json,
    read_file_safe,
    source_context_around_line,
    write_file_safe,
    run_cmd,
)

AGENT_NAME = "code_fixer"
LABEL = "Repair[CodeFixer]"
LOOP_SEC = 30.0

SYSTEM = (
    "You are the LLM KnightTrader **Code Fixer** — a master software engineer.\n\n"
    + TECHNICIAN_METHOD
    + "\n\n"
    "You diagnose and fix ANY code bug from tracebacks and error logs.\n"
    "You do NOT need prior knowledge of this codebase — read the source around the error line.\n\n"
    "WORKFLOW:\n"
    "1. SYMPTOM: Read the traceback/error\n"
    "2. READ: Study the source_context lines provided (>>> marks the error line)\n"
    "3. ROOT CAUSE: Find the exact broken logic\n"
    "4. FIX: Output a precise search-and-replace patch\n\n"
    "YOUR TOOLS:\n"
    "- read_file_safe(path) — read any source file\n"
    "- write_file_safe(path, content) — write modified content\n"
    "- list_source_files() — see all .py files\n"
    "- get_recent_errors() — get activity log errors\n"
    "- get_log_tail(filename) — read recent log lines\n"
    "- run_cmd(cmd_list) — run shell command\n\n"
    "RESPOND ONLY with valid JSON (no markdown):\n"
    "{\n"
    '  "diagnosis": "What the bug is (max 300 chars)",\n'
    '  "file": "relative/path/to/file.py",\n'
    '  "search_text": "EXACT code to find (match whitespace exactly)",\n'
    '  "replace_text": "EXACT replacement code",\n'
    '  "restart_module": "trader.agent" or null,\n'
    '  "confidence": 0-100\n'
    "}\n\n"
    "If no bug: {\"diagnosis\":\"no code bugs detected\",\"confidence\":100}\n\n"
    "RULES:\n"
    "- search_text MUST match file content EXACTLY including indentation\n"
    "- Max 1 fix per diagnosis, pick the most impactful\n"
    "- Never change more than 10 lines\n"
    "- If confidence < 60, do NOT apply the fix\n"
    "- Always read the file first before proposing a fix\n"
)


def _find_bugs() -> list[dict]:
    bugs = []
    for event in get_recent_errors(50):
        detail = event.get("detail", "")
        title = event.get("title", "")
        if "Traceback" in detail:
            tb = _extract_traceback(detail)
            if tb:
                bugs.append(tb)
        elif "ImportError" in detail or "ModuleNotFoundError" in detail:
            bugs.append({"type": "import_error", "title": title, "detail": detail[:400]})
        elif "AttributeError" in detail:
            bugs.append({"type": "attribute_error", "title": title, "detail": detail[:400]})
        elif "TypeError" in detail:
            bugs.append({"type": "type_error", "title": title, "detail": detail[:400]})
        elif "NameError" in detail:
            bugs.append({"type": "name_error", "title": title, "detail": detail[:400]})
        elif "KeyError" in detail:
            bugs.append({"type": "key_error", "title": title, "detail": detail[:400]})
    return bugs[-10:]


def _extract_traceback(detail: str) -> dict | None:
    files = re.findall(r'File "([^"]+)", line (\d+), in (\w+)', detail)
    error_match = re.search(r'(\w+Error[:\s].+?)(?:\n|$)', detail)
    if not files or not error_match:
        return None
    project_files = [f for f in files if "llm-knighttrader" in f[0] or "trader" in f[0]]
    if not project_files:
        project_files = files
    target = project_files[-1]
    abs_path = target[0]
    rel = abs_path
    try:
        from config import PROJECT_ROOT
        rel = str(Path(abs_path).resolve().relative_to(PROJECT_ROOT.resolve()))
    except (ValueError, OSError):
        if "llm-knighttrader" in abs_path.replace("\\", "/"):
            rel = abs_path.split("llm-knighttrader")[-1].lstrip("/\\")
    return {
        "type": "traceback",
        "file": rel,
        "abs_file": abs_path,
        "line": int(target[1]),
        "function": target[2],
        "error": error_match.group(1).strip()[:200],
        "full_traceback": detail[-1500:],
    }


def _apply_fix(file_path: Path, search_text: str, replace_text: str) -> bool:
    source = read_file_safe(file_path)
    if source is None:
        return False
    if search_text not in source:
        log_warn(LABEL, "search_text not found", f"file={file_path.name}")
        return False
    new_source = source.replace(search_text, replace_text, 1)
    if new_source == source:
        return False
    try:
        backup_path = file_path.with_suffix(file_path.suffix + f".bak_{int(time.time())}")
        backup_path.write_text(source, encoding="utf-8")
    except OSError:
        pass
    if write_file_safe(file_path, new_source):
        log(LABEL, "Fix applied", f"file={file_path.name}")
        return True
    return False


def _restart_module(module: str) -> bool:
    from trader.stack_control import start_single_trader
    if module == "trader.agent":
        result = start_single_trader()
        return bool(result.get("ok"))
    return False


def run_cycle(client, llm, state: dict) -> None:
    bugs = _find_bugs()
    if not bugs:
        return

    state["repairs_attempted"] += 1
    lines = [f"Found {len(bugs)} bug(s):\n"]
    for i, bug in enumerate(bugs[-5:]):
        lines.append(f"--- Bug {i+1} ---")
        if bug["type"] == "traceback":
            rel = bug.get("file", "")
            lines.append(f"File: {rel} line {bug['line']} in {bug['function']}")
            lines.append(f"Error: {bug['error']}")
            ctx = source_context_around_line(rel, bug["line"])
            if ctx:
                lines.append(f"Source context:\n{ctx}")
            lines.append(f"Traceback:\n{bug['full_traceback'][-600:]}")
        else:
            lines.append(f"Type: {bug['type']}")
            lines.append(f"Title: {bug['title']}")
            lines.append(f"Detail: {bug['detail']}")
        lines.append("")

    trader_log = get_log_tail("trader.err", 30)
    if trader_log:
        lines.append(f"\nTrader log:\n{trader_log[-600:]}")

    user_msg = "\n".join(lines)
    if len(user_msg) > 5000:
        user_msg = user_msg[:5000]

    answer = llm_ask(AGENT_NAME, llm, SYSTEM, user_msg, max_tokens=1500)
    if not answer:
        log_warn(LABEL, "LLM unavailable", f"{len(bugs)} bugs pending")
        return

    try:
        plan = parse_llm_json(answer)
    except (json.JSONDecodeError, ValueError) as exc:
        log_warn(LABEL, "LLM parse failed", str(exc)[:120])
        return

    if not plan.get("file") or not plan.get("search_text"):
        log(LABEL, "Diagnosis (no auto-fix)", plan.get("diagnosis", "")[:200])
        return

    confidence = plan.get("confidence", 0)
    if confidence < 60:
        log(LABEL, f"Low confidence ({confidence}%)", plan.get("diagnosis", "")[:200])
        return

    file_path = PROJECT_ROOT / plan["file"]
    if not file_path.is_file():
        log_err(LABEL, "File not found", plan["file"])
        return

    ok = _apply_fix(file_path, plan["search_text"], plan.get("replace_text", ""))
    if ok:
        state["repairs_succeeded"] += 1
        rc, output = run_cmd([sys.executable, "-c", f"import ast; ast.parse(open('{plan['file']}', encoding='utf-8').read()); print('OK')"])
        if rc != 0:
            log_warn(LABEL, "Possible syntax issue", output[:200])

        restart = plan.get("restart_module")
        if restart:
            log(LABEL, "Restarting after fix", restart)
            _restart_module(restart)

        log(LABEL, "Bug fixed", f"file={plan['file']} conf={confidence} | {plan.get('diagnosis','')[:200]}")
    else:
        log_warn(LABEL, "Fix failed", f"file={plan['file']}")


if __name__ == "__main__":
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
    )
