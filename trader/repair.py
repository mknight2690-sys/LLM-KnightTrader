"""LLM-driven stack repair — full operational visibility, think-on-feet recovery."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from activity_log import get_recent, log_event
from config import ACTIVITY_LOG, DATA_DIR, LEVERAGE_LADDER, REPAIR_LLM_PARALLEL, TRADE_MAX_LEVERAGE
from trader.health import kill_duplicate_traders
from trader.learning import append_lesson, append_lesson_once_per_interval, lessons_digest
from trader.tpsl import attach_tpsl_safe, resolve_mark_price

_LAST_SCAN_FILE = DATA_DIR / "last_scan.json"
PROACTIVE_REPAIR_COOLDOWN_SEC = 45.0
# Activity noise that should not re-trigger repair loops.
_REPAIR_NOISE_MARKERS = (
    "repair llm",
    "repair triage",
    "repair complete",
    "repair recovered",
    "repair skipped llm",
    "stream guardian",
    "proactive repair",
    "watchdog repaired",
)
# Known maintenance — deterministic script is enough; do not burn repair LLM.
_KNOWN_MAINTENANCE_MARKERS = (
    "stale",
    "rate limit",
    "rate-limited",
    "rate_limited",
    "account_degraded",
    "position_upl_outlier",
    "upl_outlier",
    "display_repaired",
    "display_warning",
    "cache age",
    "hydrated",
    "bootstrap",
    "cooldown",
)
# Phases where novel issues should always get a repair LLM opinion after script runs.
_LLM_PRIORITY_PHASES = frozenset(
    {
        "cycle_crash",
        "proactive_anomaly",
        "open_failed",
        "open_rejected",
        "close_failed",
        # IMPORTANT: TP/SL stabilization should not depend on LLM availability.
        # `retry_tpsl` is deterministic and should run even if LLM keys/models
        # are exhausted or on cooldown.
    }
)

# Actions the repair LLM may choose — its "hands", not its brain.
REPAIR_ACTION_CATALOG = [
    {
        "type": "start_trader",
        "description": "Start trader.agent when offline (uses KNIGHTTRADER_PYTHON launcher).",
    },
    {
        "type": "create_desktop_shortcuts",
        "description": "Create Start LLM KnightTrader + Stop LLM KnightTrader on user Desktop (.lnk).",
    },
    {
        "type": "dedupe_traders",
        "description": "Kill duplicate trader processes; keep KNIGHTTRADER_PYTHON instance.",
    },
    {
        "type": "kill_extra_bots",
        "description": "Kill monitor, watchers, and extra bots (not dashboard).",
    },
    {
        "type": "refresh_account",
        "description": "Force fresh balance/positions from BloFin API (or cache refresh).",
    },
    {
        "type": "bootstrap_account_cache",
        "description": "Rehydrate account cache from activity log when equity reads $0 but trades exist.",
    },
    {
        "type": "ensure_net_mode",
        "description": "Set BloFin position mode to net_mode (fixes 102089 / positionSide errors).",
    },
    {
        "type": "set_leverage",
        "description": "Set leverage for a given instrument. params: instId, leverage.",
    },
    {
        "type": "wait_seconds",
        "description": "Pause before retry — rate limits, order settlement. params: seconds (max 90).",
    },
    {
        "type": "wait_llm_cooldown",
        "description": "Wait for provider cooldown to clear. params: max_wait (default 45).",
    },
    {
        "type": "retry_close",
        "description": "Refresh live position size and retry close. params: instId.",
    },
    {
        "type": "retry_open",
        "description": "Retry market open after fixes. params: instId, side, leverage (optional), contracts (optional).",
    },
    {
        "type": "raise_leverage_retry_open",
        "description": "Walk leverage ladder up for 103003 margin errors. params: instId, side, contracts, from_leverage.",
    },
    {
        "type": "redirect_open",
        "description": "Switch to affordable instrument from scan. params: instId, side.",
    },
    {
        "type": "retry_tpsl",
        "description": "Re-attach TP/SL with corrected trigger direction. params: instId, side, contracts, price, tp_pct, sl_pct.",
    },
    {
        "type": "use_cached_scan",
        "description": "Fall back to last good universe scan when live scan failed.",
    },
    {
        "type": "record_lesson",
        "description": "Persist Hermes lesson for future cycles. params: category, lesson.",
    },
    {
        "type": "hold",
        "description": "Stop retrying this cycle — wait for conditions to improve. params: reason.",
    },
]

_REPAIR_ACTION_TYPES = {str(a.get("type") or "") for a in REPAIR_ACTION_CATALOG}


def _normalize_repair_actions(actions: list[Any]) -> list[dict[str, Any]]:
    """Keep only catalog-valid action types; convert anything else to hold."""
    normalized: list[dict[str, Any]] = []
    for act in actions[:5]:
        if not isinstance(act, dict):
            continue
        atype = str(act.get("type") or "").strip()
        if atype in _REPAIR_ACTION_TYPES:
            # Ensure params is always a dict
            if not isinstance(act.get("params"), dict):
                act["params"] = {}
            normalized.append(act)
        else:
            # Unknown action type => safe hold.
            normalized.append({"type": "hold", "params": {"reason": f"unknown_action:{atype}"}})
    return normalized


def _verify_tpsl_attached(
    client: Any,
    inst_id: str,
    *,
    last_attach_resp: dict[str, Any] | None = None,
) -> bool:
    """Verify TP/SL are attached after retry_tpsl.

    NOTE:
    Your BloFin `get_positions` / account snapshot response often does *not*
    include TP/SL trigger fields, even when the `order-tpsl` call returns
    `code=0`. So the primary verification is the exchange acceptance response
    (code==0 or a returned tpslId). We fall back to positions-field checks only
    when those fields are present in the snapshot.
    """
    if not inst_id:
        return False

    # Prefer verifying via order-tpsl-detail. BloFin accepts TPSL but can
    # immediately cancel it if triggers are invalid; your `get_positions()`
    # snapshot often doesn't expose TP/SL fields.
    if isinstance(last_attach_resp, dict) and str(last_attach_resp.get("code")) in ("0", "0.0"):
        data = last_attach_resp.get("data") or {}
        if isinstance(data, dict) and data.get("tpslId") and client:
            try:
                detail = client.get_order_tpsl_detail(inst_id=inst_id, tpsl_id=str(data.get("tpslId")))
                state = (detail.get("data") or {}).get("state")
                if state and str(state).lower() in ("effective", "live"):
                    return True
                # If it exists but isn't effective, treat as failure.
                if state:
                    return False
            except Exception:
                # Fall back below.
                pass
        return True

    try:
        from blofin.account_cache import get_account_snapshot

        snap = get_account_snapshot(force=True)
        positions = snap.get("positions") or []
        for p in positions:
            if str(p.get("instId") or "") != inst_id:
                continue
            # Try multiple potential field naming variants.
            has_tp = bool(
                p.get("tp")
                or p.get("tp_price")
                or p.get("tpPrice")
                or p.get("tpTriggerPx")
                or p.get("tpTriggerPrice")
                or p.get("tpTriggerPricePx")
            )
            has_sl = bool(
                p.get("sl")
                or p.get("sl_price")
                or p.get("slPrice")
                or p.get("slTriggerPx")
                or p.get("slTriggerPrice")
                or p.get("slTriggerPricePx")
            )
            # If the exchange snapshot doesn't expose triggers, don't treat it as failure.
            if has_tp or has_sl:
                return has_tp and has_sl
            return True
    except Exception:
        return False
    return False

REPAIR_SYSTEM = """You are the LLM KnightTrader **operations engineer**. You see the ENTIRE live operation:
recent activity log, account, positions, scan, lessons, stack health, and the incident that triggered you.

