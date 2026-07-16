"""Edge-driven trade engine — push high-conviction, best-parameter-aligned trades."""

from __future__ import annotations

import json
import time
from typing import Any

from activity_log import log_event
from config import OPEN_CONFIDENCE_FLOOR, FALLBACK_OPEN_CONFIDENCE_FLOOR
from trader.margin import affordable_setups, budget_for_scan_row, pick_best_affordable
from trader.order_guard import validate_open
from trader.prompts import TRADER_SYSTEM
from trader.sizing import ABSOLUTE_MIN_MARGIN


def _compact_context(state: dict[str, Any], account: dict[str, Any], scan: list[dict[str, Any]]) -> str:
    safe_scan = []
    for row in (scan or [])[:25]:
        sizing = row.get("sizing") or budget_for_scan_row(account, row)
        safe_scan.append({
            "instId": row.get("instId"),
            "side": row.get("side"),
            "score": row.get("score"),
            "confidence": row.get("confidence"),
            "est_margin": sizing.get("est_margin"),
            "margin_budget": sizing.get("margin_budget"),
            "affordable": bool(sizing.get("margin_budget") and float(sizing.get("margin_budget") or 0) >= ABSOLUTE_MIN_MARGIN),
            "c1_pct": row.get("c1_pct"),
            "c5_pct": row.get("c5_pct"),
            "min_leverage": row.get("min_leverage"),
        })
    return json.dumps({
        "equity": account.get("equity"),
        "available": account.get("available"),
        "positions_count": len(account.get("positions") or []),
        "open_confidence_floor": OPEN_CONFIDENCE_FLOOR,
        "fallback_open_confidence_floor": FALLBACK_OPEN_CONFIDENCE_FLOOR,
        "operator_instructions": state.get("operator_instructions"),
        "scan": safe_scan,
    }, indent=2)


class EdgeDecision:
    def __init__(self, decision: dict[str, Any], *, source: str = "edge_driver") -> None:
        self.decision = dict(decision or {})
        self.source = source
        self.decision.setdefault("source", source)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.decision)


def try_edge_decision(
    state: dict[str, Any],
    account: dict[str, Any],
    scan: list[dict[str, Any]],
    llm: Any,
    *,
    max_candidates: int = 8,
    min_confidence: float | None = None,
) -> EdgeDecision | None:
    if min_confidence is None:
        min_confidence = OPEN_CONFIDENCE_FLOOR
    if not scan:
        return None

    candidates = []
    for row in scan:
        side = str(row.get("side") or "").lower()
        score = float(row.get("score") or 0)
        confidence = float(row.get("confidence") or 0)
        if side not in ("buy", "sell") or abs(score) < 3:
            continue
        sizing = row.get("sizing") or budget_for_scan_row(account, row)
        margin_budget = float(sizing.get("margin_budget") or 0)
        if margin_budget < ABSOLUTE_MIN_MARGIN:
            continue
        candidates.append({
            "instId": row.get("instId"),
            "side": side,
            "score": score,
            "confidence": confidence,
            "margin_budget": margin_budget,
            "est_margin": sizing.get("est_margin"),
            "min_leverage": row.get("min_leverage"),
            "price": row.get("price"),
            "sizing": sizing,
        })

    candidates = sorted(candidates, key=lambda x: (-float(x.get("confidence") or 0), -abs(float(x.get("score") or 0)), float(x.get("margin_budget") or 0)))[:max_candidates]
    if not candidates:
        return None

    context = _compact_context(state, account, scan)
    prompt = (
        "Pick the single best executable edge from the candidates JSON below. "
        "Return ONLY JSON with schema: "
        '{"action":"open","instId":"...","side":"buy|sell","confidence":0-100,"tp_pct":2.0,"sl_pct":1.0,"size_contracts":null,"reasoning":"max 120 chars"}'
        "\nCandidate edge rules:\n"
        f"- minimum confidence = {min_confidence}\n"
        "- prefer affordable setups with strong |score| and clear reasoning\n"
        "- if no edge beats hold, return hold with low confidence\n"
        f"\n{context}\n"
        "Candidates:\n"
        + json.dumps(candidates, indent=2)
    )

    try:
        resp = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system=(TRADER_SYSTEM or "")[:2000],
            max_tokens=900,
        )
        text = resp.text or ""
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        parsed = json.loads(text)
    except Exception:
        return None

    if not isinstance(parsed, dict):
        return None
    action = str(parsed.get("action") or "hold").lower()
    if action != "open":
        return None

    inst = parsed.get("instId") or candidates[0]["instId"]
    side = str(parsed.get("side") or candidates[0]["side"]).lower()
    confidence = float(parsed.get("confidence") or 0)
    if side not in ("buy", "sell") or confidence < min_confidence:
        return None

    candidate_lookup = {c["instId"]: c for c in candidates}
    cand = candidate_lookup.get(inst) or candidates[0]
    fallback_plan = pick_best_affordable(scan, account, margin_budget=float(cand.get("margin_budget") or 0), inst_id=inst, side=side)
    if fallback_plan:
        cand = {
            "instId": fallback_plan.inst_id,
            "side": fallback_plan.side,
            "score": float(cand.get("score") or score),
            "confidence": confidence,
            "margin_budget": float(fallback_plan.margin or 0),
            "est_margin": fallback_plan.margin,
            "min_leverage": fallback_plan.leverage,
            "price": fallback_plan.price,
            "sizing": {
                "margin_budget": fallback_plan.margin,
                "est_margin": fallback_plan.margin,
                "contracts": fallback_plan.contracts,
            },
        }

    allowed, guard_reason = validate_open(state, account, {
        "action": "open",
        "instId": cand.get("instId") or inst,
        "side": side,
        "confidence": confidence,
    })
    if not allowed:
        log_event("trade", "Edge open blocked", guard_reason, {"instId": cand.get("instId") or inst, "side": side})
        return None

    decision = {
        "research": f"Edge candidate: {(cand.get('instId') or inst)} score={cand.get('score')} confidence={confidence} margin={cand.get('est_margin')}",
        "strategy_update": "Edge-driven open selected by compact LLM decision",
        "action": "open",
        "instId": cand.get("instId") or inst,
        "side": side,
        "size_contracts": (cand.get("sizing") or {}).get("contracts") if isinstance(cand.get("sizing"), dict) else None,
        "tp_pct": 2.0,
        "sl_pct": 1.0,
        "confidence": confidence,
        "reasoning": parsed.get("reasoning") or "Edge candidate from compact LLM decision",
        "edge_candidate": cand,
        "source": "edge_driver",
    }
    return EdgeDecision(decision)
