"""Desktop launcher — delegates to stack_operator for cold start/stop."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DASHBOARD_HOST, DASHBOARD_PORT
from trader.stack_operator import cold_start_stack, cold_stop_stack

DASHBOARD_URL = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"


def stop_stack() -> int:
    print("Stopping all LLM KnightTrader processes...")
    result = cold_stop_stack()
    killed = result.get("killed_pids") or []
    if killed:
        print(f"  stopped {len(killed)} process(es)")

    counts = result.get("counts") or {}
    if result.get("ok"):
        print("LLM KnightTrader stopped (dashboard, trader, monitor, watchers).")
        return 0

    print(
        "ERROR: some processes still running — "
        f"dashboard={counts.get('dashboard', 0)} trader={counts.get('trader', 0)} "
        f"monitor={counts.get('monitor', 0)} watchers={counts.get('watchers', 0)}"
    )
    return 1


def start_stack(*, open_browser: bool = False) -> int:
    """Stop everything, then start exactly one dashboard + one trader."""
    print("Starting LLM KnightTrader stack...")
    result = cold_start_stack(open_browser=open_browser)
    if not result.get("ok"):
        print(f"ERROR: {result.get('phase', 'start')} — {result.get('error', 'unknown')}")
        counts = result.get("counts")
        if counts:
            print(f"  counts: {counts}")
        return 1

    print(f"Dashboard PID {result.get('dashboard_pid')} | Trader PID {result.get('trader_pid')}")
    print(f"LLM KnightTrader ready -> {DASHBOARD_URL}")
    print("Daily use: double-click 'Start LLM KnightTrader' on desktop to restart the stack.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM KnightTrader desktop stack control")
    sub = parser.add_subparsers(dest="command", required=True)

    start_p = sub.add_parser("start", help="Stop all KT processes, then start dashboard + trader")
    start_p.add_argument("--open-browser", action="store_true")

    sub.add_parser("stop", help="Stop all KT processes (dashboard, trader, monitor, watchers)")

    args = parser.parse_args()
    if args.command == "stop":
        return stop_stack()
    return start_stack(open_browser=bool(args.open_browser))


if __name__ == "__main__":
    raise SystemExit(main())
