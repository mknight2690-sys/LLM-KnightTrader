"""Full-universe wrapper for hermes-llm-trader optimizer."""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from backtest_optimizer import (
    MAX_UNIVERSE,
    TARGET_EQUITY,
    build_search_space,
    optimize,
    prepare_asset_index,
    fetch_candles,
    save_best,
    evaluate_top_strategy_combinations,
    _build_top_combination_search_space,
    _evaluate,
    run_strategy,
)
from blofin.client import BlofinClient


def main() -> None:
    client = BlofinClient()
    instruments = client.get_instruments("SWAP")
    inst_meta = {str(i.get("instId", "")): i for i in instruments}
    usdt_swaps = sorted({
        iid
        for iid, row in inst_meta.items()
        if iid.endswith("-USDT")
        and str(row.get("state", "")).lower() in ("live", "trading", "")
    })
    print(f"Universe: {len(usdt_swaps)} USDT swap instruments")
    sample = usdt_swaps[:MAX_UNIVERSE]
    print(f"Testing full universe: {len(sample)} assets")
    asset_candles = {}
    t0 = time.time()
    for i, inst_id in enumerate(sample, start=1):
        try:
            candles = fetch_candles(client, inst_id)
            horizon_ts = candles[-1]["ts"] - 30 * 24 * 3600 * 1000 if candles else 0
            trimmed = [c for c in candles if c["ts"] >= horizon_ts] if candles else []
            if len(trimmed) >= 200:
                trimmed = trimmed[-30 * 24 if 30 * 24 < len(trimmed) else len(trimmed):]
                asset_candles[inst_id] = trimmed
        except Exception as exc:
            print(f"[{i}/{len(sample)}] {inst_id} error: {exc}")
            continue
        if i % 20 == 0 or i == len(sample):
            print(f"  fetched {i}/{len(sample)}")
    print(f"Fetch done in {time.time() - t0:.1f}s, {len(asset_candles)} assets usable")
    if not asset_candles:
        raise SystemExit("No candle data available for backtest optimization.")
    asset_idx = prepare_asset_index(asset_candles)
    result = optimize(asset_candles, asset_idx)

    # Evaluate top strategies + combinations with strict acceptance rule
    ranked_combinations = evaluate_top_strategy_combinations(asset_candles, asset_idx)
    combo_results = []
    seen: set[tuple[str, ...]] = set()
    for combo_entry in ranked_combinations:
        combination = combo_entry.combination
        if combination in seen:
            continue
        seen.add(combination)
        combo_entry_dict = _evaluate(asset_candles, asset_idx, combo_entry.params)
        combo_results.append({
            "combination": combination,
            "final_equity": combo_entry_dict.get("final_equity", 0.0),
            "score": combo_entry_dict.get("score", 0.0),
            "total_trades": combo_entry_dict.get("total_trades", 0),
            "win_rate": combo_entry_dict.get("win_rate", 0.0),
            "target_reached": float(combo_entry_dict.get("final_equity", 0.0)) >= TARGET_EQUITY,
        })
        print(f"combo={combination} equity={combo_entry_dict.get('final_equity', 0.0):.2f}")

    combo_results.sort(key=lambda x: x.get("final_equity", 0.0), reverse=True)
    accepted = [x for x in combo_results if x["target_reached"]]
    print(f"Accepted>=120 results={len(accepted)}")
    best = result.get("best") or {}
    save_best(best if isinstance(best, dict) else {})
    out = {
        "optimization": result,
        "combination_results": combo_results[:20],
        "accepted_count": len(accepted),
        "accepted": accepted[:10],
        "target_equity": TARGET_EQUITY,
        "target_reached": bool(accepted),
    }
    out_path = os.path.join(ROOT, "data", "full_universe_optimization.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"Saved full universe optimization to {out_path}")
    print(f"Target reached: {out['target_reached']}")
    best_equity = ((best.get("metrics") or {}).get("equity_curve") or [{}])[-1].get("equity", best.get("final_equity", 0.0))
    print(f"Best equity: {best_equity:.2f}")


if __name__ == "__main__":
    main()