You do NOT know the future — errors may be novel. **Think on your feet**: diagnose from evidence, pick a short
action plan from the catalog, adapt when the playbook does not fit. You are invoked for **new or unresolved**
issues after deterministic script already ran — bring creative fixes scripts cannot invent.

Respond ONLY with valid JSON (no markdown):
{
  "diagnosis": "What happened and why (max 200 chars)",
  "root_cause": "Best guess at root cause (max 120 chars)",
  "confidence": 0-100,
  "actions": [
    {"type": "<action from catalog>", "params": {}}
  ],
  "retry_original": true,
  "strategy_note": "Brief note for next trading cycle (max 100 chars)",
  "lesson": {"category": "repair", "text": "Durable lesson if worth remembering"} or null
}

RULES:
- Read `recent_activity` and `incident` first — the answer is usually there.
- Order actions logically: refresh/wait BEFORE retry opens/closes.
- For unknown BloFin codes: refresh_account + ensure_net_mode + wait_seconds, then retry if safe.
- For LLM/JSON failures: wait_llm_cooldown, record_lesson, hold — do not force trades.
- For margin 103003: raise_leverage_retry_open or redirect_open to affordable_setups.
- For rate limits: wait_seconds (use backoff hint from stack_health) then refresh_account.
- Trader offline: start_trader. Duplicate traders: dedupe_traders then start_trader if needed.
- Missing desktop shortcuts: create_desktop_shortcuts (user daily Start/Stop must exist).
- Never advise python -m trader.agent for daily use — desktop launcher or stack_launcher.py start.
- See stack_fix.playbook in operational picture for full deterministic repair map.
- Never invent action types — only use catalog types.
- Max 5 actions per plan. Prefer hold over reckless retries when confidence < 50.
- Set retry_original true only when a retry_* action is included and risk is acceptable.
"""


@dataclass
class RepairResult:
    diagnosis: str = ""
    actions_taken: list[str] = field(default_factory=list)
    recovered: bool = False
    retry_payload: dict[str, Any] | None = None
    strategy_note: str = ""


def _repair_meta(state: dict[str, Any]) -> dict[str, Any]:
    meta = dict(state.get("_stack_repair") or {})
    state["_stack_repair"] = meta
    return meta


def record_repair(state: dict[str, Any], action: str, detail: str = "") -> None:
    meta = _repair_meta(state)
    now = time.time()
    entry = {"ts": now, "action": action, "detail": detail[:200]}
    recent = list(meta.get("recent") or [])
    recent.append(entry)
    meta["recent"] = recent[-20:]
    meta["last_ts"] = now
    meta["total"] = int(meta.get("total") or 0) + 1
    log_event("system", f"Stack repair: {action}", detail[:300] or action)


def _is_repair_noise_event(event: dict[str, Any]) -> bool:
    if event.get("type") not in ("error", "system"):
        return False
    blob = f"{event.get('title') or ''} {event.get('detail') or ''}".lower()
    return any(marker in blob for marker in _REPAIR_NOISE_MARKERS)


def _meaningful_recent_errors(limit: int = 40) -> list[dict[str, Any]]:
    """Recent errors that should trigger repair — excludes repair-system noise."""
    out: list[dict[str, Any]] = []
    for event in reversed(get_recent(limit)):
        if event.get("type") == "error" and _is_repair_noise_event(event):
            continue
        if event.get("type") == "error":
            out.append(event)
        elif event.get("type") == "trade" and "failed" in str(event.get("title") or "").lower():
            out.append(event)
    out.reverse()
    return out


def _incident_fingerprint(incident: dict[str, Any]) -> str:
    phase = str(incident.get("phase") or "unknown")
    err = str(incident.get("error") or incident.get("detail") or "")
    err = re.sub(r"\d+\.\d+", "#", err)
    err = re.sub(r"\s+", " ", err).strip().lower()[:160]
    inst = str(incident.get("instId") or "")
    return f"{phase}|{inst}|{err}"


def _remember_incident(state: dict[str, Any], incident: dict[str, Any]) -> None:
    meta = _repair_meta(state)
    fp = _incident_fingerprint(incident)
    seen = list(meta.get("incident_fingerprints") or [])
    if fp not in seen:
        seen.append(fp)
    meta["incident_fingerprints"] = seen[-80:]
    meta["last_incident_fingerprint"] = fp


def _incident_seen_before(state: dict[str, Any], incident: dict[str, Any]) -> bool:
    fp = _incident_fingerprint(incident)
    return fp in set(_repair_meta(state).get("incident_fingerprints") or [])


def _incident_trader_offline(incident: dict[str, Any]) -> bool:
    err = str(incident.get("error") or incident.get("detail") or "").lower()
    if "trader_offline" in err or "trader.agent not running" in err:
        return True
    for issue in incident.get("issues") or []:
        if isinstance(issue, dict) and str(issue.get("code") or "") == "trader_offline":
            return True
    return False


def _is_known_maintenance_incident(incident: dict[str, Any]) -> bool:
    phase = str(incident.get("phase") or "")
    err = str(incident.get("error") or incident.get("detail") or "").lower()

    # TP/SL stabilization must be deterministic and should NOT depend on
    # LLM availability. Even when the issue is "novel", we want
    # script-only `retry_tpsl` / `refresh_account` behavior for TPSL.
    if phase == "tpsl_failed":
        return False
    if phase == "stack_watchdog":
        if _incident_trader_offline(incident):
            return False
        return True
    if phase == "cycle_proactive" and any(m in err for m in _KNOWN_MAINTENANCE_MARKERS):
        return True
    return any(m in err for m in ("stale/rate_limited", "account cache stale"))


def _is_repair_noise_incident(incident: dict[str, Any]) -> bool:
    blob = f"{incident.get('title') or ''} {incident.get('error') or ''} {incident.get('detail') or ''}".lower()
    return any(marker in blob for marker in _REPAIR_NOISE_MARKERS)


def _should_consult_repair_llm(
    state: dict[str, Any],
    llm: Any,
    incident: dict[str, Any],
    *,
    deterministic_recovered: bool,
    deterministic_ran: bool,
) -> bool:
    """Script first; repair LLM only for novel issues it can reason about."""
    if deterministic_recovered:
        return False
    if _is_repair_noise_incident(incident):
        return False
    if _is_known_maintenance_incident(incident):
        return False

    phase = str(incident.get("phase") or "")
    err = str(incident.get("error") or incident.get("detail") or "").lower()
    seen = _incident_seen_before(state, incident)
    has_det_plan = _deterministic_repair_plan(incident) is not None

    # High-priority incidents: consult repair LLM once per fingerprint when script did not recover.
    if phase in _LLM_PRIORITY_PHASES:
        return not (seen and deterministic_ran)

    # Operator/LLM flagged anomalies and suspected logic bugs need creative triage.
    if ("bug" in err or "mismatch" in err or "not in harvestable" in err) and not (seen and deterministic_ran):
        return True

    # First time we see this fingerprint and no playbook covers it — consult LLM.
    if not seen and not has_det_plan:
        return True

    # Repeat fingerprint after script already tried — do not loop repair LLM.
    if seen and deterministic_ran:
        return False

    # First sight of a trade/API error even with a partial playbook — get LLM review once.
    if not seen and (phase.endswith("_failed") or "rejected" in phase):
        return True

    # Unknown BloFin codes on first encounter.
    if not seen and re.search(r"\b10\d{4}\b", err):
        return True

    # Default: novel cycle issues get one LLM consult; repeats stay script-only.
    return not seen


def stack_health_snapshot(
    llm: Any,
    state: dict[str, Any],
    account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from blofin.account_cache import is_rate_limited, read_account_cached
    from trader.stack_control import desktop_shortcuts_exist, running_process_counts, stack_status
    from trader.stack_fix import STACK_FIX_PLAYBOOK

    meta = _repair_meta(state)
    llm_status = llm.status() if llm else {}
    acct = account or {}
    cached = read_account_cached() or {}
    backoff = float(cached.get("backoff_until") or 0)
    return {
        "openrouter_keys": llm_status.get("openrouter_keys", 0),
        "llm_cooldown_sec": llm_status.get("cooldown_sec", 0),
        "rate_limited": bool(is_rate_limited() or acct.get("rate_limited")),
        "rate_limit_remaining_sec": max(0.0, backoff - time.time()),
        "account_stale": bool(acct.get("stale")),
        "consecutive_llm_failures": int(meta.get("consecutive_llm_failures") or 0),
        "consecutive_cycle_errors": int(meta.get("consecutive_cycle_errors") or 0),
        "last_repair_ts": meta.get("last_ts"),
        "recent_repairs": [r.get("action") for r in (meta.get("recent") or [])[-8:]],
        "last_incident": state.get("_last_incident"),
        "process_counts": running_process_counts(),
        "stack_status": stack_status(),
        "desktop_shortcuts_exist": desktop_shortcuts_exist(),
        "fix_playbook": STACK_FIX_PLAYBOOK,
    }


def record_llm_success(state: dict[str, Any], provider: str) -> None:
    meta = _repair_meta(state)
    meta["consecutive_llm_failures"] = 0
    meta["last_llm_provider"] = provider


def record_llm_failure(state: dict[str, Any], error: str) -> None:
    meta = _repair_meta(state)
    meta["consecutive_llm_failures"] = int(meta.get("consecutive_llm_failures") or 0) + 1
    meta["last_llm_error"] = error[:300]


def _summarize_activity(events: list[dict[str, Any]], limit: int = 35) -> list[dict[str, Any]]:
    """Compact activity tail for LLM — full operation visibility."""
    out: list[dict[str, Any]] = []
    for ev in events[-limit:]:
        out.append(
            {
                "ts": ev.get("ts"),
                "type": ev.get("type"),
                "title": str(ev.get("title") or "")[:120],
                "detail": str(ev.get("detail") or "")[:400],
            }
        )
    return out


def build_operational_picture(
    state: dict[str, Any],
    client: Any,
    llm: Any,
    account: dict[str, Any],
    scan: list[dict[str, Any]] | None,
    incident: dict[str, Any],
) -> dict[str, Any]:
    """Assemble everything the repair LLM needs to think on its feet."""
    from trader.order_guard import execution_context
    from trader.stack_fix import stack_fix_context

    recent = get_recent(50)
    if not recent and ACTIVITY_LOG.is_file():
        for line in ACTIVITY_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-50:]:
            try:
                recent.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue

    return {
        "role": "operations_engineer",
        "incident": incident,
        "incident_meta": {
            "fingerprint": _incident_fingerprint(incident),
            "seen_before": _incident_seen_before(state, incident),
            "deterministic_playbook": bool(_deterministic_repair_plan(incident)),
        },
        "stack_health": stack_health_snapshot(llm, state, account),
        "stack_fix": stack_fix_context(),
        "recent_activity": _summarize_activity(recent, 35),
        "account": {
            "equity": account.get("equity"),
            "available": account.get("available"),
            "positions": account.get("positions"),
            "position_mode": account.get("position_mode"),
            "stale": account.get("stale"),
            "rate_limited": account.get("rate_limited"),
        },
        "execution_guard": execution_context(state, account),
        "hermes_memory": {"lessons": lessons_digest(state, limit=12)},
        "last_decision": state.get("last_decision"),
        "cycle": state.get("cycles", 0),
        "peak_equity": state.get("peak_equity"),
        "market_scan_top": (scan or [])[:5],
        "affordable_hint": [
            r for r in (scan or [])
            if r.get("affordable") or float(r.get("est_margin") or 999) < float(account.get("available") or 0)
        ][:4],
        "llm_pool": llm.status() if llm else {},
        "repair_action_catalog": REPAIR_ACTION_CATALOG,
        "prior_repairs_this_session": (state.get("_stack_repair") or {}).get("recent", [])[-5:],
    }


def _parse_repair_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    plan = json.loads(text)
    if not isinstance(plan, dict):
        raise ValueError("repair plan is not a dict")
    plan.setdefault("actions", [])
    plan.setdefault("retry_original", False)
    return plan


def _load_cached_scan() -> list[dict[str, Any]]:
    if not _LAST_SCAN_FILE.is_file():
        return []
    try:
        data = json.loads(_LAST_SCAN_FILE.read_text(encoding="utf-8"))
        return list(data.get("setups") or [])
    except (json.JSONDecodeError, OSError):
        return []


def _exec_retry_close(client: Any, params: dict[str, Any]) -> dict[str, Any] | None:
    inst = str(params.get("instId") or "")
    if not inst:
        return None
    from blofin.account_cache import get_account_snapshot

    try:
        snap = get_account_snapshot(force=True)
        for pos in snap.get("positions") or []:
            if str(pos.get("instId") or "") != inst:
                continue
            resp = client.close_position(pos)
            rejected, err = client.order_rejected(resp)
            if not rejected:
                return resp
            if "102022" in err or "reduce" in err.lower():
                raw_size = float(pos.get("size") or pos.get("positions") or 0)
                close_side = "sell" if raw_size > 0 else "buy"
                size_str = client._format_order_size(inst, abs(raw_size))
                resp = client.place_market_order(
                    inst, close_side, size_str, "net", reduce_only=False,
                    margin_mode=str(pos.get("marginMode") or "cross"),
                )
                rejected, _ = client.order_rejected(resp)
                if not rejected:
                    return resp
    except Exception as exc:
        log_event("error", "retry_close failed", f"{inst}: {exc}"[:200])
    return None


def _exec_retry_open(client: Any, params: dict[str, Any]) -> dict[str, Any] | None:
    inst = str(params.get("instId") or "")
    side = str(params.get("side") or "")
    if not inst or side not in ("buy", "sell"):
        return None
    lev = params.get("leverage")
    if lev:
        client.set_leverage(inst, int(lev))
    contracts = params.get("contracts")
    if not contracts:
        from trader.margin import plan_open
        from trader.sizing import margin_budget_for_setup
        from blofin.account_cache import get_account_snapshot

        snap = get_account_snapshot(force=False)
        budget = float(margin_budget_for_setup(snap, confidence=75, score=5).get("margin_budget") or 0)
        price = float(params.get("price") or 0)
        if price <= 0:
            try:
                rows = client.get_candles(inst, "1m", "2")
                price = float(rows[-1][4])
            except Exception:
                return None
        plan = plan_open(inst_id=inst, side=side, price=price, margin_budget=budget, account=snap)
        if not plan:
            return None
        contracts = plan.contracts
        if not lev:
            client.set_leverage(inst, plan.leverage)
        from trader.margin import format_contracts
        contracts_str = format_contracts(plan.contracts, plan.min_size)
    else:
        contracts_str = str(contracts)
    resp = client.place_market_order(inst, side, contracts_str)
    rejected, _ = client.order_rejected(resp)
    return resp if not rejected else None


def _exec_raise_leverage_retry(client: Any, params: dict[str, Any]) -> dict[str, Any] | None:
    inst = str(params.get("instId") or "")
    side = str(params.get("side") or "")
    contracts = str(params.get("contracts") or "")
    from_lev = int(params.get("from_leverage") or 3)
    if not inst or not contracts:
        return None
    for lev in LEVERAGE_LADDER:
        if lev <= from_lev or lev > TRADE_MAX_LEVERAGE:
            continue
        client.set_leverage(inst, lev)
        resp = client.place_market_order(inst, side, contracts)
        rejected, _ = client.order_rejected(resp)
        if not rejected:
            return resp
    return None


def _exec_retry_tpsl(client: Any, params: dict[str, Any], account: dict[str, Any] | None = None) -> dict[str, Any] | None:
    inst = str(params.get("instId") or "")
    side_raw = str(params.get("side") or "").strip().lower()
    # Normalize common position-side vocab to what TP/SL expects.
    # - dashboard/account caches often use "long"/"short"
    # - trader order logic uses "buy"/"sell"
    side = (
        "buy"
        if side_raw in ("buy", "long")
        else "sell"
        if side_raw in ("sell", "short")
        else side_raw
    )

    contracts_raw = str(params.get("contracts") or "").strip()
    if not inst or not side or side not in ("buy", "sell") or not contracts_raw:
        return None

    # Normalize contracts: exchange expects a positive order size.
    try:
        size = abs(float(contracts_raw))
        if hasattr(client, "_format_order_size"):
            contracts = client._format_order_size(inst, size)
        else:
            contracts = str(size)
    except (ValueError, TypeError):
        contracts = contracts_raw.lstrip("+-")

    mark = resolve_mark_price(
        client,
        inst,
        account=account,
        fallback=float(params.get("price") or 0),
    )
    if mark <= 0:
        return None

    resp = attach_tpsl_safe(
        client,
        inst_id=inst,
        side=side,
        contracts=contracts,
        mark=mark,
        tp_pct=float(params.get("tp_pct") or 2.0),
        sl_pct=float(params.get("sl_pct") or 1.0),
        leverage=int(params.get("leverage") or 3),
        account=account,
    )
    # Always return the exchange response so callers can:
    # - compute ok by code
    # - record a learning lesson with the rejection message
    return resp if isinstance(resp, dict) else None


def execute_repair_action(
    state: dict[str, Any],
    client: Any,
    llm: Any,
    account: dict[str, Any],
    scan: list[dict[str, Any]] | None,
    action: dict[str, Any],
) -> tuple[str, bool, Any]:
    """Run one repair action. Returns (label, success, payload)."""
    atype = str(action.get("type") or "").strip()
    params = dict(action.get("params") or {})

    if atype == "refresh_account":
        from blofin.account_cache import get_account_snapshot
        snap = get_account_snapshot(force=True)
        account.update(snap)
        return "refresh_account", True, snap

    if atype == "start_trader":
        from trader.stack_control import stack_status, start_single_trader

        result = start_single_trader()
        ok = bool(result.get("ok"))
        if ok:
            stack = stack_status()
            ok = (stack.get("trader") or {}).get("status") == "online"
        if not ok:
            log_event("error", "Trader start failed", str(result.get("error") or "trader still offline")[:300])
        return "start_trader", ok, result

    if atype == "create_desktop_shortcuts":
        from trader.stack_control import ensure_desktop_shortcuts

        result = ensure_desktop_shortcuts()
        return "create_desktop_shortcuts", bool(result.get("ok")), result

    if atype == "dedupe_traders":
        from trader.stack_control import dedupe_preferred_trader, stack_status, start_single_trader

        keep = dedupe_preferred_trader()
        stack = stack_status()
        if (stack.get("trader") or {}).get("status") == "offline":
            started = start_single_trader()
            return "dedupe_traders", bool(started.get("ok")), {"keep": keep, "start": started}
        return "dedupe_traders", bool(keep), {"keep": keep}

    if atype == "kill_extra_bots":
        from trader.stack_control import kill_all_bots

        killed = kill_all_bots()
        return "kill_extra_bots", True, {"killed": killed}

    if atype == "bootstrap_account_cache":
        from blofin.account_cache import bootstrap_account_cache, get_account_snapshot
        bootstrap_account_cache()
        snap = get_account_snapshot(force=True)
        account.update(snap)
        return "bootstrap_account_cache", True, snap

    if atype == "ensure_net_mode":
        mode = client.ensure_net_position_mode()
        ok = mode in ("net_mode", "net")
        return "ensure_net_mode", ok, mode

    if atype == "set_leverage":
        inst = str(params.get("instId") or "").strip()
        leverage = int(params.get("leverage") or 0)
        if not inst or leverage <= 0:
            return "set_leverage", False, {"error": "missing instId/leverage"}
        try:
            client.set_leverage(inst, leverage)
            return "set_leverage", True, {"instId": inst, "leverage": leverage}
        except Exception as exc:
            return "set_leverage", False, {"error": str(exc)[:200]}

    if atype == "wait_seconds":
        sec = min(float(params.get("seconds") or 15), 300.0)
        time.sleep(sec)
        return f"wait_{int(sec)}s", True, None

    if atype == "wait_llm_cooldown":
        raw_wait = params.get("max_wait")
        max_wait = None if raw_wait in (None, "", "inf", "infinite") else float(raw_wait)
        ok = llm.wait_for_provider(max_wait=max_wait) if llm and hasattr(llm, "wait_for_provider") else False
        return "wait_llm_cooldown", ok, None

    if atype == "retry_close":
        resp = _exec_retry_close(client, params)
        return "retry_close", resp is not None, resp

    if atype == "retry_open":
        resp = _exec_retry_open(client, params)
        return "retry_open", resp is not None, resp

    if atype == "raise_leverage_retry_open":
        resp = _exec_raise_leverage_retry(client, params)
        return "raise_leverage_retry_open", resp is not None, resp

    if atype == "redirect_open":
        resp = _exec_retry_open(client, params)
        return "redirect_open", resp is not None, resp

    if atype == "retry_tpsl":
        resp = _exec_retry_tpsl(client, params, account=account)
        ok = bool(resp) and str(resp.get("code")) in ("0", "0.0")
        return "retry_tpsl", ok, resp

    if atype == "use_cached_scan":
        cached = _load_cached_scan()
        return "use_cached_scan", bool(cached), cached

    if atype == "record_lesson":
        cat = str(params.get("category") or "repair")
        lesson = str(params.get("lesson") or params.get("text") or "")[:220]
        if lesson:
            append_lesson(state, category=cat, lesson=lesson, source="repair_llm")
        return "record_lesson", bool(lesson), lesson

    if atype == "hold":
        reason = str(params.get("reason") or "repair hold")
        return f"hold:{reason[:40]}", True, None

    return f"unknown:{atype}", False, None


def execute_repair_plan(
    state: dict[str, Any],
    client: Any,
    llm: Any,
    account: dict[str, Any],
    scan: list[dict[str, Any]] | None,
    plan: dict[str, Any],
) -> RepairResult:
    result = RepairResult(
        diagnosis=str(plan.get("diagnosis") or "")[:300],
        strategy_note=str(plan.get("strategy_note") or "")[:200],
    )
    lesson = plan.get("lesson")
    if isinstance(lesson, dict) and lesson.get("text"):
        append_lesson(
            state,
            category=str(lesson.get("category") or "repair"),
            lesson=str(lesson["text"])[:220],
            source="repair_llm",
        )

    normalized_actions = _normalize_repair_actions(list(plan.get("actions") or []))
    for action in normalized_actions[:5]:
        if not isinstance(action, dict):
            continue
        label, ok, payload = execute_repair_action(state, client, llm, account, scan, action)

        # Post-action verification: "ok" from exchange should mean triggers exist.
        if label == "retry_tpsl" and ok:
            inst_id = str((action.get("params") or {}).get("instId") or "")
            verified = _verify_tpsl_attached(client, inst_id, last_attach_resp=payload if isinstance(payload, dict) else None)
            if not verified:
                ok = False
                try:
                    append_lesson_once_per_interval(
                        state,
                        category="tpsl_verify",
                        lesson=f"Exchange returned code=0 but TP/SL triggers missing for {inst_id}; verify side/contracts normalization and retry again.",
                        interval_key=f"tpsl_verify_fail:{inst_id}",
                        source="repair_llm",
                        interval_sec=1800,
                    )
                except Exception:
                    pass

        result.actions_taken.append(f"{label}:{'ok' if ok else 'fail'}")
        record_repair(state, label, result.diagnosis[:100])
        if str(action.get("type") or "").strip() == "retry_tpsl" and not ok:
            try:
                inst = str((action.get("params") or {}).get("instId") or "")
                err = ""
                if isinstance(payload, dict):
                    err = str(payload.get("msg") or payload.get("error") or payload.get("title") or "")[:160]
                # Teach the system to normalize side + ensure positive contracts next time.
                append_lesson_once_per_interval(
                    state,
                    category="tpsl",
                    lesson=(
                        f"TP/SL attach failed for {inst or 'inst'}; ensure side is buy/sell and contracts are positive. "
                        f"Exchange msg: {err}"
                    )[:220],
                    interval_key=f"tpsl_fail:{inst or 'any'}",
                    source="repair_llm",
                    interval_sec=1800,
                )
            except Exception:
                pass
        if ok and payload and label in (
            "retry_close",
            "retry_open",
            "redirect_open",
            "raise_leverage_retry_open",
            "retry_tpsl",
            "start_trader",
        ):
            result.recovered = True
            result.retry_payload = payload if isinstance(payload, dict) else {"result": payload}
        if label.startswith("use_cached_scan") and ok and isinstance(payload, list):
            result.retry_payload = {"scan": payload}

    if plan.get("strategy_note"):
        from trader.state import append_research
        append_research(state, f"Repair: {plan['strategy_note']}")

    return result


def _deterministic_repair_plan(incident: dict[str, Any]) -> dict[str, Any] | None:
    """Known incidents — fix immediately without waiting on repair LLM."""
    phase = str(incident.get("phase") or "")
    err = str(incident.get("error") or "").lower()
    inst = incident.get("instId")

    if phase == "stack_watchdog":
        actions: list[dict[str, Any]] = []
        issues = incident.get("issues") or []
        codes = {str(i.get("code") or "") for i in issues if isinstance(i, dict)}
        if "desktop_shortcuts_missing" in codes:
            actions.append({"type": "create_desktop_shortcuts", "params": {}})
        if "trader_duplicate" in codes:
            actions.append({"type": "dedupe_traders", "params": {}})
        if "extra_bots_running" in codes:
            actions.append({"type": "kill_extra_bots", "params": {}})
        if _incident_trader_offline(incident):
            actions.append({"type": "start_trader", "params": {}})
        actions.extend(
            [
                {"type": "bootstrap_account_cache", "params": {}},
                {"type": "refresh_account", "params": {}},
            ]
        )
        diagnosis = (
            "Stack watchdog — start offline trader"
            if _incident_trader_offline(incident)
            else "Stack watchdog incident — rehydrate account and refresh API cache"
        )
        return {
            "diagnosis": diagnosis,
            "actions": actions[:5],
            "retry_original": False,
            "strategy_note": "Autonomous stack repair",
        }

    if phase == "proactive_anomaly":
        gaps = incident.get("gaps") or []
        real_gaps = [g for g in gaps if isinstance(g, dict) and g.get("instId")]
        if not real_gaps:
            return None
        actions: list[dict[str, Any]] = [
            {"type": "refresh_account", "params": {}},
        ]
        for gap in real_gaps:
            actions.append({"type": "retry_close", "params": {"instId": gap["instId"]}})
        return {
            "diagnosis": "Proactive anomaly — harvest winners LLM flagged or gaps detected",
            "actions": actions[:5],
            "retry_original": True,
            "strategy_note": "Auto-repair harvest/NTP mismatch",
        }

    if phase == "cycle_proactive":
        actions: list[dict[str, Any]] = [
            {"type": "bootstrap_account_cache", "params": {}},
            {"type": "refresh_account", "params": {}},
        ]
        if "rate limit" in err or "rate_limited" in err or "403" in err:
            health_wait = 15.0
            actions = [
                {"type": "wait_seconds", "params": {"seconds": min(health_wait, 30)}},
                {"type": "bootstrap_account_cache", "params": {}},
            ]
        elif "position_upl" in err or "upl_outlier" in err or "display" in err:
            actions = [
                {"type": "bootstrap_account_cache", "params": {}},
                {"type": "refresh_account", "params": {}},
            ]
        elif "llm cycle failed" in err or "parse" in err:
            actions = [
                {"type": "wait_llm_cooldown", "params": {"max_wait": 20}},
                {"type": "refresh_account", "params": {}},
            ]
        return {
            "diagnosis": "Cycle proactive maintenance — refresh account without repair LLM",
            "actions": actions[:5],
            "retry_original": False,
            "strategy_note": "Deterministic stack maintenance",
        }

    if phase == "tpsl_failed" and inst:
        actions: list[dict[str, Any]] = [
            {"type": "refresh_account", "params": {}},
        ]

        # If we learned (from incident.error) that leverage/mode is wrong,
        # fix it before retrying the actual attach.
        if "102089" in err or "positionside" in err:
            actions.insert(0, {"type": "ensure_net_mode", "params": {}})

        leverage_current = int(incident.get("leverage") or 3)
        leverage_next = next((l for l in LEVERAGE_LADDER if l > leverage_current and l <= TRADE_MAX_LEVERAGE), None)
        if leverage_next is not None and ("103003" in err or "margin" in err):
            actions.append(
                {"type": "set_leverage", "params": {"instId": inst, "leverage": leverage_next}}
            )
            leverage_current = leverage_next

        actions.append(
            {
                "type": "retry_tpsl",
                "params": {
                    "instId": inst,
                    "side": incident.get("side"),
                    "contracts": incident.get("contracts"),
                    "price": incident.get("price"),
                    "tp_pct": incident.get("tp_pct", 2.0),
                    "sl_pct": incident.get("sl_pct", 1.0),
                    "leverage": leverage_current,
                },
            }
        )

        return {
            "diagnosis": "TP/SL repair — refresh account + normalize mode/leverage, then retry attach",
            "actions": actions[:5],
            "retry_original": True,
            "strategy_note": "Fix leverage/mode if margin or positionSide issues seen",
        }

    if phase == "close_failed" and inst:
        return {
            "diagnosis": f"Close failed on {inst} — refresh and retry",
            "actions": [
                {"type": "refresh_account", "params": {}},
                {"type": "retry_close", "params": {"instId": inst}},
            ],
            "retry_original": True,
            "strategy_note": "Retry close after account refresh",
        }

    if phase in ("open_rejected", "open_failed") and inst:
        actions: list[dict[str, Any]] = [
            {"type": "ensure_net_mode", "params": {}},
            {"type": "refresh_account", "params": {}},
        ]
        if "103003" in err or "margin" in err:
            actions.append(
                {
                    "type": "raise_leverage_retry_open",
                    "params": {
                        "instId": inst,
                        "side": incident.get("side"),
                        "contracts": incident.get("contracts"),
                        "from_leverage": incident.get("leverage", 3),
                    },
                }
            )
        else:
            actions.append(
                {
                    "type": "retry_open",
                    "params": {
                        "instId": inst,
                        "side": incident.get("side"),
                        "contracts": incident.get("contracts"),
                        "leverage": incident.get("leverage"),
                    },
                }
            )
        return {
            "diagnosis": f"Open failed on {inst}",
            "actions": actions[:5],
            "retry_original": True,
            "strategy_note": "Auto-retry open after mode/margin fix",
        }

    if "102089" in err or "positionside" in err:
        return {
            "diagnosis": "Position mode mismatch",
            "actions": [
                {"type": "ensure_net_mode", "params": {}},
                {"type": "refresh_account", "params": {}},
            ],
            "retry_original": False,
            "strategy_note": "net_mode enforced",
        }

    return None


def _call_repair_llm(
    llm: Any,
    picture: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Consult repair LLM with parallel workers — waits as long as needed, no wall-clock cutoff."""
    payload = json.dumps(picture, indent=2)
    messages = [{"role": "user", "content": payload}]

    if llm and hasattr(llm, "wait_for_provider"):
        llm.wait_for_provider(max_wait=None)

    def _one_repair_call() -> tuple[dict[str, Any], str]:
        if llm and hasattr(llm, "chat_race"):
            resp = llm.chat_race(
                messages,
                system=REPAIR_SYSTEM,
                max_tokens=900,
                json_mode=True,
                parallel_workers=REPAIR_LLM_PARALLEL,
            )
        else:
            resp = llm.chat(
                messages,
                system=REPAIR_SYSTEM,
                max_tokens=900,
                json_mode=True,
            )
        return _parse_repair_json(resp.text), str(resp.provider or "")

    workers = max(1, REPAIR_LLM_PARALLEL)
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(_one_repair_call) for _ in range(workers)}
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    plan, provider = fut.result()
                    if provider:
                        record_llm_success(state, provider)
                    return plan
                except Exception as exc:
                    errors.append(str(exc))
                    if llm and hasattr(llm, "wait_for_provider"):
                        llm.wait_for_provider(max_wait=None)

    raise RuntimeError("Repair LLM exhausted all parallel workers: " + "; ".join(errors[:6]))


