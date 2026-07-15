"""
LLM KnightTrader — Central Orchestrator

Wires all agents (KT native + Owl Swarm) into a single coordinated stack.
Manages agent lifecycle, runs the trading pipeline, and exposes status
for the dashboard.

Each agent runs as its own subprocess with its own LLMWrapper instance
on a unique top-tier free model. The orchestrator coordinates the pipeline
but does NOT share LLM instances across agents — each agent is fully isolated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Project root injection
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import APP_NAME, DATA_DIR, PID_DIR, PROJECT_ROOT
from llm.model_registry import AGENT_MODEL_MAP, resolve_model_for_agent, get_rotation_state
from trader.health import _pid_alive
from trader.stack_control import (
    _enumerate_python_processes,
    _kill_pid,
    _subprocess_kwargs,
    _trader_python,
    dedupe_preferred_module,
    kill_all_bots,
    stack_status,
)

ORCHESTRATOR_ERR_LOG = DATA_DIR / "logs" / "orchestrator.err"
ORCHESTRATOR_ERR_LOG.parent.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Agent Registry — every agent in the stack
# ─────────────────────────────────────────────────────────────────────────────

# Categories for grouping in UI
CAT_TRADING = "trading"
CAT_SUPPORT = "support"
CAT_PENTEST = "pentest"
CAT_NATIVE = "native"


class AgentMeta:
    """Metadata for a single agent in the orchestrated stack."""

    def __init__(
        self,
        name: str,
        label: str,
        module: str,
        category: str,
        description: str,
        openrouter_model: str | None = None,
        interval_sec: float = 30.0,
    ):
        self.name = name
        self.label = label
        self.module = module
        self.category = category
        self.description = description
        self.openrouter_model = openrouter_model or resolve_model_for_agent(name)
        self.interval_sec = interval_sec

    def lock_file(self) -> Path:
        return PID_DIR / f"repair_agent_{self.name}.lock"

    def pid_file(self) -> Path:
        return PID_DIR / f"agent_{self.name}.pid"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "module": self.module,
            "category": self.category,
            "description": self.description,
            "openrouter_model": self.openrouter_model,
            "interval_sec": self.interval_sec,
        }


# ── All agents in the LLM KnightTrader stack ─────────────────────────────────
# Native agents already managed by stack_control.py
# Owl Swarm agents managed by the orchestrator

AGENT_REGISTRY: dict[str, AgentMeta] = {
    # ── Trading Pipeline (Owl Swarm) ──
    "director": AgentMeta(
        name="director",
        label="Trading Director",
        module="trader.agents.director",
        category=CAT_TRADING,
        description="Orchestrates the full trading pipeline each cycle",
        openrouter_model=AGENT_MODEL_MAP.get("Trading-Director"),
        interval_sec=60.0,
    ),
    "portfolio": AgentMeta(
        name="portfolio",
        label="Portfolio Manager",
        module="trader.agents.portfolio",
        category=CAT_TRADING,
        description="Portfolio sizing, allocation, and rebalancing",
        openrouter_model=AGENT_MODEL_MAP.get("Portfolio-Manager"),
        interval_sec=45.0,
    ),
    "sentiment": AgentMeta(
        name="sentiment",
        label="Sentiment Analyst",
        module="trader.agents.sentiment",
        category=CAT_TRADING,
        description="Market sentiment and social signal analysis",
        openrouter_model=AGENT_MODEL_MAP.get("Sentiment-Agent"),
        interval_sec=40.0,
    ),
    "quant": AgentMeta(
        name="quant",
        label="Quant Analyst",
        module="trader.agents.quant",
        category=CAT_TRADING,
        description="Technical analysis, signals, and statistical edge",
        openrouter_model=AGENT_MODEL_MAP.get("Quant-Analyst"),
        interval_sec=35.0,
    ),
    "risk": AgentMeta(
        name="risk",
        label="Risk Manager",
        module="trader.agents.risk",
        category=CAT_TRADING,
        description="Position risk, drawdown limits, and stop-loss logic",
        openrouter_model=AGENT_MODEL_MAP.get("Risk-Manager"),
        interval_sec=30.0,
    ),
    "execution": AgentMeta(
        name="execution",
        label="Execution Agent",
        module="trader.agents.execution",
        category=CAT_TRADING,
        description="Order routing, execution quality, and slippage control",
        openrouter_model=AGENT_MODEL_MAP.get("Execution-Agent"),
        interval_sec=25.0,
    ),
    "verifier": AgentMeta(
        name="verifier",
        label="Verifier",
        module="trader.agents.verifier",
        category=CAT_SUPPORT,
        description="Cross-checks decisions and flags inconsistencies",
        openrouter_model=AGENT_MODEL_MAP.get("Verifier-Agent"),
        interval_sec=50.0,
    ),
    "ops_monitor": AgentMeta(
        name="ops_monitor",
        label="Ops Monitor",
        module="trader.agents.ops_monitor",
        category=CAT_SUPPORT,
        description="System health, uptime, and resource monitoring",
        openrouter_model=AGENT_MODEL_MAP.get("Ops-Monitor-Agent"),
        interval_sec=60.0,
    ),
    "tactics_researcher": AgentMeta(
        name="tactics_researcher",
        label="Tactics Researcher",
        module="trader.agents.tactics_researcher",
        category=CAT_SUPPORT,
        description="Tactical research and short-term edge discovery",
        openrouter_model=AGENT_MODEL_MAP.get("Tactics-Researcher-Agent"),
        interval_sec=120.0,
    ),
    "profit_strategist": AgentMeta(
        name="profit_strategist",
        label="Profit Strategist",
        module="trader.agents.profit_strategist",
        category=CAT_SUPPORT,
        description="Profit-taking strategy and exit optimization",
        openrouter_model=AGENT_MODEL_MAP.get("Profit-Strategist-Agent"),
        interval_sec=90.0,
    ),
    "market_researcher": AgentMeta(
        name="market_researcher",
        label="Market Researcher",
        module="trader.agents.market_researcher",
        category=CAT_SUPPORT,
        description="Macro research, sector rotation, and regime detection",
        openrouter_model=AGENT_MODEL_MAP.get("Market-Researcher-Agent"),
        interval_sec=180.0,
    ),
    # ── Pentest Agents ──
    "pentest_scout": AgentMeta(
        name="pentest_scout",
        label="Pentest Scout",
        module="trader.agents.pentest_scout",
        category=CAT_PENTEST,
        description="Reconnaissance and vulnerability scanning for trade ops",
        openrouter_model=AGENT_MODEL_MAP.get("Pentest-Scout-Agent"),
        interval_sec=120.0,
    ),
    "pentest_trade_hunter": AgentMeta(
        name="pentest_trade_hunter",
        label="Pentest Trade Hunter",
        module="trader.agents.pentest_trade_hunter",
        category=CAT_PENTEST,
        description="Hunts edge-case trade opportunities and anomalies",
        openrouter_model=AGENT_MODEL_MAP.get("Pentest-Trade-Hunter-Agent"),
        interval_sec=60.0,
    ),
    "pentest_integrity": AgentMeta(
        name="pentest_integrity",
        label="Pentest Integrity",
        module="trader.agents.pentest_integrity",
        category=CAT_PENTEST,
        description="Data integrity checks and audit trail validation",
        openrouter_model=AGENT_MODEL_MAP.get("Pentest-Integrity-Agent"),
        interval_sec=90.0,
    ),
    "pentest_operator": AgentMeta(
        name="pentest_operator",
        label="Pentest Operator",
        module="trader.agents.pentest_operator",
        category=CAT_PENTEST,
        description="Coordinated pentest execution and red-team ops",
        openrouter_model=AGENT_MODEL_MAP.get("Pentest-Operator-Agent"),
        interval_sec=120.0,
    ),
}


# ── Native stack components (not managed as subprocesses by orchestrator,
#    but tracked for status display) ──────────────────────────────────────────

NATIVE_COMPONENTS: dict[str, dict[str, Any]] = {
    "trader": {
        "name": "trader",
        "label": "Main Trader",
        "category": CAT_NATIVE,
        "description": "Primary trading agent — positions, orders, account sync",
        "module": "trader.agent",
    },
    "dashboard": {
        "name": "dashboard",
        "label": "Dashboard",
        "category": CAT_NATIVE,
        "description": "Web UI and API server",
        "module": "dashboard.server",
    },
    "monitor": {
        "name": "monitor",
        "label": "Monitor",
        "category": CAT_NATIVE,
        "description": "Process monitor and babysitter",
        "module": "monitor.agent",
    },
    "watchdog": {
        "name": "watchdog",
        "label": "Watchdog",
        "category": CAT_NATIVE,
        "description": "Stack health watchdog and auto-repair",
        "module": "watch_and_fix",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Process Management
# ─────────────────────────────────────────────────────────────────────────────

def _agent_pid_from_lock(agent: AgentMeta) -> int | None:
    """Read PID from the agent's lock file (repair_agent uses .lock)."""
    lock = agent.lock_file()
    if not lock.is_file():
        return None
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    if pid > 0 and _pid_alive(pid):
        return pid
    return None


