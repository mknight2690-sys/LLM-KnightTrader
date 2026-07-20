"""Clear paper/demo artifacts and re-arm state for LIVE BloFin trading."""
from __future__ import annotations

import json
import time
from pathlib import Path

from config import DATA_DIR, STATE_FILE
from trader.optimized_params import best_params_context, load_best_params
from trader.state import load_state, save_state

PAPER_GLOBS = (
    "paper_account.json",
    "paper_account*.bak",
    "paper_account.corrupt.*",
)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        print(f"removed {path.name}")
    except OSError as exc:
        print(f"skip {path.name}: {exc}")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for pattern in PAPER_GLOBS:
        for path in DATA_DIR.glob(pattern):
            _unlink(path)

    for name in (
        "account_cache.json",
        "account_cache.lock",
        "equity_history.jsonl",
        "last_scan.json",
        "monitor_checkpoint.json",
    ):
        _unlink(DATA_DIR / name)

    # Truncate activity so dashboard does not show demo fills/chatters.
    activity = DATA_DIR / "activity.jsonl"
    if activity.is_file():
        activity.write_text("", encoding="utf-8")
        print("truncated activity.jsonl")

    bp = load_best_params()
    ctx = best_params_context()
    state = load_state()
    state["best_params"] = bp
    state["optimized_params_context"] = ctx
    state["trades"] = []
    state["order_guard"] = {"history": []}
    state["last_decision"] = None
    state["last_executed_decision"] = None
    state["peak_equity"] = 0.0
    state["_last_lesson_peak"] = 0.0
    state["execution_ready"] = True
    state["paper_trading"] = False
    state["live_mode"] = True
    state["live_cutover_at"] = time.time()
    # Drop paper-only pnl ledger so live PnL starts clean (keep lessons).
    if isinstance(state.get("pnl_ledger"), dict):
        state["pnl_ledger"] = {"entries": [], "opens": {}}
    save_state(state)
    print(
        json.dumps(
            {
                "ok": True,
                "mode": "live",
                "strategy": bp.get("STRATEGY"),
                "margin": bp.get("MARGIN_USE_RATIO") or bp.get("RISK_PER_TRADE"),
                "best_backtest_equity": ctx.get("final_equity"),
                "state": str(STATE_FILE),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
