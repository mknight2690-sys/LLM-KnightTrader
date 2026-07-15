"""
OpsMonitor Agent — Ops-Monitor-Agent from Owl Swarm.
Runs on: nvidia/nemotron-3-super-120b-a12b:free
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trader.repair_agent import agent_main

AGENT_NAME = "Ops-Monitor-Agent"
LABEL = "OpsMonitor"
LOOP_SEC = 90.0

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
    append_research(state, f"OpsMonitor: {resp.text[:240]}")


def main() -> None:
    agent_main(
        name=AGENT_NAME,
        label=LABEL,
        run_cycle_fn=run_cycle,
        interval_sec=LOOP_SEC,
        llm_pool_name=AGENT_NAME,
        openrouter_model="nvidia/nemotron-3-super-120b-a12b:free",
    )


if __name__ == "__main__":
    main()