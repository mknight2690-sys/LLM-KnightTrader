"""Repair Agent 3 — Order Guardian.

Monitors open orders and positions to ensure they are filled correctly:
- Detects stuck orders that never fill
- Identifies positions with missing TP/SL
- Handles order rejections and API errors during execution
- Closes positions that are anomalous or at risk
- Ensures the account state is consistent
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
from trader.repair_agent import (
    agent_main,
    log,
    log_err,
    log_warn,
    llm_ask,
)

AGENT_NAME = "order_guardian"
LABEL = "Repair[OrderGuardian]"
LOOP_SEC = 30.0

ORDER_GUARDIAN_SYSTEM = (
    "You are the LLM KnightTrader **Order Guardian** — a risk management and order operations agent.\n\n"
    "You monitor open orders and positions to ensure they are handled correctly:\n"
    "- Stuck orders that never fill after a reasonable time\n"
    "- Positions missing TP/SL protection\n"
    "- Order rejections due to margin, leverage, or API issues\n"
    "- Inconsistent account state (position count mismatch)\n"
    "- Unusual PnL that suggests a position tracking bug\n\n"
    "You are ONLY called when there is a potential order/position issue.\n\n"
    "Respond ONLY with valid JSON (no markdown):\n"
    "{\n"
    '  "issue_found": true|false,\n'
    '  "severity": "ok|warning|critical",\n'
    '  "diagnosis": "What you found (max 200 chars)",\n'
    '  "actions": [\n'
    '    {"type": "close_position", "params": {"instId": "BTC-USDT-SWAP", "reason": "..."}},\n'
    '    {"type": "retry_order", "params": {"instId": "...", "side": "buy", "sz": "0.01", "lever": "3"}},\n'
    '    {"type": "attach_tpsl", "params": {"instId": "...", "tp_pct": "5", "sl_pct": "3"}},\n'
    '    {"type": "hold", "params": {"reason": "waiting for fill"}}\n'
    '  ],\n'
    '  "lesson": {"category": "order_ops", "text": "Durable lesson"} or null\n'
    "}\n\n"
    "If no issue: {\"issue_found\":false,\"severity\":\"ok\",\"diagnosis\":\"all orders healthy\"}\n\n"
    "RULES:\n"
    "- Only act on real issues you can see in the data.\n"
    "- close_position only for anomalous or at-risk positions.\n"
    "- Max 3 actions per check.\n"
    "- Prefer hold over aggressive action when uncertain.\n"
)


def _get_account_snapshot(client) -> dict:
    """Get current account state from client."""
    try:
        return client.parse_account_snapshot(force=False)
    except Exception:
        return {}


def _get_recent_trade_errors() -> list[dict]:
    """Find recent trade-related errors in the activity log."""
    results = []
    for event in get_recent(40):
        if event.get("type") not in ("error", "trade"):
            continue
        title = (event.get("title") or "").lower()
        detail = (event.get("detail") or "").lower()
        if any(k in title or k in detail for k in (
            "order", "open", "close", "tpsl", "fill", "reject",
            "margin", "lever", "position", "103003", "102089",
        )):
            if "repair" not in title and "watchdog" not in title:
                results.append(event)
    return results[-10:]


def _execute_close(client, inst_id: str, reason: str) -> bool:
    """Close a position by instrument ID."""
    try:
        result = client.close_position({"instId": inst_id})
        log(LABEL, "Position closed", f"{inst_id}: {reason}")
        return True
    except Exception as exc:
        log_err(LABEL, "Close failed", f"{inst_id}: {exc}")
        return False


def run_cycle(client, llm, state: dict) -> None:
    now = time.time()
    account = _get_account_snapshot(client)
    if not account:
        return

    positions = account.get("positions", [])
    errors = _get_recent_trade_errors()

    equity = account.get("equity", 0)
    available = account.get("available", 0)
    position_count = len(positions)

    if not positions and not errors:
        return

    has_missing_tpsl = False
    position_lines = []
    for pos in positions[:10]:
        inst_id = pos.get("instId", "?")
        side = pos.get("tdSide", pos.get("side", "?"))
        sz = pos.get("sz", pos.get("contracts", "?"))
        lever = pos.get("lever", "?")
        upl = pos.get("upl", pos.get("unrealizedPnl", 0))
        has_tp = pos.get("tp") is not None or pos.get("tpTriggerPx")
        has_sl = pos.get("sl") is not None or pos.get("slTriggerPx")
        if not has_tp or not has_sl:
            has_missing_tpsl = True
        position_lines.append(
            f"  {inst_id} {side} sz={sz} lever={lever} upl={upl} tp={has_tp} sl={has_sl}"
        )

    if not errors and not has_missing_tpsl and position_count <= 5:
        return

    state["repairs_attempted"] += 1
    lines = [
        f"equity={equity} available={available} positions={position_count}",
    ]
    if positions:
        lines.append("positions:")
        lines.extend(position_lines)
    if errors:
        lines.append(f"\nrecent trade errors ({len(errors)}):")
        for e in errors[-3:]:
            lines.append(f"  [{e.get('title','')}] {e.get('detail','')[:150]}")

    user_msg = "\n".join(lines)
    answer = llm_ask(AGENT_NAME, llm, ORDER_GUARDIAN_SYSTEM, user_msg, max_tokens=500)

    if not answer:
        log_warn(LABEL, "LLM unavailable for order check", f"positions={position_count}")
        return

    try:
        plan = json.loads(answer)
    except (json.JSONDecodeError, ValueError):
        stripped = answer.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
            if stripped.endswith("```"):
                stripped = stripped[:-3]
            plan = json.loads(stripped.strip())
        else:
            log_err(LABEL, "Could not parse LLM response", answer[:200])
            return

    if not plan.get("issue_found"):
        return

    severity = plan.get("severity", "warning")
    diagnosis = plan.get("diagnosis", "")
    actions = plan.get("actions", [])
    actions_taken = []

    for action in actions:
        atype = action.get("type", "")
        params = action.get("params", {})
        if atype == "close_position":
            inst_id = params.get("instId", "")
            reason = params.get("reason", "guardian action")
            if inst_id:
                _execute_close(client, inst_id, reason)
                actions_taken.append(f"closed {inst_id}")
        elif atype == "hold":
            reason = params.get("reason", "")
            actions_taken.append(f"hold:{reason[:60]}")

    state["last_action_ts"] = now
    if actions_taken:
        state["repairs_succeeded"] += 1
        log(LABEL, f"Order action (severity={severity})", f"actions: {'; '.join(actions_taken)} | {diagnosis}")
    else:
        log(LABEL, f"No order action (severity={severity})", diagnosis)


if __name__ == "__main__":
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
    )
