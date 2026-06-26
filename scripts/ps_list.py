"""List/kill hermes-llm-trader python processes."""
from __future__ import annotations

import subprocess
import sys


def main() -> None:
    kill = "--kill" in sys.argv
    out = subprocess.check_output(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*hermes-llm-trader*' } | "
            "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress",
        ],
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).strip()
    if not out:
        print("none")
        return
    print(out)
    if kill:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                r"C:\Users\mknig\hermes-llm-trader\scripts\kill_all.ps1",
            ],
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


if __name__ == "__main__":
    main()
