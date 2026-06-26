import subprocess
import os

for pid in [8044, 9776, 10164, 10348, 10400, 11056, 13360, 15256]:
    try:
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command',
             f'Get-CimInstance Win32_Process -Filter "ProcessId={pid}" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty CommandLine'],
            stderr=subprocess.DEVNULL, text=True, timeout=5,
        ).strip()
        if out:
            print(f'{pid}: {out[:150]}')
        else:
            print(f'{pid}: dead')
    except Exception as e:
        print(f'{pid}: error {e}')
