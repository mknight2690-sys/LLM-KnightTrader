"""One-shot: kill KnightTrader-related python processes."""
from __future__ import annotations

import json
import os
import subprocess
import sys

KEYS = (
    "hermes-llm-trader",
    "trader.agent",
    "dashboard.server",
    "stack_launcher",
    "repair_agent",
    "stack_watchdog",
    "scripts.stack_launcher",
)

ps = (
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
    "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
)
out = subprocess.check_output(
    ["powershell", "-NoProfile", "-Command", ps],
    text=True,
    timeout=30,
).strip()
if not out or out == "null":
    print("no python processes")
    sys.exit(0)
rows = json.loads(out)
if isinstance(rows, dict):
    rows = [rows]
killed: list[int] = []
for row in rows:
    pid = int(row.get("ProcessId") or 0)
    cmd = str(row.get("CommandLine") or "")
    if not pid or pid == os.getpid():
        continue
    if any(k in cmd for k in KEYS):
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        killed.append(pid)
print("killed", killed)
