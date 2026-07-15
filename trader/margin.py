"""Margin + leverage planning for small-account BloFin opens."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from config import LEVERAGE_LADDER, MARGIN_USE_RATIO, TRADE_MAX_LEVERAGE


@dataclass
class OpenPlan:
    inst_id: str
    side: str
    contracts: float
    leverage: int
    price: float
    notional: float
    margin: float
    contract_value: float
    min_size: float


def _inst_map() -> dict[str, dict[str, Any]]:
    from blofin.market_cache import get_cached_instruments

    rows = get_cached_instruments(allow_stale=True) or []
    return {str(r.get("instId") or ""): r for r in rows if r.get("instId")}


def get_instrument(inst_id: str) -> dict[str, Any] | None:
    return _inst_map().get(inst_id)


def contract_notional(size: float, price: float, contract_value: float) -> float:
    return abs(size) * max(price, 0.0) * max(contract_value, 0.0)


def margin_for(size: float, price: float, contract_value: float, leverage: float) -> float:
    lev = max(float(leverage), 1.0)
    return contract_notional(size, price, contract_value) / lev


def _max_leverage_for_inst(inst: dict[str, Any]) -> int:
    try:
        cap = int(float(inst.get("maxLeverage") or TRADE_MAX_LEVERAGE))
    except (TypeError, ValueError):
        cap = TRADE_MAX_LEVERAGE
    return max(1, min(cap, TRADE_MAX_LEVERAGE))


def _responsible_cap(margin_budget: float, account: dict[str, Any] | None) -> int:
    """Cap leverage based on how large this position's margin slice is vs equity.

    Smaller allocations can run higher leverage; larger allocations stay conservative.
    The cap is a soft default — plan_open may exceed it when required to afford the
    instrument's minimum contract margin.
    """
    if not account:
        return TRADE_MAX_LEVERAGE
    equity = float(account.get("equity") or account.get("available") or 0)
    if equity <= 0:
        return TRADE_MAX_LEVERAGE
    ratio = float(margin_budget) / equity
    if ratio >= 0.20:
        cap = 10
    elif ratio >= 0.10:
        cap = 15
    elif ratio >= 0.05:
        cap = 20
    elif ratio >= 0.02:
        cap = 30
    else:
        cap = 50
    return max(3, min(cap, TRADE_MAX_LEVERAGE))


def _leverage_steps(inst: dict[str, Any], cap: int | None = None) -> list[int]:
    cap = min(cap or TRADE_MAX_LEVERAGE, _max_leverage_for_inst(inst))
    steps = [lev for lev in LEVERAGE_LADDER if lev <= cap]
    if cap not in steps:
        steps.append(cap)
    return sorted(set(steps), reverse=True)


def plan_open(
    *,
    inst_id: str,
    side: str,
    price: float,
    margin_budget: float,
    size_contracts: int | float | None = None,
    account: dict[str, Any] | None = None,
) -> OpenPlan | None:
    inst = get_instrument(inst_id)
    if not inst or price <= 0 or margin_budget <= 0:
        return None

    ct_val = float(inst.get("contractValue") or inst.get("ctVal") or 1.0)
    min_size = float(inst.get("minSize") or inst.get("lotSize") or 1.0)
    contracts = float(size_contracts) if size_contracts and float(size_contracts) > 0 else min_size
    contracts = max(min_size, contracts)

    budget = float(margin_budget)
    inst_cap = _max_leverage_for_inst(inst)
    soft_cap = _responsible_cap(budget, account)
    all_steps = _leverage_steps(inst, inst_cap)

    chosen_lev: int | None = None
    # Prefer leverage up to the responsible soft cap.
    for lev in all_steps:
        if lev > soft_cap:
            continue
        if margin_for(min_size, price, ct_val, lev) <= budget:
            chosen_lev = lev
            break
    # If the min contract margin cannot be met within the soft cap, raise leverage
    # just enough to afford the position (still capped by instrument + TRADE_MAX_LEVERAGE).
    if chosen_lev is None:
        for lev in all_steps:
            if lev <= soft_cap:
                continue
            if margin_for(min_size, price, ct_val, lev) <= budget:
                chosen_lev = lev
                break

    if chosen_lev is None:
        return None

    # Scale contracts up to use budget (conviction/sentiment-sized notional).
    step = min_size
    while True:
        next_size = contracts + step
        req = margin_for(next_size, price, ct_val, chosen_lev)
        if req <= budget:
            contracts = next_size
        else:
            break

    chosen_margin = margin_for(contracts, price, ct_val, chosen_lev)
    notional = contract_notional(contracts, price, ct_val)
    return OpenPlan(
        inst_id=inst_id,
        side=side,
        contracts=contracts,
        leverage=chosen_lev,
        price=price,
        notional=notional,
        margin=chosen_margin,
        contract_value=ct_val,
        min_size=min_size,
    )


def annotate_scan_row(row: dict[str, Any], margin_budget: float, account: dict[str, Any] | None = None) -> dict[str, Any]:
    inst = str(row.get("instId") or "")
    price = float(row.get("price") or 0)
    side = row.get("side")
    out = dict(row)
    if not inst or not side or price <= 0:
        out["affordable"] = False
        return out
    plan = plan_open(
        inst_id=inst,
        side=str(side),
        price=price,
        margin_budget=margin_budget,
        account=account,
    )
    if not plan:
        out["affordable"] = False
        return out
    out["affordable"] = True
    out["min_leverage"] = plan.leverage
    out["est_margin"] = round(plan.margin, 4)
    out["min_contracts"] = plan.contracts
    out["sized_contracts"] = plan.contracts
    return out


def affordable_setups(
    scan: list[dict[str, Any]],
    account: dict[str, Any],
    *,
    budget_fn=None,
) -> list[dict[str, Any]]:
    """Each row sized independently from its scan score + inferred conviction."""
    from trader.sizing import budget_for_scan_row

    fn = budget_fn or budget_for_scan_row
    rows: list[dict[str, Any]] = []
    for row in scan:
        if row.get("error") or not row.get("side"):
            continue
        if abs(int(row.get("score") or 0)) < 3:
            continue
        sizing = fn(account, row)
        budget = float(sizing.get("margin_budget") or 0)
        annotated = annotate_scan_row(row, budget, account)
        if annotated.get("affordable"):
            annotated["sizing"] = sizing
            rows.append(annotated)
    rows.sort(key=lambda r: (abs(int(r.get("score") or 0)), -float(r.get("est_margin") or 999)), reverse=True)
    return rows


def pick_best_affordable(
    scan: list[dict[str, Any]],
    account: dict[str, Any],
    *,
    margin_budget: float | None = None,
    inst_id: str | None = None,
    side: str | None = None,
) -> OpenPlan | None:
    if inst_id and side:
        price = 0.0
        for row in scan:
            if row.get("instId") == inst_id:
                price = float(row.get("price") or 0)
                break
        if price <= 0:
            return None
        if margin_budget is None:
            from trader.sizing import budget_for_scan_row

            for row in scan:
                if row.get("instId") == inst_id:
                    margin_budget = float(budget_for_scan_row(account, row).get("margin_budget") or 0)
                    break
            if margin_budget is None:
                from trader.sizing import margin_budget_for_setup

                margin_budget = float(margin_budget_for_setup(account).get("margin_budget") or 0)
        return plan_open(
            inst_id=inst_id,
            side=side,
            price=price,
            margin_budget=float(margin_budget),
            account=account,
        )

    setups = affordable_setups(scan, account)
    if not setups:
        return None
    best = setups[0]
    budget = float(best.get("sizing", {}).get("margin_budget") or 0)
    return plan_open(
        inst_id=str(best["instId"]),
        side=str(best["side"]),
        price=float(best["price"]),
        margin_budget=budget,
        account=account,
    )


def format_contracts(size: float, min_size: float) -> str:
    if min_size >= 1:
        return str(max(1, int(size)))
    decimals = max(0, -int(round(math.log10(min_size)))) if min_size > 0 else 2
    fmt = f"{{:.{decimals}f}}"
    return fmt.format(size).rstrip("0").rstrip(".") or str(min_size)
