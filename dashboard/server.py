"""LLM KnightTrader dashboard — FastAPI + WebSocket."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from activity_log import get_recent, load_history, log_event, subscribe, tail_new_events, unsubscribe
from blofin.account_cache import bootstrap_account_cache, read_account_cached, refresh_account_if_stale
from blofin.client import BlofinClient
from config import APP_NAME, CHAT_AGENT_NAME, DASHBOARD_HOST, DASHBOARD_PORT, MISSION_PROMPT, TARGET_EQUITY
from llm.wrapper import LLMWrapper
from trader.prompts import CHAT_SYSTEM
from trader.directives import operator_instructions
from trader.baseline import parse_baseline_command, progress_summary, set_user_baseline
from trader.learning import lessons_digest
from trader.order_guard import execution_context
from trader.stack_control import restart_traders, stack_status
from trader.stack_operator import run_operator_cycle
from trader.stack_watchdog import diagnose_stack, mark_dashboard_boot, run_stack_watchdog
from blofin.account_cache import guard_account_stream
from trader.state import append_chat, append_user_directive, load_state, reload_chat_fields, save_state

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _last_tail_ts, _main_loop
    _main_loop = asyncio.get_running_loop()
    load_history()
    subscribe(_on_activity)
    if get_recent(1):
        _last_tail_ts = float(get_recent(1)[-1].get("ts") or 0)
    await asyncio.to_thread(_bootstrap_account_cache)
    try:
        await asyncio.to_thread(refresh_account_if_stale, force=True)
    except FileNotFoundError as exc:
        log_event("error", "BloFin credentials missing — dashboard online, trading blocked", str(exc)[:240])
    except Exception as exc:
        log_event("error", "Account cache refresh failed at startup", str(exc)[:240])
    mark_dashboard_boot()
    tail_task = asyncio.create_task(_tail_activity_loop())
    account_task = asyncio.create_task(_account_stream_loop())
    watchdog_task = asyncio.create_task(_stack_watchdog_loop())
    log_event("system", f"{APP_NAME} dashboard online", f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
    try:
        yield
    finally:
        unsubscribe(_on_activity)
        tail_task.cancel()
        account_task.cancel()
        watchdog_task.cancel()
        for task in (tail_task, account_task, watchdog_task):
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title=f"{APP_NAME} Dashboard", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_client: BlofinClient | None = None
_llm: LLMWrapper | None = None
_ws_clients: set[WebSocket] = set()
_last_tail_ts: float = 0.0
_main_loop: asyncio.AbstractEventLoop | None = None


def _get_client() -> BlofinClient:
    global _client
    if _client is None:
        _client = BlofinClient()
    return _client


def _get_llm() -> LLMWrapper:
    global _llm
    if _llm is None:
        _llm = LLMWrapper()
    return _llm


async def _broadcast_json(payload: dict[str, Any]) -> None:
    dead: list[WebSocket] = []
    for ws in list(_ws_clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


async def _broadcast_activity(event: dict[str, Any]) -> None:
    await _broadcast_json({"type": "activity", "event": event})


def _schedule_activity_broadcast(event: dict[str, Any]) -> None:
    """Thread-safe: push activity events to all websocket clients."""
    loop = _main_loop
    if loop is None or not loop.is_running():
        return

    def _dispatch() -> None:
        asyncio.create_task(_broadcast_activity(event))

    loop.call_soon_threadsafe(_dispatch)


def _on_activity(event: dict[str, Any]) -> None:
    _schedule_activity_broadcast(event)


class ChatRequest(BaseModel):
    message: str


class BaselineSetRequest(BaseModel):
    """Set performance baseline (Baseline Δ zero point)."""
    equity: float | None = None
    use_current: bool = True
    reason: str = "user set via dashboard"


def _apply_user_baseline(
    state: dict[str, Any],
    account: dict[str, Any],
    *,
    equity: float | None = None,
    use_current: bool = True,
    reason: str = "user set via dashboard",
) -> dict[str, Any]:
    eq = None if use_current and equity is None else equity
    bl = set_user_baseline(state, account, equity=eq, reason=reason)
    save_state(state)
    summary = progress_summary(state, account)
    log_event(
        "system",
        "Baseline set by user",
        f"${bl.get('baseline_equity'):.4f} — Δ now vs current ${float(account.get('equity') or 0):.4f}",
        {"performance_baseline": summary},
    )
    return summary


def _bootstrap_account_cache() -> None:
    from blofin.account_cache import bootstrap_account_cache

    bootstrap_account_cache()


async def _stack_watchdog_loop() -> None:
    """Full-time stack operator — reconcile processes + account repair + LLM escalation."""
    # Operator cycle runs every ~15s. It handles process dedupe, trader start,
    # monitor/watcher cleanup, and escalates to repair LLM if stuck.
    while True:
        await asyncio.sleep(15)
        try:
            result = await asyncio.to_thread(run_operator_cycle)
            if result.get("repaired") or result.get("llm_used") or result.get("actions"):
                from blofin.account_cache import read_account_cached

                account = await asyncio.to_thread(read_account_cached)
                await _broadcast_json(
                    {
                        "type": "stack_repair",
                        "result": result,
                        "account": account,
                    }
                )
        except Exception as exc:
            log_event("error", "Stack operator cycle failed", str(exc)[:200])


async def _account_stream_loop() -> None:
    """Push verified account to UI — guardian checks live BloFin every ~3s."""
    from blofin.account_cache import read_account_cached

    tick = 0
    while True:
        await asyncio.sleep(0.5)
        tick += 1
        try:
            if tick % 6 == 0:
                result = await asyncio.to_thread(guard_account_stream)
                account = result.get("account") or await asyncio.to_thread(read_account_cached)
                payload: dict[str, Any] = {
                    "type": "account_update",
                    "account": account,
                    "stream_guardian": {
                        "ok": result.get("ok", True),
                        "live_verified": result.get("live_verified", False),
                        "refreshed": result.get("refreshed", False),
                        "live_age_sec": result.get("live_age_sec"),
                    },
                }
                if result.get("drift"):
                    payload["stream_guardian"]["drift"] = result["drift"][:5]
            else:
                account = await asyncio.to_thread(read_account_cached)
                payload = {"type": "account_update", "account": account}
            await _broadcast_json(payload)
        except Exception as exc:
            await _broadcast_json({"type": "account_error", "error": str(exc)})


def _refresh_market_marks() -> None:
    from blofin.market_cache import fetch_public_tickers, get_cached_instruments, get_cached_tickers

    try:
        fetch_public_tickers()
    except Exception:
        get_cached_tickers(allow_stale=True)
    try:
        if not get_cached_instruments(allow_stale=True):
            _get_client().get_instruments("SWAP")
    except Exception:
        pass


async def _tail_activity_loop() -> None:
    global _last_tail_ts
    while True:
        await asyncio.sleep(2)
        rows = await asyncio.to_thread(tail_new_events, _last_tail_ts)
        if rows:
            _last_tail_ts = float(rows[-1].get("ts") or _last_tail_ts)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def api_health() -> dict[str, str]:
    return {"status": "ok", "app": APP_NAME}


@app.get("/api/stack/status")
async def api_stack_status() -> dict[str, Any]:
    stack = await asyncio.to_thread(stack_status)
    watchdog = await asyncio.to_thread(diagnose_stack)
    return {"stack": stack, "issues": watchdog, "healthy": not watchdog}


@app.post("/api/stack/repair")
async def api_stack_repair() -> dict[str, Any]:
    result = await asyncio.to_thread(run_stack_watchdog, allow_llm=True)
    return result


@app.post("/api/stack/restart")
async def api_stack_restart() -> dict[str, Any]:
    result = await asyncio.to_thread(restart_traders)
    log_event(
        "system",
        "Restart traders requested",
        json.dumps(
            {
                "mode": result.get("mode"),
                "spawned_pid": result.get("spawned_pid"),
                "ok": result.get("ok"),
                "error": result.get("error"),
            }
        ),
    )
    return result


@app.get("/api/account")
async def api_account(force: bool = False) -> dict[str, Any]:
    try:
        if force:
            await asyncio.to_thread(refresh_account_if_stale, force=True)
        else:
            await asyncio.to_thread(refresh_account_if_stale)
        account = await asyncio.to_thread(read_account_cached)
    except Exception as exc:
        account = read_account_cached()
        account["error"] = str(exc)
    return {"account": account}


@app.post("/api/baseline/set")
async def api_baseline_set(req: BaselineSetRequest) -> dict[str, Any]:
    state = load_state()
    account = await asyncio.to_thread(read_account_cached)
    if req.equity is not None and req.equity <= 0:
        return {"ok": False, "error": "equity must be positive"}
    try:
        summary = await asyncio.to_thread(
            _apply_user_baseline,
            state,
            account,
            equity=req.equity,
            use_current=req.use_current if req.equity is None else False,
            reason=req.reason[:200],
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "performance_baseline": summary}


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    state = load_state()
    llm = _get_llm()
    account = await asyncio.to_thread(read_account_cached)
    return {
        "app_name": APP_NAME,
        "chat_agent_name": CHAT_AGENT_NAME,
        "mission": MISSION_PROMPT,
        "target_equity": TARGET_EQUITY,
        "account": account,
        "state": {
            "cycles": state.get("cycles", 0),
            "peak_equity": state.get("peak_equity", 0),
            "last_decision": state.get("last_decision"),
            "research_count": len(state.get("research_notes", [])),
            "trade_count": len(state.get("trades", [])),
        },
        "llm": llm.status(),
        "performance_baseline": progress_summary(state, account),
    }


@app.get("/api/activity")
async def api_activity(limit: int = 200) -> dict[str, Any]:
    global _last_tail_ts
    rows = await asyncio.to_thread(tail_new_events, _last_tail_ts)
    if rows:
        _last_tail_ts = float(rows[-1].get("ts") or _last_tail_ts)
    return {"events": get_recent(limit)}


@app.get("/api/trades")
async def api_trades() -> dict[str, Any]:
    state = load_state()
    return {"trades": state.get("trades", [])}


@app.get("/api/research")
async def api_research() -> dict[str, Any]:
    state = load_state()
    return {
        "notes": state.get("research_notes", []),
        "lessons": state.get("lessons", [])[-30:],
    }


@app.get("/api/chat")
async def api_chat_history() -> dict[str, Any]:
    state = load_state()
    return {"messages": state.get("chat_history", [])}


def _run_chat(req: ChatRequest) -> dict[str, Any]:
    state = reload_chat_fields(load_state())
    append_chat(state, "user", req.message)
    append_user_directive(state, req.message, source="chat")
    save_state(state)
    log_event("chat", "You", req.message, {"status": "received", "wired_to_trader": True})

    def _is_manual_repair_message(msg: str) -> bool:
        m = (msg or "").strip().lower()
        # Explicit "repair/stop trading and fix" commands to avoid accidental trading actions.
        return m.startswith("repair:") or m.startswith("fix:") or m.startswith("repair ") or m.startswith("fix ")

    if _is_manual_repair_message(req.message):
        # Queue: repair techs watch for this in the activity log.
        log_event("system", "Manual repair request", req.message[:2000], {"source": "dashboard_chat"})
        reply = (
            "Repair request queued for the autonomous repair techs. "
            "Watch the Activity Log and repair_* logs for progress."
        )
        append_chat(state, "assistant", reply)
        save_state(state)
        log_event("chat", CHAT_AGENT_NAME, reply, {"status": "queued_manual_repair"})
        return {"reply": reply, "queued_manual_repair": True}

    try:
        from blofin.account_cache import read_account_cached

        account = read_account_cached()
    except Exception as exc:
        account = {"error": str(exc)}

    baseline_cmd = parse_baseline_command(req.message)
    if baseline_cmd and "error" not in account:
        try:
            summary = _apply_user_baseline(
                state,
                account,
                equity=baseline_cmd.get("equity"),
                use_current=bool(baseline_cmd.get("use_current")),
                reason=str(baseline_cmd.get("reason") or "user chat"),
            )
            eq = summary.get("baseline_equity")
            ch = summary.get("equity_change_usd")
            pct = summary.get("equity_change_pct")
            reply = (
                f"Baseline set to **${eq:.4f}**. Baseline Δ is now "
                f"**{'+' if (ch or 0) >= 0 else ''}${ch:.4f}** ({'+' if (pct or 0) >= 0 else ''}{pct:.1f}%) "
                f"vs current equity **${float(summary.get('current_equity') or 0):.4f}**."
            )
            append_chat(state, "assistant", reply)
            save_state(state)
            log_event("chat", CHAT_AGENT_NAME, reply, {"status": "complete", "baseline_set": True})
            return {"reply": reply, "performance_baseline": summary, "baseline_set": True}
        except ValueError as exc:
            reply = f"Could not set baseline: {exc}"
            append_chat(state, "assistant", reply)
            save_state(state)
            return {"reply": reply, "error": str(exc)}

    account_brief = {
        "equity": account.get("equity"),
        "available": account.get("available"),
        "positions": account.get("positions"),
        "upl_total": account.get("upl_total"),
        "stale": account.get("stale"),
        "rate_limited": account.get("rate_limited"),
    }
    ops = operator_instructions(state)
    context = json.dumps(
        {
            "account": account_brief,
            "hermes_memory": {"lessons": lessons_digest(state, limit=12)},
            "execution_guard": execution_context(state, account) if "error" not in account else {},
            "last_decision": state.get("last_decision"),
            "recent_research": state.get("research_notes", [])[-3:],
            "recent_trades": state.get("trades", [])[-3:],
            "operator_instructions": ops,
            "recent_chat_thread": state.get("chat_history", [])[-12:],
        },
        indent=2,
    )
    llm = _get_llm()
    log_event("chat", CHAT_AGENT_NAME, "Thinking…", {"status": "thinking", "message": req.message[:200]})
    if hasattr(llm, "wait_for_provider"):
        llm.wait_for_provider(max_wait=None)
    chat_fn = llm.chat_race if hasattr(llm, "chat_race") else llm.chat
    response = chat_fn(
        [
            {
                "role": "user",
                "content": (
                    f"Context:\n{context}\n\n"
                    f"User message: {req.message}\n\n"
                    "Reply as the live trading agent. Acknowledge the user's instruction and confirm "
                    "it is saved to operator_instructions for the autonomous trader loop."
                ),
            },
        ],
        system=CHAT_SYSTEM,
        max_tokens=2000,
    )
    append_chat(state, "assistant", response.text)
    save_state(state)
    log_event(
        "chat",
        CHAT_AGENT_NAME,
        response.text,
        {"provider": response.provider, "model": response.model, "status": "complete"},
    )
    return {
        "reply": response.text,
        "provider": response.provider,
        "model": response.model,
        "latency_ms": response.latency_ms,
        "status": "complete",
    }


@app.post("/api/chat")
async def api_chat(req: ChatRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_run_chat, req)
    except Exception as exc:
        log_event("error", "Chat failed", str(exc), {"message": req.message[:200]})
        return {
            "reply": f"I hit an error processing that: {exc}. Your message was saved — try again in a moment.",
            "provider": "error",
            "model": "",
            "latency_ms": 0,
            "status": "error",
            "error": str(exc),
        }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _ws_clients.add(ws)
    try:
        account = await asyncio.to_thread(read_account_cached)
        await ws.send_json({"type": "snapshot", "events": get_recent(100), "account": account})
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "dashboard.server:app",
        host=DASHBOARD_HOST,
        port=DASHBOARD_PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
