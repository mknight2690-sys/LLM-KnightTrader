"""Repair Agent 2 — Code Fixer.

A master software engineer diagnostic technician.
Reads error logs, tracebacks, and source code to find and fix bugs.

Can:
- Read any source file to understand context
- Parse tracebacks to find exact file + line
- Search for patterns across the codebase
- Apply precise search-and-replace fixes
- Run git to check recent changes, diff, blame
- Restart modules after fixes
- Run Python syntax check after edits
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

from trader.repair_agent import (
    agent_main,
    get_log_tail,
    get_recent_errors,
    list_source_files,
    log,
    log_err,
    log_warn,
    llm_ask,
    read_file_safe,
    run_cmd,
    write_file_safe,
)

AGENT_NAME = "code_fixer"
LABEL = "Repair[CodeFixer]"
LOOP_SEC = 45.0

SYSTEM = """You are the LLM KnightTrader **Code Fixer** — an autonomous software engineer and diagnostic technician.

You find and fix bugs like a mechanic who diagnoses engine problems from sound and vibration.
You don't need prior knowledge of the codebase — you read errors, trace the cause through the code,
form a hypothesis, then fix it precisely.

## YOUR TOOLS
- Read any file: read_file_safe(path) — path is relative to project root
- Edit files: write_file_safe(path, content) — writes entire file content
- Search codebase: run_cmd(["grep", "-r", "pattern", "trader/"], timeout=10)
- Check recent changes: run_cmd(["git", "log", "--oneline", "-10"])
- See what changed: run_cmd(["git", "diff", "HEAD~1"])
- Check syntax: run_cmd(["python", "-c", "import py_compile; py_compile.compile('file.py', doraise=True)"])
- List all source files: list_source_files()
- Read error logs: get_log_tail("trader.err", 50)
- Get recent errors: get_recent_errors(50)

## DIAGNOSTIC PROCESS
1. Get recent errors from activity log
2. For each error with a traceback, extract the file and line number
3. READ that file to understand the context around the error
4. Form a hypothesis about what's wrong
5. Check git log for recent changes that might have introduced the bug
6. Apply a precise fix
7. Verify syntax after editing

## RESPONSE FORMAT
Respond ONLY with valid JSON (no markdown):
{
  "bug_found": true|false,
  "diagnosis": "What the bug is (max 300 chars)",
  "reasoning": "How you traced it (max 500 chars)",
  "actions": [
    {"type": "read_file", "path": "trader/agent.py"},
    {"type": "edit_file", "path": "trader/agent.py", "search": "exact text to find", "replace": "replacement text"},
    {"type": "run_command", "cmd": ["python", "-c", "import py_compile; py_compile.compile('trader/agent.py', doraise=True)"], "timeout": 10},
    {"type": "restart_trader"},
    {"type": "hold", "reason": "..."}
  ],
  "confidence": 0-100
}

If no bugs found: {"bug_found":false,"diagnosis":"no code bugs detected","reasoning":"","actions":[]}

