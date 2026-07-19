"""Local paper trading ledger — virtual equity, live market marks."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from config import DATA_DIR, FEE_TAKER, PAPER_START_EQUITY, SLIPPAGE

LEDGER_PATH = DATA_DIR / "paper_account.json"
_lock = threading.RLock()


def _default_state() -> dict[str, Any]:
    cash = float(PAPER_START_EQUITY)
    return {
        "cash": cash,
        "starting_equity": cash,
        "positions": {},  # instId -> position dict
        "leverage": {},  # instId -> int
        "tpsl": {},  # instId -> {tp, sl, size}
        "fills": [],
        "updated_at": time.time(),
    }


def _load() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not LEDGER_PATH.is_file():
        state = _default_state()
        _save(state)
        return state
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("bad ledger")
        data.setdefault("positions", {})
        data.setdefault("leverage", {})
        data.setdefault("tpsl", {})
        data.setdefault("fills", [])
        data.setdefault("cash", float(PAPER_START_EQUITY))
        data.setdefault("starting_equity", float(PAPER_START_EQUITY))
        if isinstance(data.get("positions"), list):
            data["positions"] = {}
        if not isinstance(data.get("leverage"), dict):
            data["leverage"] = {}
        if not isinstance(data.get("tpsl"), dict):
            data["tpsl"] = {}
        return data
    except Exception:
        state = _default_state()
        _save(state)
        return state


def _save(state: dict[str, Any]) -> None:
    state["updated_at"] = time.time()
    tmp = LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(LEDGER_PATH)


def reset_paper_account(*, equity: float | None = None) -> dict[str, Any]:
    with _lock:
        start = float(equity if equity is not None else PAPER_START_EQUITY)
        state = _default_state()
        state["cash"] = start
        state["starting_equity"] = start
        _save(state)
        return state


def _inst_meta(inst_id: str) -> tuple[float, float]:
    """Return (contract_value, min_size)."""
    try:
        from blofin.market_cache import get_cached_instruments

        for row in get_cached_instruments(allow_stale=True) or []:
            if str(row.get("instId") or "") == inst_id:
                ct = float(row.get("contractValue") or row.get("ctVal") or 1.0)
                mn = float(row.get("minSize") or row.get("lotSize") or 1.0)
                return max(ct, 1e-12), max(mn, 1e-12)
    except Exception:
        pass
    return 1.0, 1.0


def _mark_price(inst_id: str, fallback: float = 0.0) -> float:
    try:
        from blofin.market_cache import get_cached_tickers

        for row in get_cached_tickers(allow_stale=True) or []:
            if str(row.get("instId") or "") == inst_id:
                px = float(row.get("last") or row.get("markPrice") or 0)
                if px > 0:
                    return px
    except Exception:
        pass
    if fallback > 0:
        return fallback
    try:
        from blofin.client import BlofinClient

        rows = BlofinClient().get_candles(inst_id, "1m", "2")
        return float(rows[-1][4])
    except Exception:
        return fallback


def _position_upl(pos: dict[str, Any], mark: float) -> float:
    entry = float(pos.get("entry") or 0)
    size = float(pos.get("size") or 0)
    ct = float(pos.get("contract_value") or 1.0)
    if entry <= 0 or mark <= 0 or abs(size) <= 0:
        return 0.0
    # Long if size > 0
    return (mark - entry) * size * ct


def _margin_used(pos: dict[str, Any], leverage: float) -> float:
    entry = float(pos.get("entry") or 0)
    size = abs(float(pos.get("size") or 0))
    ct = float(pos.get("contract_value") or 1.0)
    lev = max(float(leverage or 1), 1.0)
    return (size * entry * ct) / lev


def snapshot(*, force_marks: bool = True) -> dict[str, Any]:
    """Dashboard/trader-shaped account snapshot from the paper ledger."""
    with _lock:
        state = _load()
        positions_map = state.get("positions") or {}
        if not isinstance(positions_map, dict):
            positions_map = {}
            state["positions"] = positions_map
        positions_out: list[dict[str, Any]] = []
        upl_total = 0.0
        margin_total = 0.0
        for inst, pos in list(positions_map.items()):
            size = float(pos.get("size") or 0)
            if abs(size) <= 0:
                continue
            mark = _mark_price(inst, float(pos.get("mark") or pos.get("entry") or 0)) if force_marks else float(pos.get("mark") or 0)
            if mark <= 0:
                mark = float(pos.get("entry") or 0)
            pos["mark"] = mark
            lev = float(state.get("leverage", {}).get(inst) or pos.get("leverage") or 3)
            upl = _position_upl(pos, mark)
            pos["upl"] = upl
            margin = _margin_used(pos, lev)
            upl_total += upl
            margin_total += margin
            positions_out.append(
                {
                    "instId": inst,
                    "side": "long" if size > 0 else "short",
                    "size": size,
                    "entry": float(pos.get("entry") or 0),
                    "mark": mark,
                    "upl": upl,
                    "upl_ratio": (upl / margin) if margin > 0 else 0.0,
                    "initial_margin": margin,
                    "leverage": lev,
                    "marginMode": "cross",
                    "notional": abs(size) * mark * float(pos.get("contract_value") or 1.0),
                }
            )
        cash = float(state.get("cash") or 0)
        equity = cash + upl_total
        available = max(0.0, cash - margin_total + min(0.0, upl_total))
        # Simpler available: equity - margin locked
        available = max(0.0, equity - margin_total)
        raw_positions = {
            "code": "0",
            "msg": "paper",
            "data": [
                {
                    "instId": p["instId"],
                    "positions": str(p["size"]),
                    "averagePrice": str(p["entry"]),
                    "markPrice": str(p["mark"]),
                    "unrealizedPnl": str(p["upl"]),
                    "leverage": str(p["leverage"]),
                    "marginMode": "cross",
                    "positionSide": "net",
                }
                for p in positions_out
            ],
        }
        bal_raw = {
            "code": "0",
            "msg": "paper",
            "data": {
                "totalEquity": str(equity),
                "details": [
                    {
                        "currency": "USDT",
                        "equity": str(equity),
                        "available": str(available),
                        "availableEquity": str(available),
                        "balance": str(cash),
                    }
                ],
            },
        }
        _save(state)
        return {
            "equity": round(equity, 8),
            "available": round(available, 8),
            "positions": positions_out,
            "position_mode": "net_mode",
            "fetched_at": time.time(),
            "ok": True,
            "authoritative": True,
            "paper": True,
            "starting_equity": float(state.get("starting_equity") or PAPER_START_EQUITY),
            "balance_raw": bal_raw,
            "positions_raw": raw_positions,
            "backoff_until": 0.0,
        }


def set_leverage(inst_id: str, leverage: int | str) -> dict[str, Any]:
    with _lock:
        state = _load()
        try:
            lev = int(float(leverage))
        except (TypeError, ValueError):
            lev = 3
        state.setdefault("leverage", {})[inst_id] = max(1, lev)
        if inst_id in state.get("positions", {}):
            state["positions"][inst_id]["leverage"] = max(1, lev)
        _save(state)
        return {"code": "0", "msg": "paper leverage set", "data": {"leverage": str(lev)}}


def place_market_order(
    *,
    inst_id: str,
    side: str,
    size: str | float,
    reduce_only: bool = False,
    leverage: int | None = None,
) -> dict[str, Any]:
    with _lock:
        state = _load()
        try:
            qty = abs(float(size))
        except (TypeError, ValueError):
            return {"code": "1", "msg": "invalid size", "data": []}
        if qty <= 0:
            return {"code": "1", "msg": "size must be > 0", "data": []}

        ct, _min = _inst_meta(inst_id)
        mark = _mark_price(inst_id)
        if mark <= 0:
            return {"code": "1", "msg": "no mark price", "data": []}

        # Live-parity fill: adverse slippage + taker fee (same idea as backtests).
        fill_px = mark * (1.0 + SLIPPAGE) if side.lower() == "buy" else mark * (1.0 - SLIPPAGE)

        signed = qty if side.lower() == "buy" else -qty
        pos = dict(state.get("positions", {}).get(inst_id) or {})
        cur = float(pos.get("size") or 0)
        lev = int(leverage or state.get("leverage", {}).get(inst_id) or pos.get("leverage") or 5)
        lev = max(1, lev)

        # Reduce / close
        if reduce_only or (cur != 0 and (cur > 0) != (signed > 0)):
            if abs(cur) <= 0:
                return {
                    "code": "1",
                    "msg": "All operations failed",
                    "data": [{"code": "102022", "msg": "No positions on this contract."}],
                }
            close_qty = min(abs(signed), abs(cur))
            close_signed = close_qty if cur > 0 else -close_qty
            entry = float(pos.get("entry") or fill_px)
            realized = (fill_px - entry) * close_signed * ct
            notional = close_qty * fill_px * ct
            fee = notional * FEE_TAKER
            # Cross-margin style: cash only moves by realized PnL and fees (margin is not a cash deduct).
            state["cash"] = float(state.get("cash") or 0) + realized - fee
            new_size = cur - close_signed
            if abs(new_size) < 1e-12:
                state["positions"].pop(inst_id, None)
                state.get("tpsl", {}).pop(inst_id, None)
            else:
                pos["size"] = new_size
                pos["mark"] = fill_px
                pos["contract_value"] = ct
                pos["leverage"] = lev
                state["positions"][inst_id] = pos
            oid = f"paper-{uuid.uuid4().hex[:12]}"
            state.setdefault("fills", []).append(
                {
                    "ts": time.time(),
                    "instId": inst_id,
                    "side": side,
                    "size": close_qty,
                    "price": fill_px,
                    "fee": fee,
                    "realized": realized,
                    "orderId": oid,
                    "reduce": True,
                }
            )
            state["fills"] = state["fills"][-200:]
            _save(state)
            return {
                "code": "0",
                "msg": "",
                "data": [{"orderId": oid, "clientOrderId": "", "code": "0", "msg": "Order placed"}],
            }

        # Open / add same direction
        notional = qty * fill_px * ct
        fee = notional * FEE_TAKER
        margin = notional / lev
        cash = float(state.get("cash") or 0)
        locked = 0.0
        for i, p in (state.get("positions") or {}).items():
            if not isinstance(p, dict):
                continue
            il = float(state.get("leverage", {}).get(i) or p.get("leverage") or lev)
            locked += _margin_used(p, il)
        free = cash - locked
        if margin + fee > free + 1e-9:
            return {
                "code": "1",
                "msg": "All operations failed",
                "data": [{"code": "103003", "msg": "Insufficient margin in account"}],
            }

        state["cash"] = cash - fee
        if abs(cur) > 0 and (cur > 0) == (signed > 0):
            old_notional = abs(cur) * float(pos.get("entry") or fill_px) * ct
            new_notional = notional
            new_size = cur + signed
            avg = (old_notional + new_notional) / (abs(new_size) * ct) if abs(new_size) > 0 else fill_px
            pos.update(
                {
                    "size": new_size,
                    "entry": avg,
                    "mark": fill_px,
                    "contract_value": ct,
                    "leverage": lev,
                }
            )
        else:
            pos = {
                "size": signed,
                "entry": fill_px,
                "mark": fill_px,
                "contract_value": ct,
                "leverage": lev,
            }
        state.setdefault("positions", {})[inst_id] = pos
        state.setdefault("leverage", {})[inst_id] = lev
        oid = f"paper-{uuid.uuid4().hex[:12]}"
        state.setdefault("fills", []).append(
            {
                "ts": time.time(),
                "instId": inst_id,
                "side": side,
                "size": qty,
                "price": fill_px,
                "fee": fee,
                "margin": margin,
                "orderId": oid,
                "reduce": False,
            }
        )
        state["fills"] = state["fills"][-200:]
        _save(state)
        return {
            "code": "0",
            "msg": "",
            "data": [{"orderId": oid, "clientOrderId": "", "code": "0", "msg": "Order placed"}],
        }


def attach_tpsl(inst_id: str, size: str, tp: float, sl: float) -> dict[str, Any]:
    with _lock:
        state = _load()
        if inst_id not in state.get("positions", {}):
            return {"code": "1", "msg": "no paper position", "data": {}}
        tid = f"paper-tpsl-{uuid.uuid4().hex[:10]}"
        state.setdefault("tpsl", {})[inst_id] = {
            "tpslId": tid,
            "size": size,
            "tp": float(tp),
            "sl": float(sl),
        }
        _save(state)
        return {"code": "0", "msg": "Order placed", "data": {"tpslId": tid, "code": "0"}}


def cancel_tpsl(inst_id: str | None = None) -> dict[str, Any]:
    with _lock:
        state = _load()
        if inst_id:
            state.get("tpsl", {}).pop(inst_id, None)
        else:
            state["tpsl"] = {}
        _save(state)
        return {"code": "0", "msg": "paper tpsl cancelled", "data": []}


def apply_tpsl_triggers() -> list[dict[str, Any]]:
    """Close paper positions that hit stored TP/SL; return fill responses."""
    results: list[dict[str, Any]] = []
    with _lock:
        state = _load()
        for inst, rules in list(state.get("tpsl", {}).items()):
            pos = state.get("positions", {}).get(inst)
            if not pos:
                continue
            mark = _mark_price(inst, float(pos.get("mark") or 0))
            size = float(pos.get("size") or 0)
            tp = float(rules.get("tp") or 0)
            sl = float(rules.get("sl") or 0)
            hit = False
            if size > 0:
                hit = (tp > 0 and mark >= tp) or (sl > 0 and mark <= sl)
                close_side = "sell"
            else:
                hit = (tp > 0 and mark <= tp) or (sl > 0 and mark >= sl)
                close_side = "buy"
            if hit:
                resp = place_market_order(
                    inst_id=inst,
                    side=close_side,
                    size=abs(size),
                    reduce_only=True,
                )
                results.append({"instId": inst, "response": resp, "mark": mark})
    return results


def handle_request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any] | None:
    """Mirror BloFin private REST inside the paper ledger.

    Returns None when the path should hit the real/live HTTP transport (market data).
    Agent/client call sites stay identical between demo and live.
    """
    params = params or {}
    method_u = method.upper()
    path_only = path.split("?", 1)[0]

    # Market data always goes to live OpenAPI.
    if path_only.startswith("/api/v1/market/"):
        return None

    if path_only == "/api/v1/account/balance" and method_u == "GET":
        return snapshot().get("balance_raw") or {"code": "0", "data": {}}

    if path_only in ("/api/v1/account/positions", "/api/v1/trade/positions") and method_u == "GET":
        raw = snapshot().get("positions_raw") or {"code": "0", "data": []}
        inst = params.get("instId")
        if inst:
            rows = [r for r in (raw.get("data") or []) if r.get("instId") == inst]
            return {"code": "0", "msg": "success", "data": rows}
        return raw

    if path_only == "/api/v1/account/position-mode" and method_u == "GET":
        return {"code": "0", "data": {"positionMode": "net_mode"}}

    if path_only == "/api/v1/account/set-position-mode" and method_u == "POST":
        return {"code": "0", "data": {"positionMode": "net_mode"}}

    if path_only == "/api/v1/account/set-leverage" and method_u == "POST":
        b = body if isinstance(body, dict) else {}
        return set_leverage(str(b.get("instId") or ""), b.get("leverage") or 5)

    if path_only == "/api/v1/trade/order" and method_u == "POST":
        b = body if isinstance(body, dict) else {}
        reduce = str(b.get("reduceOnly") or "false").lower() in ("true", "1", "yes")
        return place_market_order(
            inst_id=str(b.get("instId") or ""),
            side=str(b.get("side") or "buy"),
            size=b.get("size") or "0",
            reduce_only=reduce,
        )

    if path_only == "/api/v1/trade/order-tpsl" and method_u == "POST":
        b = body if isinstance(body, dict) else {}
        return attach_tpsl(
            str(b.get("instId") or ""),
            str(b.get("size") or "0"),
            float(b.get("tpTriggerPrice") or 0),
            float(b.get("slTriggerPrice") or 0),
        )

    if path_only == "/api/v1/trade/cancel-tpsl" and method_u == "POST":
        inst = None
        if isinstance(body, list) and body:
            inst = (body[0] or {}).get("instId")
        elif isinstance(body, dict):
            inst = body.get("instId")
        return cancel_tpsl(str(inst) if inst else None)

    if path_only == "/api/v1/trade/orders-tpsl-pending" and method_u == "GET":
        state = _load()
        rows = []
        for inst, rules in (state.get("tpsl") or {}).items():
            if params.get("instId") and params.get("instId") != inst:
                continue
            rows.append(
                {
                    "instId": inst,
                    "tpslId": rules.get("tpslId"),
                    "size": rules.get("size"),
                    "tpTriggerPrice": rules.get("tp"),
                    "slTriggerPrice": rules.get("sl"),
                }
            )
        return {"code": "0", "msg": "success", "data": rows}

    if path_only == "/api/v1/trade/order-tpsl-detail" and method_u == "GET":
        return {"code": "0", "msg": "success", "data": {}}

    # Unknown private route — no-op success so live-only endpoints don't crash demo.
    return {"code": "0", "msg": "paper noop", "data": {}}

