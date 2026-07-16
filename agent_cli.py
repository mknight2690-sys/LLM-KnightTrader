#!/usr/bin/env python3
"""
LLM KnightTrader — AI Agent CLI
A standalone systems operator CLI powered by NVIDIA GLM 5.1.
Each interaction spawns a fresh LLMWrapper instance (isolated, no shared state).

Usage:
    python agent_cli.py                    # Interactive mode
    python agent_cli.py "your prompt"      # Single-shot mode
    python agent_cli.py --auto             # Auto-confirm destructive ops (dangerous)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

# Ensure project root on path so we can import llm.wrapper and credentials
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm.wrapper import LLMWrapper
from credentials import discover_llm_env_keys

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION = "1.0.0"
HISTORY_FILE = ROOT / "data" / "agent_cli_history.jsonl"
CONFIG_FILE = ROOT / "data" / "agent_cli_config.json"

# ---------------------------------------------------------------------------
# System Prompt — Systems Operator
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are LLM KnightTrader Agent CLI — an autonomous systems operator with full local machine access.

You operate via TOOL CALLS only. Every response must be valid JSON with this schema:
{{
  "thought": "Your reasoning (max 400 chars)",
  "tool_calls": [
    {{"tool": "<name>", "params": {{<args>}}}}
  ],
  "final_answer": "Direct answer to user (only if no tools needed)"
}}

Rules:
- Prefer tool_calls over explanations. Execute first, explain after.
- Chain tool_calls logically: read → analyze → write → verify.
- For destructive ops (write, delete, run), include a safety check in thought.
- If the user asks something simple (greeting, math, definition), use final_answer.
- Always report concrete results — file paths edited, commands run, outputs seen.

Available tools:
read_file      {{path: str, offset?: int, lines?: int}} — Read a text file. Offset negative = from end.
write_file     {{path: str, content: str, mode?: "overwrite"|"append"}} — Write text. Default overwrite.
edit_file      {{path: str, old_string: str, new_string: str}} — Surgical replace. old_string must be exact.
search_files   {{pattern: str, path?: str, glob?: str}} — ripgrep search. Returns matches with context.
list_dir       {{path: str, depth?: int}} — List directory contents.
bash           {{command: str, timeout?: int, description?: str}} — Run a shell command. Returns stdout+stderr.
python         {{code: str}} — Execute Python code in a sandbox subprocess. Returns stdout+stderr.
http_get       {{url: str}} — Fetch a URL. Returns text content.
status         {{}} — Return current working dir, time, env summary.
git_status       {{}} — Git status + current branch.
git_diff         {{file?: str, staged?: bool}} — Show git diff (optionally for a file).
git_log          {{count?: int}} — Recent git log --oneline.
read_activity_log {{limit?: int}} — Read recent activity.jsonl events.
read_state       {{}} — Read trading state.json (sanitized).
read_equity_history {{limit?: int}} — Read equity history entries.
list_processes   {{filter?: str}} — List running processes (tasklist).
kill_process     {{pid?: int, name?: str}} — Kill a process by PID or name. DESTRUCTIVE.
diff_files       {{path_a: str, path_b: str}} — Unified diff of two files.
tail_log         {{path: str, lines?: int}} — Tail N lines from a log file.
bot_health       {{}} — Snapshot of trading stack health (dashboard/trader/monitor).

Context:
- Project root: {project_root}
- OS: Windows (Git Bash available)
- Python: {python_path}
- Current dir: {cwd}
"""


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def _tool_read_file(params: dict[str, Any]) -> dict[str, Any]:
    path = Path(params["path"]).expanduser().resolve()
    if not path.is_file():
        return {"ok": False, "error": f"Not a file: {path}"}
    try:
        offset = params.get("offset", 0)
        lines = params.get("lines")
        text = path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        if offset < 0:
            start = max(0, len(all_lines) + offset)
            end = len(all_lines)
        else:
            start = max(0, offset)
            end = len(all_lines) if lines is None else start + lines
        chunk = "\n".join(all_lines[start:end])
        return {"ok": True, "path": str(path), "lines": f"{start+1}-{min(end, len(all_lines))}", "content": chunk}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_write_file(params: dict[str, Any]) -> dict[str, Any]:
    path = Path(params["path"]).expanduser().resolve()
    mode = params.get("mode", "overwrite")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with open(path, "a", encoding="utf-8") as f:
                f.write(params["content"])
        else:
            path.write_text(params["content"], encoding="utf-8")
        return {"ok": True, "path": str(path), "bytes": len(params["content"].encode("utf-8"))}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_edit_file(params: dict[str, Any]) -> dict[str, Any]:
    path = Path(params["path"]).expanduser().resolve()
    if not path.is_file():
        return {"ok": False, "error": f"Not a file: {path}"}
    try:
        text = path.read_text(encoding="utf-8")
        old = params["old_string"]
        new = params["new_string"]
        if old not in text:
            return {"ok": False, "error": "old_string not found in file"}
        text = text.replace(old, new, 1)
        path.write_text(text, encoding="utf-8")
        return {"ok": True, "path": str(path), "replaced": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_search_files(params: dict[str, Any]) -> dict[str, Any]:
    pattern = params["pattern"]
    search_path = Path(params.get("path", ".")).expanduser().resolve()
    glob_filter = params.get("glob", "")
    try:
        cmd = ["rg", "-n", "-C", "2", "--no-heading", pattern]
        if glob_filter:
            cmd.extend(["-g", glob_filter])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(search_path),
        )
        lines = result.stdout.strip().splitlines()[:80]
        return {"ok": True, "matches": len(lines), "output": "\n".join(lines)}
    except FileNotFoundError:
        # Fallback to grep if ripgrep not available
        try:
            cmd = ["grep", "-r", "-n", "-C", "2", pattern]
            if glob_filter:
                cmd.extend(["--include", glob_filter])
            cmd.append(str(search_path))
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            lines = result.stdout.strip().splitlines()[:80]
            return {"ok": True, "matches": len(lines), "output": "\n".join(lines), "note": "used grep fallback"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_list_dir(params: dict[str, Any]) -> dict[str, Any]:
    path = Path(params.get("path", ".")).expanduser().resolve()
    depth = params.get("depth", 1)
    try:
        items: list[str] = []
        for root, dirs, files in os.walk(path):
            current_depth = len(Path(root).relative_to(path).parts)
            if current_depth >= depth:
                del dirs[:]
                continue
            for f in sorted(files)[:30]:
                items.append(f"  {Path(root).relative_to(path) / f}")
            for d in sorted(dirs)[:20]:
                items.append(f"  {Path(root).relative_to(path) / d}/")
        return {"ok": True, "path": str(path), "items": items[:60]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_bash(params: dict[str, Any]) -> dict[str, Any]:
    cmd = params["command"]
    timeout = params.get("timeout", 60)
    desc = params.get("description", cmd[:60])
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (result.stdout or "") + (result.stderr or "")
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout[:4000],
            "stderr": result.stderr[:2000],
            "description": desc,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout}s", "description": desc}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "description": desc}


def _tool_python(params: dict[str, Any]) -> dict[str, Any]:
    code = params["code"]
    try:
        py_path = sys.executable
        script = f"import json; {code}"
        result = subprocess.run(
            [py_path, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout[:4000],
            "stderr": result.stderr[:2000],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_http_get(params: dict[str, Any]) -> dict[str, Any]:
    url = params["url"]
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "30", url],
            capture_output=True,
            text=True,
            timeout=35,
        )
        return {"ok": result.returncode == 0, "url": url, "content": result.stdout[:8000]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_status(_params: dict[str, Any]) -> dict[str, Any]:
    import platform
    return {
        "ok": True,
        "cwd": str(Path.cwd()),
        "project_root": str(ROOT),
        "os": platform.system(),
        "python": sys.executable,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "env_keys": list(discover_llm_env_keys().keys()),
    }


# --- Git tools ---

def _tool_git_status(_params: dict[str, Any]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, timeout=15, cwd=str(ROOT)
        )
        return {"ok": True, "status": result.stdout[:4000] or "clean", "branch": _git_branch()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _git_branch() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=5, cwd=str(ROOT))
        return r.stdout.strip()
    except Exception:
        return "unknown"


def _tool_git_diff(params: dict[str, Any]) -> dict[str, Any]:
    file = params.get("file", "")
    staged = params.get("staged", False)
    try:
        cmd = ["git", "diff", "--stat"]
        if staged:
            cmd.append("--staged")
        if file:
            cmd.append(file)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, cwd=str(ROOT))
        return {"ok": True, "diff": result.stdout[:6000]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_git_log(params: dict[str, Any]) -> dict[str, Any]:
    count = params.get("count", 10)
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-n", str(count)],
            capture_output=True, text=True, timeout=15, cwd=str(ROOT)
        )
        return {"ok": True, "log": result.stdout[:4000]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# --- Trading state tools ---

def _tool_read_activity_log(params: dict[str, Any]) -> dict[str, Any]:
    log_path = ROOT / "data" / "activity.jsonl"
    limit = params.get("limit", 50)
    try:
        if not log_path.is_file():
            return {"ok": False, "error": "activity.jsonl not found"}
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        events = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return {"ok": True, "count": len(events), "events": events}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_read_state(_params: dict[str, Any]) -> dict[str, Any]:
    state_path = ROOT / "data" / "state.json"
    try:
        if not state_path.is_file():
            return {"ok": False, "error": "state.json not found"}
        data = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
        # Strip sensitive fields
        safe = {k: v for k, v in data.items() if k not in ("openrouter_keys", "api_key", "secret_key")}
        return {"ok": True, "state": safe}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_read_equity_history(params: dict[str, Any]) -> dict[str, Any]:
    path = ROOT / "data" / "equity_history.jsonl"
    limit = params.get("limit", 100)
    try:
        if not path.is_file():
            return {"ok": False, "error": "equity_history.jsonl not found"}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        entries = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return {"ok": True, "count": len(entries), "entries": entries}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# --- Process management ---

def _tool_list_processes(params: dict[str, Any]) -> dict[str, Any]:
    filter_str = params.get("filter", "")
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV"],
            capture_output=True, text=True, timeout=15
        )
        lines = result.stdout.strip().splitlines()
        header = lines[0] if lines else ""
        rows = lines[1:]
        if filter_str:
            rows = [r for r in rows if filter_str.lower() in r.lower()]
        return {"ok": True, "header": header, "processes": rows[:40]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_kill_process(params: dict[str, Any]) -> dict[str, Any]:
    pid = params.get("pid")
    name = params.get("name", "")
    if not pid and not name:
        return {"ok": False, "error": "pid or name required"}
    try:
        if pid:
            result = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=15
            )
        else:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True, text=True, timeout=15
            )
        return {"ok": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# --- Utility tools ---

def _tool_diff_files(params: dict[str, Any]) -> dict[str, Any]:
    path_a = Path(params["path_a"]).expanduser().resolve()
    path_b = Path(params["path_b"]).expanduser().resolve()
    try:
        text_a = path_a.read_text(encoding="utf-8", errors="replace").splitlines()
        text_b = path_b.read_text(encoding="utf-8", errors="replace").splitlines()
        import difflib
        diff = list(difflib.unified_diff(text_a, text_b, lineterm="", fromfile=str(path_a), tofile=str(path_b)))
        return {"ok": True, "diff_lines": diff[:80], "diff_text": "\n".join(diff[:80])}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _tool_tail_log(params: dict[str, Any]) -> dict[str, Any]:
    log_path = Path(params["path"]).expanduser().resolve()
    lines = params.get("lines", 50)
    try:
        if not log_path.is_file():
            return {"ok": False, "error": f"Not a file: {log_path}"}
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = all_lines[-lines:]
        return {"ok": True, "path": str(log_path), "lines": f"{max(1, len(all_lines)-lines+1)}-{len(all_lines)}", "content": "\n".join(tail)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# --- Bot health snapshot ---

def _tool_bot_health(_params: dict[str, Any]) -> dict[str, Any]:
    try:
        # Write a temp script to avoid backslash hell in inline -c
        import tempfile
        script = f"""import sys
sys.path.insert(0, r"{str(ROOT)}")
from trader.stack_control import stack_status
import json
print(json.dumps(stack_status()))
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(script)
            tmp = f.name
        result = subprocess.run(
            [sys.executable, tmp],
            capture_output=True, text=True, timeout=20, cwd=str(ROOT)
        )
        Path(tmp).unlink(missing_ok=True)
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            return {"ok": True, "health": data}
        return {"ok": False, "error": result.stderr[:500]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


TOOL_MAP = {
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "search_files": _tool_search_files,
    "list_dir": _tool_list_dir,
    "bash": _tool_bash,
    "python": _tool_python,
    "http_get": _tool_http_get,
    "status": _tool_status,
    "git_status": _tool_git_status,
    "git_diff": _tool_git_diff,
    "git_log": _tool_git_log,
    "read_activity_log": _tool_read_activity_log,
    "read_state": _tool_read_state,
    "read_equity_history": _tool_read_equity_history,
    "list_processes": _tool_list_processes,
    "kill_process": _tool_kill_process,
    "diff_files": _tool_diff_files,
    "tail_log": _tool_tail_log,
    "bot_health": _tool_bot_health,
}


# ---------------------------------------------------------------------------
# LLM Interaction
# ---------------------------------------------------------------------------

def _format_system_prompt() -> str:
    return SYSTEM_PROMPT.format(
        project_root=str(ROOT),
        python_path=sys.executable,
        cwd=str(Path.cwd()),
    )


def _call_llm(messages: list[dict[str, str]], auto: bool = False) -> dict[str, Any]:
    """Spawn a fresh LLMWrapper instance — isolated, no shared state."""
    llm = LLMWrapper(
        provider_priority=("nous",),
        pool_name="agent_cli",
        nvidia_model="stepfun/step-3.7-flash:free",
    )
    resp = llm.chat(
        messages=messages,
        system=_format_system_prompt(),
        max_tokens=3000,
    )
    return _parse_response(resp.text)


def _parse_response(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"thought": "empty response", "tool_calls": [], "final_answer": "(no response)"}
    # Strip markdown fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    # Extract JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return {"thought": "non-dict response", "tool_calls": [], "final_answer": text[:500]}
        return parsed
    except json.JSONDecodeError:
        # Try to salvage a JSON object
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {"thought": "parse failed", "tool_calls": [], "final_answer": text[:500]}


# ---------------------------------------------------------------------------
# Execution Loop
# ---------------------------------------------------------------------------

def _is_destructive(tool: str) -> bool:
    return tool in ("write_file", "edit_file", "bash", "kill_process")


def _run_tool(tool: str, params: dict[str, Any], auto: bool) -> dict[str, Any]:
    if tool not in TOOL_MAP:
        return {"ok": False, "error": f"Unknown tool: {tool}"}
    
    if _is_destructive(tool) and not auto:
        desc = params.get("description", json.dumps(params)[:120])
        confirm = input(f"  [CONFIRM] {tool}: {desc} — proceed? [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            return {"ok": False, "error": "User cancelled", "cancelled": True}
    
    result = TOOL_MAP[tool](params)
    return result


def run_turn(user_input: str, history: list[dict[str, str]], auto: bool) -> str:
    messages = history + [{"role": "user", "content": user_input}]
    
    resp = _call_llm(messages, auto=auto)
    tool_calls = resp.get("tool_calls") or []
    final = resp.get("final_answer") or ""
    
    if not tool_calls:
        return final or "(no response)"
    
    # Execute tool calls
    tool_results: list[dict[str, Any]] = []
    for tc in tool_calls:
        tool_name = tc.get("tool")
        params = tc.get("params") or {}
        print(f"  → {tool_name}({json.dumps(params)[:120]}...)")
        result = _run_tool(tool_name, params, auto)
        tool_results.append({"tool": tool_name, "result": result})
        if result.get("cancelled"):
            return "Cancelled by user."
    
    # Feed results back to LLM for final synthesis
    synthesis_prompt = (
        f"You executed {len(tool_results)} tool call(s). Here are the results:\n"
        + json.dumps(tool_results, indent=2, default=str)[:6000]
        + "\n\nProvide a concise final answer summarizing what was done and what was found."
    )
    messages.append({"role": "user", "content": synthesis_prompt})
    
    resp2 = _call_llm(messages, auto=auto)
    return resp2.get("final_answer") or resp2.get("thought") or json.dumps(tool_results, indent=2)[:500]


# ---------------------------------------------------------------------------
# History Persistence
# ---------------------------------------------------------------------------

def _load_history() -> list[dict[str, str]]:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not HISTORY_FILE.is_file():
        return []
    try:
        history: list[dict[str, str]] = []
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines()[-50:]:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("role") in ("user", "assistant"):
                    history.append({"role": entry["role"], "content": entry["content"]})
            except json.JSONDecodeError:
                continue
        return history
    except Exception:
        return []


def _save_history(history: list[dict[str, str]]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        for entry in history[-2:]:
            f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="LLM KnightTrader AI Agent CLI")
    parser.add_argument("prompt", nargs="?", help="Single-shot prompt (skip interactive)")
    parser.add_argument("--auto", action="store_true", help="Auto-confirm destructive operations (dangerous)")
    parser.add_argument("--no-history", action="store_true", help="Don't load/save conversation history")
    parser.add_argument("--version", action="store_true", help="Show version and exit")
    args = parser.parse_args()

    if args.version:
        print(f"LLM KnightTrader Agent CLI v{VERSION}")
        print(f"Project root: {ROOT}")
        print(f"Python: {sys.executable}")
        return 0

    # Verify NVIDIA key is available
    keys = discover_llm_env_keys()
    if not keys.get("NVIDIA_API_KEY"):
        print("ERROR: NVIDIA_API_KEY not found.")
        print("Set it in .env, credentials/blofin.txt, or C:\\Users\\mknig\\Documents\\Nvidia API Key.txt")
        return 1

    history: list[dict[str, str]] = [] if args.no_history else _load_history()

    # Single-shot mode
    if args.prompt:
        result = run_turn(args.prompt, history, auto=args.auto)
        print(result)
        if not args.no_history:
            history.extend([{"role": "user", "content": args.prompt}, {"role": "assistant", "content": result}])
            _save_history(history)
        return 0

    # Interactive mode
    print(textwrap.dedent(f"""\
        ╔══════════════════════════════════════════════════════════════╗
        ║  LLM KnightTrader Agent CLI v{VERSION}                        ║
        ║  Powered by NVIDIA GLM 5.1  —  Fresh instance per turn       ║
        ║  Type 'exit', 'quit', or press Ctrl+C to leave               ║
        ╚══════════════════════════════════════════════════════════════╝
    """))
    if args.auto:
        print("⚠️  AUTO-MODE: Destructive operations will run without confirmation!\n")

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            print("Goodbye.")
            break
        if user_input.lower() == "clear":
            history = []
            print("History cleared.")
            continue
        if user_input.lower() == "status":
            print(json.dumps(_tool_status({}), indent=2))
            continue

        print()
        result = run_turn(user_input, history, auto=args.auto)
        print(f"\n{result}")

        if not args.no_history:
            history.extend([
                {"role": "user", "content": user_input},
                {"role": "assistant", "content": result},
            ])
            _save_history(history)

    return 0


if __name__ == "__main__":
    sys.exit(main())
