"""Load optimized backtest parameters for live trading.

This module intentionally prefers manual `data/optimized_params.json` injection
over automatic retraining loops, because live parameter changes are high-impact.
"""

from __future__ import annotations

import json
import os
from typing import Any

_OPT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "optimized_params.json")


def load_best_params() -> dict[str, Any]:
    try:
        with open(os.path.abspath(_OPT_PATH), "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload.get("best_params") or {}
    except (OSError, json.JSONDecodeError):
        return {}


def best_params_context() -> dict[str, Any]:
    try:
        with open(os.path.abspath(_OPT_PATH), "r", encoding="utf-8") as f:
            payload = json.load(f)
        metrics = payload.get("metrics") or {}
        return {
            "loaded": bool(payload.get("best_params")),
            "strategy": (payload.get("best_params") or {}).get("STRATEGY"),
            "final_equity": metrics.get("final_equity"),
            "win_rate": metrics.get("win_rate"),
            "total_trades": metrics.get("total_trades"),
            "timestamp": payload.get("timestamp"),
        }
    except (OSError, json.JSONDecodeError):
        return {"loaded": False}
