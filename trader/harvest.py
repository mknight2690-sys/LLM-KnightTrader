"""BloHunter-style winner harvesting — only close at +NTP% or better."""

from __future__ import annotations

from typing import Any

from config import BLOHUNTER_HARVEST_NTP_PCT

ANOMALY_KEYWORDS = (
    "bug",
    "not in harvestable",
    "harvestable list",
    "should harvest",
    "possible bug",
    "mismatch",
    "not harvesting",
)


def enrich_positions_for_harvest(
    positions: list[dict[str, Any]],
    positions_raw: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Merge exchange unrealizedPnlRatio / initialMargin into position rows for harvest."""
    raw_by: dict[str, dict[str, Any]] = {}
    if positions_raw:
        for row in positions_raw.get("data") or []:
            inst = str(row.get("instId") or "")
            if inst:
                raw_by[inst] = row

    enriched: list[dict[str, Any]] = []
    for pos in positions:
        row = dict(pos)
        inst = str(row.get("instId") or "")
        raw = raw_by.get(inst, {})
        ratio = float(raw.get("unrealizedPnlRatio") or row.get("upl_ratio") or 0)
        margin = float(raw.get("initialMargin") or row.get("initial_margin") or 0)
        if ratio != 0:
            row["upl_ratio"] = ratio
            row["ntp_pct"] = ratio * 100.0
        if margin > 0:
            row["initial_margin"] = margin
        if raw.get("markPrice"):
            row["mark"] = float(raw["markPrice"])
        if raw.get("averagePrice"):
            row["entry"] = float(raw["averagePrice"])
        if raw.get("unrealizedPnl") not in (None, ""):
            row["upl"] = float(raw.get("unrealizedPnl") or 0)
        sz = raw.get("positions")
        if sz not in (None, ""):
            row["size"] = float(sz)
        enriched.append(row)
    return enriched


def position_pnl_pct(pos: dict[str, Any]) -> float:
    """NTP % = unrealized ROI on margin (BloFin unrealizedPnlRatio), not raw price move."""
    if pos.get("ntp_pct") is not None:
        return float(pos["ntp_pct"])

    ratio = float(pos.get("upl_ratio") or pos.get("unrealizedPnlRatio") or 0)
    if ratio != 0:
        return ratio * 100.0

    margin = float(pos.get("initial_margin") or pos.get("initialMargin") or pos.get("imr") or 0)
    upl = float(pos.get("upl") or pos.get("unrealizedPnl") or 0)
    if margin > 0:
        return upl / margin * 100.0

    entry = float(pos.get("entry") or pos.get("avgPx") or 0)
    mark = float(pos.get("mark") or pos.get("markPrice") or 0)
    if entry > 0 and mark > 0:
        raw_size = float(pos.get("size") or pos.get("positions") or 0)
        if raw_size < 0:
            return (entry - mark) / entry * 100.0
        return (mark - entry) / entry * 100.0

    notional = abs(float(pos.get("size") or 0)) * mark if mark else 0
    if notional > 0 and upl != 0:
        return upl / notional * 100.0
    return 0.0


def harvestable_winner(pos: dict[str, Any], *, ntp_pct: float | None = None) -> bool:
    floor = float(ntp_pct if ntp_pct is not None else BLOHUNTER_HARVEST_NTP_PCT)
    return position_pnl_pct(pos) >= floor


def list_harvestable(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pos in positions:
        pct = position_pnl_pct(pos)
        if harvestable_winner(pos):
            row = dict(pos)
            row["pnl_pct"] = round(pct, 2)
            out.append(row)
    return out


def list_held_losers(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in positions if position_pnl_pct(p) < BLOHUNTER_HARVEST_NTP_PCT]


def detect_harvest_gaps(
    positions: list[dict[str, Any]],
    positions_raw: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Winners above NTP floor that harvest logic would miss — must be closed proactively."""
    enriched = enrich_positions_for_harvest(positions, positions_raw)
    listed = {str(p.get("instId")) for p in list_harvestable(enriched)}
    gaps: list[dict[str, Any]] = []
    for pos in enriched:
        inst = str(pos.get("instId") or "")
        ntp = position_pnl_pct(pos)
        if ntp >= BLOHUNTER_HARVEST_NTP_PCT and inst not in listed:
            row = dict(pos)
            row["pnl_pct"] = round(ntp, 2)
            gaps.append(row)
    return gaps


def decision_reports_anomaly(decision: dict[str, Any] | None) -> bool:
    if not decision:
        return False
    text = f"{decision.get('research', '')} {decision.get('reasoning', '')} {decision.get('strategy_update', '')}".lower()
    return any(keyword in text for keyword in ANOMALY_KEYWORDS)


def can_close_position(
    pos: dict[str, Any],
    account: dict[str, Any],
    *,
    emergency: bool = False,
) -> tuple[bool, str]:
    """BloHunter: only harvest winners at +NTP%; hold losers and sub-floor greens."""
    pct = position_pnl_pct(pos)
    inst = str(pos.get("instId") or "")

    if harvestable_winner(pos):
        return True, f"harvest winner {inst} at +{pct:.2f}% NTP (>= {BLOHUNTER_HARVEST_NTP_PCT}%)"

    if emergency:
        return True, f"margin emergency close {inst} at {pct:.2f}%"

    if pct >= 0:
        return False, f"{inst} +{pct:.2f}% NTP below +{BLOHUNTER_HARVEST_NTP_PCT}% floor — let run"
    return False, f"{inst} {pct:.2f}% loser — BloHunter holds losers, no harvest"


def margin_stress(account: dict[str, Any]) -> bool:
    equity = float(account.get("equity") or 0)
    available = float(account.get("available") or 0)
    if equity <= 0:
        return False
    return available / equity < 0.15


def harvest_context(positions: list[dict[str, Any]], positions_raw: dict[str, Any] | None = None) -> dict[str, Any]:
    enriched = enrich_positions_for_harvest(positions, positions_raw)
    harvest = list_harvestable(enriched)
    held = list_held_losers(enriched)
    gaps = detect_harvest_gaps(enriched, positions_raw)
    return {
        "ntp_harvest_floor_pct": BLOHUNTER_HARVEST_NTP_PCT,
        "harvestable_winners": [
            {"instId": p.get("instId"), "pnl_pct": p.get("pnl_pct", position_pnl_pct(p))}
            for p in harvest
        ],
        "held_losers_subfloor": [
            {"instId": p.get("instId"), "pnl_pct": round(position_pnl_pct(p), 2)}
            for p in held
        ],
        "position_ntp": [
            {
                "instId": p.get("instId"),
                "ntp_pct": round(position_pnl_pct(p), 2),
                "harvestable": harvestable_winner(p),
            }
            for p in enriched
        ],
        "harvest_gaps": [
            {"instId": p.get("instId"), "pnl_pct": p.get("pnl_pct", position_pnl_pct(p))}
            for p in gaps
        ],
        "policy": "Only close harvestable_winners (+NTP% margin ROI or better). Never clip sub-floor greens or harvest losers.",
    }
