"""
Director Agent — Trading-Director from Owl Swarm.
Orchestrates the full trading pipeline: portfolio → sentiment → quant → risk → execution.
Runs on: nvidia/nemotron-3-ultra-550b-a55b:free
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trader.repair_agent import agent_main

AGENT_NAME = "Trading-Director"
LABEL = "Director"
LOOP_SEC = 60.0

DIRECTOR_PROMPT = (
    "You are the Trading Director for LLM KnightTrader. "
    "Your job is to orchestrate the full trading pipeline each cycle.\n\n"
    "1. Read account state, positions, and available margin.\n"
    "2. Scan the BloFin universe for tradeable setups.\n"
    "3. Rank candidates by asymmetric R:R potential.\n"
    "4. Hand off to the pipeline: Portfolio → Sentiment → Quant → Risk → Execution.\n"
    "5. Verify each stage completed before proceeding.\n"
    "6. Log decisions to activity.jsonl with reasoning.\n\n"
    "Output JSON only: {cycle_plan, top_candidates, pipeline_status, notes}"
)


def run_cycle(client, llm, state) -> None:
    from trader.state import append_research
    resp = llm.chat(
        messages=[{"role": "user", "content": "Run full cycle assessment"}],
        system=DIRECTOR_PROMPT,
        max_tokens=1500,
        json_mode=True,
    )
    append_research(state, f"Director: {resp.text[:240]}")


def main() -> None:
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
        llm_pool_name=AGENT_NAME,
        openrouter_model="nvidia/nemotron-3-ultra-550b-a55b:free",
    )


if __name__ == "__main__":
    main()