def _agent_pid_from_cmdline(agent: AgentMeta) -> int | None:
    """Fallback: scan running processes for the agent's module."""
    mod = agent.module
    for row in _enumerate_python_processes():
        cmd = row.get("cmd", "")
        if f"-m {mod}" in cmd or mod.replace(".", "\\") in cmd or mod.replace(".", "/") in cmd:
            pid = row.get("pid", 0)
            if pid and _pid_alive(pid):
                return pid
    return None


def agent_status(agent: AgentMeta) -> dict[str, Any]:
    """Return live status for a single agent."""
    pid = _agent_pid_from_lock(agent) or _agent_pid_from_cmdline(agent)
    alive = bool(pid and _pid_alive(pid))
    return {
        "name": agent.name,
        "label": agent.label,
        "module": agent.module,
        "category": agent.category,
        "description": agent.description,
        "openrouter_model": agent.openrouter_model,
        "interval_sec": agent.interval_sec,
        "status": "online" if alive else "offline",
        "pid": pid,
        "alive": alive,
    }


def all_agent_statuses() -> list[dict[str, Any]]:
    """Return status for every registered agent."""
    return [agent_status(meta) for meta in AGENT_REGISTRY.values()]


def all_native_statuses() -> dict[str, dict[str, Any]]:
    """Return status for native stack components (from stack_control)."""
    stack = stack_status()
    out: dict[str, Any] = {}
    for key, comp in NATIVE_COMPONENTS.items():
        if key == "trader":
            out[key] = {
                **comp,
                "status": stack.get("trader", {}).get("status", "offline"),
                "pid": stack.get("trader", {}).get("pid"),
                "count": stack.get("trader", {}).get("count", 0),
            }
        elif key == "dashboard":
            out[key] = {
                **comp,
                "status": stack.get("dashboard", {}).get("status", "offline"),
                "pid": stack.get("dashboard", {}).get("pid"),
                "count": stack.get("dashboard", {}).get("count", 0),
            }
        elif key == "monitor":
            out[key] = {
                **comp,
                "status": stack.get("monitor", {}).get("status", "offline"),
                "pid": stack.get("monitor", {}).get("pid"),
                "count": stack.get("monitor", {}).get("count", 0),
            }
        else:
            out[key] = {**comp, "status": "unknown", "pid": None, "count": 0}
    return out