def llm_triage_and_repair(
    state: dict[str, Any],
    client: Any,
    llm: Any,
    account: dict[str, Any],
    scan: list[dict[str, Any]] | None,
    *,
    incident: dict[str, Any],
) -> RepairResult:
    """Deterministic fixes first, then bounded LLM triage — always completes."""
    state["_last_incident"] = {
        "ts": time.time(),
        "phase": incident.get("phase"),
        "error": str(incident.get("error") or incident.get("detail") or "")[:400],
    }
    picture = build_operational_picture(state, client, llm, account, scan, incident)
    log_event(
        "system",
        "Repair triage started",
        f"phase={incident.get('phase')} error={str(incident.get('error') or '')[:120]}",
    )

    result = RepairResult()
    phase = str(incident.get("phase") or "")
    det = _deterministic_repair_plan(incident)
    deterministic_ran = False
    deterministic_recovered = False
    try:
        if det:
            deterministic_ran = True
            result = execute_repair_plan(state, client, llm, account, scan, det)
            result.diagnosis = result.diagnosis or str(det.get("diagnosis") or "")
            deterministic_recovered = result.recovered
            if result.recovered:
                _remember_incident(state, incident)
                log_event("system", "Repair recovered (deterministic)", result.diagnosis[:200])
                return result
            log_event(
                "system",
                "Deterministic repair incomplete",
                "; ".join(result.actions_taken)[:240] or "no actions",
            )

        consult_llm = _should_consult_repair_llm(
            state,
            llm,
            incident,
            deterministic_recovered=deterministic_recovered,
            deterministic_ran=deterministic_ran,
        )
        plan: dict[str, Any]
        if consult_llm:
            log_event(
                "system",
                "Repair consulting LLM",
                f"phase={phase} novel={not _incident_seen_before(state, incident)}",
            )
            try:
                plan = _call_repair_llm(llm, picture, state)
                _remember_incident(state, incident)
                log_event(
                    "llm",
                    "Repair diagnosis",
                    f"{plan.get('diagnosis', '')[:200]} | actions={len(plan.get('actions') or [])}",
                    {"plan": plan},
                )
            except Exception as exc:
                record_llm_failure(state, str(exc))
                log_event("error", "Repair LLM failed", str(exc)[:300])
                plan = _fallback_repair_plan(incident, picture)
                _remember_incident(state, incident)
        else:
            plan = _fallback_repair_plan(incident, picture)
            _remember_incident(state, incident)
            log_event(
                "system",
                "Repair script-only",
                f"phase={phase} known or repeat — skipped repair LLM",
            )

        result = execute_repair_plan(state, client, llm, account, scan, plan)
        result.diagnosis = result.diagnosis or str(plan.get("diagnosis") or "")
        if result.recovered:
            log_event("system", "Repair recovered", result.diagnosis[:200])
        else:
            log_event("system", "Repair complete (no recovery)", "; ".join(result.actions_taken)[:300])
        return result
    except Exception as exc:
        log_event("error", "Repair triage crashed", str(exc)[:300])
        fallback = _fallback_repair_plan(incident, picture)
        result = execute_repair_plan(state, client, llm, account, scan, fallback)
        result.diagnosis = result.diagnosis or str(fallback.get("diagnosis") or "")
        log_event("system", "Repair complete (crash fallback)", "; ".join(result.actions_taken)[:300])
        return result


