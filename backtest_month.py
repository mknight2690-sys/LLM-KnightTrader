"""Backtest momentum-scalp strategy over Blofin USDT-swap universe.
Uses ATR-based stop/take-profit with strict trend filter and per-asset cooldown.

Philosophy:
- Start with $40.
- Trend filter: close > EMA50 + 0.5*ATR (bullish) or close < EMA50 - 0.5*ATR (bearish).
- Entry: close pricing in direction of trend, previous close also aligned.
- Exit: TP = 3x ATR, SL = 1x ATR.
- Cooldown: 24h per asset after each trade closes.
- Sizing: risk 5% of equity per trade.
- Fees: 0.12% round-trip per trade.
"""
from __future__ import annotations

import json
import os
import time as time_mod
from datetime import datetime, timezone
from typing import Any

from blofin.client import BlofinClient

ROOT = os.path.dirname(os.path.abspath(__file__))
EQUITY_START = 40.0
RISK_PER_TRADE = 0.05
RR_RATIO = 3.0
FEE_ROUND_TRIP = 0.0012
CANDLE_BAR = "1h"
CANDLE_LIMIT = "1440"
EMA_PERIOD = 50
ATR_PERIOD = 14
COOLDOWN_BARS = 12
BACKTEST_DAYS = 30
HARVEST_PCT = 0.05
MAX_UNIVERSE = 486
TREND_ATR_K = 0.45
TP_RR = 3.0
SL_RR = 1.0
MIN_ATR_PCT = 0.0008
MAX_TRADE_RISK_PCT = 0.5
STRICT_EXIT = True


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    k = 2.0 / (period + 1)
    prev = None
    for v in values:
        if prev is None:
            prev = sum(values[:period]) / period if len(values) >= period else v
            out.append(prev if len(values) >= period else None)
        else:
            prev = v * k + prev * (1 - k)
            out.append(prev)
    return out


def atr(candles: list[dict[str, float]], period: int) -> list[float | None]:
    trs: list[float] = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    out: list[float | None] = [None] * len(candles)
    if len(trs) >= period:
        first = sum(trs[:period]) / period
        out[period] = first
        k = 1.0 / period
        prev = first
        for i in range(period + 1, len(candles)):
            prev = trs[i - 1] * k + prev * (1 - k)
            out[i] = prev
    return out


def fetch_candles(client: BlofinClient, inst_id: str) -> list[dict[str, float]]:
    resp = client.request(
        "GET",
        "/api/v1/market/candles",
        params={"instId": inst_id, "bar": CANDLE_BAR, "limit": CANDLE_LIMIT},
    )
    if resp.get("code") != "0":
        return []
    return [
        {
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
        }
        for r in (resp.get("data") or [])
    ]


