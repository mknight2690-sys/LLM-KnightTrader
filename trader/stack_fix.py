"""Stack fix playbook — what the watchdog and repair LLM should do for each issue."""

from __future__ import annotations

from typing import Any

STACK_FIX_PLAYBOOK = """
LLM KnightTrader stack repair (stack_operator + repair LLM):

FULL-TIME STACK OPERATOR (dashboard loop every 15s)
- reconcile_stack: kill monitor/watchers, dedupe dashboard+trader, start trader if offline
- After 2 failed reconciles: repair LLM triage with stack_fix_context
- cold_start_stack / cold_stop_stack: desktop Start and Stop shortcuts
- restart_traders: dashboard button + spawn stack_launcher.py start (detached)

PROCESS / LAUNCHER
- trader_offline (after 45s dashboard boot grace): reconcile_stack → start_trader
- trader_duplicate / dashboard_duplicate: dedupe, then start if needed
- extra_bots_running (monitor/watchers): kill_extra_bots via reconcile
- desktop_shortcuts_missing: create_desktop_shortcuts (MANDATORY during agent setup)
- Daily user control: desktop Start LLM KnightTrader / Stop LLM KnightTrader ONLY
- Never use python -m trader.agent or python -m dashboard.server for daily ops
- Full cold restart: python scripts/stack_launcher.py start

CREDENTIALS
- credentials_missing: ask user for BloFin keys in chat or file path; write credentials/blofin.txt + .env
- Auto-discovery order: BLOFIN_CREDENTIALS_PATH, credentials/blofin.txt, Downloads compendium

ACCOUNT / CACHE
- account_display_corrupt / live_stream_drift: bootstrap_account_cache + refresh_account
- account_rate_limited: wait (backoff), then refresh_account — do not hammer API
- account_cache_stale: refresh_account
- equity $0 but trades exist: bootstrap_account_cache

TRADING
- Position mode 102089: ensure_net_mode then retry
- Margin 103003: raise_leverage_retry_open or redirect_open
- LLM failures: wait_llm_cooldown, hold — do not force trades

WINDOWS
- Stale trader.lock / trader.pid: stop stack (stack_launcher.py stop) clears locks
- Trader launch: -c "from trader.agent import main; main()" with KNIGHTTRADER_PYTHON pinned
- Dashboard launch: -c "from dashboard.server import main; main()"
- taskkill /T for process trees on stop
""".strip()


def stack_fix_context() -> dict[str, Any]:
    """Live stack facts for repair LLM and watchdog."""
    from credentials import resolve_blofin_credentials_path
    from trader.stack_control import desktop_shortcuts_exist, desktop_shortcut_paths, running_process_counts, stack_status

    paths = desktop_shortcut_paths()
    return {
        "playbook": STACK_FIX_PLAYBOOK,
        "process_counts": running_process_counts(),
        "stack_status": stack_status(),
        "desktop_shortcuts_exist": desktop_shortcuts_exist(),
        "desktop_shortcut_paths": {k: str(v) for k, v in paths.items()},
        "blofin_credentials_found": resolve_blofin_credentials_path() is not None,
    }