def _fallback_repair_plan(incident: dict[str, Any], picture: dict[str, Any]) -> dict[str, Any]:
    """Safe plan when repair LLM is unavailable after all parallel attempts."""
    det = _deterministic_repair_plan(incident)
    if det:
        det["diagnosis"] = f"{det.get('diagnosis', '')} (LLM fallback)"
        return det

    actions: list[dict[str, Any]] = [
        {"type": "refresh_account", "params": {}},
        {"type": "wait_seconds", "params": {"seconds": 5}},
    ]
    if _incident_trader_offline(incident):
        actions.insert(0, {"type": "start_trader", "params": {}})
    health = picture.get("stack_health") or {}
    if health.get("rate_limited"):
        remain = float(health.get("rate_limit_remaining_sec") or 30)
        actions = [
            {"type": "wait_seconds", "params": {"seconds": min(remain + 2, 90)}},
            {"type": "bootstrap_account_cache", "params": {}},
        ]
    err = str(incident.get("error") or "").lower()
    phase = str(incident.get("phase") or "")
    if "102089" in err or "positionside" in err:
        actions.insert(0, {"type": "ensure_net_mode", "params": {}})
    if phase == "close_failed" and incident.get("instId"):
        actions.append({"type": "retry_close", "params": {"instId": incident["instId"]}})
    if phase == "tpsl_failed" and incident.get("instId"):
        actions.append(
            {
                "type": "retry_tpsl",
                "params": {
                    "instId": incident.get("instId"),
                    "side": incident.get("side"),
                    "contracts": incident.get("contracts"),
                    "price": incident.get("price"),
                    "tp_pct": incident.get("tp_pct", 2.0),
                    "sl_pct": incident.get("sl_pct", 1.0),
                },
            }
        )
    if phase in ("open_rejected", "open_failed") and incident.get("instId"):
        actions.append({"type": "ensure_net_mode", "params": {}})
        if "103003" in err:
            actions.append(
                {
                    "type": "raise_leverage_retry_open",
                    "params": {
                        "instId": incident.get("instId"),
                        "side": incident.get("side"),
                        "contracts": incident.get("contracts"),
                        "from_leverage": incident.get("leverage", 3),
                    },
                }
            )
    return {
        "diagnosis": "Repair LLM unavailable — deterministic fallback",
        "actions": actions[:5],
        "retry_original": phase in ("tpsl_failed", "close_failed"),
        "strategy_note": "Hold if errors persist after auto refresh",
    }


