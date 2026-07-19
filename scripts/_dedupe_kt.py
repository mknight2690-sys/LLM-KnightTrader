"""Kill duplicate dashboard/trader processes; keep newest of each."""
from __future__ import annotations

import json
import subprocess
import sys


def main() -> None:
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", ps],
        text=True,
        timeout=30,
    ).strip()
    rows = json.loads(out) if out and out != "null" else []
    if isinstance(rows, dict):
        rows = [rows]
    traders: list[int] = []
    dashes: list[int] = []
    for r in rows:
        cmd = str(r.get("CommandLine") or "")
        pid = int(r.get("ProcessId") or 0)
        if not pid:
            continue
        if "trader.agent" in cmd and "repair_agent" not in cmd and "repair_agents" not in cmd:
            traders.append(pid)
        if "dashboard.server" in cmd:
            dashes.append(pid)
    print("traders", traders)
    print("dashboards", dashes)
    keep_t = max(traders) if traders else None
    keep_d = max(dashes) if dashes else None
    killed: list[tuple[str, int]] = []
    for pid in traders:
        if keep_t is not None and pid != keep_t:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            killed.append(("trader", pid))
    for pid in dashes:
        if keep_d is not None and pid != keep_d:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            killed.append(("dash", pid))
    print("kept_dashboard", keep_d, "kept_trader", keep_t, "killed", killed)


if __name__ == "__main__":
    main()
