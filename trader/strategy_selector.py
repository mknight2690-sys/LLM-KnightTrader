"""Evaluate multiple strategy mixes and select the best combination for live trading.

This module loads backtest optimization results, mixes top strategies, and selects
the best config for manual review before live application.
"""

from __future__ import annotations

import json
import os
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPTIMIZED_PARAMS_PATH = os.path.join(ROOT, "data", "optimized_params.json")
SELECTED_PARAMS_PATH = os.path.join(ROOT, "data", "selected_params.json")


def load_optimized_results() -> dict[str, Any]:
    try:
        with open(OPTIMIZED_PARAMS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _score(result: dict[str, Any]) -> float:
    return float(result.get("score") or 0.0)


def _risk_score(result: dict[str, Any]) -> float:
    max_equity = float(result.get("max_equity") or 0.0)
    min_equity = float(result.get("min_equity") or 0.0)
    trades = int(result.get("total_trades") or 0)
    if trades <= 0 or max_equity <= 0.0:
        return -1.0
    drawdown = max(0.0, (max_equity - min_equity) / max_equity)
    win_rate = float(result.get("win_rate") or 0.0)
    expectancy = float(result.get("expectancy_r") or 0.0)
    return win_rate * 0.45 + max(0.0, expectancy) * 0.35 - drawdown * 0.2


def _normalize(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        params = result.get("params") or {}
        strategy = str(params.get("STRATEGY") or "")
        key = json.dumps(params, sort_keys=True, default=str)
        if not strategy or key in seen:
            continue
        seen.add(key)
        copy = dict(result)
        copy["params"] = params
        copy["score"] = _score(result)
        copy["risk_score"] = _risk_score(result)
        normalized.append(copy)
    return normalized


def select_best(results: list[dict[str, Any]], top_k: int = 5) -> dict[str, Any]:
    cleaned = _normalize(results)
    cleaned.sort(key=lambda x: (x.get("risk_score", -1), x.get("score", -1)), reverse=True)
    top = cleaned[:top_k]
    return {
        "selected": top,
        "best": top[0] if top else None,
        "count": len(top),
    }


def save_selected(selection: dict[str, Any]) -> None:
    payload = {
        "selected": selection.get("selected") or [],
        "best": selection.get("best"),
        "count": selection.get("count", 0),
        "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    os.makedirs(os.path.dirname(SELECTED_PARAMS_PATH), exist_ok=True)
    with open(SELECTED_PARAMS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def best_for_live() -> dict[str, Any] | None:
    selection = select_best(load_optimized_results().get("results") or [])
    save_selected(selection)
    return selection.get("best")


if __name__ == "__main__":
    payload = load_optimized_results()
    results = payload.get("results") or []
    if not results:
        print("No optimization results available yet.")
        raise SystemExit(0)
    selection = select_best(results)
    save_selected(selection)
    print(f"Selected {selection['count']} strategies.")
    if selection.get("best"):
        best = selection["best"]
        print(f"Best: {best.get('params', {}).get('STRATEGY')} | score={best.get('score')} | risk_score={best.get('risk_score')}")