RULES:
- Always read the file before editing it
- "search" must match EXACTLY including whitespace and indentation
- Max 1 file edit per diagnosis (pick the most impactful fix)
- Never edit more than 10 lines at a time
- If confidence < 60, just report the bug — don't fix it
- After editing, verify with a syntax check command
- If you see a NameError/ImportError, check if it's a missing import or renamed function
- If you see a TypeError, check argument types and function signatures
- If you see a KeyError/IndexError, check the data structure being accessed
"""


def _extract_traceback_files(detail: str) -> list[tuple[str, int]]:
    """Extract (file, line) from Python traceback."""
    results = []
    for m in re.finditer(r'File "([^"]+)", line (\d+)', detail):
        path = m.group(1)
        line = int(m.group(2))
        if "llm-knighttrader" in path:
            rel = path.split("llm-knighttrader\\")[-1] if "llm-knighttrader\\" in path else path
            results.append((rel, line))
    return results


def _execute_action(action: dict, state: dict) -> str:
    atype = action.get("type", "")
    if atype == "read_file":
        path = action.get("path", "")
        content = read_file_safe(ROOT / path) if path else None
        return f"read {path}: {len(content)} chars" if content else f"read {path}: not found"
    elif atype == "edit_file":
        path = action.get("path", "")
        search = action.get("search", "")
        replace = action.get("replace", "")
        if path and search and replace:
            file_path = ROOT / path
            source = read_file_safe(file_path)
            if source and search in source:
                new_source = source.replace(search, replace, 1)
                if write_file_safe(file_path, new_source):
                    state["repairs_succeeded"] += 1
                    return f"edited {path}"
                return f"edit failed (write error)"
            return f"search text not found in {path}"
        return "edit skipped (missing params)"
    elif atype == "run_command":
        cmd = action.get("cmd", [])
        if cmd:
            rc, out = run_cmd(cmd, timeout=action.get("timeout", 10))
            return f"cmd {' '.join(cmd[:3])}: rc={rc} out={out[:100]}"
        return "no command"
    elif atype == "restart_trader":
        from trader.stack_control import start_single_trader
        result = start_single_trader()
        if result.get("ok"):
            state["repairs_succeeded"] += 1
        return f"restart: {result}"
    elif atype == "hold":
        return f"holding: {action.get('reason', 'observing')}"
    return f"unknown action: {atype}"


def run_cycle(client, llm, state: dict) -> None:
    now = time.time()
    errors = get_recent_errors(50)

    # Filter to only code-related errors (tracebacks, ImportErrors, etc.)
    code_errors = []
    for e in errors:
        detail = e.get("detail", "")
        title = e.get("title", "")
        if any(k in detail or k in title for k in (
            "Traceback", "Error", "ImportError", "ModuleNotFoundError",
            "TypeError", "KeyError", "IndexError", "AttributeError",
            "NameError", "SyntaxError", "ValueError",
        )):
            if "repair" not in title.lower() and "watchdog" not in title.lower():
                code_errors.append(e)

    if not code_errors:
        return

    state["repairs_attempted"] += 1

    # Build context for LLM
    lines = [f"{len(code_errors)} code errors found:"]

    # Extract files from tracebacks
    tb_files = set()
    for e in code_errors[-10:]:
        detail = e.get("detail", "")[:400]
        title = e.get("title", "")
        lines.append(f"[{title}] {detail[:200]}")
        for f, ln in _extract_traceback_files(detail):
            tb_files.add(f)

    # Include trader error log
    trader_err = get_log_tail("trader.err", 40)
    if trader_err:
        lines.append(f"\ntrader.err:\n{trader_err[:500]}")

    # Include git log for context
    rc, git_log = run_cmd(["git", "log", "--oneline", "-5"], timeout=5)
    if rc == 0 and git_log:
        lines.append(f"\nrecent commits:\n{git_log[:300]}")

    user_msg = "\n".join(lines)

    answer = llm_ask(AGENT_NAME, llm, SYSTEM, user_msg, max_tokens=1500)

    if not answer:
        log_warn(LABEL, "LLM unavailable", f"{len(code_errors)} code errors pending")
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
    reasoning = plan.get("reasoning", "")
    actions = plan.get("actions", [])

    if confidence < 60:
        log(LABEL, f"Low confidence diagnosis ({confidence}%)", diagnosis)
        if reasoning:
            log(LABEL, "Reasoning", reasoning[:300])
        return

    actions_taken = []
    for action in actions:
        result = _execute_action(action, state)
        actions_taken.append(f"{action.get('type','')}: {result}")

    log(LABEL, f"Fix applied (confidence={confidence}%)", diagnosis)
    if reasoning:
        log(LABEL, "Reasoning", reasoning[:300])
    if actions_taken:
        log(LABEL, "Actions", "; ".join(actions_taken))


if __name__ == "__main__":
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
    )
