"""Monitor paper stack until N successful opens; clear obvious wreckage lightly."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "activity.jsonl"
TARGET_OPENS = 3
POLL_SEC = 45


def _get(url: str, timeout: float = 8.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"_error": str(exc)}


def _tail_trade_events(since_ts: float) -> list[dict]:
    if not LOG.is_file():
        return []
    size = LOG.stat().st_size
    max_bytes = 2_000_000
    with LOG.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()
        text = f.read().decode("utf-8", errors="replace")
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = float(ev.get("ts") or 0)
        if ts <= since_ts:
            continue
        title = str(ev.get("title") or "")
        typ = ev.get("type")
        if typ in ("trade", "error", "system") and (
            title.startswith("Opened ")
            or title.startswith("Open ")
            or title.startswith("Opening ")
            or "Cycle error" in title
            or "Paper" in title
            or "Demo/paper" in title
            or "started" in title.lower()
            or "utf-8" in str(ev.get("detail") or "").lower()
            or "Decision:" in title
        ):
            out.append(ev)
    return out


def _successful_open(ev: dict) -> bool:
    title = str(ev.get("title") or "")
    detail = str(ev.get("detail") or "")
    if not title.startswith("Opened "):
        return False
    if '"code": "0"' in detail or '"code":"0"' in detail or "paper-" in detail:
        return True
    data = ev.get("data") or {}
    # paper often embeds ok in detail JSON
    return "Order placed" in detail


def main() -> None:
    start_ts = time.time()
    print(f"monitor start ts={start_ts}", flush=True)
    opens: list[str] = []
    last_ts = start_ts - 5
    idle_rounds = 0
    while len(opens) < TARGET_OPENS:
        health = _get("http://127.0.0.1:8765/api/health")
        status = _get("http://127.0.0.1:8765/api/status")
        if health.get("_error"):
            print(f"waiting for stack... ({health['_error'][:80]})", flush=True)
            idle_rounds += 1
            time.sleep(POLL_SEC)
            continue
        equity = (status.get("account") or {}).get("equity")
        paper = status.get("paper_trading")
        cycles = (status.get("state") or {}).get("cycles")
        print(
            f"up equity={equity} paper={paper} cycles={cycles} opens={len(opens)}/{TARGET_OPENS}",
            flush=True,
        )
        events = _tail_trade_events(last_ts)
        for ev in events:
            last_ts = max(last_ts, float(ev.get("ts") or 0))
            title = str(ev.get("title") or "")
            detail = str(ev.get("detail") or "")[:160]
            print(f"  [{ev.get('type')}] {title} | {detail}", flush=True)
            if _successful_open(ev):
                key = f"{title}|{ev.get('ts')}"
                if key not in opens:
                    opens.append(key)
                    print(f"  SUCCESS open #{len(opens)}: {title}", flush=True)
        if not events:
            idle_rounds += 1
        else:
            idle_rounds = 0
        if idle_rounds >= 8 and len(opens) == 0:
            # ~6 min of idle after up — surface blockers once
            print("NOTE: still no opens — check confidence/hold decisions above", flush=True)
            idle_rounds = 0
        time.sleep(POLL_SEC)
    print(f"DONE: {len(opens)} successful opens observed", flush=True)
    for o in opens:
        print(" ", o, flush=True)


if __name__ == "__main__":
    main()
