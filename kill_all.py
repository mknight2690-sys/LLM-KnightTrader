import subprocess
import os
import sys

# Kill all python processes related to knighttrader
if sys.platform == "win32":
    try:
        cmd = ("Get-CimInstance Win32_Process -Filter \"(Name='python.exe' OR Name='pythonw.exe') "
               "AND CommandLine LIKE '%llm-knighttrader%'\" | "
               "Select-Object ProcessId | ConvertTo-Json -Compress")
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command', cmd],
            stderr=subprocess.DEVNULL, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).strip()
        if out and out != "null":
            import json
            rows = json.loads(out)
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                pid = int(row.get("ProcessId", 0))
                if pid and pid != os.getpid():
                    subprocess.Popen(
                        ['taskkill', '/PID', str(pid), '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    print(f"Killed {pid}")
    except Exception as e:
        print(f"Error: {e}")