def start_agent(agent: AgentMeta) -> dict[str, Any]:
    """Start an agent as a detached subprocess."""
    # Check if already running
    existing = _agent_pid_from_lock(agent) or _agent_pid_from_cmdline(agent)
    if existing and _pid_alive(existing):
        return {"ok": True, "pid": existing, "already_running": True, "agent": agent.name}

    # Ensure log dir
    ORCHESTRATOR_ERR_LOG.parent.mkdir(parents=True, exist_ok=True)
    err_log = open(ORCHESTRATOR_ERR_LOG, "a", encoding="utf-8")
    err_log.write(f"\n--- {agent.name} start {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    err_log.flush()

    python_bin = _trader_python()
    kwargs = _subprocess_kwargs(stderr=err_log)
    cmd = [python_bin, "-m", agent.module]

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "agent": agent.name}

    # Write PID file
    agent.pid_file().write_text(str(proc.pid), encoding="utf-8")

    # Give it a moment to establish its own lock
    time.sleep(1.5)
    verified_pid = _agent_pid_from_lock(agent) or _agent_pid_from_cmdline(agent)

    if verified_pid and _pid_alive(verified_pid):
        return {
            "ok": True,
            "pid": verified_pid,
            "agent": agent.name,
            "started": True,
        }

    # If process exited quickly, clean up
    if proc.poll() is not None:
        return {
            "ok": False,
            "error": f"Agent exited immediately (code {proc.returncode})",
            "agent": agent.name,
        }

    return {
        "ok": True,
        "pid": proc.pid,
        "agent": agent.name,
        "started": True,
        "warning": "PID verification inconclusive, process spawned",
    }


