"""Three full-time LLM repair agents — launched by stack_launcher on desktop Start.

1. watchdog      — stack/process health (like a master mechanic for the engine room)
2. code_fixer    — traceback/code bugs (software engineer technician)
3. order_guardian — trades, positions, TP/SL, margin errors (trade-floor technician)

All three use TECHNICIAN_METHOD: observe symptoms, hypothesize, test, act, verify.
They delegate to trader.repair.llm_triage_and_repair for the full action catalog.
"""

from trader.repair_agent import REPAIR_AGENT_MODULES

__all__ = ["REPAIR_AGENT_MODULES"]