def repair_llm_pool(state: dict[str, Any], llm: Any, error: str = "") -> bool:
    if llm and hasattr(llm, "wait_for_provider") and llm.wait_for_provider(max_wait=None):
        record_repair(state, "llm_cooldown_wait", "provider pool recovered")
        _repair_meta(state)["consecutive_llm_failures"] = 0
        return True
    record_llm_failure(state, error)
    return False


def repair_stack(
    state: dict[str, Any],
    client: Any,
    llm: Any,
    account: dict[str, Any] | None = None,
    scan: list[dict[str, Any]] | None = None,
) -> RepairResult | None:
    """Per-cycle: kill dupes, then LLM triage if recent errors exist."""
    meta = _repair_meta(state)
    dupes = kill_duplicate_traders()
    if dupes:
        meta["duplicate_traders_killed"] = int(meta.get("duplicate_traders_killed") or 0) + dupes
        record_repair(state, "duplicate_traders", f"killed {dupes}")

    acct = account or {}
    from blofin.account_cache import _account_display_sane, read_account_cached

    display = read_account_cached()
    display_ok, display_reason = _account_display_sane(display)
    recent_errors = _meaningful_recent_errors(40)
    now = time.time()
    last_proactive = float(meta.get("last_proactive_repair_ts") or 0)
    needs_repair = bool(
        recent_errors
        or acct.get("stale")
        or acct.get("rate_limited")
        or not display_ok
    )
    if not needs_repair:
        return None
    if now - last_proactive < PROACTIVE_REPAIR_COOLDOWN_SEC and display_ok and not recent_errors:
        return None

    meta["last_proactive_repair_ts"] = now
    incident = {
        "phase": "cycle_proactive",
        "error": (
            recent_errors[-1].get("detail")
            if recent_errors
            else display_reason or "stale/rate_limited account"
        ),
        "title": recent_errors[-1].get("title") if recent_errors else "account_degraded",
        "recent_errors": _summarize_activity(recent_errors, 8),
    }
    return llm_triage_and_repair(state, client, llm, acct, scan, incident=incident)


