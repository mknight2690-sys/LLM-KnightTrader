"""Periodic dashboard + log health check for babysitting."""

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
OUT = ROOT / "data" / "babysit_12m.jsonl"
HOST = "127.0.0.1"
PORT = 8765
PYTHON = sys.executable
START = ROOT / "scripts" / "restart_services.ps1"


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


TARGET_POSITIONS = 3


def _activity_fresh(max_age_sec: float = 180.0) -> bool:
    if not LOG.is_file():
        return False
    return time.time() - LOG.stat().st_mtime <= max_age_sec


def _positions_complete(acct: dict) -> bool:
    positions = acct.get("positions") or []
    if len(positions) < TARGET_POSITIONS:
        return False
    for pos in positions:
        if float(pos.get("entry") or 0) <= 0:
            return False
        if float(pos.get("mark") or 0) <= 0:
            return False
        if pos.get("upl") is None:
            return False
        lev = pos.get("leverage")
        if lev in (None, "", "?"):
            return False
    return True


def _activity_api_ok(activity: dict | None, max_age_sec: float = 180.0) -> bool:
    events = (activity or {}).get("events") or []
    if not events:
        return False
    last = events[-1]
    ts = float(last.get("ts") or 0)
    if ts <= 0:
        return False
    return time.time() - ts <= max_age_sec


def _fix_account_cache() -> list[str]:
    from blofin.account_cache import bootstrap_account_cache, read_account_cached

    fixes: list[str] = []
    bootstrap_account_cache()
    acct = read_account_cached()
    if float(acct.get("equity") or 0) > 0 or acct.get("positions"):
        fixes.append("cache_bootstrapped")
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


def _wait_dashboard(retries: int = 6, delay: float = 3.0) -> bool:
    for _ in range(retries):
        if _http_json("/api/health"):
            return True
        time.sleep(delay)
    return False


def _wait_ready(max_sec: float = 30.0) -> None:
    """Let dashboard + activity buffer settle after stack (re)start."""
    deadline = time.time() + max_sec
    while time.time() < deadline:
        health = _http_json("/api/health")
        activity = _http_json("/api/activity?limit=5")
        account = _http_json("/api/account")
        acct = (account or {}).get("account") or {}
        if (
            health
            and _positions_complete(acct)
            and _activity_api_ok(activity)
        ):
            return
        time.sleep(2)


def run_pass(minute: int, since_ts: float) -> dict:
    fixes: list[str] = []
    health = _http_json("/api/health")
    account = _http_json("/api/account")
    activity = _http_json("/api/activity?limit=5")
    acct = (account or {}).get("account") or {}
    events = (activity or {}).get("events") or []

    if not health:
        fixes.append(_restart_stack())
        health = _wait_dashboard()
        account = _http_json("/api/account")
        activity = _http_json("/api/activity?limit=5")
        acct = (account or {}).get("account") or {}
        events = (activity or {}).get("events") or []

    pos_count = len(acct.get("positions") or [])
    if health and (
        (float(acct.get("equity") or 0) <= 0 and pos_count == 0)
        or pos_count < TARGET_POSITIONS
        or not _positions_complete(acct)
    ):
        fixes.extend(_fix_account_cache())
        account = _http_json("/api/account")
        acct = (account or {}).get("account") or {}
        pos_count = len(acct.get("positions") or [])

    errors = _tail_errors(since_ts)
    last_event = events[-1] if events else {}
    positions_ok = pos_count >= TARGET_POSITIONS and _positions_complete(acct)
    disk_fresh = _activity_fresh()
    api_fresh = _activity_api_ok(activity)
    all_ok = bool(health) and positions_ok and disk_fresh and api_fresh
    return {
        "minute": minute,
        "ts": time.time(),
        "dashboard_up": bool(health),
        "equity": acct.get("equity"),
        "positions": pos_count,
        "positions_ok": positions_ok,
        "position_ids": [p.get("instId") for p in acct.get("positions") or []],
        "activity_fresh": disk_fresh,
        "activity_api_ok": api_fresh,
        "all_ok": all_ok,
        "last_event": (last_event.get("type"), last_event.get("title")),
        "stale": acct.get("stale"),
        "rate_limited": acct.get("rate_limited"),
        "new_errors": len(errors),
        "error_titles": [e.get("title") for e in errors[:5]],
        "fixes": fixes,
    }


def main() -> None:
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    since = time.time()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    run_id = int(since)
    passes: list[dict] = []
    with OUT.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps({"run_id": run_id, "event": "start", "minutes": minutes}) + "\n")
        fp.flush()
        _wait_ready()
        i = 0
        while minutes <= 0 or i < minutes:
            if i:
                time.sleep(60)
            now = time.time()
            summary = run_pass(i + 1, since)
            summary["run_id"] = run_id
            since = now
            passes.append(summary)
            fp.write(json.dumps(summary) + "\n")
            fp.flush()
            print(json.dumps(summary), flush=True)
            i += 1
        ok_count = sum(1 for p in passes if p.get("all_ok"))
        final = {
            "run_id": run_id,
            "event": "complete",
            "minutes": minutes,
            "passes": len(passes),
            "all_ok_count": ok_count,
            "all_ok": ok_count == len(passes),
        }
        fp.write(json.dumps(final) + "\n")
        fp.flush()
        print(json.dumps(final), flush=True)


if __name__ == "__main__":
    main()
