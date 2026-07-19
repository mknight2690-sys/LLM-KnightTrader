"""Iterative universe backtest optimizer for Blofin USDT-swap.

Target: starting from $40 and hold/trade for roughly one month with
realistic fees and slippage across the Blofin USDT swap universe.
Steps:
1. Build a much larger parameter search space.
2. Run walk-forward backtests for every candidate config.
3. Keep elite performers each iteration and mutate/crossover them.
4. Repeat until either the target is hit or the iteration budget is exhausted.
"""

from __future__ import annotations

import json
import os
import random
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from blofin.client import BlofinClient

ROOT = os.path.dirname(os.path.abspath(__file__))
EQUITY_START = 40.0
CANDLE_BAR = "1h"
CANDLE_LIMIT = "1440"
FEE_ROUND_TRIP = 0.0012
SLIPPAGE = 0.0005
MAX_UNIVERSE = 486
BACKTEST_DAYS = 30
TARGET_EQUITY = 120.0
MAX_ITERATIONS = 6
POPULATION_SIZE = 200
ELITE_COUNT = 25
MUTATION_RATE = 0.25
FOLDS = 3
OPTIMIZED_PARAMS_PATH = os.path.join(ROOT, "data", "optimized_params.json")


def fetch_candles(client: BlofinClient, inst_id: str) -> list[dict[str, float]]:
    try:
        resp = client.request(
            "GET",
            "/api/v1/market/candles",
            params={"instId": inst_id, "bar": CANDLE_BAR, "limit": CANDLE_LIMIT},
        )
        if not isinstance(resp, dict):
            print(f"[data] skipped {inst_id}: non-json payload type={type(resp).__name__}")
            return []
        if resp.get("code") != "0":
            print(f"[data] skipped {inst_id}: non-0 code={resp.get('code')} msg={str(resp.get('msg', ''))[:120]}")
            return []
        rows = resp.get("data") or []
        if not rows:
            print(f"[data] skipped {inst_id}: empty candles")
            return []
        candles: list[dict[str, float]] = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            try:
                candles.append({
                    "ts": int(r[0]),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                })
            except (TypeError, ValueError):
                continue
        if not candles:
            print(f"[data] skipped {inst_id}: no parseable rows")
            return []
        oldest = min(c["ts"] for c in candles)
        newest = max(c["ts"] for c in candles)
        print(f"[data] {inst_id} candles={len(candles)} bar={CANDLE_BAR} oldest={oldest} newest={newest}")
        return candles
    except Exception as exc:  # noqa: BLE001
        print(f"[data] skipped {inst_id}: fetch error={exc}")
        return []


