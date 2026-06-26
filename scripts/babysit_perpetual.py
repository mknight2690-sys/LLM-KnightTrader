"""Perpetual minute-by-minute stack + dashboard babysitter."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG = ROOT / "data" / "activity.jsonl"
OUT = ROOT / "data" / "babysit_perpetual.jsonl"
HOST = "127.0.0.1"
PORT = 8765
PYTHON = sys.executable
START = ROOT / "scripts" / "restart_services.ps1"
TARGET_POSITIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def _http_json(path: str, timeout: float = 8.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}{path}", timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _restart_stack() -> str:
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(START)],
        cwd=str(ROOT),
        timeout=120,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return "restarted_services"


def _fix_account_cache() -> list[str]:
    from blofin.account_cache import bootstrap_account_cache, get_account_snapshot, is_rate_limited, read_account_cached

    fixes: list[str] = []
    bootstrap_account_cache()
    acct = read_account_cached()
    if float(acct.get("equity") or 0) > 0 or acct.get("positions"):
        fixes.append("cache_bootstrapped")

    if not is_rate_limited():
        live = get_account_snapshot(force=True)
        if live.get("ok") and len(live.get("positions") or []) >= len(acct.get("positions") or []):
            fixes.append("live_refresh")
    return fixes


def _tail_errors(since_ts: float) -> list[dict]:
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
        if float(ev.get("ts") or 0) > since_ts and ev.get("type") == "error":
            out.append(ev)
    return out


def run_pass(minute: int, since_ts: float) -> dict:
    fixes: list[str] = []
    health = _http_json("/api/health")
    account = _http_json("/api/account")
    acct = (account or {}).get("account") or {}

    if not health:
        fixes.append(_restart_stack())
        time.sleep(10)
        health = _http_json("/api/health")
        account = _http_json("/api/account")
        acct = (account or {}).get("account") or {}

    pos_count = len(acct.get("positions") or [])
    if health and (float(acct.get("equity") or 0) <= 0 and pos_count == 0):
        fixes.extend(_fix_account_cache())
        account = _http_json("/api/account")
        acct = (account or {}).get("account") or {}
        pos_count = len(acct.get("positions") or [])

    elif health and pos_count < TARGET_POSITIONS:
        fixes.extend(_fix_account_cache())
        account = _http_json("/api/account")
        acct = (account or {}).get("account") or {}
        pos_count = len(acct.get("positions") or [])

    errors = _tail_errors(since_ts)
    position_ids = [p.get("instId") for p in acct.get("positions") or []]
    return {
        "minute": minute,
        "ts": time.time(),
        "dashboard_up": bool(health),
        "equity": acct.get("equity"),
        "positions": pos_count,
        "position_ids": position_ids,
        "target_positions": TARGET_POSITIONS,
        "positions_ok": pos_count >= TARGET_POSITIONS,
        "stale": acct.get("stale"),
        "rate_limited": acct.get("rate_limited"),
        "from_trades": acct.get("from_trades"),
        "new_errors": len(errors),
        "error_titles": [e.get("title") for e in errors[:5]],
        "fixes": fixes,
    }


def main() -> None:
    since = time.time()
    minute = 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    print(f"babysit_perpetual started target_positions={TARGET_POSITIONS}", flush=True)
    with OUT.open("a", encoding="utf-8") as fp:
        while True:
            if minute:
                time.sleep(60)
            minute += 1
            now = time.time()
            summary = run_pass(minute, since)
            since = now
            fp.write(json.dumps(summary) + "\n")
            fp.flush()
            print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