def stop_agent(agent: AgentMeta) -> dict[str, Any]:
    """Stop an agent by killing its lock owner and any matching processes."""
    killed: list[int] = []
    errors: list[str] = []

    # Try lock PID first
    lock_pid = _agent_pid_from_lock(agent)
    if lock_pid and _kill_pid(lock_pid):
        killed.append(lock_pid)

    # Also try cmdline match
    cmd_pid = _agent_pid_from_cmdline(agent)
    if cmd_pid and cmd_pid not in killed and _kill_pid(cmd_pid):
        killed.append(cmd_pid)

    # Clean up PID file
    try:
        agent.pid_file().unlink(missing_ok=True)
    except OSError as exc:
        errors.append(str(exc))

    # Clean up lock file if no process owns it
    try:
        lock = agent.lock_file()
        if lock.is_file():
            raw = lock.read_text(encoding="utf-8").strip()
            try:
                owner = int(raw)
                if not _pid_alive(owner):
                    lock.unlink(missing_ok=True)
            except ValueError:
                lock.unlink(missing_ok=True)
    except OSError as exc:
        errors.append(str(exc))

    return {
        "ok": len(killed) > 0,
        "killed_pids": killed,
        "agent": agent.name,
        "errors": errors[:3] if errors else None,
    }


def restart_agent(agent: AgentMeta) -> dict[str, Any]:
    """Stop then start an agent."""
    stop_agent(agent)
    time.sleep(1.0)
    return start_agent(agent)


def start_all_agents() -> dict[str, Any]:
    """Start every registered agent."""
    results: list[dict[str, Any]] = []
    for meta in AGENT_REGISTRY.values():
        res = start_agent(meta)
        results.append(res)
        # Small stagger to avoid thundering herd on LLM APIs
        time.sleep(0.5)
    return {"ok": all(r.get("ok") for r in results), "results": results}


def stop_all_agents() -> dict[str, Any]:
    """Stop every registered agent."""
    results: list[dict[str, Any]] = []
    for meta in AGENT_REGISTRY.values():
        res = stop_agent(meta)
        results.append(res)
    return {"ok": all(r.get("ok") for r in results), "results": results}


