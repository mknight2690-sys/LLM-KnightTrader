"""Auto-triage activity log issues during a watch session."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "activity.jsonl"
REPORT = ROOT / "data" / "watch_session.jsonl"
PYTHON = sys.executable


def tail_since(since_ts: float) -> list[dict]:
    if not LOG.is_file():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if float(ev.get("ts") or 0) > since_ts:
            out.append(ev)
    return out


def triage(batch: list[dict]) -> list[str]:
    actions: list[str] = []
    titles = " ".join((e.get("title") or "") for e in batch)
    if "Expanded USDT scan" in titles:
        actions.append("zombie_traders")
    if any("Equity $0.0000" in (e.get("title") or "") for e in batch if e.get("type") == "account"):
        actions.append("rehydrate_account")
    if any("content-safety" in (e.get("title") or "") for e in batch):
        actions.append("llm_safety_skip")
    return actions


def apply(action: str) -> None:
    if action == "zombie_traders":
        subprocess.run([PYTHON, str(ROOT / "scripts" / "kill_agents.py")], check=False)
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "restart_services.ps1")],
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    if action == "rehydrate_account":
        subprocess.run([PYTHON, "-c", f"import sys; sys.path.insert(0, r'{ROOT}'); from blofin.account_cache import bootstrap_account_cache; bootstrap_account_cache()"], check=False)


def main() -> None:
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    since = time.time()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("a", encoding="utf-8") as fp:
        for i in range(minutes):
            time.sleep(60)
            now = time.time()
            batch = tail_since(since)
            since = now
            errors = [e for e in batch if e.get("type") == "error"]
            fixes = []
            for act in triage(batch):
                apply(act)
                fixes.append(act)
            summary = {
                "minute": i + 1,
                "ts": now,
                "events": len(batch),
                "errors": len(errors),
                "error_titles": [e.get("title") for e in errors[-8:]],
                "fixes": fixes,
                "sample": [e.get("title") for e in batch[-5:]],
            }
            fp.write(json.dumps(summary) + "\n")
            fp.flush()
            print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
