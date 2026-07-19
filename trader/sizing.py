"""Intelligent per-trade margin sizing — conviction, sentiment, available margin."""

from __future__ import annotations

from typing import Any

from config import MARGIN_USE_RATIO, MAX_POSITIONS, TEST_ACCOUNT_EQUITY

# Smallest margin slice we'll attempt an open with (exchange min varies by inst).
ABSOLUTE_MIN_MARGIN = 0.05


def _open_count(account: dict[str, Any]) -> int:
    n = 0
    for pos in account.get("positions") or []:
        size = abs(float(pos.get("size") or pos.get("positions") or 0))
        if size > 0:
            n += 1
    return n


def _risk_available(account: dict[str, Any]) -> float:
    """Cap sizing to the $40-style test equity even if demo wallet is larger."""
    available = float(account.get("available") or 0)
    equity = float(account.get("equity") or available)
    risk_equity = min(equity, float(TEST_ACCOUNT_EQUITY or equity or 0)) if TEST_ACCOUNT_EQUITY else equity
    # Never size off more cash than is actually free.
    return max(0.0, min(available, risk_equity))


def sentiment_strength(score: float) -> float:
    """Normalize scan score (typically |3|–|10+|) to 0.3–1.0."""
    return min(1.0, max(0.3, abs(float(score or 0)) / 8.0))


def conviction_factor(confidence: float, *, default: float = 65.0) -> float:
    """LLM confidence 0–100 → 0.35–1.0 sizing weight."""
    c = float(confidence) if confidence and float(confidence) > 0 else default
    return max(0.35, min(1.0, c / 100.0))


def portfolio_heat_factor(open_count: int) -> float:
    """Soft dilution as book grows — not a hard position cap."""
    return 1.0 / (1.0 + 0.25 * max(0, open_count))


def margin_budget_for_setup(
    account: dict[str, Any],
    *,
    confidence: float = 65.0,
    score: float = 3.0,
    open_count: int | None = None,
) -> dict[str, Any]:
    """
    Size each open from conviction × sentiment × available margin × portfolio heat.
    Hard caps: MAX_POSITIONS and TEST_ACCOUNT_EQUITY ($40-style risk envelope).
    """
    available = _risk_available(account)
    if open_count is None:
        open_count = _open_count(account)
    if MAX_POSITIONS > 0 and open_count >= MAX_POSITIONS:
        return {
            "margin_budget": 0.0,
            "available_margin": round(available, 4),
            "deployable_pool": 0.0,
            "conviction_factor": 0.0,
            "sentiment_factor": 0.0,
            "portfolio_heat": 0.0,
            "combined_weight": 0.0,
            "open_position_count": open_count,
            "blocked": "max_positions",
        }

    pool = available * MARGIN_USE_RATIO
    sent = sentiment_strength(score)
    conv = conviction_factor(confidence)
    heat = portfolio_heat_factor(open_count)

    # Stronger conviction + sharper sentiment → larger slice of the pool.
    weight = conv * (0.45 + 0.55 * sent)
    budget = pool * weight * heat

    # Per-trade ceiling: max 12%–25% of deployable pool (was up to 50%).
    cap = pool * (0.12 + 0.13 * conv)
    # Absolute small-account ceiling: never more than 12.5% of the $40 risk envelope.
    abs_cap = max(0.05, 0.125 * float(TEST_ACCOUNT_EQUITY or available or 40.0))
    budget = min(budget, cap, abs_cap)

    return {
        "margin_budget": round(max(0.0, budget), 4),
        "available_margin": round(available, 4),
        "deployable_pool": round(pool, 4),
        "conviction_factor": round(conv, 3),
        "sentiment_factor": round(sent, 3),
        "portfolio_heat": round(heat, 3),
        "combined_weight": round(weight, 3),
        "open_position_count": open_count,
        "risk_equity_cap": float(TEST_ACCOUNT_EQUITY or 0),
    }


def scan_score_for(account: dict[str, Any], inst_id: str) -> float:
    for row in account.get("_scan") or []:
        if str(row.get("instId") or "") == inst_id:
            return float(row.get("score") or 0)
    return 0.0


def inferred_confidence_from_score(score: float) -> float:
    """When LLM confidence absent, infer from scan strength."""
    return min(95.0, 58.0 + abs(float(score or 0)) * 4.0)


def budget_for_decision(account: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    inst = str(decision.get("instId") or "")
    score = scan_score_for(account, inst)
    conf = float(decision.get("confidence") or 0)
    if conf <= 0:
        conf = inferred_confidence_from_score(score)
    return margin_budget_for_setup(account, confidence=conf, score=score)


def budget_for_scan_row(account: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    score = float(row.get("score") or 0)
    conf = inferred_confidence_from_score(score)
    return margin_budget_for_setup(account, confidence=conf, score=score)
