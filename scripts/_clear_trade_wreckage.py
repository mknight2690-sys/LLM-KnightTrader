"""Clear demo trade wreckage that blocks fresh opens; keep lessons + best params."""
from __future__ import annotations

import json
import time

from blofin.paper_ledger import LEDGER_PATH, ensure_paper_seeded
from config import PAPER_START_EQUITY, STATE_FILE
from trader.optimized_params import best_params_context, load_best_params
from trader.state import load_state, save_state


def main() -> None:
    bp = load_best_params()
    ctx = best_params_context()
    state = load_state()
    state["best_params"] = bp
    state["optimized_params_context"] = ctx
    state["peak_equity"] = float(state.get("peak_equity") or PAPER_START_EQUITY)
    if float(state["peak_equity"]) < float(PAPER_START_EQUITY):
        state["peak_equity"] = float(PAPER_START_EQUITY)
    state["_last_lesson_peak"] = float(state["peak_equity"])
    # Wipe execution churn history / ghost opens from prior sessions.
    state["order_guard"] = {"history": []}
    state["trades"] = []
    state["last_decision"] = None
    state["last_executed_decision"] = None
    state["execution_ready"] = True
    save_state(state)

    ledger = ensure_paper_seeded(equity=PAPER_START_EQUITY)
    print(
        json.dumps(
            {
                "ok": True,
                "cash": ledger.get("cash"),
                "positions": list((ledger.get("positions") or {}).keys()),
                "strategy": bp.get("STRATEGY"),
                "margin": bp.get("MARGIN_USE_RATIO") or bp.get("RISK_PER_TRADE"),
                "peak": state["peak_equity"],
                "ctx_equity": ctx.get("final_equity"),
                "ts": time.time(),
                "ledger_path": str(LEDGER_PATH),
                "state_path": str(STATE_FILE),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