def stop_all_agents_and_bots() -> dict[str, Any]:
    """Full stop: all orchestrated agents + all native bots + dashboard."""
    agent_results = stop_all_agents()
    killed = kill_all_bots(exclude_pids={os.getpid()})
    return {
        "ok": True,
        "agents": agent_results,
        "native_killed": killed,
        "message": "Full stop complete — all agents and native bots stopped",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline Coordination
# ─────────────────────────────────────────────────────────────────────────────

TRADING_PIPELINE = ["director", "portfolio", "sentiment", "quant", "risk", "execution", "verifier"]


def run_pipeline_cycle() -> dict[str, Any]:
    """
    Run one coordinated trading pipeline cycle.
    Each agent is triggered in sequence via their subprocess (if running).
    If an agent is offline, the pipeline skips it and logs a warning.
    """
    from activity_log import log_event

    results: list[dict[str, Any]] = []
    t0 = time.time()

    for name in TRADING_PIPELINE:
        meta = AGENT_REGISTRY.get(name)
        if not meta:
            continue

        status = agent_status(meta)
        if not status["alive"]:
            log_event("warning", f"Pipeline skip: {meta.label}", "Agent offline — skipping stage")
            results.append({"agent": name, "skipped": True, "reason": "offline"})
            continue

        # Agents run autonomously in their own subprocesses.
        # The pipeline just verifies they're alive and logs the stage.
        # For a tighter integration, we'd send IPC signals or use a message queue.
        # For now, the "pipeline" is the orchestrator confirming each stage is active.
        results.append({
            "agent": name,
            "ok": True,
            "pid": status["pid"],
            "model": status["openrouter_model"],
        })

    elapsed = round(time.time() - t0, 2)
    log_event("system", "Pipeline cycle complete", f"{len(results)} stages in {elapsed}s")
    return {"ok": True, "stages": results, "elapsed_sec": elapsed}


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard-facing API helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_full_stack_status() -> dict[str, Any]:
    """Return complete status of the entire orchestrated stack."""
    return {
        "app_name": APP_NAME,
        "agents": all_agent_statuses(),
        "native": all_native_statuses(),
        "rotation": get_rotation_state(),
        "stack": stack_status(),
    }


def get_agent_by_name(name: str) -> AgentMeta | None:
    return AGENT_REGISTRY.get(name)


def agent_exists(name: str) -> bool:
    return name in AGENT_REGISTRY


# ─────────────────────────────────────────────────────────────────────────────
# CLI entrypoint (for testing / manual control)
# ─────────────────────────────────────────────────────────────────────────────

def _main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="LLM KnightTrader Orchestrator")
    sub = parser.add_subparsers(dest="cmd")

    # status
    sub.add_parser("status", help="Show full stack status")

    # start
    p_start = sub.add_parser("start", help="Start an agent or all agents")
    p_start.add_argument("agent", nargs="?", help="Agent name (omit for all)")

    # stop
    p_stop = sub.add_parser("stop", help="Stop an agent or all agents")
    p_stop.add_argument("agent", nargs="?", help="Agent name (omit for all)")

    # restart
    p_restart = sub.add_parser("restart", help="Restart an agent or all agents")
    p_restart.add_argument("agent", nargs="?", help="Agent name (omit for all)")

    # pipeline
    sub.add_parser("pipeline", help="Run one pipeline cycle")

    # stop-all
    sub.add_parser("stop-all", help="Stop everything (agents + native bots)")

    args = parser.parse_args()

    if args.cmd == "status":
        print(json.dumps(get_full_stack_status(), indent=2, default=str))
    elif args.cmd == "start":
        if args.agent:
            meta = get_agent_by_name(args.agent)
            if not meta:
                print(f"Unknown agent: {args.agent}")
                sys.exit(1)
            print(json.dumps(start_agent(meta), indent=2, default=str))
        else:
            print(json.dumps(start_all_agents(), indent=2, default=str))
    elif args.cmd == "stop":
        if args.agent:
            meta = get_agent_by_name(args.agent)
            if not meta:
                print(f"Unknown agent: {args.agent}")
                sys.exit(1)
            print(json.dumps(stop_agent(meta), indent=2, default=str))
        else:
            print(json.dumps(stop_all_agents(), indent=2, default=str))
    elif args.cmd == "restart":
        if args.agent:
            meta = get_agent_by_name(args.agent)
            if not meta:
                print(f"Unknown agent: {args.agent}")
                sys.exit(1)
            print(json.dumps(restart_agent(meta), indent=2, default=str))
        else:
            results = []
            for meta in AGENT_REGISTRY.values():
                results.append(restart_agent(meta))
                time.sleep(0.5)
            print(json.dumps({"ok": all(r.get("ok") for r in results), "results": results}, indent=2, default=str))
    elif args.cmd == "pipeline":
        print(json.dumps(run_pipeline_cycle(), indent=2, default=str))
    elif args.cmd == "stop-all":
        print(json.dumps(stop_all_agents_and_bots(), indent=2, default=str))
    else:
        parser.print_help()


if __name__ == "__main__":
    _main()
