"""Repair Agent 2 — Code Fixer.

Monitors error logs and activity log for bugs, crashes, and code-level issues.
When it finds a problem it can diagnose, it reads the relevant source files,
identifies the bug, and applies a code fix. Then restarts affected modules.

Focus areas:
- Python tracebacks in error logs
- LLM response parsing failures
- BloFin API error codes that indicate code bugs
- Logic errors flagged by the trader LLM itself
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
from config import PROJECT_ROOT
from trader.repair_agent import (
    agent_main,
    log,
    log_err,
    log_warn,
    llm_ask,
    read_file_safe,
    write_file_safe,
)

AGENT_NAME = "code_fixer"
LABEL = "Repair[CodeFixer]"
LOOP_SEC = 45.0

CODEFIXER_SYSTEM = """You are the LLM KnightTrader **Code Fixer** — an autonomous software engineer.

You monitor the stack for code-level bugs: tracebacks, logic errors, API misuse, parsing failures.
When you find a real bug, you read the source, understand the issue, and output a precise fix.

You are conservative — only fix things you understand. Never rewrite large sections.
Only fix bugs that are causing actual errors in the logs.

Respond ONLY with valid JSON (no markdown):
{
  "bug_found": true|false,
  "file": "relative/path/to/file.py" or null,
  "line": 123 or null,
  "diagnosis": "What the bug is (max 200 chars)",
  "fix_type": "replace_line|replace_block|add_import|none",
  "fix_content": "The replacement code (if applicable)",
  "search_text": "The exact text to find in the file (for replace_block)",
  "restart_module": "trader.agent" or null,
  "confidence": 0-100,
  "lesson": {"category": "code_fix", "text": "Durable lesson"} or null
}

If no bug found: {"bug_found":false,"diagnosis":"no code bugs detected"}

RULES:
- Only fix bugs you can see in tracebacks or error logs.
- fix_content must be valid Python that fits the surrounding context.
- search_text must match EXACTLY including whitespace.
- Max 1 fix per diagnosis. If multiple bugs, pick the most impactful.
- Never fix more than 5 lines of code at a time.
- If confidence < 60, do not apply the fix — just report it.
"""


def _find_recent_tracebacks() -> list[dict]:
    """Find events with traceback or error detail in the last N log entries."""
    results = []
    for event in get_recent(50):
        if event.get("type") != "error":
            continue
        detail = event.get("detail", "")
        title = event.get("title", "")
        if "Traceback" in detail or "Error" in title or "error" in title.lower():
            if "repair" in title.lower() or "watchdog" in title.lower():
                continue
            results.append(event)
    return results[-10:]


def _apply_fix(file_path: Path, search_text: str, fix_content: str) -> bool:
    """Apply a search-and-replace fix to a file."""
    source = read_file_safe(file_path)
    if source is None:
        return False
    if search_text not in source:
        log_warn(LABEL, "search_text not found", f"file={file_path.name}")
        return False
    new_source = source.replace(search_text, fix_content, 1)
    if new_source == source:
        return False
    backup = file_path.read_text(encoding="utf-8")
    backup_path = file_path.with_suffix(file_path.suffix + ".bak")
    try:
        backup_path.write_text(backup, encoding="utf-8")
    except OSError:
        pass
    if write_file_safe(file_path, new_source):
        log(LABEL, "Fix applied", f"file={file_path.name}")
        return True
    return False


def _restart_module(module: str) -> bool:
    """Kill and restart a module. Returns True if restart initiated."""
    from trader.stack_control import start_single_trader
    if module == "trader.agent":
        result = start_single_trader()
        return bool(result.get("ok"))
    return False


def run_cycle(client, llm, state: dict) -> None:
    now = time.time()
    tracebacks = _find_recent_tracebacks()
    if not tracebacks:
        return

    state["repairs_attempted"] += 1
    recent = tracebacks[-5:]
    lines = []
    for e in recent:
        title = e.get("title", "")
        detail = e.get("detail", "")[:300]
        lines.append(f"[{title}] {detail}")

    user_msg = "Recent errors/tracebacks:\n" + "\n---\n".join(lines)

    answer = llm_ask(AGENT_NAME, llm, CODEFIXER_SYSTEM, user_msg, max_tokens=600)
    if not answer:
        log_warn(LABEL, "LLM unavailable for code fix", f"{len(tracebacks)} errors pending")
        return

    try:
        plan = json.loads(answer)
    except (json.JSONDecodeError, ValueError):
        stripped = answer.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
            if stripped.endswith("```"):
                stripped = stripped[:-3]
            plan = json.loads(stripped.strip())
        else:
            log_err(LABEL, "Could not parse LLM response", answer[:200])
            return

    if not plan.get("bug_found"):
        return

    confidence = plan.get("confidence", 0)
    diagnosis = plan.get("diagnosis", "")
    file_path_str = plan.get("file")
    fix_type = plan.get("fix_type", "none")

    if confidence < 60 or fix_type == "none":
        log(LABEL, f"Low confidence fix skipped ({confidence}%)", diagnosis)
        return

    if file_path_str and fix_type in ("replace_line", "replace_block"):
        file_path = PROJECT_ROOT / file_path_str
        if not file_path.is_file():
            log_err(LABEL, "File not found", file_path_str)
            return

        search_text = plan.get("search_text", "")
        fix_content = plan.get("fix_content", "")

        if search_text and fix_content:
            ok = _apply_fix(file_path, search_text, fix_content)
            if ok:
                state["repairs_succeeded"] += 1
                restart_module = plan.get("restart_module")
                if restart_module:
                    log(LABEL, "Restarting module after fix", restart_module)
                    _restart_module(restart_module)
            else:
                log_warn(LABEL, "Fix application failed", f"file={file_path.name}")
        else:
            log_warn(LABEL, "Empty fix from LLM", diagnosis)
    else:
        log(LABEL, "Diagnosis (no auto-fix)", f"confidence={confidence}: {diagnosis}")


if __name__ == "__main__":
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
    )
