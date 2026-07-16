"""Main autonomous trading loop driven by cloud LLM."""

from __future__ import annotations

import json
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Ensure project root on path when run as script
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from activity_log import log_event, get_recent, load_history
from blofin.client import BlofinClient
from config import APP_NAME, ACCOUNT_REFRESH_SEC, MISSION_PROMPT, TARGET_EQUITY, TRADE_MAX_LEVERAGE, TRADE_MODE, TRADER_LOOP_SEC, OPEN_CONFIDENCE_FLOOR, apply_best_params
from llm.wrapper import LLMWrapper
from trader.prompts import DEFAULT_USER_DIRECTIVES, TRADER_SYSTEM
from trader.blohunter_knowledge import load_blohunter_tactics
from trader.learning import learn_from_activity_tail, learn_from_cycle, lessons_digest
from trader.margin import affordable_setups, annotate_scan_row, format_contracts, pick_best_affordable, plan_open
from trader.health import acquire_trader_lock, normalize_decision, release_trader_lock
from trader.optimized_params import best_params_context, load_best_params
from trader.repair import (
    llm_triage_and_repair,
    record_llm_failure,
    record_llm_success,
    repair_cycle_crash,
    repair_llm_pool,
    repair_stack,
    stack_health_snapshot,
)
from trader.pnl_tracker import capture_close_pnl, record_open_entry
from trader.baseline import (
    baseline_context_for_llm,
    ensure_baseline_armed,
    snapshot_if_due,
    try_detect_injection,
    update_baseline_peak,
)
from trader.directives import operator_instructions
from trader.order_guard import (
    apply_open_guard,
    bootstrap_order_guard_from_trades,
    execution_context,
    record_execution,
    validate_open,
)
from trader.harvest import (
    can_close_position,
    decision_reports_anomaly,
    detect_harvest_gaps,
    enrich_positions_for_harvest,
    list_harvestable,
    position_pnl_pct,
    margin_stress,
)
from trader.sizing import ABSOLUTE_MIN_MARGIN, budget_for_decision, budget_for_scan_row
from trader.tpsl import attach_tpsl_safe, resolve_mark_price
from trader.sync_tpsl import sync_tpsl
from trader.state import append_research, append_trade, append_user_directive, load_state, reload_chat_fields, save_state

_running = True


def _handle_stop(signum, frame) -> None:  # noqa: ANN001
    global _running
    _running = False
    log_event("system", "Trader stopping", f"signal {signum}")


def _parse_json_response(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty LLM response")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*", text)
        if not match:
            raise
        blob = match.group(0)
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            repaired = _repair_truncated_json(blob)
            if repaired:
                parsed = repaired
            else:
                fields = _extract_json_fields(blob)
                if fields:
                    fields.setdefault("action", "hold")
                    parsed = fields
                else:
                    raise
    if not isinstance(parsed, dict):
        raise ValueError("LLM response is not JSON object")
    return normalize_decision(parsed)


def _repair_truncated_json(blob: str) -> dict[str, Any] | None:
    """Close truncated strings/braces from token-limited LLM output."""
    in_string = False
    escape = False
    depth = 0
    for ch in blob:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
    repaired = blob.rstrip().rstrip(",")
    if in_string:
        repaired += '"'
    while depth > 0:
        repaired += "}"
        depth -= 1
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