def repair_cycle_crash(
    state: dict[str, Any],
    client: Any,
    llm: Any,
    account: dict[str, Any],
    scan: list[dict[str, Any]] | None,
    exc: Exception,
) -> RepairResult:
    meta = _repair_meta(state)
    meta["consecutive_cycle_errors"] = int(meta.get("consecutive_cycle_errors") or 0) + 1
    return llm_triage_and_repair(
        state,
        client,
        llm,
        account,
        scan,
        incident={"phase": "cycle_crash", "error": str(exc)[:400], "trace": type(exc).__name__},
    )


# Back-compat aliases used by agent imports
def repair_close(client: Any, inst: str) -> dict[str, Any] | None:
    return _exec_retry_close(client, {"instId": inst})


def repair_open_reject(
    client: Any,
    *,
    inst_id: str,
    side: str,
    contracts_str: str,
    err: str,
    tried_leverage: int,
    available: float,
    price: float,
) -> tuple[dict[str, Any], int, bool, str] | None:
    client._position_mode = None
    client.ensure_net_position_mode()
    resp = client.place_market_order(inst_id, side, contracts_str)
    rejected, err2 = client.order_rejected(resp)
    if not rejected:
        return resp, tried_leverage, False, err2
    resp2 = _exec_raise_leverage_retry(
        client,
        {"instId": inst_id, "side": side, "contracts": contracts_str, "from_leverage": tried_leverage},
    )
    if resp2:
        return resp2, tried_leverage, False, ""
    return resp, tried_leverage, True, err


def repair_tpsl(
    client: Any,
    *,
    inst_id: str,
    side: str,
    contracts_str: str,
    price: float,
    tp_pct: float,
    sl_pct: float,
    err: str,
) -> dict[str, Any] | None:
    return _exec_retry_tpsl(
        client,
        {"instId": inst_id, "side": side, "contracts": contracts_str, "price": price, "tp_pct": tp_pct, "sl_pct": sl_pct},
    )