def run_backtest() -> dict[str, Any]:
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
    print(f"Testing sample: {len(sample)} assets")

    asset_candles: dict[str, list[dict[str, float]]] = {}
    t0 = time_mod.time()
    for i, inst_id in enumerate(sample):
        try:
            candles = fetch_candles(client, inst_id)
            horizon_ts = candles[-1]["ts"] - BACKTEST_DAYS * 24 * 3600 * 1000 if candles else 0
            trimmed = [c for c in candles if c["ts"] >= horizon_ts] if candles else []
            if len(trimmed) >= EMA_PERIOD + ATR_PERIOD + 5:
                asset_candles[inst_id] = trimmed
        except Exception as exc:
            print(f"[{i+1}/{len(sample)}] {inst_id} error: {exc}")
            continue
        if (i + 1) % 10 == 0:
            print(f"  fetched {i+1}/{len(sample)}")
    print(f"Fetch done in {time_mod.time() - t0:.1f}s, {len(asset_candles)} assets usable")

    if not asset_candles:
        return {"error": "no candle data"}

    all_ts = sorted({c["ts"] for cs in asset_candles.values() for c in cs})
    print(f"Window: {datetime.fromtimestamp(all_ts[0]/1000, tz=timezone.utc).isoformat()} -> {datetime.fromtimestamp(all_ts[-1]/1000, tz=timezone.utc).isoformat()}")

    asset_idx: dict[str, dict[str, Any]] = {}
    for inst_id, candles in asset_candles.items():
        closes = [c["close"] for c in candles]
        ema_vals = ema(closes, EMA_PERIOD)
        atr_vals = atr(candles, ATR_PERIOD)
        asset_idx[inst_id] = {
            "closes": closes,
            "ema": ema_vals,
            "atr": atr_vals,
            "idx": {c["ts"]: i for i, c in enumerate(candles)},
        }

    def _contract_value(inst_id: str) -> float:
        row = inst_meta.get(inst_id, {})
        try:
            return float(row.get("contractValue", 1))
        except (TypeError, ValueError):
            return 1.0

    equity = EQUITY_START
    trades = 0
    wins = 0
    losses = 0
    fee_paid = 0.0
    max_equity = equity
    min_equity = equity
    cooldown: dict[str, int] = {}
    open_trades: dict[str, dict[str, Any]] = {}
    equity_curve: list[dict[str, Any]] = []

    for ts in all_ts:
        # Exits
        for inst_id, trade in list(open_trades.items()):
            idx = asset_idx.get(inst_id, {}).get("idx", {}).get(ts)
            if idx is None:
                continue
            candle = asset_candles[inst_id][idx]
            exit_price = None
            entry = trade["entry"]
            pnl_pct_to_date = 0.0
            if trade["side"] == "long":
                pnl_pct_to_date = (candle["high"] - entry) / entry
                if candle["low"] <= trade["sl"]:
                    exit_price = trade["sl"]
                elif pnl_pct_to_date >= HARVEST_PCT:
                    exit_price = entry * (1 + HARVEST_PCT)
            else:
                pnl_pct_to_date = (entry - candle["low"]) / entry
                if candle["high"] >= trade["sl"]:
                    exit_price = trade["sl"]
                elif pnl_pct_to_date >= HARVEST_PCT:
                    exit_price = entry * (1 - HARVEST_PCT)
            if exit_price is not None:
                cval = trade["contract_value"]
                pnl = trade["size"] * (exit_price - trade["entry"]) * cval if trade["side"] == "long" else trade["size"] * (trade["entry"] - exit_price) * cval
                pnl -= trade["notional"] * FEE_ROUND_TRIP
                equity += pnl
                fee_paid += trade["notional"] * FEE_ROUND_TRIP
                trades += 1
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                cooldown[inst_id] = idx + COOLDOWN_BARS
                open_trades.pop(inst_id, None)

        equity = max(0.01, equity)
        if equity > max_equity:
            max_equity = equity
        if equity < min_equity:
            min_equity = equity

        if len(equity_curve) == 0 or ts - equity_curve[-1]["ts"] >= 6 * 3600 * 1000:
            equity_curve.append({"ts": ts, "equity": equity})

        # Entries
        for inst_id, candles in asset_candles.items():
            if inst_id in open_trades:
                continue
            idx_map = asset_idx.get(inst_id, {}).get("idx", {})
            idx = idx_map.get(ts)
            if idx is None or idx < EMA_PERIOD + ATR_PERIOD + 2:
                continue
            if cooldown.get(inst_id, -1) >= idx:
                continue
            meta = asset_idx[inst_id]
            ema_val = meta["ema"][idx]
            atr_val = meta["atr"][idx]
            prev_close = meta["closes"][idx - 1]
            cur_close = candles[idx]["close"]
            if ema_val is None or atr_val is None or atr_val <= 0:
                continue

            # Strict trend filter: close must be clearly above/below EMA + ATR
            above_trend = cur_close > ema_val + TREND_ATR_K * atr_val and prev_close > ema_val - TREND_ATR_K * atr_val
            below_trend = cur_close < ema_val - TREND_ATR_K * atr_val and prev_close < ema_val + TREND_ATR_K * atr_val
            atr_quality = (atr_val / max(cur_close, 1e-9)) >= MIN_ATR_PCT
            if above_trend and atr_quality:
                side = "long"
                entry_price = candles[idx]["open"]
                sl = entry_price - SL_RR * atr_val
                tp = entry_price + TP_RR * atr_val
            elif below_trend and atr_quality:
                side = "short"
                entry_price = candles[idx]["open"]
                sl = entry_price + SL_RR * atr_val
                tp = entry_price - TP_RR * atr_val
            else:
                continue

            price_risk = abs(entry_price - sl)
            if price_risk <= 0:
                continue
            cval = _contract_value(inst_id)
            risk_usd = equity * RISK_PER_TRADE
            size = risk_usd / (price_risk * cval)
            notional = size * entry_price * cval
            fee_entry = notional * FEE_ROUND_TRIP
            if fee_entry >= risk_usd * 0.5 or notional > equity * MAX_TRADE_RISK_PCT:
                continue
            open_trades[inst_id] = {
                "side": side,
                "entry": entry_price,
                "sl": sl,
                "tp": tp,
                "size": size,
                "notional": notional,
                "contract_value": cval,
                "start_idx": idx,
            }

    for inst_id, trade in list(open_trades.items()):
        candles = asset_candles.get(inst_id, [])
        if not candles:
            continue
        last = candles[-1]
        cval = trade["contract_value"]
        entry = trade["entry"]
        side = trade["side"]
        if side == "long":
            if last["high"] >= trade["tp"]:
                exit_price = trade["tp"]
            elif last["low"] <= trade["sl"]:
                exit_price = trade["sl"]
            elif STRICT_EXIT:
                exit_price = last["close"]
            else:
                continue
        else:
            if last["low"] <= trade["tp"]:
                exit_price = trade["tp"]
            elif last["high"] >= trade["sl"]:
                exit_price = trade["sl"]
            elif STRICT_EXIT:
                exit_price = last["close"]
            else:
                continue
        pnl = trade["size"] * (exit_price - entry) * cval if side == "long" else trade["size"] * (entry - exit_price) * cval
        pnl -= trade["notional"] * FEE_ROUND_TRIP
        equity += pnl
        fee_paid += trade["notional"] * FEE_ROUND_TRIP
        trades += 1
        if pnl > 0:
            wins += 1
        else:
            losses += 1

    equity_curve.append({"ts": all_ts[-1], "equity": equity})
    win_rate = (wins / trades) if trades else 0.0
    expectancy = ((win_rate * RR_RATIO) - ((1 - win_rate) * 1.0)) if trades else 0.0

    print(f"\n=== Backtest Result ===")
    print(f"Initial equity: ${EQUITY_START:.2f}")
    print(f"Final equity:   ${equity:.2f}")
    print(f"Total trades:   {trades}")
    print(f"Wins/Losses:    {wins}/{losses}")
    print(f"Win rate:       {win_rate*100:.1f}%")
    print(f"Expectancy (R): {expectancy:.2f}")
    print(f"Fees paid:      ${fee_paid:.2f}")
    print(f"Max equity:     ${max_equity:.2f}")
    print(f"Min equity:     ${min_equity:.2f}")

    result = {
        "initial_equity": EQUITY_START,
        "final_equity": round(equity, 2),
        "total_trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "expectancy_r": round(expectancy, 4),
        "fees_paid": round(fee_paid, 2),
        "max_equity": round(max_equity, 2),
        "min_equity": round(min_equity, 2),
        "assets_tested": len(asset_candles),
        "equity_curve": equity_curve[-100:],
    }
    out_path = os.path.join(ROOT, "data", "backtest_month_result.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"Saved to {out_path}")
    return result


if __name__ == "__main__":
    run_backtest()