def _extract_json_fields(blob: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key in (
        "research", "strategy_update", "reasoning", "action",
        "instId", "side", "size_contracts", "tp_pct", "sl_pct", "confidence",
    ):
        m = re.search(rf'"{key}"\s*:\s*"([^"]*)', blob)
        if m:
            fields[key] = m.group(1)
            continue
        m2 = re.search(rf'"{key}"\s*:\s*([^,\n}}]+)', blob)
        if m2:
            val = m2.group(1).strip().strip('"')
            if val == "null":
                fields[key] = None
            elif val.isdigit():
                fields[key] = int(val)
            else:
                try:
                    fields[key] = float(val)
                except ValueError:
                    fields[key] = val
    return fields


def _build_context(
    state: dict[str, Any],
    account: dict[str, Any],
    scan: list[dict[str, Any]],
    llm: LLMWrapper | None = None,
) -> str:
    recent_research = state.get("research_notes", [])[-5:]
    recent_trades = state.get("trades", [])[-5:]
    available = float(account.get("available") or 0)
    affordable = affordable_setups(scan, account)[:12]
    market_scan = []
    for row in scan[:12]:
        sizing = budget_for_scan_row(account, row)
        annotated = annotate_scan_row(row, float(sizing.get("margin_budget") or 0))
        annotated["sizing"] = sizing
        market_scan.append(annotated)
    return json.dumps(
        {
            "mission": MISSION_PROMPT,
            "target_equity": TARGET_EQUITY,
            "trade_mode": TRADE_MODE,
            "max_leverage": TRADE_MAX_LEVERAGE,
            "blohunter_tactics_digest": load_blohunter_tactics(max_chars=2500),
            "cycle": state.get("cycles", 0),
            "operator_instructions": operator_instructions(state),
            "hermes_memory": {
                "lessons": lessons_digest(state, limit=15),
                "recent_research": recent_research,
            },
            "execution_guard": execution_context(state, account),
            "account": {
                "equity": account["equity"],
                "available_margin": available,
                "positions": account["positions"],
                "position_mode": account.get("position_mode", "net_mode"),
            },
            "affordable_setups": affordable,
            "market_scan": market_scan,
            "scan_legend": {
                "long": "side=buy and score>=3",
                "short": "side=sell and score<=-3",
                "affordable": "est_margin from conviction × sentiment × available margin (see sizing on each row)",
            },
            "recent_research": recent_research,
            "recent_trades": recent_trades,
            "lessons_count": len(state.get("lessons") or []),
            "peak_equity": state.get("peak_equity", 0),
            "target_equity": TARGET_EQUITY,
            "execution_ready": state.get("execution_ready", False),
            "execution_notes": state.get("execution_notes", ""),
            "stack_health": stack_health_snapshot(llm, state, account),
            "recent_activity": get_recent(30),
            "last_incident": state.get("_last_incident"),
            "recent_repairs": (state.get("_stack_repair") or {}).get("recent", [])[-6:],
            "performance_baseline": baseline_context_for_llm(state, account),
        },
        indent=2,
    )


_LAST_SCAN_FILE = ROOT / "data" / "last_scan.json"


def _load_last_scan() -> list[dict[str, Any]]:
    if not _LAST_SCAN_FILE.is_file():
        return []
    try:
        data = json.loads(_LAST_SCAN_FILE.read_text(encoding="utf-8"))
        return list(data.get("setups") or [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_last_scan(setups: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    _LAST_SCAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LAST_SCAN_FILE.write_text(
        json.dumps({"ts": time.time(), "meta": meta, "setups": setups[:100]}, indent=2),
        encoding="utf-8",
    )


def _scan_universe(client: BlofinClient, state: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        setups, meta = client.scan_all_tradable()
        stale = " (stale tickers)" if meta.get("stale") else ""
        log_event(
            "research",
            "Full USDT universe scan",
            f"{meta.get('scored_count', 0)} scored / {meta.get('tradable_count', 0)} tradable — 1 tickers API call{stale}",
        )
        _save_last_scan(setups, meta)
        return setups
    except Exception as exc:
        log_event("error", "Universe scan failed", str(exc))
        fallback = _load_last_scan()
        if fallback:
            log_event("research", "Using cached scan", f"{len(fallback)} setups from last good scan")
            return fallback
        from blofin.account_cache import is_rate_limited

        if is_rate_limited():
            log_event("research", "Scan deferred", "BloFin rate limited — waiting for cooldown")
            return []
        log_event("research", "Scan empty", "no tickers cache; skipping candle fallback")
        return []


def _account_trade_blocked(account: dict[str, Any]) -> str | None:
    from blofin.account_cache import is_rate_limited

    if is_rate_limited() or account.get("rate_limited"):
        return "BloFin rate limited"
    if account.get("stale") and float(account.get("equity") or 0) <= 0 and not account.get("positions"):
        return "stale account snapshot (rate limit)"
    return None


def _record_close(
    client: BlofinClient,
    results: list[dict[str, Any]],
    pos: dict[str, Any],
    state: dict[str, Any],
    llm: LLMWrapper,
    account: dict[str, Any],
    scan: list[dict[str, Any]],
) -> bool:
    inst = str(pos.get("instId") or "")
    realized_pnl = capture_close_pnl(pos)

    # Before closing, remove any existing TPSL triggers so the close is not
    # blocked/cancelled by lingering TP/SL OCO orders.
    try:
        pending = client.get_orders_tpsl_pending(inst_id=inst)
        tpsl_ids = [
            str(row.get("tpslId"))
            for row in (pending.get("data") or [])
            if row.get("tpslId")
        ]
        if tpsl_ids:
            client.cancel_tpsl(inst_id=inst, tpsl_ids=tpsl_ids)
    except Exception:
        pass

    resp = client.close_position(pos)
    rejected, err = client.order_rejected(resp)
    row = {
        "action": "close_failed" if rejected else "close",
        "instId": inst,
        "side": pos.get("side"),
        "response": resp,
        "ok": not rejected,
        "realized_pnl": realized_pnl if not rejected else None,
    }
    results.append(row)
    if rejected:
        triage = llm_triage_and_repair(
            state,
            client,
            llm,
            account,
            scan,
            incident={
                "phase": "close_failed",
                "instId": inst,
                "error": err,
                "response": resp,
                "position": pos,
            },
        )
        if triage.recovered and triage.retry_payload:
            row["action"] = "close"
            row["response"] = triage.retry_payload
            row["ok"] = True
            results[-1] = row
            log_event("trade", f"Closed {inst}", json.dumps(triage.retry_payload)[:300], {"instId": inst})
            return True
        log_event("error", f"Close failed {inst}", err[:300], {"response": resp})
        log_event("trade", f"Close failed {inst}", err[:200])
        return False
    log_event("trade", f"Closed {inst}", json.dumps(resp)[:300], {"instId": inst})
    return True


def _refresh_account_after_trade(client: BlofinClient) -> None:
    from blofin.account_cache import get_account_snapshot

    try:
        get_account_snapshot(force=True)
    except Exception as exc:
        log_event("error", "Account refresh after trade failed", str(exc)[:200])


def _execute_decision(
    client: BlofinClient,
    decision: dict[str, Any],
    account: dict[str, Any],
    state: dict[str, Any],
    llm: LLMWrapper,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    action = (decision.get("action") or "hold").lower()
    block = _account_trade_blocked(account)

    if block and action in ("open", "close", "close_all"):
        log_event("trade", f"{action} blocked", block)
        return results

    if action == "close_all":
        closed_any = False
        emergency = margin_stress(account)
        for pos in list(account.get("positions", [])):
            ok_close, reason = can_close_position(pos, account, emergency=emergency)
            if not ok_close:
                log_event("trade", "Harvest skip", reason, {"instId": pos.get("instId")})
                continue
            if _record_close(client, results, pos, state, llm, account, account.get("_scan") or []):
                closed_any = True
                log_event("trade", "Harvested winner", reason, {"instId": pos.get("instId")})
        if closed_any:
            _refresh_account_after_trade(client)
        return results

    if action == "close":
        inst = decision.get("instId")
        if not inst:
            return results
        for pos in account.get("positions", []):
            if pos["instId"] != inst:
                continue
            ok_close, reason = can_close_position(pos, account, emergency=margin_stress(account))
            if not ok_close:
                log_event("trade", "Close blocked (harvest)", reason, {"instId": inst})
                results.append({"action": "close_blocked", "instId": inst, "ok": False, "reason": reason})
                return results
            if _record_close(client, results, pos, state, llm, account, account.get("_scan") or []):
                _refresh_account_after_trade(client)
                log_event("trade", "Harvested winner", reason, {"instId": inst})
            break
        return results

    if action != "open":
        return results

    inst = decision.get("instId")
    side = decision.get("side")
    if not inst or side not in ("buy", "sell"):
        return results

    allowed, guard_reason = validate_open(state, account, decision)
    if not allowed:
        log_event("trade", "Open blocked (guard)", guard_reason, {"instId": inst, "side": side})
        results.append({"action": "open_blocked", "instId": inst, "side": side, "ok": False, "reason": guard_reason})
        return results

    if block:
        log_event("trade", "Open blocked", block)
        return results

    if account.get("available", 0) < ABSOLUTE_MIN_MARGIN:
        log_event("trade", "Open blocked", "insufficient margin")
        return results

    confidence = float(decision.get("confidence") or 0)
    if confidence < OPEN_CONFIDENCE_FLOOR:
        log_event("trade", "Open skipped", f"confidence {confidence} < {OPEN_CONFIDENCE_FLOOR}")
        return results

    sizing = budget_for_decision(account, decision)
    margin_budget = float(sizing.get("margin_budget") or 0)
    if margin_budget < ABSOLUTE_MIN_MARGIN:
        log_event(
            "trade",
            "Open skipped",
            f"margin budget ${margin_budget:.2f} below floor for conf {confidence}",
            sizing,
        )
        return results

    # Find price from scan or candles
    price = 0.0
    for row in account.get("_scan", []):
        if row.get("instId") == inst:
            price = float(row.get("price") or 0)
            break
    if price <= 0:
        try:
            rows = client.get_candles(inst, "1m", "2")
            price = float(rows[-1][4])
        except Exception:
            log_event("error", "Price fetch failed", inst)
            return results

    client.ensure_net_position_mode()

    scan_rows = account.get("_scan") or []
    size_hint = decision.get("size_contracts")
    plan = plan_open(
        inst_id=inst,
        side=side,
        price=price,
        margin_budget=margin_budget,
        size_contracts=size_hint,
        account=account,
    )
    if not plan:
        plan = pick_best_affordable(scan_rows, account, margin_budget=margin_budget, inst_id=inst, side=side)
    if not plan:
        affordable = affordable_setups(scan_rows, account)
        same_side = [r for r in affordable if r.get("side") == side]
        pick = (same_side or affordable)[:1]
        if pick:
            row = pick[0]
            plan = pick_best_affordable(
                scan_rows, account, inst_id=str(row["instId"]), side=str(row["side"])
            )
    if plan and plan.inst_id != inst:
        log_event(
            "trade",
            "Open redirected",
            f"{inst} -> {plan.inst_id} (affordable @ {plan.leverage}x)",
        )
        inst = plan.inst_id
        side = plan.side
    if not plan:
        log_event("trade", "Open skipped", f"no affordable plan for {inst} (${margin_budget:.2f} budget)", sizing)
        return results

    lev_resp = client.set_leverage(plan.inst_id, plan.leverage)
    if lev_resp.get("code") not in ("0", None):
        log_event("error", f"Leverage set failed {plan.inst_id}", json.dumps(lev_resp)[:200])

    contracts_str = format_contracts(plan.contracts, plan.min_size)
    price = plan.price
    log_event(
        "trade",
        f"Opening {side} {plan.inst_id}",
        f"{contracts_str} @ {plan.leverage}x est margin ${plan.margin:.4f} (budget ${margin_budget:.2f})",
    )

    resp = client.place_market_order(plan.inst_id, side, contracts_str)
    rejected, err = client.order_rejected(resp)
    if rejected:
        triage = llm_triage_and_repair(
            state,
            client,
            llm,
            account,
            scan_rows,
            incident={
                "phase": "open_rejected",
                "instId": plan.inst_id,
                "side": side,
                "contracts": contracts_str,
                "leverage": plan.leverage,
                "price": price,
                "error": err,
                "response": resp,
            },
        )
        if triage.recovered and triage.retry_payload:
            resp = triage.retry_payload
            rejected, err = client.order_rejected(resp)
            if not rejected:
                log_event("trade", "Open retry ok", f"{plan.inst_id} after LLM repair: {triage.diagnosis[:80]}")

    results.append({
        "action": "open",
        "instId": plan.inst_id,
        "side": side,
        "size": contracts_str,
        "leverage": plan.leverage,
        "entry_price": price,
        "response": resp,
    })
    if rejected:
        if "<!DOCTYPE html>" in err or "403" in err:
            from blofin.account_cache import note_rate_limit

            note_rate_limit(90.0)
        log_event("error", f"Order rejected {plan.inst_id}", err[:300], {"response": resp})
        log_event("trade", f"Open failed {side} {plan.inst_id}", err)
        return results

    _refresh_account_after_trade(client)

    log_event(
        "trade",
        f"Opened {side} {plan.inst_id} x{contracts_str}",
        json.dumps(resp)[:400],
        {"instId": plan.inst_id, "side": side, "contracts": contracts_str, "leverage": plan.leverage},
    )
    record_open_entry(
        state,
        inst_id=plan.inst_id,
        side=side,
        price=price,
        size=contracts_str,
        leverage=int(plan.leverage),
    )

    if resp.get("code") == "0":
        # --- Manual ATR-based TP/SL (backtest method) ---
        # Cancel any stale order-tpsl so we control risk in-code.
        try:
            pending = client.get_orders_tpsl_pending(inst_id=plan.inst_id)
            ids = [str(r.get("tpslId")) for r in (pending.get("data") or []) if r.get("tpslId")]
            if ids:
                client.cancel_tpsl(inst_id=plan.inst_id, tpsl_ids=ids)
        except Exception:
            pass

        try:
            rows = client.get_candles(plan.inst_id, "5m", "10")
            highs = [float(r[2]) for r in rows]
            lows = [float(r[3]) for r in rows]
            trs = [h - l for h, l in zip(highs, lows)]
            atr = sum(trs) / max(len(trs), 1) if trs else (plan.price * 0.01)
        except Exception:
            atr = plan.price * 0.01

        sl_dist = max(atr * 0.5, plan.price * 0.015)
        tp_dist = sl_dist * 2.0
        if side == "buy":
            sl = plan.price - sl_dist
            tp = plan.price + tp_dist
        else:
            sl = plan.price + sl_dist
            tp = plan.price - tp_dist

        manual_plan = {
            "side": side,
            "entry": plan.price,
            "sl": sl,
            "tp": tp,
            "leverage": int(plan.leverage),
            "contracts": contracts_str,
        }
        state.setdefault("_manual_tpsl", {})[plan.inst_id] = manual_plan
        log_event(
            "trade",
            f"Manual TP/SL set {plan.inst_id}",
            f"entry={plan.price:.6f} SL={sl:.6f} TP={tp:.6f} leverage={plan.leverage}x",
            manual_plan,
        )
        results.append({"action": "manual_tpsl_set", "instId": plan.inst_id, "plan": manual_plan})

    return results


def _repair_missing_tpsl(
    client: BlofinClient,
    state: dict[str, Any],
    account: dict[str, Any],
) -> None:
    """Attach TP/SL on open positions that never got tpsl_ok after their last open."""
    # Exchange truth: verify we have LIVE TPSL triggers. The `get_positions()`
    # payload often does not include TP/SL trigger fields, so relying purely
    # on snapshots (or old trade history) can produce false positives.
    for pos in account.get("positions") or []:
        inst = str(pos.get("instId") or "")
        raw_size = float(pos.get("size") or pos.get("positions") or 0)
        if not inst or abs(raw_size) <= 0:
            continue
        pending = client.get_orders_tpsl_pending(inst_id=inst)
        live_rows = [
            r
            for r in (pending.get("data") or [])
            if str(r.get("state") or "").lower() == "live"
        ]
        if live_rows:
            continue

        side = "buy" if raw_size > 0 else "sell"
        contracts = client._format_order_size(inst, abs(raw_size))
        mark = resolve_mark_price(client, inst, account=account)
        if mark <= 0:
            continue

        log_event("trade", f"Repair TP/SL {inst}", f"no live TPSL — attaching at mark {mark:.6f}")
        resp = attach_tpsl_safe(
            client,
            inst_id=inst,
            side=side,
            contracts=contracts,
            mark=mark,
            tp_pct=5.0,
            sl_pct=2.0,
            leverage=int(float(pos.get("leverage") or 3)),
            account=account,
        )
        row = {
            "action": "tpsl" if str(resp.get("code")) not in ("0", "0.0") else "tpsl_ok",
            "instId": inst,
            "response": resp,
            "ts": time.time(),
        }
        if str(resp.get("code")) in ("0", "0.0"):
            append_trade(state, row)


def _enforce_manual_tpsl(
    client: BlofinClient,
    state: dict[str, Any],
    account: dict[str, Any],
) -> list[dict[str, Any]]:
    """Close positions whose mark hit manual ATR-based TP or SL before harvest."""
    manual_plans = state.get("_manual_tpsl", {}) or {}
    if not manual_plans:
        return []

    results: list[dict[str, Any]] = []
    positions = {
        str(p.get("instId") or ""): p
        for p in account.get("positions", [])
        if abs(float(p.get("size") or p.get("positions") or 0)) > 0
    }

    for inst, plan in list(manual_plans.items()):
        pos = positions.get(inst)
        if not pos:
            # Position gone — drop stale plan.
            manual_plans.pop(inst, None)
            continue

        entry = float(plan.get("entry") or 0)
        sl = float(plan.get("sl") or 0)
        tp = float(plan.get("tp") or 0)
        side = str(plan.get("side") or "")
        if not entry or not sl or not tp or not side:
            continue

        mark = float(pos.get("mark") or pos.get("markPrice") or 0)
        if mark <= 0:
            try:
                rows = client.get_candles(inst, "1m", "2")
                mark = float(rows[-1][4])
            except Exception:
                continue

        hit_tp = mark >= tp if side == "buy" else mark <= tp
        hit_sl = mark <= sl if side == "buy" else mark >= sl
        if not hit_tp and not hit_sl:
            continue

        try:
            if _record_close(client, results, pos, state, None, account, []):
                manual_plans.pop(inst, None)
                reason = f"manual {'TP' if hit_tp else 'SL'} {inst} mark={mark:.6f} TP={tp:.6f} SL={sl:.6f}"
                log_event("trade", reason, {}, {"instId": inst, "reason": reason})
        except Exception as exc:
            log_event("error", f"Manual TP/SL close failed {inst}", str(exc)[:200])
    return results


def _harvest_winners(
    client: BlofinClient,
    llm: LLMWrapper,
    state: dict[str, Any],
    account: dict[str, Any],
    scan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deterministic harvest of winners already above the NTP floor."""
    positions = enrich_positions_for_harvest(
        account.get("positions", []),
        account.get("positions_raw"),
    )
    account["positions"] = positions
    targets = list_harvestable(positions)
    if not targets:
        return []

    results: list[dict[str, Any]] = []
    for pos in targets:
        if _record_close(client, results, pos, state, llm, account, scan):
            log_event(
                "trade",
                "Harvested winner",
                f"{pos.get('instId')} NTP {position_pnl_pct(pos):.2f}%",
                {"instId": pos.get("instId"), "ntp_pct": position_pnl_pct(pos)},
            )
    if results:
        _refresh_account_after_trade(client)
        fresh = client.parse_account_snapshot(force=True)
        account.update(fresh)
        account["_scan"] = scan
        for tr in results:
            tr["ts"] = time.time()
            append_trade(state, tr)
        record_execution(
            state,
            {"action": "close_all", "instId": None, "reasoning": "harvest winners"},
            results,
        )
    return results


def _proactive_repairs(
    client: BlofinClient,
    llm: LLMWrapper,
    state: dict[str, Any],
    account: dict[str, Any],
    scan: list[dict[str, Any]],
    *,
    decision: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Always act when structural anomalies or LLM bug flags are detected."""
    positions = enrich_positions_for_harvest(
        account.get("positions", []),
        account.get("positions_raw"),
    )
    account["positions"] = positions
    gaps = detect_harvest_gaps(positions, account.get("positions_raw"))
    llm_flag = decision_reports_anomaly(decision)

    if not gaps and not llm_flag:
        return []

    reasons = []
    if gaps:
        reasons.append("harvest_gaps:" + ",".join(str(g.get("instId")) for g in gaps[:6]))
    if llm_flag:
        reasons.append("llm_anomaly_flag")
    log_event("system", "Proactive repair triggered", "; ".join(reasons)[:300])

    results: list[dict[str, Any]] = []
    targets = list_harvestable(positions) or gaps
    for pos in targets:
        ok_close, reason = can_close_position(pos, account)
        if not ok_close:
            continue
        if _record_close(client, results, pos, state, llm, account, scan):
            log_event(
                "trade",
                "Proactive harvest repair",
                reason,
                {"instId": pos.get("instId"), "ntp_pct": position_pnl_pct(pos)},
            )

    if results:
        _refresh_account_after_trade(client)
        fresh = client.parse_account_snapshot(force=True)
        account.update(fresh)
        account["_scan"] = scan
        for tr in results:
            tr["ts"] = time.time()
            append_trade(state, tr)
        record_execution(
            state,
            {"action": "close_all", "instId": None, "reasoning": "proactive harvest repair"},
            results,
        )
        return results

    from trader.repair import llm_triage_and_repair

    llm_triage_and_repair(
        state,
        client,
        llm,
        account,
        scan,
        incident={
            "phase": "proactive_anomaly",
            "error": "; ".join(reasons),
            "gaps": [{"instId": g.get("instId"), "ntp_pct": position_pnl_pct(g)} for g in gaps[:8]],
        },
    )
    return results


def _fallback_decision(
    scan: list[dict[str, Any]],
    account: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Rule-based fallback when LLM providers refuse or fail."""
    if _account_trade_blocked(account):
        return {
            "research": "LLM unavailable; API cooldown active",
            "strategy_update": "Hold until BloFin rate limit clears",
            "action": "hold",
            "instId": None,
            "side": None,
            "size_contracts": None,
            "tp_pct": 2.0,
            "sl_pct": 1.0,
            "confidence": 0,
            "reasoning": "Rate limited — no trades",
        }
    affordable = affordable_setups(scan, account)
    best = affordable[0] if affordable else None
    if not best:
        best = next((s for s in scan if not s.get("error") and s.get("side") and abs(s.get("score", 0)) >= 3), None)
    if best:
        sizing = best.get("sizing") or budget_for_scan_row(account, best)
        margin_budget = float(sizing.get("margin_budget") or 0)
    else:
        margin_budget = 0.0
    if (
        best
        and margin_budget >= ABSOLUTE_MIN_MARGIN
    ):
        candidate = {
            "research": (
                f"Momentum scan: {best['coin']} score {best['score']} "
                f"1m {best.get('c1_pct')}% 5m {best.get('c5_pct')}% "
                f"~{best.get('min_leverage', '?')}x est ${best.get('est_margin', '?')}"
            ),
            "strategy_update": "Fallback momentum entry when LLM unavailable",
            "action": "open",
            "instId": best["instId"],
            "side": best["side"],
            "size_contracts": None,
            "tp_pct": 2.0,
            "sl_pct": 1.0,
            "confidence": 55,
            "reasoning": f"Rule-based {'short' if best.get('side') == 'sell' else 'long'} momentum fallback",
        }
        ok, _ = validate_open(state, account, candidate)
        if ok:
            return candidate
    return {
        "research": "LLM unavailable; holding and continuing scan",
        "strategy_update": "Wait for clearer momentum or LLM recovery",
        "action": "hold",
        "instId": None,
        "side": None,
        "size_contracts": None,
        "tp_pct": 2.0,
        "sl_pct": 1.0,
        "confidence": 0,
        "reasoning": "No high-conviction setup or LLM blocked",
    }


def run_cycle(client: BlofinClient, llm: LLMWrapper, state: dict[str, Any]) -> dict[str, Any]:
    state = reload_chat_fields(state)
    from blofin.account_cache import cache_age_sec

    ops = operator_instructions(state)
    latest_chat_ts = max((float(m.get("ts") or 0) for m in ops.get("recent_chat_messages") or []), default=0.0)
    if latest_chat_ts > float(state.get("_last_operator_chat_ts") or 0):
        state["_last_operator_chat_ts"] = latest_chat_ts
        if ops.get("latest_user_message"):
            log_event(
                "system",
                "Operator heard",
                str(ops["latest_user_message"])[:240],
                {"wired": True, "directives_count": len(ops.get("directives") or [])},
            )

    account = client.parse_account_snapshot(force=cache_age_sec() > ACCOUNT_REFRESH_SEC * 2)
    try_detect_injection(state, account)
    update_baseline_peak(state, float(account.get("equity") or 0))
    scan = _scan_universe(client, state)
    repair_result = repair_stack(state, client, llm, account, scan)
    if repair_result and repair_result.retry_payload and isinstance(repair_result.retry_payload.get("scan"), list):
        scan = repair_result.retry_payload["scan"]
    if account.get("stale") or account.get("rate_limited"):
        account = client.parse_account_snapshot(force=True)
    account["_scan"] = scan

    manual_results = _enforce_manual_tpsl(client, state, account)
    harvest_results = _harvest_winners(client, llm, state, account, scan)
    harvest_results.extend(_proactive_repairs(client, llm, state, account, scan, decision=None))
    harvest_results.extend(manual_results)
    manual_inst_ids = set(state.get("_manual_tpsl", {}) or {})
    sync_summary = sync_tpsl(client, account, manual_inst_ids=manual_inst_ids)
    if sync_summary.get("attached") or sync_summary.get("orphan_cancelled"):
        log_event(
            "trade",
            "TP/SL sync complete",
            json.dumps({k: sync_summary[k] for k in ("positions", "live_tpsl_total", "orphan_cancelled", "missing_attached")})[:500],
        )

    if not account.get("cached"):
        log_event(
            "account",
            f"Equity ${account['equity']:.4f}"
            + (" (stale)" if account.get("stale") else ""),
            f"Available ${account['available']:.4f} | Positions: {len(account['positions'])}",
            {
                "equity": account["equity"],
                "available": account["available"],
                "positions": account["positions"],
                "position_mode": account.get("position_mode"),
                "balance_raw": account.get("balance_raw"),
                "positions_raw": account.get("positions_raw"),
            },
        )

    if account["equity"] > state.get("peak_equity", 0):
        state["peak_equity"] = account["equity"]

    context = _build_context(state, account, scan, llm)
    log_event("research", "Market scan complete", json.dumps(scan[:5], indent=2)[:800])

    response = None
    decision = None
    try:
        if hasattr(llm, "chat_race"):
            response = llm.chat_race(
                [
                    {
                        "role": "user",
                        "content": (
                            "Analyze and decide next action. "
                            "operator_instructions contains live dashboard chat from the human — follow it.\n"
                            f"{context}"
                        ),
                    }
                ],
                system=TRADER_SYSTEM,
                max_tokens=2200,
                json_mode=True,
            )
        else:
            response = llm.chat(
                [
                    {
                        "role": "user",
                        "content": (
                            "Analyze and decide next action. "
                            "operator_instructions contains live dashboard chat from the human — follow it.\n"
                            f"{context}"
                        ),
                    }
                ],
                system=TRADER_SYSTEM,
                max_tokens=2200,
                json_mode=True,
            )
        decision = _parse_json_response(response.text)
        record_llm_success(state, response.provider)
    except Exception as exc:
        record_llm_failure(state, str(exc))
        repair_llm_pool(state, llm, str(exc))
        log_event("error", "LLM cycle failed", str(exc))
        triage = llm_triage_and_repair(
            state,
            client,
            llm,
            account,
            scan,
            incident={"phase": "llm_cycle_failed", "error": str(exc)[:400]},
        )
        try:
            compact = json.dumps(
                {
                    "operator_instructions": operator_instructions(state),
                    "account": {
                        "equity": account["equity"],
                        "available": account["available"],
                        "positions": account["positions"],
                    },
                    "top_scan": scan[:5],
                    "cycle": state.get("cycles", 0),
                }
            )
            if hasattr(llm, "chat_race"):
                response = llm.chat_race(
                    [{"role": "user", "content": f"Return compact JSON decision only:\n{compact}"}],
                    system=TRADER_SYSTEM,
                    max_tokens=800,
                    json_mode=True,
                )
            else:
                response = llm.chat(
                    [{"role": "user", "content": f"Return compact JSON decision only:\n{compact}"}],
                    system=TRADER_SYSTEM,
                    max_tokens=800,
                    json_mode=True,
                )
            decision = _parse_json_response(response.text)
            record_llm_success(state, response.provider)
            log_event("research", "LLM retry succeeded", "compact context")
        except Exception as retry_exc:
            record_llm_failure(state, str(retry_exc))
            decision = _fallback_decision(scan, account, state)
            decision["reasoning"] = f"fallback after LLM error: {exc}"

    if decision is None:
        decision = _fallback_decision(scan, account, state)
        decision["reasoning"] = "fallback — no decision parsed"

    if response is None:
        pass
    elif "unsafe" in response.text.lower() or (response.text.strip().startswith("{") and '"error"' in response.text[:120]):
        decision = _fallback_decision(scan, account, state)
        decision["reasoning"] = "fallback after invalid LLM response"

    state["last_decision"] = decision
    if decision.get("research"):
        append_research(state, decision["research"])
        log_event("research", "Agent research", decision["research"][:1000])
    if decision.get("strategy_update"):
        append_research(state, f"Strategy: {decision['strategy_update']}")
        log_event("research", "Strategy update", decision["strategy_update"][:800])

    if str(decision.get("action", "hold") or "hold").lower() == "open" and (
        not decision.get("instId") or not decision.get("side")
    ):
        decision = {
            "research": decision.get("research", ""),
            "strategy_update": decision.get("strategy_update", ""),
            "action": "hold",
            "instId": None,
            "side": None,
            "size_contracts": None,
            "tp_pct": decision.get("tp_pct"),
            "sl_pct": decision.get("sl_pct"),
            "confidence": decision.get("confidence", 0),
            "reasoning": "malformed open guidance suppressed to avoid repeated guard spam",
        }
    decision = apply_open_guard(state, account, decision)

    log_event(
        "llm",
        f"Decision: {decision.get('action', 'hold')}",
        decision.get("reasoning", "")[:600],
        decision,
    )

    trade_results = _execute_decision(client, decision, account, state, llm)
    repair_trades = _proactive_repairs(client, llm, state, account, scan, decision=decision)
    for tr in repair_trades:
        tr["ts"] = time.time()
        append_trade(state, tr)
    for tr in trade_results:
        tr["ts"] = time.time()
        append_trade(state, tr)

    all_trade_results = list(harvest_results) + repair_trades + trade_results
    record_execution(state, decision, trade_results)

    learn_from_cycle(
        state,
        account=account,
        decision=decision,
        trade_results=all_trade_results,
        peak_equity=float(state.get("peak_equity") or 0),
    )

    snapshot_if_due(state, account, every_cycles=5)

    state["cycles"] = state.get("cycles", 0) + 1
    meta = state.setdefault("_stack_repair", {})
    meta["consecutive_cycle_errors"] = 0
    save_state(state)

    if account["equity"] >= TARGET_EQUITY:
        log_event("system", "TARGET REACHED", f"Equity ${account['equity']:,.2f}")

    return {"account": account, "decision": decision, "trades": trade_results}


def main() -> None:
    try:
        best_params = load_best_params()
        context = best_params_context()
    except Exception as exc:  # noqa: BLE001
        best_params = {}
        context = {"loaded": False, "error": str(exc)}

    if not acquire_trader_lock():
        msg = f"{APP_NAME}: another trader.agent is already running — exiting"
        print(msg, file=sys.stderr, flush=True)
        log_event("system", "Trader start blocked", msg)
        sys.exit(1)

    load_history()
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    log_event("system", f"{APP_NAME} started", MISSION_PROMPT[:200])
    if context.get("loaded"):
        log_event("system", "Optimized params loaded", json.dumps(context)[:1200])
    if context.get("error"):
        log_event("error", "Optimized params load failed", context["error"])
    client = BlofinClient()
    mode = client.ensure_net_position_mode()
    log_event("system", "BloFin position mode", f"{mode} (orders use positionSide=net)")
    state = load_state()
    for directive in DEFAULT_USER_DIRECTIVES:
        append_user_directive(state, directive, source="bootstrap")
    if not state.get("lessons"):
        learn_from_activity_tail(state, tail_lines=800)
    bootstrap_order_guard_from_trades(state)
    apply_best_params(best_params or {})
    state.setdefault("best_params", best_params)
    state.setdefault("optimized_params_context", context)
    if state.get("peak_equity"):
        state["_last_lesson_peak"] = float(state.get("_last_lesson_peak") or state["peak_equity"])
    save_state(state)
    state["execution_ready"] = True
    state["execution_notes"] = (
        "Position mode: net_mode. Smart margin game mode: auto leverage ladder 3x→50x "
        "to satisfy min contract margin; prefers affordable low-notional perps."
    )
    save_state(state)
    llm = LLMWrapper(
        provider_priority=("nous",),
        pool_name="trader",
        nvidia_model="stepfun/step-3.7-flash:free",
    )
    log_event("system", "LLM pool ready", json.dumps(llm.status()))

    from blofin.account_cache import get_account_snapshot

    startup_account = get_account_snapshot(force=True)
    ensure_baseline_armed(state, startup_account)
    save_state(state)
    log_event(
        "system",
        "Injection tracking ready",
        json.dumps(baseline_context_for_llm(state, startup_account))[:400],
    )

    while _running:
        try:
            run_cycle(client, llm, state)
        except Exception as exc:
            log_event("error", "Cycle error", str(exc))
            from blofin.account_cache import get_account_snapshot

            acct = get_account_snapshot(force=False)
            repair_cycle_crash(state, client, llm, acct, acct.get("_scan") or [], exc)
            save_state(state)
        for _ in range(int(TRADER_LOOP_SEC)):
            if not _running:
                break
            time.sleep(1)

    log_event("system", f"{APP_NAME} stopped", "clean shutdown")
    save_state(state)


if __name__ == "__main__":
    main()
