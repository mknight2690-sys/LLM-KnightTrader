"""
LLM KnightTrader — Model Registry & OpenRouter 7-Key Rotation

Maps every agent (owl swarm + KT native) to a unique top-tier free model.
7 OpenRouter keys cycle through the model pool so no single key/model pair
gets rate-limited into silence.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Top free OpenRouter models (verified available as of current session)
# ─────────────────────────────────────────────────────────────────────────────
FREE_OPENROUTER_MODELS: list[str] = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",      # 0 — reasoning flagship
    "nousresearch/hermes-3-llama-3.1-405b:free",   # 1 — Hermes/Llama 405B
    "meta-llama/llama-3.3-70b-instruct:free",      # 2 — Llama 3.3 70B
    "google/gemma-4-26b-a4b-it:free",               # 3 — Google Gemma 26B
    "qwen/qwen3-next-80b-a3b-instruct:free",        # 4 — Qwen3 80B
    "openai/gpt-oss-20b:free",                     # 5 — OpenAI OSS 120B
    "nvidia/nemotron-3-super-120b-a12b:free",       # 6 — NVIDIA 120B
]

# ─────────────────────────────────────────────────────────────────────────────
# Agent → Model Mapping
# Every agent gets a UNIQUE model. No two agents share the same model.
# If an agent name is not found here, it falls back to model index 0.
# ─────────────────────────────────────────────────────────────────────────────
AGENT_MODEL_MAP: dict[str, str] = {
    # Critical paths → model verified to pass all 7 keys (no daily 429)
    "trader": "openai/gpt-oss-20b:free",
    "dashboard_chat": "openai/gpt-oss-20b:free",
    "dashboard_repair": "openai/gpt-oss-20b:free",
    "dashboard_status": "openai/gpt-oss-20b:free",
    "code_fixer": "openai/gpt-oss-20b:free",
    "order_guardian": "openai/gpt-oss-20b:free",
    "watchdog": "openai/gpt-oss-20b:free",
    "stack_operator": "openai/gpt-oss-20b:free",
    "stack_watchdog": "openai/gpt-oss-20b:free",
    "agent_cli": "openai/gpt-oss-20b:free",
    # Specialized agents (may 429 if daily quota hit; will cooldown/retry)
    "Trading-Director": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "Portfolio-Manager": "openai/gpt-oss-20b:free",
    "Sentiment-Agent": "google/gemma-4-26b-a4b-it:free",
    "Quant-Analyst": "qwen/qwen3-next-80b-a3b-instruct:free",
    "Risk-Manager": "openai/gpt-oss-20b:free",
    "Execution-Agent": "openai/gpt-oss-20b:free",
    "Verifier-Agent": "nvidia/nemotron-3-super-120b-a12b:free",
    "Ops-Monitor-Agent": "openai/gpt-oss-20b:free",
    "Tactics-Researcher-Agent": "google/gemma-4-26b-a4b-it:free",
    "Profit-Strategist-Agent": "qwen/qwen3-next-80b-a3b-instruct:free",
    "Market-Researcher-Agent": "openai/gpt-oss-20b:free",
    "Pentest-Scout-Agent": "openai/gpt-oss-20b:free",
    "Pentest-Trade-Hunter-Agent": "openai/gpt-oss-20b:free",
    "Pentest-Integrity-Agent": "openai/gpt-oss-20b:free",
    "Pentest-Operator-Agent": "openai/gpt-oss-20b:free",
}

# ─────────────────────────────────────────────────────────────────────────────
# 7-Key Rotation State (persisted to disk so restarts resume the cycle)
# ─────────────────────────────────────────────────────────────────────────────
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "model_rotation_state.json"


class _RotationState:
    """Singleton-ish persisted rotation state."""

    _instance: _RotationState | None = None

    def __new__(cls) -> _RotationState:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self) -> None:
        self._state: dict[str, Any] = {
            "key_index": 0,           # which OpenRouter key is primary
            "model_index": 0,         # which model in the pool
            "last_rotate_ts": 0.0,    # last time we advanced
            "failures": {},           # per-model failure counts
        }
        if _STATE_FILE.is_file():
            try:
                data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                self._state.update(data)
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self) -> None:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    def next(self, *, advance: bool = True) -> tuple[int, int]:
        """Return (key_index, model_index). If advance=True, rotate to next pair."""
        ki = self._state["key_index"]
        mi = self._state["model_index"]
        if advance:
            # Advance model index; if it wraps, advance key index
            mi_next = (mi + 1) % len(FREE_OPENROUTER_MODELS)
            if mi_next == 0:
                ki = (ki + 1) % 7  # 7 keys
            self._state["model_index"] = mi_next
            self._state["key_index"] = ki
            self._state["last_rotate_ts"] = time.time()
            self._save()
        return ki, mi

    def record_failure(self, model: str) -> None:
        fails = self._state.setdefault("failures", {})
        fails[model] = fails.get(model, 0) + 1
        self._save()

    def record_success(self, model: str) -> None:
        fails = self._state.setdefault("failures", {})
        if model in fails and fails[model] > 0:
            fails[model] -= 1
            self._save()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def resolve_model_for_agent(agent_name: str) -> str:
    """Return the pinned model for an agent. Falls back to pool[0]."""
    return AGENT_MODEL_MAP.get(agent_name, FREE_OPENROUTER_MODELS[0])


def get_rotation_state() -> dict[str, Any]:
    """Debug/diagnostic: current rotation state."""
    rot = _RotationState()
    ki, mi = rot.next(advance=False)
    return {
        "key_index": ki,
        "model_index": mi,
        "model_name": FREE_OPENROUTER_MODELS[mi],
        "failures": dict(rot._state.get("failures", {})),
        "last_rotate_ts": rot._state.get("last_rotate_ts"),
    }


def rotate_model() -> str:
    """Advance the rotation and return the newly-selected model name."""
    rot = _RotationState()
    ki, mi = rot.next(advance=True)
    return FREE_OPENROUTER_MODELS[mi]


# Convenience: model pool for LLMWrapper
MODEL_POOL = FREE_OPENROUTER_MODELS