def get_universe(client: BlofinClient) -> list[str]:
    return client.list_usdt_swap_ids(limit=MAX_UNIVERSE)


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    k = 2.0 / (period + 1)
    prev = None
    seed: list[float] = []
    for i, v in enumerate(values):
        if v is None:
            out.append(None)
            continue
        if len(seed) < period:
            seed.append(v)
            out.append(None)
            continue
        if prev is None:
            prev = sum(seed) / period
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def atr(candles: list[dict[str, float]], period: int = 14) -> list[float | None]:
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


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / avg_loss if avg_loss != 0 else 100.0
    out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, len(closes)):
        gain = max(closes[i] - closes[i - 1], 0.0)
        loss = max(closes[i - 1] - closes[i], 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 100.0
        out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def bollinger_bands(
    closes: list[float], period: int = 20, k: float = 2.0
) -> tuple[list[float | None], list[float | None]]:
    if len(closes) < period:
        return [None] * len(closes), [None] * len(closes)
    mid: list[float | None] = []
    std: list[float | None] = []
    for i in range(len(closes)):
        if i < period - 1:
            mid.append(None)
            std.append(None)
            continue
        window = closes[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        mid.append(mean)
        std.append(variance ** 0.5)
    return mid, std


def prepare_asset_index(asset_candles: dict[str, list[dict[str, float]]]) -> dict[str, dict[str, Any]]:
    asset_idx: dict[str, dict[str, Any]] = {}
    for inst_id, candles in asset_candles.items():
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        volumes = [c["volume"] for c in candles]
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        ema200 = ema(closes, 200)
        ema100 = ema(closes, 100)
        boll_mid, boll_std = bollinger_bands(closes, 20, 2.0)
        asset_idx[inst_id] = {
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "volumes": volumes,
            "ema20": ema20,
            "ema50": ema50,
            "ema100": ema100,
            "ema200": ema200,
            "atr": atr(candles, 14),
            "rsi": rsi(closes, 14),
            "boll_mid": boll_mid,
            "boll_std": boll_std,
            "idx": {c["ts"]: i for i, c in enumerate(candles)},
        }
    return asset_idx


def _strategy_from_params(params: dict[str, Any]) -> Callable[..., bool]:
    name = params["STRATEGY"]
    if name == "ema_atr":
        return strategy_ema_atr
    if name == "rsi":
        return strategy_rsi
    if name == "bollinger":
        return strategy_bollinger
    if name == "momentum":
        return strategy_momentum
    if name == "mean_reversion":
        return strategy_mean_reversion
    if name == "vwap_breakout":
        return strategy_vwap_breakout
    if name == "ensemble_long":
        return strategy_ensemble_long
    raise KeyError(f"Unsupported strategy: {name}")


def strategy_ema_atr(inst_id: str, idx: int, meta: dict[str, Any], candle: dict[str, float], params: dict[str, Any]) -> bool:
    if idx < max(params.get("EMA_PERIOD", 50), params.get("ATR_PERIOD", 14)) + 1:
        return False
    ema_period = params.get("EMA_PERIOD", 50)
    ema_key = {10: "ema20", 20: "ema20", 50: "ema50", 100: "ema100", 200: "ema200"}.get(ema_period)
    if ema_key is None:
        return False
    ema_val = meta[ema_key][idx]
    atr_val = meta["atr"][idx]
    prev_close = meta["closes"][idx - 1]
    cur_close = candle["close"]
    if ema_val is None or atr_val is None or atr_val <= 0:
        return False
    atr_quality = (atr_val / max(cur_close, 1e-9)) >= params.get("MIN_ATR_PCT", 0.001)
    trend_k = params.get("TREND_ATR_K", 0.35)
    if params.get("DIRECTION", "long") == "long":
        trend = cur_close > ema_val + trend_k * atr_val and prev_close >= min(ema_val, cur_close)
    else:
        trend = cur_close < ema_val - trend_k * atr_val and prev_close <= max(ema_val, cur_close)
    return bool(trend and atr_quality)


def strategy_rsi(inst_id: str, idx: int, meta: dict[str, Any], candle: dict[str, float], params: dict[str, Any]) -> bool:
    if idx < 15:
        return False
    rsi_val = meta["rsi"][idx]
    prev_rsi = meta["rsi"][idx - 1]
    if rsi_val is None or prev_rsi is None:
        return False
    direction = params.get("DIRECTION", "long")
    long_entry = rsi_val <= params.get("RSI_LONG", 30) and rsi_val > prev_rsi
    short_entry = rsi_val >= params.get("RSI_SHORT", 70) and rsi_val < prev_rsi
    if direction == "short":
        return short_entry
    return long_entry


def strategy_bollinger(inst_id: str, idx: int, meta: dict[str, Any], candle: dict[str, float], params: dict[str, Any]) -> bool:
    if idx < 21:
        return False
    mid = meta["boll_mid"][idx - 1]
    std = meta["boll_std"][idx - 1]
    cur_close = candle["close"]
    direction = params.get("DIRECTION", "long")
    if mid is None or std is None or std <= 0 or cur_close <= 0:
        return False
    if direction == "long":
        return cur_close < mid - params.get("BOLL_K", 2.0) * std and candle["close"] > candle["open"]
    return cur_close > mid + params.get("BOLL_K", 2.0) * std and candle["close"] < candle["open"]


def strategy_momentum(inst_id: str, idx: int, meta: dict[str, Any], candle: dict[str, float], params: dict[str, Any]) -> bool:
    period = max(params.get("MOMENTUM_PERIOD", 5), 1)
    if idx < period + 1:
        return False
    cur_close = candle["close"]
    prev_close = meta["closes"][idx - period]
    momentum = (cur_close - prev_close) / max(prev_close, 1e-9)
    return momentum >= params.get("MOMENTUM_THRESHOLD", 0.008)


def strategy_mean_reversion(inst_id: str, idx: int, meta: dict[str, Any], candle: dict[str, float], params: dict[str, Any]) -> bool:
    if idx < 21:
        return False
    rsi_val = meta["rsi"][idx]
    mid = meta["boll_mid"][idx - 1]
    std = meta["boll_std"][idx - 1]
    cur_close = candle["close"]
    if rsi_val is None or mid is None or std is None or std <= 0:
        return False
    flat = abs(cur_close - mid) <= params.get("MR_BAND_STRENGTH", 0.5) * std
    if params.get("DIRECTION", "long") == "long":
        return cur_close > mid and rsi_val <= params.get("RSI_LONG", 30) and flat
    return cur_close < mid and rsi_val >= params.get("RSI_SHORT", 70) and flat


def strategy_vwap_breakout(inst_id: str, idx: int, meta: dict[str, Any], candle: dict[str, float], params: dict[str, Any]) -> bool:
    lookback = max(params.get("VWAP_PERIOD", 20), 1)
    if idx < lookback + 1:
        return False
    closes = meta["closes"][idx - lookback : idx]
    highs = meta["highs"][idx - lookback : idx]
    lows = meta["lows"][idx - lookback : idx]
    volumes = meta["volumes"][idx - lookback : idx]
    if not volumes:
        return False
    tp = sum((h + l + c) / 3 * v for h, l, c, v in zip(highs, lows, closes, volumes)) / max(sum(volumes), 1e-9)
    cur_close = candle["close"]
    breakout = cur_close > tp * (1 + params.get("VWAP_THRESHOLD", 0.001))
    pullback = params.get("VWAP_PULLBACK", True) and min(candle["open"], cur_close) < tp
    if params.get("DIRECTION", "long") == "long":
        return breakout and pullback
    return not breakout and not pullback


def strategy_ensemble_long(inst_id: str, idx: int, meta: dict[str, Any], candle: dict[str, float], params: dict[str, Any]) -> bool:
    votes = 0
    required = params.get("ENSEMBLE_VOTES", 2)
    if strategy_ema_atr(inst_id, idx, meta, candle, params):
        votes += 1
    if strategy_rsi(inst_id, idx, meta, candle, params):
        votes += 1
    if strategy_bollinger(inst_id, idx, meta, candle, params):
        votes += 1
    if strategy_momentum(inst_id, idx, meta, candle, params):
        votes += 1
    long_only = params.get("ENSEMBLE_LONG_ONLY", True)
    if votes >= required:
        return True
    if not long_only:
        short_params = dict(params)
        short_params.pop("ENSEMBLE_SHORT", None)
        short_params["RSI_SHORT"] = params.get("RSI_SHORT", 70)
        short_params["DIRECTION"] = "short"
        if strategy_rsi(inst_id, idx, meta, candle, short_params):
            votes += 1
    return votes >= required


def _round_or_none(value: float | None, decimals: int) -> float | None:
    return round(value, decimals) if value is not None else None


def run_strategy(
    asset_candles: dict[str, list[dict[str, float]]],
    asset_idx: dict[str, dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    equity = float(EQUITY_START)
    trades = 0
    wins = 0
    losses = 0
    fee_paid = 0.0
    max_equity = equity
    min_equity = equity
    cooldown: dict[str, int] = {}
    open_trades: dict[str, dict[str, Any]] = {}
    equity_curve: list[dict[str, Any]] = []
    max_positions = max(int(params.get("MAX_POSITIONS", 10)), 1)
    max_exposure = equity * max(float(params.get("MAX_EXPOSURE_PCT", 0.18)), 0.05)

    all_ts = sorted({c["ts"] for cs in asset_candles.values() for c in cs})
    if not all_ts:
        return {
            "final_equity": round(equity, 2),
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "fee_paid": 0.0,
            "max_equity": round(max_equity, 2),
            "min_equity": round(min_equity, 2),
            "equity_curve": [],
            "params": params,
        }

    step = max(int(len(all_ts) / max(FOLDS, 1)), 1)
    next_step_start = 0

    for fold in range(FOLDS):
        test_end = min(next_step_start + step, len(all_ts))
        test_ts = set(all_ts[next_step_start:test_end])
        train_ts = set(all_ts[:next_step_start] + all_ts[test_end:])
        next_step_start = min(next_step_start + step, len(all_ts))
        if not test_ts:
            continue

        for ts in sorted(test_ts):
            for inst_id, trade in list(open_trades.items()):
                idx_map = asset_idx.get(inst_id, {}).get("idx", {})
                idx = idx_map.get(ts)
                if idx is None:
                    continue
                candle = asset_candles[inst_id][idx]
                exit_price = None
                side = trade["side"]
                rate_candles = meta = asset_idx[inst_id]

                if side == "long":
                    if candle["low"] <= trade["sl"]:
                        exit_price = _round_or_none(trade["sl"], 8)
                    elif candle["high"] >= trade["tp"]:
                        exit_price = _round_or_none(trade["tp"], 8)
                    elif trade.get("trailing_enabled"):
                        if candle["high"] > trade["high_since_entry"]:
                            trade["high_since_entry"] = float(candle["high"])
                        new_sl = trade["high_since_entry"] * (1 - float(params.get("TRAILING_SL_PCT", 0.0)))
                        if new_sl > trade["sl"]:
                            trade["sl"] = _round_or_none(new_sl, 8)
                else:
                    if candle["high"] >= trade["sl"]:
                        exit_price = _round_or_none(trade["sl"], 8)
                    elif candle["low"] <= trade["tp"]:
                        exit_price = _round_or_none(trade["tp"], 8)
                    elif trade.get("trailing_enabled"):
                        if candle["low"] < trade["low_since_entry"]:
                            trade["low_since_entry"] = float(candle["low"])
                        new_sl = trade["low_since_entry"] * (1 + float(params.get("TRAILING_SL_PCT", 0.0)))
                        if new_sl < trade["sl"]:
                            trade["sl"] = _round_or_none(new_sl, 8)

                if exit_price is not None:
                    entry = float(trade["entry"])
                    cval = float(trade["contract_value"])
                    size = float(trade["size"])
                    notional = float(trade["notional"])
                    pnl = size * (exit_price - entry) * cval if side == "long" else size * (entry - exit_price) * cval
                    pnl -= notional * FEE_ROUND_TRIP * 2 + notional * SLIPPAGE * 2
                    equity += pnl
                    fee_paid += notional * FEE_ROUND_TRIP * 2 + notional * SLIPPAGE * 2
                    trades += 1
                    if pnl > 0:
                        wins += 1
                    else:
                        losses += 1
                    cooldown[inst_id] = idx + max(int(params.get("COOLDOWN_BARS", 12)), 1)
                    open_trades.pop(inst_id, None)

            equity = max(0.02, equity)
            max_equity = max(max_equity, equity)
            min_equity = min(min_equity, equity)
            if equity >= params.get("TAKE_PROFIT_EQUITY", TARGET_EQUITY):
                open_trades.clear()
                equity_curve.append({"ts": ts, "equity": round(equity, 2)})
                continue
            if not equity_curve or ts - equity_curve[-1]["ts"] >= 6 * 3600 * 1000:
                equity_curve.append({"ts": ts, "equity": round(equity, 2)})

            current_notional = sum(float(t["notional"]) for t in open_trades.values())
            if len(open_trades) >= max_positions or current_notional >= max_exposure or equity <= 0.01:
                continue
            for inst_id, candles in asset_candles.items():
                if inst_id in open_trades:
                    continue
                idx_map = asset_idx.get(inst_id, {}).get("idx", {})
                idx = idx_map.get(ts)
                if idx is None or idx < max(int(params.get("LOOKBACK_MIN", 60)), 21):
                    continue
                if cooldown.get(inst_id, -1) >= idx:
                    continue
                candle = candles[idx]
                if not _should_trade(asset_idx[inst_id], idx, candle, params):
                    continue
                if not _strategy_from_params(params)(inst_id, idx, asset_idx[inst_id], candle, params):
                    continue
                entry_price = candle["open"] * (1 + SLIPPAGE) if params.get("DIRECTION", "long") == "long" else candle["open"] * (1 - SLIPPAGE)
                atr_val = asset_idx[inst_id]["atr"][idx]
                if atr_val is None or atr_val <= 0:
                    continue
                sl_distance = float(params.get("SL_RR", 1.0)) * float(atr_val)
                tp_multiplier = float(params.get("TP_RR", 2.5))
                if params.get("DIRECTION", "long") == "long":
                    sl = entry_price - sl_distance
                    tp = entry_price + tp_multiplier * sl_distance
                else:
                    sl = entry_price + sl_distance
                    tp = entry_price - tp_multiplier * sl_distance
                price_risk = abs(entry_price - sl)
                if price_risk <= 0:
                    continue
                cval = float(params.get("CONTRACT_VALUE", 1.0))
                risk_usd = equity * max(float(params.get("RISK_PER_TRADE", 0.05)), 0.005)
                size = risk_usd / (price_risk * cval)
                notional = size * entry_price * cval
                if size <= 0 or notional <= 0:
                    continue
                if notional > equity * 0.4:
                    size = (equity * 0.4) / (entry_price * cval)
                    notional = size * entry_price * cval
                if notional < equity * 0.005:
                    continue
                open_trades[inst_id] = {
                    "side": params.get("DIRECTION", "long"),
                    "entry": entry_price,
                    "sl": sl,
                    "tp": tp,
                    "size": size,
                    "notional": notional,
                    "contract_value": cval,
                    "start_idx": idx,
                    "high_since_entry": float(candle["high"]),
                    "low_since_entry": float(candle["low"]),
                    "trailing_enabled": params.get("TRAILING_SL_PCT", 0.0) > 0.0,
                }

    return {
        "final_equity": _round_or_none(equity, 2) or 0.0,
        "total_trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / trades, 4) if trades else 0.0,
        "fee_paid": round(fee_paid, 2),
        "max_equity": round(max_equity, 2),
        "min_equity": round(min_equity, 2),
        "equity_curve": equity_curve[-100:],
        "params": params,
    }


def _should_trade(meta: dict[str, Any], idx: int, candle: dict[str, float], params: dict[str, Any]) -> bool:
    if idx < int(params.get("LOOKBACK_MIN", 60)):
        return False
    atr_val = meta["atr"][idx]
    cur_close = candle["close"]
    if atr_val is None or atr_val <= 0:
        return False
    if (atr_val / max(cur_close, 1e-9)) < float(params.get("MIN_ATR_PCT", 0.0007)):
        return False
    volume = meta["volumes"][idx]
    if params.get("USE_VOLUME_FILTER", True):
        if volume < float(params.get("MIN_VOLUME", 50000)):
            return False
    if params.get("USE_TREND_FILTER", True):
        trend_period = int(params.get("TREND_FILTER_PERIOD", 50))
        if idx < trend_period:
            return False
        ema200 = meta.get("ema200", [None] * len(meta["closes"]))[idx]
        if ema200 is None:
            return False
        if params.get("DIRECTION", "long") == "long" and cur_close < ema200:
            return False
        if params.get("DIRECTION", "long") == "short" and cur_close > ema200:
            return False
    return True


def build_search_space() -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    strategies = [
        "ema_atr", "rsi", "bollinger", "momentum", "mean_reversion", "vwap_breakout", "ensemble_long"
    ]
    for _ in range(POPULATION_SIZE):
        strategy = random.choice(strategies)
        params: dict[str, Any] = {
            "STRATEGY": strategy,
            "EMA_PERIOD": random.choice([10, 20, 50, 100, 200]),
            "ATR_PERIOD": random.choice([8, 10, 14, 20]),
            "RISK_PER_TRADE": random.choice([0.02, 0.04, 0.08, 0.12]),
            "TP_RR": random.choice([2.0, 2.5, 3.0, 3.5, 4.5]),
            "SL_RR": random.choice([0.8, 1.0, 1.2, 1.4]),
            "MIN_ATR_PCT": random.choice([0.0004, 0.0007, 0.001, 0.0015]),
            "COOLDOWN_BARS": random.choice([6, 12, 18, 24, 32]),
            "LOOKBACK_MIN": random.choice([35, 50, 60, 80, 100]),
            "MAX_POSITIONS": random.choice([5, 8, 12, 18]),
            "MAX_EXPOSURE_PCT": random.choice([0.10, 0.15, 0.22, 0.30]),
            "TRAILING_SL_PCT": random.choice([0.0, 0.008, 0.012, 0.018, 0.025]),
            "BREAKEVEN_SL_PCT": random.choice([0.0, 0.008, 0.012, 0.016, 0.02]),
            "DIRECTION": random.choice(["long", "short", "both"]),
            "USE_VOLUME_FILTER": random.choice([True, False]),
            "USE_TREND_FILTER": random.choice([True, False]),
            "TREND_FILTER_PERIOD": random.choice([50, 100, 200]),
            "MIN_VOLUME": random.choice([30000, 60000, 120000, 240000]),
            "TAKE_PROFIT_EQUITY": TARGET_EQUITY,
            "CONTRACT_VALUE": 1.0,
        }
        if strategy == "rsi":
            params["RSI_LONG"] = random.choice([20, 25, 30, 35, 40])
            params["RSI_SHORT"] = random.choice([60, 70, 75, 80, 85])
        elif strategy == "bollinger":
            params["BOLL_K"] = random.choice([1.2, 1.5, 1.8, 2.0, 2.5, 3.0])
        elif strategy == "momentum":
            params["MOMENTUM_PERIOD"] = random.choice([3, 5, 8, 12, 20])
            params["MOMENTUM_THRESHOLD"] = random.choice([0.003, 0.006, 0.01, 0.018, 0.03])
        elif strategy == "mean_reversion":
            params["MR_BAND_STRENGTH"] = random.choice([0.3, 0.5, 0.7, 1.0])
            params["RSI_LONG"] = random.choice([20, 25, 30, 35])
            params["RSI_SHORT"] = random.choice([60, 70, 75, 80])
        elif strategy == "vwap_breakout":
            params["VWAP_PERIOD"] = random.choice([10, 20, 30, 50])
            params["VWAP_THRESHOLD"] = random.choice([0.0006, 0.001, 0.0015, 0.0025])
            params["VWAP_PULLBACK"] = random.choice([True, False])
        elif strategy == "ensemble_long":
            params["ENSEMBLE_VOTES"] = random.choice([1, 2, 3])
            params["ENSEMBLE_LONG_ONLY"] = random.random() > 0.25
            params["TP_RR"] = random.choice([2.0, 2.5, 3.0, 3.5])
            params["RISK_PER_TRADE"] = random.choice([0.03, 0.06, 0.10])
        if params.get("DIRECTION") == "both":
            params["RSI_SHORT"] = params.get("RSI_SHORT", 70)
        configs.append(params)
    random.shuffle(configs)
    return configs


def _crossover(parent1: dict[str, Any], parent2: dict[str, Any]) -> dict[str, Any]:
    child = dict(parent1)
    keys = list(child.keys())
    for key in keys:
        if key in parent2 and random.random() < 0.5:
            child[key] = parent2[key]
    return child


def _mutate(params: dict[str, Any]) -> dict[str, Any]:
    mutated = dict(params)
    if random.random() < MUTATION_RATE:
        mutated["DIRECTION"] = random.choice(["long", "short", "both"])
    if random.random() < MUTATION_RATE:
        mutated["STRATEGY"] = random.choice(["ema_atr", "rsi", "bollinger", "momentum", "mean_reversion", "vwap_breakout", "ensemble_long"])
    if random.random() < MUTATION_RATE:
        mutated["EMA_PERIOD"] = random.choice([10, 20, 50, 100, 200])
    if random.random() < MUTATION_RATE:
        mutated["ATR_PERIOD"] = random.choice([8, 10, 14, 20])
    if random.random() < MUTATION_RATE:
        mutated["RISK_PER_TRADE"] = random.choice([0.02, 0.04, 0.08, 0.12])
    if random.random() < MUTATION_RATE:
        mutated["TP_RR"] = random.choice([2.0, 2.5, 3.0, 3.5, 4.5])
    if random.random() < MUTATION_RATE:
        mutated["SL_RR"] = random.choice([0.8, 1.0, 1.2, 1.4])
    if random.random() < MUTATION_RATE:
        mutated["COOLDOWN_BARS"] = random.choice([6, 12, 18, 24, 32])
    if random.random() < MUTATION_RATE:
        mutated["MAX_POSITIONS"] = random.choice([5, 8, 12, 18])
    if random.random() < MUTATION_RATE:
        mutated["TRAILING_SL_PCT"] = random.choice([0.0, 0.008, 0.012, 0.018, 0.025])
    if random.random() < MUTATION_RATE:
        mutated["BREAKEVEN_SL_PCT"] = random.choice([0.0, 0.008, 0.012, 0.016, 0.02])
    if random.random() < MUTATION_RATE:
        mutated["MIN_ATR_PCT"] = random.choice([0.0004, 0.0007, 0.001, 0.0015])
    return mutated


@dataclass
class _StrategyCombination:
    score: float
    params: dict[str, Any]
    combination: tuple[str, ...]


def _dedupe_combinations(seen: set[tuple[str, ...]], combination: tuple[str, ...]) -> bool:
    normalized = tuple(dict.fromkeys(combination))
    if normalized in seen:
        return False
    seen.add(normalized)
    return True


def _build_top_combination_search_space(
    top_strategies: tuple[str, ...] = ("ensemble_long", "momentum", "mean_reversion", "rsi", "bollinger", "ema_atr", "vwap_breakout"),
) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    configs: list[dict[str, Any]] = []
    risk_basis = [
        {"RISK_PER_TRADE": 0.03, "TP_RR": 3.0, "SL_RR": 1.0, "MAX_EXPOSURE_PCT": 0.18, "MAX_POSITIONS": 12},
        {"RISK_PER_TRADE": 0.05, "TP_RR": 3.5, "SL_RR": 1.0, "MAX_EXPOSURE_PCT": 0.22, "MAX_POSITIONS": 10},
        {"RISK_PER_TRADE": 0.06, "TP_RR": 2.5, "SL_RR": 1.2, "MAX_EXPOSURE_PCT": 0.18, "MAX_POSITIONS": 14},
    ]

    def _append_single(name: str, basis: dict[str, Any]) -> None:
        if not _dedupe_combinations(seen, (name, name)):
            return
        params = {
            "STRATEGY": name,
            "EMA_PERIOD": 50,
            "ATR_PERIOD": 14,
            "RISK_PER_TRADE": basis["RISK_PER_TRADE"],
            "TP_RR": basis["TP_RR"],
            "SL_RR": basis["SL_RR"],
            "MIN_ATR_PCT": 0.0007,
            "COOLDOWN_BARS": 12,
            "LOOKBACK_MIN": 60,
            "MAX_POSITIONS": basis["MAX_POSITIONS"],
            "MAX_EXPOSURE_PCT": basis["MAX_EXPOSURE_PCT"],
            "TRAILING_SL_PCT": 0.012,
            "BREAKEVEN_SL_PCT": 0.012,
            "DIRECTION": "long",
            "USE_VOLUME_FILTER": True,
            "USE_TREND_FILTER": True,
            "TREND_FILTER_PERIOD": 100,
            "MIN_VOLUME": 60000,
            "TAKE_PROFIT_EQUITY": TARGET_EQUITY,
            "CONTRACT_VALUE": 1.0,
        }
        if name == "rsi":
            params.update({"RSI_LONG": 30, "RSI_SHORT": 70})
        elif name == "bollinger":
            params.update({"BOLL_K": 2.0})
        elif name == "momentum":
            params.update({"MOMENTUM_PERIOD": 8, "MOMENTUM_THRESHOLD": 0.008})
        elif name == "mean_reversion":
            params.update({"MR_BAND_STRENGTH": 0.5, "RSI_LONG": 30, "RSI_SHORT": 70})
        elif name == "vwap_breakout":
            params.update({"VWAP_PERIOD": 20, "VWAP_THRESHOLD": 0.001, "VWAP_PULLBACK": True})
        elif name == "ensemble_long":
            params.update({"ENSEMBLE_VOTES": 2, "ENSEMBLE_LONG_ONLY": True, "RSI_SHORT": 70})
        configs.append(params)

    def _append_combo(combo: tuple[str, ...], basis: dict[str, Any]) -> None:
        if not _dedupe_combinations(seen, combo):
            return
        params = {
            "STRATEGY": "ensemble_long",
            "EMA_PERIOD": 50,
            "ATR_PERIOD": 14,
            "RISK_PER_TRADE": basis["RISK_PER_TRADE"],
            "TP_RR": basis["TP_RR"],
            "SL_RR": basis["SL_RR"],
            "MIN_ATR_PCT": 0.0007,
            "COOLDOWN_BARS": 12,
            "LOOKBACK_MIN": 60,
            "MAX_POSITIONS": basis["MAX_POSITIONS"],
            "MAX_EXPOSURE_PCT": basis["MAX_EXPOSURE_PCT"],
            "TRAILING_SL_PCT": 0.012,
            "BREAKEVEN_SL_PCT": 0.012,
            "DIRECTION": "long",
            "USE_VOLUME_FILTER": True,
            "USE_TREND_FILTER": True,
            "TREND_FILTER_PERIOD": 100,
            "MIN_VOLUME": 60000,
            "TAKE_PROFIT_EQUITY": TARGET_EQUITY,
            "CONTRACT_VALUE": 1.0,
            "ENSEMBLE_VOTES": len(combo),
            "ENSEMBLE_LONG_ONLY": True,
            "ENSEMBLE_SHORT": False,
            "RSI_SHORT": 70,
        }
        if "rsi" in combo:
            params["RSI_LONG"] = 30
        if "bollinger" in combo:
            params["BOLL_K"] = 2.0
        if "momentum" in combo:
            params.update({"MOMENTUM_PERIOD": 8, "MOMENTUM_THRESHOLD": 0.008})
        if "mean_reversion" in combo:
            params.update({"MR_BAND_STRENGTH": 0.5, "RSI_LONG": 30})
        if "vwap_breakout" in combo:
            params.update({"VWAP_PERIOD": 20, "VWAP_THRESHOLD": 0.001, "VWAP_PULLBACK": True})
        configs.append(params)

    for basis in risk_basis:
        for name in top_strategies:
            _append_single(name, basis)
    top_combos = [
        top_strategies[:2],
        top_strategies[:3],
        top_strategies[:4],
        top_strategies[:5],
    ]
    for basis in risk_basis:
        for combo in top_combos:
            _append_combo(combo, basis)
    return configs


def _combination_label(params: dict[str, Any]) -> tuple[str, ...]:
    strategy = params.get("STRATEGY")
    if strategy != "ensemble_long":
        return (strategy,)
    members = {
        "ema_atr": True,
        "rsi": "RSI_LONG" in params,
        "bollinger": "BOLL_K" in params,
        "momentum": "MOMENTUM_PERIOD" in params,
        "mean_reversion": "MR_BAND_STRENGTH" in params,
        "vwap_breakout": "VWAP_PERIOD" in params,
    }
    return tuple(sorted({name for name, active in members.items() if active})) or ("ensemble_long",)


def _evaluate(
    asset_candles: dict[str, list[dict[str, float]]],
    asset_idx: dict[str, dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    metrics = run_strategy(asset_candles, asset_idx, params)
    metrics["score"] = float(metrics.get("final_equity", 0.0))
    return metrics


def evaluate_top_strategy_combinations(asset_candles: dict[str, list[dict[str, float]]], asset_idx: dict[str, dict[str, Any]]) -> list[_StrategyCombination]:
    seen: set[tuple[str, ...]] = set()
    ranked: list[_StrategyCombination] = []
    for params in _build_top_combination_search_space():
        metrics = _evaluate(asset_candles, asset_idx, params)
        combination = _combination_label(metrics.get("params") or params)
        if not _dedupe_combinations(seen, combination):
            continue
        ranked.append(_StrategyCombination(score=metrics["score"], params=metrics.get("params") or params, combination=combination))
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[:20]


def optimize(asset_candles: dict[str, list[dict[str, float]]], asset_idx: dict[str, dict[str, Any]]) -> dict[str, Any]:
    population = build_search_space()
    all_results: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    best_score = -1.0
    t0 = time_mod.time()

    print("Evaluating top strategies + ranked combinations...")
    ranked_combinations = evaluate_top_strategy_combinations(asset_candles, asset_idx)
    for rank in ranked_combinations:
        all_results.append({"score": rank.score, "final_equity": rank.score, "params": rank.params, "combination": rank.combination, "total_trades": 0})
        print(f"  ranked combination={rank.combination} equity=${rank.score:.2f}")
        if rank.score > best_score:
            best_score = rank.score
            best = all_results[-1]
    combo_seeds = [item["params"] for item in all_results[:10] if item.get("params")]
    population = list(combo_seeds) + build_search_space()[: max(0, POPULATION_SIZE - len(combo_seeds))]

    for iteration in range(1, MAX_ITERATIONS + 1):
        results: list[dict[str, Any]] = []
        iter_best: dict[str, Any] | None = None
        iter_best_score = -1.0
        print(f"\n=== Iteration {iteration}/{MAX_ITERATIONS} | population={len(population)} ===")
        for i, params in enumerate(population, start=1):
            metrics = _evaluate(asset_candles, asset_idx, params)
            results.append(metrics)
            if metrics["score"] > best_score:
                best_score = metrics["score"]
                best = metrics
            if metrics["score"] > iter_best_score:
                iter_best_score = metrics["score"]
                iter_best = metrics
            if i % 12 == 0 or i == len(population):
                elapsed = time_mod.time() - t0
                print(f"  [{i}/{len(population)}] iter_best=${iter_best_score:.2f} best=${best_score:.2f} elapsed={elapsed:.1f}s")
        all_results.extend(results)
        all_results.sort(key=lambda item: item.get("score", -1), reverse=True)
        print(f"Iteration {iteration} complete | best_final_equity={iter_best.get('final_equity', 0):.2f} | global_best={best_score:.2f}")
        if best_score >= TARGET_EQUITY and best is not None:
            best["target_reached"] = True
            print(f"Target reached: ${best.get('final_equity', 0):.2f} >= ${TARGET_EQUITY:.2f}")
            break
        if iteration >= MAX_ITERATIONS:
            break
        results.sort(key=lambda item: item.get("score", -1), reverse=True)
        elite = [item["params"] for item in results[:ELITE_COUNT]]
        new_population = list(elite)
        while len(new_population) < POPULATION_SIZE:
            parent1 = random.choice(elite)
            parent2 = random.choice(elite)
            child = _crossover(parent1, parent2)
            child = _mutate(child)
            new_population.append(child)
        population = new_population

    top_results = sorted(all_results, key=lambda item: item.get("score", -1), reverse=True)[:20]
    if best is None:
        best = {"final_equity": 0.0, "score": -1.0, "params": {}, "total_trades": 0}
    best["target_reached"] = bool(best.get("final_equity", 0) >= TARGET_EQUITY)
    print(f"\nOptimization complete in {time_mod.time() - t0:.1f}s | best equity={best.get('final_equity', 0):.2f} | target={TARGET_EQUITY}")
    return {
        "best": best,
        "results": top_results,
        "iterations": MAX_ITERATIONS,
        "target_equity": TARGET_EQUITY,
        "target_reached": best.get("target_reached", False),
    }


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(params)
    for key in ["SL_PRICE", "TP_PRICE", "INITIAL_SL"]:
        cleaned.pop(key, None)
    return cleaned


def save_best(metrics: dict[str, Any]) -> None:
    if not metrics:
        return
    metrics = dict(metrics)
    params = _clean_params(metrics.get("params") or {})
    metrics["params"] = params
    payload = {
        "best_params": params,
        "metrics": metrics,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target_equity": TARGET_EQUITY,
        "target_reached": bool(metrics.get("final_equity", 0) >= TARGET_EQUITY),
    }
    os.makedirs(os.path.dirname(OPTIMIZED_PARAMS_PATH) or ".", exist_ok=True)
    with open(OPTIMIZED_PARAMS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    print(f"Saved optimized params to {OPTIMIZED_PARAMS_PATH}")
    try:
        with open(OPTIMIZED_PARAMS_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        saved_equity = (((saved.get("metrics") or {}).get("equity_curve") or [{}])[-1].get("equity"))
        print(f"Verified saved final equity approx={saved_equity} target={TARGET_EQUITY}")
    except Exception as exc:  # noqa: BLE001
        print(f"Save verification failed: {exc}")


if __name__ == "__main__":
    client = BlofinClient()
    universe = get_universe(client)
    print(f"Universe: {len(universe)} USDT swap instruments")
    sample = universe[:MAX_UNIVERSE]
    print(f"Testing full universe: {len(sample)} assets")
    asset_candles: dict[str, list[dict[str, float]]] = {}
    t0 = time_mod.time()
    for i, inst_id in enumerate(sample, start=1):
        try:
            candles = fetch_candles(client, inst_id)
            horizon_ts = candles[-1]["ts"] - BACKTEST_DAYS * 24 * 3600 * 1000 if candles else 0
            trimmed = [c for c in candles if c["ts"] >= horizon_ts] if candles else []
            if len(trimmed) >= 200:
                trimmed = trimmed[-BACKTEST_DAYS * 24 if BACKTEST_DAYS * 24 < len(trimmed) else len(trimmed):]
                asset_candles[inst_id] = trimmed
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(sample)}] {inst_id} error: {exc}")
            continue
        if i % 20 == 0 or i == len(sample):
            print(f"  fetched {i}/{len(sample)}")
    print(f"Fetch done in {time_mod.time() - t0:.1f}s, {len(asset_candles)} assets usable")
    if not asset_candles:
        raise SystemExit("No candle data available for backtest optimization.")
    asset_idx = prepare_asset_index(asset_candles)
    result = optimize(asset_candles, asset_idx)
    save_best(result.get("best", {}))
    print(f"Target reached: {result.get('target_reached', False)}")
    print(f"Best equity: {result.get('best', {}).get('final_equity', 0):.2f}")
