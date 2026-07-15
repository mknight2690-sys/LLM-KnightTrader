"""
Owl Swarm Agent Orchestrator — registers all swarm agents into LLM KnightTrader.

Each agent is a standalone process that runs on a pinned OpenRouter model
(from the 7-key rotation pool). Agents communicate via the shared activity log
and state.json.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import all agent modules so they can be launched via -m
from trader.agents import (
    director,
    portfolio,
    sentiment,
    risk,
    execution,
    quant,
    verifier,
    ops_monitor,
    tactics_researcher,
    profit_strategist,
    market_researcher,
    pentest_scout,
    pentest_trade_hunter,
    pentest_integrity,
    pentest_operator,
)

AGENT_REGISTRY = {
    "director": director,
    "portfolio": portfolio,
    "sentiment": sentiment,
    "risk": risk,
    "execution": execution,
    "quant": quant,
    "verifier": verifier,
    "ops_monitor": ops_monitor,
    "tactics_researcher": tactics_researcher,
    "profit_strategist": profit_strategist,
    "market_researcher": market_researcher,
    "pentest_scout": pentest_scout,
    "pentest_trade_hunter": pentest_trade_hunter,
    "pentest_integrity": pentest_integrity,
    "pentest_operator": pentest_operator,
}


def run_agent(name: str) -> None:
    """Launch a single swarm agent by name."""
    mod = AGENT_REGISTRY.get(name)
    if not mod:
        raise ValueError(f"Unknown agent: {name}. Available: {list(AGENT_REGISTRY.keys())}")
    mod.main()


def get_agent_meta(name: str) -> dict[str, Any] | None:
    """Return metadata for a registered agent (from the orchestrator)."""
    try:
        from trader.orchestrator import AGENT_REGISTRY
        meta = AGENT_REGISTRY.get(name)
        return meta.to_dict() if meta else None
    except Exception:
        return None


def list_agents() -> list[dict[str, Any]]:
    """Return metadata for all registered agents."""
    try:
        from trader.orchestrator import AGENT_REGISTRY
        return [meta.to_dict() for meta in AGENT_REGISTRY.values()]
    except Exception:
        return []


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("agent", help="Agent name to run")
    args = parser.parse_args()
    run_agent(args.agent)
