"""Repair Agent 3 — Order Guardian.

Monitors orders and positions like a trade-floor technician:
- Stuck orders, missing TP/SL, rejections, margin errors, inconsistent account state.
Uses the full repair action catalog (retry_close, retry_tpsl, ensure_net_mode, etc.).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from activity_log import get_recent
from trader.repair import REPAIR_ACTION_CATALOG
from trader.repair_agent import (
    TECHNICIAN_METHOD,
    agent_main,
    execute_catalog_actions,
    get_log_tail,
    log,
    log_err,
    log_warn,
    maybe_autorepair_global,
    llm_ask,
    parse_llm_json,
    check_account_early_and_repair,
    check_dashboard_sections_and_repair,
    triage_with_repair_engine,
)

AGENT_NAME = "order_guardian"
LABEL = "Repair[OrderGuardian]"
LOOP_SEC = 30.0

ORDER_GUARDIAN_SYSTEM = (
    "You are the LLM KnightTrader **Order Guardian** — a trade operations technician.\n\n"
    + TECHNICIAN_METHOD
    + "\n\n"
    "You monitor open orders and positions:\n"
    "- Stuck orders that never fill\n"
    "- Positions missing TP/SL protection\n"
    "- Order rejections (margin 103003, position mode 102089, etc.)\n"
    "- Inconsistent account state\n\n"
    "Respond ONLY with valid JSON (no markdown):\n"
    "{\n"
    '  "diagnosis": "What you found (max 200 chars)",\n'
    '  "root_cause": "Best guess (max 120 chars)",\n'
    '  "confidence": 0-100,\n'
    '  "actions": [\n'
    '    {"type": "<from repair_action_catalog>", "params": {}}\n'
    "  ],\n"
    '  "retry_original": true|false,\n'
    '  "lesson": {"category": "order_ops", "text": "..."} or null\n'
    "}\n\n"
    "If no issue: {\"diagnosis\":\"all orders healthy\",\"confidence\":100,\"actions\":[],\"retry_original\":false}\n\n"
    "RULES:\n"
    "- ONLY use action types from repair_action_catalog.\n"
    "- refresh_account BEFORE retry_close/retry_open/retry_tpsl.\n"
    "- For 103003 margin errors: raise_leverage_retry_open or redirect_open.\n"
    "- For 102089: ensure_net_mode first.\n"
    "- Max 5 actions. Prefer hold when confidence < 50.\n"
)


def _get_account_snapshot(client) -> dict:
    try:
        from blofin.account_cache import get_account_snapshot

        return get_account_snapshot(force=False) or {}
    except Exception:
        try:
            return client.parse_account_snapshot(force=False) or {}
        except Exception:
            return {}


def _get_recent_trade_errors() -> list[dict]:
    results = []
    for event in get_recent(50):
        if event.get("type") not in ("error", "trade"):
            continue
        title = (event.get("title") or "").lower()
        detail = (event.get("detail") or "").lower()
        if any(
            k in title or k in detail
            for k in (
                "order",
                "open",
                "close",
                "tpsl",
                "fill",
                "reject",
                "margin",
                "lever",
                "position",
                "103003",
                "102089",
                "102022",
            )
        ):
            if "repair" not in title and "watchdog" not in title:
                results.append(event)
    return results[-10:]


def _trade_fingerprint(account: dict, errors: list[dict]) -> str:
    parts = []
    for pos in account.get("positions") or []:
        inst = pos.get("instId", "?")
        upl = pos.get("upl", pos.get("unrealizedPnl", 0))
        parts.append(f"pos:{inst}:{upl}")
        # Make missing-tpsl visible to the fingerprint so we can retry when
        # the missing set changes.
        has_tp = bool(pos.get("tp") or pos.get("tpTriggerPx"))
        has_sl = bool(pos.get("sl") or pos.get("slTriggerPx"))
        if not has_tp or not has_sl:
            parts.append(f"missing_tpsl:{inst}")
    for e in errors[-3:]:
        parts.append(f"err:{e.get('title', '')[:30]}")
    return "|".join(parts) if parts else "ok"


def run_cycle(client, llm, state: dict) -> None:
    # Early warning net: fix corrupted dashboard account numbers ASAP.
    try:
        check_account_early_and_repair(
            client,
            llm,
            state,
            label=LABEL,
        )
    except Exception as exc:
        log_warn(LABEL, "Early account check crashed", str(exc)[:200])

    # Dashboard sections scanner: failed trades / invalid last decisions.
    try:
        check_dashboard_sections_and_repair(
            client,
            llm,
            state,
            label=LABEL,
        )
    except Exception as exc:
        log_warn(LABEL, "Dashboard section scan crashed", str(exc)[:200])

    account = _get_account_snapshot(client)
    if not account:
        return

    positions = account.get("positions", [])
    errors = _get_recent_trade_errors()
    fp = _trade_fingerprint(account, errors)

    missing_positions: list[dict] = []
    position_lines = []
    for pos in positions[:10]:
        inst_id = pos.get("instId", "?")
        side = pos.get("tdSide", pos.get("side", "?"))
        sz = pos.get("sz", pos.get("contracts", pos.get("size", "?")))
        lever = pos.get("lever", "?")
        upl = pos.get("upl", pos.get("unrealizedPnl", 0))
        has_tp = bool(pos.get("tp") or pos.get("tpTriggerPx"))
        has_sl = bool(pos.get("sl") or pos.get("slTriggerPx"))
        if not has_tp or not has_sl:
            missing_positions.append(pos)
        position_lines.append(
            f"  {inst_id} {side} sz={sz} lever={lever} upl={upl} tp={has_tp} sl={has_sl}"
        )

    if not errors and not missing_positions and len(positions) <= 5:
        return

    seen = state.setdefault("trade_fingerprints", {})
    if isinstance(seen, list):
        seen = {}
        state["trade_fingerprints"] = seen
    if time.time() - float(seen.get(fp) or 0) < 60.0:
        return

    state["repairs_attempted"] += 1
    lines = [
        f"equity={account.get('equity')} available={account.get('available')} "
        f"positions={len(positions)} stale={account.get('stale')} "
        f"rate_limited={account.get('rate_limited')}",
        f"\nrepair_action_catalog: {json.dumps(REPAIR_ACTION_CATALOG)[:2000]}",
    ]
    if positions:
        lines.append("positions:")
        lines.extend(position_lines)
    if errors:
        lines.append(f"\nrecent trade errors ({len(errors)}):")
        for e in errors[-5:]:
            lines.append(f"  [{e.get('title', '')}] {e.get('detail', '')[:180]}")
    trade_log = get_log_tail("trader.err", 25)
    if trade_log:
        lines.append(f"\ntrader.err tail:\n{trade_log[-500:]}")

    user_msg = "\n".join(lines)[:6000]
    answer = llm_ask(AGENT_NAME, llm, ORDER_GUARDIAN_SYSTEM, user_msg, max_tokens=900)

    if answer:
        try:
            plan = parse_llm_json(answer)
            actions = plan.get("actions") or []
            if actions and int(plan.get("confidence") or 0) >= 50:
                taken = execute_catalog_actions(client, llm, state, actions, label=LABEL)
                if taken:
                    state["repairs_succeeded"] += 1
                    seen[fp] = time.time()
                    log(LABEL, "Catalog actions", "; ".join(taken)[:300])
                    return
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            log_warn(LABEL, "LLM parse failed, escalating", str(exc)[:120])

    # Escalate to full repair engine for trade errors
    if errors:
        last = errors[-1]
        detail = str(last.get("detail") or "")
        phase = "open_failed"
        if "close" in detail.lower():
            phase = "close_failed"
        elif "tpsl" in detail.lower() or "tp" in detail.lower():
            phase = "tpsl_failed"
        elif "reject" in detail.lower():
            phase = "open_rejected"

        incident = {
            "phase": phase,
            "error": detail[:400],
            "title": last.get("title", ""),
            "instId": (positions[0].get("instId") if positions else None),
        }
        result = triage_with_repair_engine(client, llm, state, incident, label=LABEL)
        if result and (result.recovered or result.actions_taken):
            state["repairs_succeeded"] += 1
            seen[fp] = time.time()
        return

    if missing_positions:
        # Aggressive but bounded: fix up to 3 missing positions per cycle.
        # Deterministic repair plan will refresh account + retry_tpsl safely.
        to_fix = missing_positions[:3]
        recovered_any = False
        for pos in to_fix:
            inst = pos.get("instId", "")
            if not inst:
                continue
            # Normalize TP/SL params:
            # - dashboard/account caches use "long"/"short"
            # - TP/SL attach expects "buy"/"sell"
            # - exchange expects a positive contracts/order size
            side_raw = str(pos.get("tdSide", pos.get("side", "buy")) or "").strip().lower()
            if side_raw in ("buy", "long"):
                side = "buy"
            elif side_raw in ("sell", "short"):
                side = "sell"
            else:
                side = side_raw or "buy"

            sz_raw = pos.get("sz", pos.get("contracts", pos.get("size", "")))
            try:
                sz_val = float(sz_raw)
                sz_abs = abs(sz_val)
            except (TypeError, ValueError):
                sz_abs = 0.0
            if hasattr(client, "_format_order_size") and sz_abs > 0:
                contracts = client._format_order_size(inst, sz_abs)
            else:
                contracts = str(sz_abs if sz_abs > 0 else sz_raw)

            incident = {
                "phase": "tpsl_failed",
                "error": f"missing TP/SL on {inst}",
                "instId": inst,
                "side": side,
                "contracts": contracts,
            }
            result = triage_with_repair_engine(client, llm, state, incident, label=LABEL)
            if result and result.recovered:
                recovered_any = True

        if recovered_any:
            state["repairs_succeeded"] += 1
            seen[fp] = time.time()

    # Final safety net: if order ops didn't recover but novel errors exist,
    # let the full repair engine triage.
    try:
        maybe_autorepair_global(
            client,
            llm,
            state,
            label=LABEL,
            max_incidents=1,
            cooldown_sec=240.0,
        )
    except Exception as exc:
        log_warn(LABEL, "Global autorepair failed", str(exc)[:200])


if __name__ == "__main__":
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
    )
