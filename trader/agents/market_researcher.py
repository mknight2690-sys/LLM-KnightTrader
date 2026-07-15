"""
Market Agent — Market-Researcher-Agent from Owl Swarm.
Runs on: qwen/qwen-2.5-72b-instruct:free
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trader.repair_agent import agent_main

AGENT_NAME = "Market-Researcher-Agent"
LABEL = "Market"
LOOP_SEC = 120.0

SYSTEM_PROMPT = """
"""


def run_cycle(client, llm, state) -> None:
    from trader.state import append_research
    resp = llm.chat(
        messages=[{"role": "user", "content": "Run cycle"}],
        system=SYSTEM_PROMPT,
        max_tokens=1500,
        json_mode=True,
    )
    append_research(state, f"Market: {resp.text[:240]}")


def main() -> None:
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
        llm_pool_name=AGENT_NAME,
        openrouter_model="qwen/qwen-2.5-72b-instruct:free",
    )


if __name__ == "__main__":
    main()