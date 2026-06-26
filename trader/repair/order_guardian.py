"""Repair Agent 3 — Order Guardian.

A master risk management and order operations diagnostic technician.
Monitors orders, positions, and account state to find and fix issues.

Can:
- Read account state (balance, positions, PnL)
- Detect stuck/unfilled orders
- Identify positions missing TP/SL
- Close problematic positions
- Check BloFin API connectivity
- Read source code to understand order flow
- Edit code to fix order handling bugs
- Restart modules after fixes
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trader.repair_agent import (
    agent_main,
    get_log_tail,
    get_recent_errors,
    log,
    log_err,
    log_warn,
    llm_ask,
    read_file_safe,
    run_cmd,
    write_file_safe,
)

AGENT_NAME = "order_guardian"
LABEL = "Repair[OrderGuardian]"
LOOP_SEC = 30.0

SYSTEM = """You are the LLM KnightTrader **Order Guardian** — a risk management diagnostic technician.

You monitor orders and positions like a mechanic listens for engine knocks.
You detect: stuck orders, missing TP/SL, margin issues, API errors, tracking bugs.

## YOUR TOOLS
- Read any file: read_file_safe(path)
- Edit files: write_file_safe(path, content)
- Check account: client.parse_account_snapshot(force=True)
- Get positions: account.get("positions", [])
- Close position: client.close_position({"instId": "..."})
- Check API: client.get_balance()
- Read logs: get_log_tail("trader.err", 50)
- Get errors: get_recent_errors(50)
- Run commands: run_cmd(["command", "arg1"], timeout=10)
- List source files: list_source_files()

## WHAT TO CHECK
1. Get account snapshot — check equity, available balance, positions
2. Check for positions missing TP/SL protection
3. Check for recent trade errors in activity log
4. Check trader error log for order-related issues
5. If you see order flow bugs, read the relevant source (order_guard.py, agent.py, etc.)

## RESPONSE FORMAT
Respond ONLY with valid JSON (no markdown):
{
  "healthy": true|false,
  "diagnosis": "What you found (max 300 chars)",
  "reasoning": "Your diagnostic steps (max 500 chars)",
  "actions": [
    {"type": "close_position", "instId": "BTC-USDT-SWAP", "reason": "anomalous"},
    {"type": "read_file", "path": "trader/order_guard.py"},
    {"type": "edit_file", "path": "trader/order_guard.py", "search": "exact text", "replace": "new text"},
    {"type": "restart_trader"},
    {"type": "hold", "reason": "observing"}
  ],
  "confidence": 0-100
}

If everything is fine: {"healthy":true,"diagnosis":"all orders healthy","reasoning":"","actions":[],"confidence":100}

RULES:
- Only close positions that are truly anomalous or at risk
- Max 3 actions per cycle
- Prefer hold over aggressive action when uncertain
- If editing code, search must match EXACTLY
- If you see repeated order rejections, check leverage/margin settings in code
- If you see TP/SL failures, check the tpsl.py source
"""


def _get_account(client) -> dict:
    try:
        return client.parse_account_snapshot(force=False)
    except Exception:
        return {}


def _execute_action(action: dict, client, state: dict) -> str:
    atype = action.get("type", "")
    if atype == "close_position":
        inst_id = action.get("instId", "")
        reason = action.get("reason", "guardian action")
        if inst_id:
            try:
                result = client.close_position({"instId": inst_id})
                state["repairs_succeeded"] += 1
                return f"closed {inst_id}: {result}"
            except Exception as exc:
                return f"close failed {inst_id}: {exc}"
        return "no instId"
    elif atype == "read_file":
        path = action.get("path", "")
        content = read_file_safe(ROOT / path) if path else None
        return f"read {path}: {len(content)} chars" if content else f"read {path}: not found"
    elif atype == "edit_file":
        path = action.get("path", "")
        search = action.get("search", "")
        replace = action.get("replace", "")
        if path and search and replace:
            file_path = ROOT / path
            source = read_file_safe(file_path)
            if source and search in source:
                new_source = source.replace(search, replace, 1)
                if write_file_safe(file_path, new_source):
                    state["repairs_succeeded"] += 1
                    return f"edited {path}"
                return f"edit failed"
            return f"search not found in {path}"
        return "edit skipped"
    elif atype == "restart_trader":
        from trader.stack_control import start_single_trader
        result = start_single_trader()
        if result.get("ok"):
            state["repairs_succeeded"] += 1
        return f"restart: {result}"
    elif atype == "run_command":
        cmd = action.get("cmd", [])
        if cmd:
            rc, out = run_cmd(cmd, timeout=action.get("timeout", 10))
            return f"cmd {' '.join(cmd[:3])}: rc={rc} out={out[:100]}"
        return "no command"
    elif atype == "hold":
        return f"holding: {action.get('reason', 'observing')}"
    return f"unknown action: {atype}"


def run_cycle(client, llm, state: dict) -> None:
    now = time.time()
    account = _get_account(client)
    if not account:
        return

    positions = account.get("positions", [])
    errors = get_recent_errors(40)

    # Filter to trade/order related errors
    trade_errors = []
    for e in errors:
        title = (e.get("title") or "").lower()
        detail = (e.get("detail") or "").lower()
        if any(k in title or k in detail for k in (
            "order", "open", "close", "tpsl", "fill", "reject",
            "margin", "lever", "position", "103003", "102089",
            "insufficient", "balance",
        )):
            if "repair" not in title and "watchdog" not in title:
                trade_errors.append(e)

    equity = account.get("equity", 0)
    available = account.get("available", 0)
    position_count = len(positions)

    # Check for missing TP/SL
    missing_tpsl = []
    for pos in positions:
        has_tp = pos.get("tp") is not None or pos.get("tpTriggerPx")
        has_sl = pos.get("sl") is not None or pos.get("slTriggerPx")
        if not has_tp or not has_sl:
            missing_tpsl.append(pos.get("instId", "?"))

    # Everything normal
    if not trade_errors and not missing_tpsl and position_count <= 10:
        return

    state["repairs_attempted"] += 1

    lines = [
        f"equity={equity} available={available} positions={position_count}",
    ]
    if positions:
        lines.append("positions:")
        for pos in positions[:10]:
            inst_id = pos.get("instId", "?")
            side = pos.get("tdSide", pos.get("side", "?"))
            sz = pos.get("sz", pos.get("contracts", "?"))
            lever = pos.get("lever", "?")
            upl = pos.get("upl", pos.get("unrealizedPnl", 0))
            has_tp = pos.get("tp") is not None or pos.get("tpTriggerPx")
            has_sl = pos.get("sl") is not None or pos.get("slTriggerPx")
            lines.append(f"  {inst_id} {side} sz={sz} lever={lever} upl={upl} tp={has_tp} sl={has_sl}")

    if missing_tpsl:
        lines.append(f"\npositions missing TP/SL: {', '.join(missing_tpsl)}")

    if trade_errors:
        lines.append(f"\nrecent trade errors ({len(trade_errors)}):")
        for e in trade_errors[-5:]:
            lines.append(f"  [{e.get('title','')}] {e.get('detail','')[:200]}")

    # Include trader error log
    trader_err = get_log_tail("trader.err", 30)
    if trader_err:
        lines.append(f"\ntrader.err:\n{trader_err[:400]}")

    user_msg = "\n".join(lines)
    answer = llm_ask(AGENT_NAME, llm, SYSTEM, user_msg, max_tokens=1200)

    if not answer:
        log_warn(LABEL, "LLM unavailable", f"errors={len(trade_errors)} missing_tpsl={len(missing_tpsl)}")
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

    if plan.get("healthy"):
        return

    diagnosis = plan.get("diagnosis", "")
    reasoning = plan.get("reasoning", "")
    actions = plan.get("actions", [])
    confidence = plan.get("confidence", 0)

    if confidence < 60:
        log(LABEL, f"Low confidence ({confidence}%)", diagnosis)
        return

    actions_taken = []
    for action in actions:
        result = _execute_action(action, client, state)
        actions_taken.append(f"{action.get('type','')}: {result}")

    log(LABEL, f"Action (confidence={confidence}%)", diagnosis)
    if reasoning:
        log(LABEL, "Reasoning", reasoning[:300])
    if actions_taken:
        log(LABEL, "Actions", "; ".join(actions_taken))


if __name__ == "__main__":
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
    )
